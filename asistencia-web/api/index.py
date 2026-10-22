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

app = FastAPI(title="Asistencia Movilnet ERP v4.0")

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
    unidad_administrativa: Optional[str] = ""
    gerencia_general: Optional[str] = ""

class AsistenciaDiaRequest(BaseModel):
    p00: str
    fecha: str
    estatus: str
    motivo: Optional[str] = ""

class EnvioRRHHRequest(BaseModel):
    unidad_nombre: str
    rol_usuario: str

# ==========================================
# 1. GESTIÓN DE USUARIOS Y ROLES
# ==========================================
@app.post("/api/login")
def login(req: LoginRequest, db = Depends(get_db)):
    cursor = db.cursor()
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    
    if req.username == "admin" and req.password == "admin":
        cursor.execute("SELECT id FROM usuarios WHERE username = 'admin'")
        if not cursor.fetchone():
            cursor.execute("INSERT INTO usuarios (username, password_hash, rol) VALUES ('admin', %s, 'Recursos Humanos')", (pwd_hash,))
            db.commit()

    cursor.execute("SELECT rol, username FROM usuarios WHERE username = %s AND password_hash = %s", (req.username, pwd_hash))
    user = cursor.fetchone()
    
    if user: return {"success": True, "rol": user['rol'], "username": user['username']}
    else: raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

