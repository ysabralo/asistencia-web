from fastapi import FastAPI, HTTPException, UploadFile, File, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional
import hashlib
import os
import io
import pandas as pd
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

app = FastAPI(title="Asistencia Movilnet ERP v3.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    DATABASE_URL = os.environ.get("DATABASE_URL")
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="FALTA_VARIABLE: No se encontró DATABASE_URL")
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ERROR_DB: {str(e)}")
    try:
        yield conn
    finally:
        conn.close()

ESTATUS_PERMITIDOS = [
    "Asistencia Normal", "Reposo", "Vacaciones", 
    "Permiso otorgado", "Falta Injustificada", 
    "Dia Descanso Legal", "Dia Libre Compensatorio"
]

class LoginRequest(BaseModel):
    username: str
    password: str

class UsuarioRequest(BaseModel):
    username: str
    password: str
    rol: str

class AsistenciaDiaRequest(BaseModel):
    p00: str
    fecha: str
    estatus: str
    motivo: Optional[str] = ""

class EnvioRRHHRequest(BaseModel):
    unidad_nombre: str
    rol_usuario: str

class MovimientoPersonalRequest(BaseModel):
    p00: str
    tipo_movimiento: str
    unidad_destino: Optional[str] = None
    registrado_por: str

# ==========================================
# 1. AUTENTICACIÓN Y GESTIÓN DE USUARIOS
# ==========================================
@app.post("/api/login")
def login(req: LoginRequest, db = Depends(get_db)):
    cursor = db.cursor()
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    
    # Auto-crear el admin si no existe en la base de datos (Para el primer arranque)
    if req.username == "admin" and req.password == "admin":
        cursor.execute("SELECT id FROM usuarios WHERE username = 'admin'")
        if not cursor.fetchone():
            cursor.execute("INSERT INTO usuarios (username, password_hash, rol) VALUES ('admin', %s, 'Recursos Humanos')", (pwd_hash,))
            db.commit()

    cursor.execute("SELECT rol, username FROM usuarios WHERE username = %s AND password_hash = %s", (req.username, pwd_hash))
    user = cursor.fetchone()
    
    if user:
        return {"success": True, "rol": user['rol'], "username": user['username']}
    else:
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