@app.get("/api/rrhh/usuarios")
def listar_usuarios(db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT id, username, rol, unidad_administrativa, gerencia_general FROM usuarios ORDER BY rol, username")
    return cursor.fetchall()

@app.post("/api/rrhh/usuarios")
def crear_usuario(req: UsuarioRequest, db = Depends(get_db)):
    cursor = db.cursor()
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    try:
        cursor.execute("""
            INSERT INTO usuarios (username, password_hash, rol, unidad_administrativa, gerencia_general) 
            VALUES (%s, %s, %s, %s, %s)
        """, (req.username, pwd_hash, req.rol, req.unidad_administrativa, req.gerencia_general))
        db.commit()
        return {"success": True, "mensaje": "Usuario creado exitosamente con su jurisdicción."}
    except psycopg.errors.UniqueViolation:
        db.rollback()
        raise HTTPException(status_code=400, detail="El nombre de usuario ya existe")

@app.delete("/api/rrhh/usuarios/{username}")
def eliminar_usuario(username: str, db = Depends(get_db)):
    if username == "admin": raise HTTPException(status_code=400, detail="No se puede eliminar el administrador")
    cursor = db.cursor()
    cursor.execute("DELETE FROM usuarios WHERE username = %s", (username,))
    db.commit()
    return {"success": True, "mensaje": "Usuario eliminado"}

# ==========================================
# 2. CARGA DE ESTRUCTURA Y NÓMINA (EXCEL)
# ==========================================
@app.post("/api/rrhh/cargar-organizacion")
async def cargar_organizacion(file: UploadFile = File(...), db = Depends(get_db)):
    cursor = db.cursor()
    try:
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents))
        
        if 'Unidad_Administrativa' not in df.columns or 'Gerencia_General' not in df.columns:
            raise HTTPException(status_code=400, detail="El archivo Excel debe tener las columnas 'Unidad_Administrativa' y 'Gerencia_General'.")
            
        cargas = 0
        for _, fila in df.iterrows():
            ua = str(fila['Unidad_Administrativa']).strip()
            gg = str(fila['Gerencia_General']).strip()
            if ua and ua != 'nan' and gg and gg != 'nan':
                cursor.execute("""
                    INSERT INTO organizacion (unidad_administrativa, gerencia_general)
                    VALUES (%s, %s)
                    ON CONFLICT (unidad_administrativa, gerencia_general) DO NOTHING
                """, (ua, gg))
                cargas += 1
                
        db.commit()
        return {"success": True, "mensaje": f"Estructura Oficial Actualizada.\nGerencias registradas: {cargas}"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {str(e)}")

@app.get("/api/rrhh/organizacion")
def obtener_organizacion(db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT unidad_administrativa, gerencia_general FROM organizacion ORDER BY unidad_administrativa ASC, gerencia_general ASC")
    filas = cursor.fetchall()
    
    estructura = {}
    for f in filas:
        ua = f['unidad_administrativa']
        gg = f['gerencia_general']
        if ua not in estructura:
            estructura[ua] = []
        estructura[ua].append(gg)
        
    return {"success": True, "estructura": estructura}

@app.post("/api/rrhh/cargar-maestro")
async def cargar_maestro(file: UploadFile = File(...), db = Depends(get_db)):
    cursor = db.cursor()
    try:
        contents = await file.read()
        archivo_ram = io.BytesIO(contents)
        
        df_config = pd.read_excel(archivo_ram, sheet_name="Configuracion")
        df_personal = pd.read_excel(archivo_ram, sheet_name="Personal_Activo")
        df_absentismos = pd.read_excel(archivo_ram, sheet_name="Absentismos")
        
        f_ini = pd.to_datetime(df_config['Fecha_Inicio'].iloc[0]).strftime('%Y-%m-%d')
        f_fin = pd.to_datetime(df_config['Fecha_Fin'].iloc[0]).strftime('%Y-%m-%d')
        
        cursor.execute("DELETE FROM configuracion_sistema")
        cursor.execute("INSERT INTO configuracion_sistema (id, semana_inicio, semana_fin, estatus) VALUES (1, %s, %s, 'Abierta')", (f_ini, f_fin))
        
        nuevos_emp = 0
        for _, fila in df_personal.iterrows():
            p00 = str(fila['P00']).strip()
            if p00 and p00 != 'nan':
                unidad = str(fila['Unidad_Administrativa']).strip()
                gerencia = str(fila['Gerencia_General']).strip()
                cedula = str(fila['Cedula']).strip()
                nombres = str(fila['Nombres_Apellidos']).strip()
                f_ingreso = pd.to_datetime(fila['Fecha_Ingreso']).strftime('%Y-%m-%d')
                
                cursor.execute("INSERT INTO organizacion (unidad_administrativa, gerencia_general) VALUES (%s, %s) ON CONFLICT (unidad_administrativa, gerencia_general) DO NOTHING", (unidad, gerencia))
                
                cursor.execute("""
                    INSERT INTO empleados_maestro (p00, cedula, nombres_apellidos, fecha_ingreso, unidad_administrativa, gerencia_general, estatus)
                    VALUES (%s, %s, %s, %s, %s, %s, 'Activo')
                    ON CONFLICT (p00) DO UPDATE SET unidad_administrativa = EXCLUDED.unidad_administrativa, gerencia_general = EXCLUDED.gerencia_general, estatus = 'Activo'
                """, (p00, cedula, nombres, f_ingreso, unidad, gerencia))
                nuevos_emp += 1

        cursor.execute("DELETE FROM ausentismos_rrhh")
        cargas_aus = 0
        for _, fila in df_absentismos.iterrows():
            p00 = str(fila['P00']).strip()
            if p00 and p00 != 'nan':
                tipo = str(fila['Tipo_Ausentismo']).strip()
                f_ini_aus = pd.to_datetime(fila['Fecha_Inicio']).strftime('%Y-%m-%d')
                f_fin_aus = pd.to_datetime(fila['Fecha_Fin']).strftime('%Y-%m-%d')
                cursor.execute("INSERT INTO ausentismos_rrhh (p00, tipo_ausentismo, fecha_inicio, fecha_fin) VALUES (%s, %s, %s, %s)", (p00, tipo, f_ini_aus, f_fin_aus))
                cargas_aus += 1
                
        db.commit()
        return {"success": True, "mensaje": f"Semana habilitada.\nPersonal: {nuevos_emp} | Absentismos: {cargas_aus}"}
        
    except KeyError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Falta una columna o hoja: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error en el formato del archivo: {str(e)}")

# ==========================================
# 3. CONTROL DE ASISTENCIA (FILTRADO DINÁMICO)
# ==========================================
@app.get("/api/mi-asistencia/{username}")
def obtener_asistencia_unidad(username: str, db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config: return {"semana_activa": False, "empleados": []}
        
    semana_ini, semana_fin = config['semana_inicio'], config['semana_fin']

    cursor.execute("SELECT unidad_administrativa, gerencia_general FROM usuarios WHERE username = %s", (username,))
    usr = cursor.fetchone()
    if not usr: raise HTTPException(status_code=404, detail="Usuario no encontrado")

    query_emp = """
        SELECT e.p00, e.nombres_apellidos, e.cedula, e.unidad_administrativa, e.gerencia_general, a.fecha, a.estatus, a.motivo
        FROM empleados_maestro e
        LEFT JOIN asistencia_diaria a ON e.p00 = a.p00 AND a.fecha BETWEEN %s AND %s
        WHERE e.estatus = 'Activo'
    """
    params = [semana_ini, semana_fin]
    
    if usr['unidad_administrativa']:
        query_emp += " AND e.unidad_administrativa = %s"
        params.append(usr['unidad_administrativa'])
    if usr['gerencia_general']:
        query_emp += " AND e.gerencia_general = %s"
        params.append(usr['gerencia_general'])
        
    query_emp += " ORDER BY e.unidad_administrativa ASC, e.gerencia_general ASC, e.nombres_apellidos ASC, a.fecha ASC"
    
    cursor.execute(query_emp, params)
    registros = cursor.fetchall()
    
    cursor.execute("SELECT p00, tipo_ausentismo, fecha_inicio, fecha_fin FROM ausentismos_rrhh WHERE fecha_inicio <= %s AND fecha_fin >= %s", (semana_fin, semana_ini))
    ausentismos = cursor.fetchall()
    
    empleados_dict = {}
    for reg in registros:
        p00 = reg['p00']
        if p00 not in empleados_dict:
            empleados_dict[p00] = {
                "p00": p00, "nombres_apellidos": reg['nombres_apellidos'], 
                "unidad": reg['unidad_administrativa'], "gerencia": reg['gerencia_general'], 
                "asistencia": {}
            }
        if reg['fecha']: 
            fecha_str = reg['fecha'].strftime('%Y-%m-%d')
            empleados_dict[p00]["asistencia"][fecha_str] = {"estatus": reg['estatus'], "motivo": reg['motivo']}
            
    cursor.execute("SELECT estatus_entrega FROM estado_entregas WHERE unidad_nombre = %s AND semana_inicio = %s AND semana_fin = %s", (username, semana_ini, semana_fin))
    entrega = cursor.fetchone()
    bloqueado = True if entrega and entrega['estatus_entrega'] == 'Enviado a RRHH' else False
            
    return {
        "semana_activa": True, "fecha_inicio": semana_ini.strftime('%Y-%m-%d'), "fecha_fin": semana_fin.strftime('%Y-%m-%d'),
        "bloqueado_por_envio": bloqueado, "empleados": list(empleados_dict.values()), "ausentismos_fijos": ausentismos
    }

@app.post("/api/guardar-dia")
def guardar_dia(req: AsistenciaDiaRequest, db = Depends(get_db)):
    if req.estatus not in ESTATUS_PERMITIDOS: raise HTTPException(status_code=400, detail="Estatus inválido.")
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
    return {"success": True}

@app.post("/api/enviar-a-rrhh")
def enviar_a_rrhh(req: EnvioRRHHRequest, db = Depends(get_db)):
    if req.rol_usuario == "Reportador": raise HTTPException(status_code=403, detail="Perfil no autorizado.")

    cursor = db.cursor()
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config: raise HTTPException(status_code=400, detail="No hay semana activa.")
    semana_ini, semana_fin = config['semana_inicio'], config['semana_fin']
    
    cursor.execute("SELECT unidad_administrativa, gerencia_general FROM usuarios WHERE username = %s", (req.unidad_nombre,))
    usr = cursor.fetchone()
    
    q_emp = "SELECT COUNT(*) as total_emp FROM empleados_maestro WHERE estatus = 'Activo'"
    p_emp = []
    if usr['unidad_administrativa']: q_emp += " AND unidad_administrativa = %s"; p_emp.append(usr['unidad_administrativa'])
    if usr['gerencia_general']: q_emp += " AND gerencia_general = %s"; p_emp.append(usr['gerencia_general'])
    
    cursor.execute(q_emp, p_emp)
    total_empleados = cursor.fetchone()['total_emp']
    if total_empleados == 0: raise HTTPException(status_code=400, detail="No tiene personal adscrito.")
    dias_esperados = total_empleados * 7

    q_reg = """SELECT COUNT(*) as total_registros FROM asistencia_diaria a
               INNER JOIN empleados_maestro e ON a.p00 = e.p00 
               WHERE a.fecha BETWEEN %s AND %s"""
    p_reg = [semana_ini, semana_fin]
    if usr['unidad_administrativa']: q_reg += " AND e.unidad_administrativa = %s"; p_reg.append(usr['unidad_administrativa'])
    if usr['gerencia_general']: q_reg += " AND e.gerencia_general = %s"; p_reg.append(usr['gerencia_general'])
    
    cursor.execute(q_reg, p_reg)
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
# 4. EXPORTACIONES E INTERFAZ
# ==========================================
@app.get("/api/rrhh/monitoreo-entregas")
def monitoreo_entregas(db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config: return {"semana_activa": False, "entregas": []}
    
    # Evaluar qué usuarios (jurisdicciones) han entregado
    cursor.execute("""
        SELECT u.username as unidad, u.unidad_administrativa, u.gerencia_general,
               (
                   SELECT COUNT(e.p00) FROM empleados_maestro e 
                   WHERE e.estatus = 'Activo' 
                   AND (e.unidad_administrativa = u.unidad_administrativa OR u.unidad_administrativa = '' OR u.unidad_administrativa IS NULL)
                   AND (e.gerencia_general = u.gerencia_general OR u.gerencia_general = '' OR u.gerencia_general IS NULL)
               ) as total_empleados,
               ent.estatus_entrega, ent.fecha_envio
        FROM usuarios u
        LEFT JOIN estado_entregas ent ON u.username = ent.unidad_nombre AND ent.semana_inicio = %s AND ent.semana_fin = %s
        WHERE u.rol IN ('Cierre de Unidad')
        ORDER BY u.unidad_administrativa ASC, u.gerencia_general ASC, u.username ASC
    """, (config['semana_inicio'], config['semana_fin']))
    
    resultados = cursor.fetchall()
    for res in resultados:
        res['fecha_envio'] = res['fecha_envio'].strftime('%d/%m/%Y %I:%M %p') if res['fecha_envio'] else 'Pendiente'
        res['unidad_administrativa'] = res['unidad_administrativa'] if res['unidad_administrativa'] else "JURISDICCIÓN GLOBAL"
            
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
        WHERE a.fecha BETWEEN %s AND %s ORDER BY e.unidad_administrativa ASC, e.gerencia_general ASC, e.p00 ASC, a.fecha ASC
    """, (config['semana_inicio'], config['semana_fin']))
    
    df_detalle = pd.DataFrame(cursor.fetchall())
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        if not df_detalle.empty:
            df_detalle['fecha'] = pd.to_datetime(df_detalle['fecha']).dt.strftime('%d/%m/%Y')
            df_detalle.columns = ['Fecha', 'P00', 'Cédula', 'Nombres y Apellidos', 'Unidad Administrativa (VP)', 'Gerencia General', 'Estatus Asistencia', 'Motivo']
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
    raise HTTPException(status_code=404, detail="Archivo HTML no encontrado.")