@app.get("/api/rrhh/usuarios")
def listar_usuarios(db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT id, username, rol FROM usuarios ORDER BY rol, username")
    return cursor.fetchall()

@app.post("/api/rrhh/usuarios")
def crear_usuario(req: UsuarioRequest, db = Depends(get_db)):
    cursor = db.cursor()
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    try:
        cursor.execute("INSERT INTO usuarios (username, password_hash, rol) VALUES (%s, %s, %s)", 
                       (req.username, pwd_hash, req.rol))
        db.commit()
        return {"success": True, "mensaje": "Usuario creado exitosamente"}
    except psycopg.errors.UniqueViolation:
        db.rollback()
        raise HTTPException(status_code=400, detail="El nombre de usuario ya existe")

@app.delete("/api/rrhh/usuarios/{username}")
def eliminar_usuario(username: str, db = Depends(get_db)):
    if username == "admin":
        raise HTTPException(status_code=400, detail="No se puede eliminar el usuario administrador principal")
    cursor = db.cursor()
    cursor.execute("DELETE FROM usuarios WHERE username = %s", (username,))
    db.commit()
    return {"success": True, "mensaje": "Usuario eliminado"}

# ==========================================
# 2. CARGA MAESTRA (EXCEL) - APERTURA DE SEMANA
# ==========================================
@app.post("/api/rrhh/cargar-maestro")
async def cargar_maestro(file: UploadFile = File(...), db = Depends(get_db)):
    cursor = db.cursor()
    try:
        contents = await file.read()
        archivo_ram = io.BytesIO(contents)
        
        # Leer las 3 hojas del Excel
        df_config = pd.read_excel(archivo_ram, sheet_name="Configuracion")
        df_personal = pd.read_excel(archivo_ram, sheet_name="Personal_Activo")
        df_absentismos = pd.read_excel(archivo_ram, sheet_name="Absentismos")
        
        f_ini = pd.to_datetime(df_config['Fecha_Inicio'].iloc[0]).strftime('%Y-%m-%d')
        f_fin = pd.to_datetime(df_config['Fecha_Fin'].iloc[0]).strftime('%Y-%m-%d')
        
        # 1. Configurar la Semana
        cursor.execute("DELETE FROM configuracion_sistema")
        cursor.execute("INSERT INTO configuracion_sistema (id, semana_inicio, semana_fin, estatus) VALUES (1, %s, %s, 'Abierta')", (f_ini, f_fin))
        
        # 2. Procesar Estructura y Personal
        nuevos_emp = 0
        for _, fila in df_personal.iterrows():
            p00 = str(fila['P00']).strip()
            if p00 and p00 != 'nan':
                unidad = str(fila['Unidad_Administrativa']).strip()
                gerencia = str(fila['Gerencia_General']).strip()
                cedula = str(fila['Cedula']).strip()
                nombres = str(fila['Nombres_Apellidos']).strip()
                f_ingreso = pd.to_datetime(fila['Fecha_Ingreso']).strftime('%Y-%m-%d')
                
                # Crear estructura si no existe
                cursor.execute("""
                    INSERT INTO organizacion (unidad_administrativa, gerencia_general) 
                    VALUES (%s, %s) ON CONFLICT (unidad_administrativa) DO NOTHING
                """, (unidad, gerencia))
                
                # Actualizar o insertar empleado
                cursor.execute("""
                    INSERT INTO empleados_maestro (p00, cedula, nombres_apellidos, fecha_ingreso, unidad_administrativa, gerencia_general, estatus)
                    VALUES (%s, %s, %s, %s, %s, %s, 'Activo')
                    ON CONFLICT (p00) DO UPDATE 
                    SET unidad_administrativa = EXCLUDED.unidad_administrativa,
                        gerencia_general = EXCLUDED.gerencia_general,
                        estatus = 'Activo'
                """, (p00, cedula, nombres, f_ingreso, unidad, gerencia))
                nuevos_emp += 1

        # 3. Procesar Absentismos (Reposos, Vacaciones)
        cursor.execute("DELETE FROM ausentismos_rrhh")
        cargas_aus = 0
        for _, fila in df_absentismos.iterrows():
            p00 = str(fila['P00']).strip()
            if p00 and p00 != 'nan':
                tipo = str(fila['Tipo_Ausentismo']).strip()
                f_ini_aus = pd.to_datetime(fila['Fecha_Inicio']).strftime('%Y-%m-%d')
                f_fin_aus = pd.to_datetime(fila['Fecha_Fin']).strftime('%Y-%m-%d')
                
                cursor.execute("""
                    INSERT INTO ausentismos_rrhh (p00, tipo_ausentismo, fecha_inicio, fecha_fin) 
                    VALUES (%s, %s, %s, %s)
                """, (p00, tipo, f_ini_aus, f_fin_aus))
                cargas_aus += 1
                
        db.commit()
        return {"success": True, "mensaje": f"Semana del {f_ini} habilitada.\nPersonal procesado: {nuevos_emp}\nAbsentismos cargados: {cargas_aus}"}
        
    except KeyError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Falta una columna en el Excel: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error en el formato del archivo: {str(e)}")

# ==========================================
# 3. OPERACIONES DE UNIDADES ADMINISTRATIVAS
# ==========================================
@app.get("/api/mi-asistencia/{unidad_nombre}")
def obtener_asistencia_unidad(unidad_nombre: str, db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config: return {"semana_activa": False, "empleados": []}
        
    semana_ini = config['semana_inicio']
    semana_fin = config['semana_fin']

    cursor.execute("""
        SELECT e.p00, e.nombres_apellidos, e.cedula, a.fecha, a.estatus, a.motivo
        FROM empleados_maestro e
        LEFT JOIN asistencia_diaria a ON e.p00 = a.p00 AND a.fecha BETWEEN %s AND %s
        WHERE e.unidad_administrativa = %s AND e.estatus = 'Activo'
        ORDER BY e.nombres_apellidos ASC, a.fecha ASC
    """, (semana_ini, semana_fin, unidad_nombre))
    
    registros = cursor.fetchall()
    
    cursor.execute("""
        SELECT p00, tipo_ausentismo, fecha_inicio, fecha_fin 
        FROM ausentismos_rrhh WHERE fecha_inicio <= %s AND fecha_fin >= %s
    """, (semana_fin, semana_ini))
    ausentismos = cursor.fetchall()
    
    empleados_dict = {}
    for reg in registros:
        p00 = reg['p00']
        if p00 not in empleados_dict:
            empleados_dict[p00] = {"p00": p00, "nombres_apellidos": reg['nombres_apellidos'], "cedula": reg['cedula'], "asistencia": {}}
        if reg['fecha']: 
            fecha_str = reg['fecha'].strftime('%Y-%m-%d')
            empleados_dict[p00]["asistencia"][fecha_str] = {"estatus": reg['estatus'], "motivo": reg['motivo']}
            
    cursor.execute("""SELECT estatus_entrega FROM estado_entregas WHERE unidad_nombre = %s AND semana_inicio = %s AND semana_fin = %s""", (unidad_nombre, semana_ini, semana_fin))
    entrega = cursor.fetchone()
    bloqueado = True if entrega and entrega['estatus_entrega'] == 'Enviado a RRHH' else False
            
    return {
        "semana_activa": True, "fecha_inicio": semana_ini.strftime('%Y-%m-%d'), "fecha_fin": semana_fin.strftime('%Y-%m-%d'),
        "bloqueado_por_envio": bloqueado, "empleados": list(empleados_dict.values()), "ausentismos_fijos": ausentismos
    }

@app.post("/api/guardar-dia")
def guardar_dia(req: AsistenciaDiaRequest, db = Depends(get_db)):
    if req.estatus not in ESTATUS_PERMITIDOS: raise HTTPException(status_code=400, detail=f"Estatus inválido.")
    if req.estatus == "Falta Injustificada" and not req.motivo.strip(): raise HTTPException(status_code=400, detail="Debe indicar motivo.")

    cursor = db.cursor()
    cursor.execute("SELECT estatus FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if config and config['estatus'] == 'Cerrada': raise HTTPException(status_code=403, detail="Semana cerrada.")

    cursor.execute("""
        INSERT INTO asistencia_diaria (p00, fecha, estatus, motivo) VALUES (%s, %s, %s, %s)
        ON CONFLICT (p00, fecha) DO UPDATE SET estatus = EXCLUDED.estatus, motivo = EXCLUDED.motivo
    """, (req.p00, req.fecha, req.estatus, req.motivo))
    db.commit()
    return {"success": True, "mensaje": "Día registrado."}

@app.post("/api/enviar-a-rrhh")
def enviar_a_rrhh(req: EnvioRRHHRequest, db = Depends(get_db)):
    if req.rol_usuario == "Reportador": raise HTTPException(status_code=403, detail="Perfil no autorizado para certificar.")

    cursor = db.cursor()
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config: raise HTTPException(status_code=400, detail="No hay semana activa.")
        
    semana_ini, semana_fin = config['semana_inicio'], config['semana_fin']
    
    cursor.execute("SELECT COUNT(*) as total_emp FROM empleados_maestro WHERE unidad_administrativa = %s AND estatus = 'Activo'", (req.unidad_nombre,))
    total_empleados = cursor.fetchone()['total_emp']
    if total_empleados == 0: raise HTTPException(status_code=400, detail="No tiene personal.")

    dias_esperados = total_empleados * 7

    cursor.execute("""
        SELECT COUNT(*) as total_registros FROM asistencia_diaria a
        INNER JOIN empleados_maestro e ON a.p00 = e.p00
        WHERE e.unidad_administrativa = %s AND a.fecha BETWEEN %s AND %s
    """, (req.unidad_nombre, semana_ini, semana_fin))
    dias_registrados = cursor.fetchone()['total_registros']

    if dias_registrados < dias_esperados:
        raise HTTPException(status_code=403, detail=f"Reporte al {(dias_registrados/dias_esperados)*100:.1f}%. Faltan días por justificar.")

    cursor.execute("""
        INSERT INTO estado_entregas (unidad_nombre, semana_inicio, semana_fin, estatus_entrega, fecha_envio)
        VALUES (%s, %s, %s, 'Enviado a RRHH', NOW())
        ON CONFLICT (unidad_nombre, semana_inicio, semana_fin) DO UPDATE SET estatus_entrega = 'Enviado a RRHH', fecha_envio = NOW()
    """, (req.unidad_nombre, semana_ini, semana_fin))
    db.commit()
    return {"success": True, "mensaje": "Reporte certificado y enviado a RRHH."}

# ==========================================
# 4. MÓDULO DE RECURSOS HUMANOS (RRHH)
# ==========================================
@app.post("/api/rrhh/movimiento-personal")
def movimiento_personal(req: MovimientoPersonalRequest, db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT unidad_administrativa FROM empleados_maestro WHERE p00 = %s", (req.p00,))
    emp = cursor.fetchone()
    if not emp: raise HTTPException(status_code=404, detail="El código P00 no existe.")
        
    unidad_origen = emp['unidad_administrativa']
    
    if req.tipo_movimiento == 'Egreso':
        cursor.execute("UPDATE empleados_maestro SET estatus = 'Egresado' WHERE p00 = %s", (req.p00,))
    elif req.tipo_movimiento == 'Transferencia':
        if not req.unidad_destino: raise HTTPException(status_code=400, detail="Indique unidad destino.")
        cursor.execute("UPDATE empleados_maestro SET unidad_administrativa = %s WHERE p00 = %s", (req.unidad_destino, req.p00))
        
    cursor.execute("""
        INSERT INTO movimientos_personal (p00, tipo_movimiento, unidad_origen, unidad_destino, registrado_por)
        VALUES (%s, %s, %s, %s, %s)
    """, (req.p00, req.tipo_movimiento, unidad_origen, req.unidad_destino, req.registrado_por))
    db.commit()
    return {"success": True, "mensaje": f"Movimiento ejecutado con éxito."}

@app.get("/api/rrhh/monitoreo-entregas")
def monitoreo_entregas(db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config: return {"semana_activa": False, "entregas": []}
    
    cursor.execute("""
        SELECT o.unidad_administrativa as unidad, o.gerencia_general, COUNT(e.p00) as total_empleados,
               ent.estatus_entrega, ent.fecha_envio
        FROM organizacion o
        LEFT JOIN empleados_maestro e ON o.unidad_administrativa = e.unidad_administrativa AND e.estatus = 'Activo'
        LEFT JOIN estado_entregas ent ON o.unidad_administrativa = ent.unidad_nombre AND ent.semana_inicio = %s AND ent.semana_fin = %s
        GROUP BY o.unidad_administrativa, o.gerencia_general, ent.estatus_entrega, ent.fecha_envio
        ORDER BY o.gerencia_general ASC, o.unidad_administrativa ASC
    """, (config['semana_inicio'], config['semana_fin']))
    
    resultados = cursor.fetchall()
    for res in resultados:
        if res['fecha_envio']: res['fecha_envio'] = res['fecha_envio'].strftime('%d/%m/%Y %I:%M %p')
        else: res['estatus_entrega'] = 'Pendiente'
            
    return {"semana_activa": True, "fecha_inicio": config['semana_inicio'].strftime('%d/%m/%Y'), "fecha_fin": config['semana_fin'].strftime('%d/%m/%Y'), "entregas": resultados}

@app.get("/api/rrhh/descargar-consolidado")
def descargar_consolidado(db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config: raise HTTPException(status_code=400, detail="No hay semana activa.")
        
    cursor.execute("""
        SELECT a.fecha, e.p00, e.cedula, e.nombres_apellidos, e.unidad_administrativa, e.gerencia_general, a.estatus, a.motivo 
        FROM asistencia_diaria a INNER JOIN empleados_maestro e ON a.p00 = e.p00
        WHERE a.fecha BETWEEN %s AND %s ORDER BY e.gerencia_general ASC, e.unidad_administrativa ASC, e.p00 ASC, a.fecha ASC
    """, (config['semana_inicio'], config['semana_fin']))
    
    registros = cursor.fetchall()
    df_detalle = pd.DataFrame(registros)
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        if not df_detalle.empty:
            df_detalle['fecha'] = pd.to_datetime(df_detalle['fecha']).dt.strftime('%d/%m/%Y')
            df_detalle.columns = ['Fecha', 'P00', 'Cédula', 'Nombres y Apellidos', 'Unidad Administrativa', 'Gerencia General', 'Estatus Asistencia', 'Motivo']
            df_detalle.to_excel(writer, index=False, sheet_name="Asistencia_Consolidada")
        else:
            pd.DataFrame(["No hay registros"]).to_excel(writer, index=False, sheet_name="Sin_Datos")
            
    output.seek(0)
    headers = {'Content-Disposition': f'attachment; filename="Nomina_{config["semana_inicio"]}.xlsx"', 'Access-Control-Expose-Headers': 'Content-Disposition'}
    return StreamingResponse(output, headers=headers, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.get("/")
@app.get("/index.html")
def servir_interfaz():
    ruta_html = os.path.join(os.path.dirname(os.path.dirname(__file__)), "index.html")
    if os.path.exists(ruta_html): return FileResponse(ruta_html)
    raise HTTPException(status_code=404, detail="Archivo index.html no encontrado.")
