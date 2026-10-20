from fastapi import FastAPI, HTTPException, UploadFile, File, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import hashlib
import os
import io
import pandas as pd
from datetime import datetime
import msoffcrypto

# Librería de PostgreSQL moderna y limpia
import psycopg
from psycopg.rows import dict_row

app = FastAPI(title="Backend Web Asistencia Movilnet v2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# CONEXIÓN A SUPABASE
# ==========================================
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

# ==========================================
# MODELOS DE DATOS Y REGLAS ESTRICTAS
# ==========================================
ESTATUS_PERMITIDOS = [
    "Asistencia Normal",
    "Reposo",
    "Vacaciones",
    "Permiso otorgado",
    "Falta Injustificada",
    "Dia Descanso",
    "Dia Compensatorio"
]

class LoginRequest(BaseModel):
    username: str
    password: str

class AsistenciaDiaRequest(BaseModel):
    id_empleado: str
    fecha: str
    estatus: str
    motivo: Optional[str] = ""

class EnvioRRHHRequest(BaseModel):
    unidad_nombre: str

# ==========================================
# RUTAS DE LA API (Endpoints Centrales)
# ==========================================
@app.post("/api/login")
def login(req: LoginRequest, db = Depends(get_db)):
    cursor = db.cursor()
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    
    cursor.execute("SELECT rol, username FROM usuarios WHERE username = %s AND password_hash = %s", (req.username, pwd_hash))
    user = cursor.fetchone()
    
    if user:
        rol = user['rol']
        
        # Migración automática de roles legados a web
        if rol == "Sub-Unidad":
            rol = "Sub-Unidad Base"
            cursor.execute("UPDATE usuarios SET rol = %s WHERE username = %s", (rol, req.username))
            db.commit()
        elif rol == "Unidad Principal":
            rol = "Unidad Principal (Envío RRHH)"
            cursor.execute("UPDATE usuarios SET rol = %s WHERE username = %s", (rol, req.username))
            db.commit()
            
        return {"success": True, "rol": rol, "username": req.username}
    else:
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

@app.get("/api/estado-sistema")
def estado_sistema(db = Depends(get_db)):
    cursor = db.cursor()
    
    cursor.execute("SELECT semana_inicio, semana_fin, estatus FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    
    cursor.execute("SELECT COUNT(*) as total FROM mi_unidad")
    total_plantilla = cursor.fetchone()['total']
    
    cursor.execute("SELECT COUNT(*) as total FROM historial_semanas")
    hay_historial = cursor.fetchone()['total'] > 0

    semana_activa = config is not None
    semana_abierta = config and config['estatus'] == 'Abierta'
    semana_cerrada = config and config['estatus'] == 'Cerrada'

    faltantes = 0
    if semana_activa:
        cursor.execute('''
            SELECT COUNT(*) as emp_completos FROM (
                SELECT r.id_empleado FROM registro_asistencia r
                INNER JOIN mi_unidad u ON r.id_empleado = u.id_empleado
                WHERE r.fecha BETWEEN %s AND %s 
                GROUP BY r.id_empleado HAVING COUNT(*) = 7
            ) as subquery
        ''', (config['semana_inicio'], config['semana_fin']))
        empleados_completos = cursor.fetchone()['emp_completos']
        faltantes = total_plantilla - empleados_completos

    return {
        "semana_activa": semana_activa,
        "semana_abierta": semana_abierta,
        "semana_cerrada": semana_cerrada,
        "fecha_inicio": config['semana_inicio'].strftime('%Y-%m-%d') if config else None,
        "fecha_fin": config['semana_fin'].strftime('%Y-%m-%d') if config else None,
        "total_plantilla": total_plantilla,
        "faltantes": faltantes,
        "hay_historial": hay_historial
    }

# ==========================================
# RUTAS DE EXPORTACIÓN / APERTURA RRHH
# ==========================================
@app.post("/api/cargar-plantilla")
async def cargar_plantilla(file: UploadFile = File(...), db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT estatus FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if config and config['estatus'] == 'Cerrada':
        raise HTTPException(status_code=403, detail="La semana ya fue exportada y CERRADA.")

    try:
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents))
        
        cursor.execute("DELETE FROM mi_unidad")
        registros_exitosos = 0
        registros_rechazados = 0
        
        for index, fila in df.iterrows():
            id_emp = str(fila.get('ID_Empleado', '')).strip()
            if not id_emp or id_emp == 'nan': continue
            
            origen_val = str(fila.get('Origen', 'Propio')).strip()
            if origen_val not in ['Propio', 'Sub-Unidad']: origen_val = 'Propio'
            
            unidad_val = str(fila.get('Unidad', 'Sin Asignar')).strip()
            if not unidad_val or unidad_val == 'nan': unidad_val = 'Sin Asignar'
            
            cursor.execute("SELECT nombre_completo FROM empleados WHERE id_empleado = %s", (id_emp,))
            if cursor.fetchone():
                cursor.execute("""
                    INSERT INTO mi_unidad (id_empleado, origen, unidad) 
                    VALUES (%s, %s, %s)
                    ON CONFLICT (id_empleado) DO UPDATE 
                    SET origen = EXCLUDED.origen, unidad = EXCLUDED.unidad
                """, (id_emp, origen_val, unidad_val))
                registros_exitosos += 1
            else:
                registros_rechazados += 1
        
        db.commit()
        return {"success": True, "mensaje": f"Plantilla actualizada.\nExitosos: {registros_exitosos}"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al procesar Excel: {str(e)}")

@app.post("/api/importar-semana")
async def importar_semana(file: UploadFile = File(...), db = Depends(get_db)):
    cursor = db.cursor()
    try:
        contents = await file.read()
        archivo_encriptado = io.BytesIO(contents)
        archivo_desencriptado = io.BytesIO()
        
        try:
            of = msoffcrypto.OfficeFile(archivo_encriptado)
            of.load_key(password="RRHH_Clave_2026")
            of.decrypt(archivo_desencriptado)
        except Exception:
            raise HTTPException(status_code=400, detail="Contraseña incorrecta o el archivo no es el oficial de RRHH.")
            
        df_config = pd.read_excel(archivo_desencriptado, sheet_name="Configuracion")
        df_personal = pd.read_excel(archivo_desencriptado, sheet_name="Personal_Activo")
        df_novedades = pd.read_excel(archivo_desencriptado, sheet_name="Novedades")
        
        f_ini = pd.to_datetime(df_config['Fecha_Inicio'].iloc[0]).strftime('%Y-%m-%d')
        f_fin = pd.to_datetime(df_config['Fecha_Fin'].iloc[0]).strftime('%Y-%m-%d')
        
        cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
        row_config = cursor.fetchone()
        es_misma_semana = False
        if row_config and str(row_config['semana_inicio']) == f_ini and str(row_config['semana_fin']) == f_fin:
            es_misma_semana = True

        cursor.execute("DELETE FROM configuracion_sistema")
        cursor.execute("INSERT INTO configuracion_sistema (id, semana_inicio, semana_fin, estatus) VALUES (1, %s, %s, 'Abierta')", (f_ini, f_fin))
        cursor.execute("INSERT INTO historial_semanas (semana_inicio, semana_fin) VALUES (%s, %s) ON CONFLICT DO NOTHING", (f_ini, f_fin))
        
        for idx, fila in df_personal.iterrows():
            emp = str(fila['ID_Empleado']).strip()
            if emp and emp != 'nan':
                nombre = str(fila['Nombre_Completo']).strip()
                f_ingreso_raw = fila.get('Fecha_Ingreso', None)
                f_ingreso_db = '2000-01-01' if pd.isna(f_ingreso_raw) else pd.to_datetime(f_ingreso_raw).strftime('%Y-%m-%d')
                
                cursor.execute('''
                    INSERT INTO empleados (id_empleado, nombre_completo, tipo_horario, fecha_ingreso) 
                    VALUES (%s, %s, 'Sin Asignar', %s)
                    ON CONFLICT (id_empleado) DO UPDATE 
                    SET nombre_completo = EXCLUDED.nombre_completo,
                        fecha_ingreso = EXCLUDED.fecha_ingreso
                ''', (emp, nombre, f_ingreso_db))
                
        cursor.execute("DELETE FROM novedades_rrhh")
        cargas = 0
        for idx, fila in df_novedades.iterrows():
            emp = str(fila['ID_Empleado']).strip()
            if emp and emp != 'nan':
                tipo_nov = str(fila['Tipo_Novedad']).strip().capitalize()
                f_ini_nov = pd.to_datetime(fila['Fecha_Inicio']).strftime('%Y-%m-%d')
                f_fin_nov = pd.to_datetime(fila['Fecha_Fin']).strftime('%Y-%m-%d')
                
                cursor.execute("INSERT INTO novedades_rrhh (id_empleado, tipo_novedad, fecha_inicio, fecha_fin) VALUES (%s, %s, %s, %s)", 
                               (emp, tipo_nov, f_ini_nov, f_fin_nov))
                cargas += 1
                
        db.commit()
        if es_misma_semana: msg = f"Matriz actualizada con éxito.\nSe conservó su trabajo actual.\nNuevas novedades inyectadas: {cargas}"
        else: msg = f"Nueva semana habilitada ({f_ini} al {f_fin}).\nBase de datos actualizada con {cargas} novedades."
            
        return {"success": True, "mensaje": msg}
    except KeyError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"El Excel no tiene la estructura correcta. Falta la columna: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al procesar el archivo: {str(e)}")

# ==========================================
# NUEVAS RUTAS: REGISTRO Y ENVÍO PROFESIONAL
# ==========================================
@app.get("/api/mi-asistencia/{unidad_nombre}")
def obtener_asistencia_unidad(unidad_nombre: str, db = Depends(get_db)):
    """ Envía al Frontend la lista de empleados y su asistencia actual """
    cursor = db.cursor()
    
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config:
        return {"semana_activa": False, "empleados": []}
        
    cursor.execute("""
        SELECT e.id_empleado, e.nombre_completo, r.fecha, r.estatus, r.motivo
        FROM mi_unidad u
        JOIN empleados e ON u.id_empleado = e.id_empleado
        LEFT JOIN registro_asistencia r ON e.id_empleado = r.id_empleado 
            AND r.fecha BETWEEN %s AND %s
        WHERE u.unidad = %s
        ORDER BY e.nombre_completo ASC, r.fecha ASC
    """, (config['semana_inicio'], config['semana_fin'], unidad_nombre))
    
    registros = cursor.fetchall()
    
    empleados_dict = {}
    for reg in registros:
        emp_id = reg['id_empleado']
        if emp_id not in empleados_dict:
            empleados_dict[emp_id] = {
                "id_empleado": emp_id,
                "nombre_completo": reg['nombre_completo'],
                "asistencia": {}
            }
        
        if reg['fecha']: 
            fecha_str = reg['fecha'].strftime('%Y-%m-%d')
            empleados_dict[emp_id]["asistencia"][fecha_str] = {
                "estatus": reg['estatus'],
                "motivo": reg['motivo']
            }
            
    cursor.execute("""
        SELECT estatus_entrega FROM estado_entregas 
        WHERE unidad_nombre = %s AND semana_inicio = %s AND semana_fin = %s
    """, (unidad_nombre, config['semana_inicio'], config['semana_fin']))
    entrega = cursor.fetchone()
    bloqueado = True if entrega and entrega['estatus_entrega'] == 'Enviado a RRHH' else False
            
    return {
        "semana_activa": True,
        "fecha_inicio": config['semana_inicio'].strftime('%Y-%m-%d'),
        "fecha_fin": config['semana_fin'].strftime('%Y-%m-%d'),
        "bloqueado_por_envio": bloqueado,
        "empleados": list(empleados_dict.values())
    }

@app.post("/api/guardar-dia")
def guardar_dia(req: AsistenciaDiaRequest, db = Depends(get_db)):
    """ Endpoint web para que las Unidades Principales marquen 1 día """
    if req.estatus not in ESTATUS_PERMITIDOS:
        raise HTTPException(status_code=400, detail=f"Estatus inválido. Solo se permite: {', '.join(ESTATUS_PERMITIDOS)}")
    
    if req.estatus == "Falta Injustificada" and not req.motivo.strip():
        raise HTTPException(status_code=400, detail="Debe indicar un motivo para la Falta Injustificada.")

    cursor = db.cursor()
    cursor.execute("SELECT estatus FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if config and config['estatus'] == 'Cerrada':
        raise HTTPException(status_code=403, detail="La semana maestra ya fue cerrada por RRHH.")

    cursor.execute("""
        INSERT INTO registro_asistencia (fecha, id_empleado, estatus, motivo)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (fecha, id_empleado) DO UPDATE 
        SET estatus = EXCLUDED.estatus, motivo = EXCLUDED.motivo
    """, (req.fecha, req.id_empleado, req.estatus, req.motivo))
    
    db.commit()
    return {"success": True, "mensaje": "Día registrado exitosamente."}

@app.post("/api/enviar-a-rrhh")
def enviar_a_rrhh(req: EnvioRRHHRequest, db = Depends(get_db)):
    """ Botón de Envío: Calcula el 100% matemáticamente """
    cursor = db.cursor()
    
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config:
        raise HTTPException(status_code=400, detail="No hay una semana activa configurada por RRHH.")
        
    semana_ini, semana_fin = config['semana_inicio'], config['semana_fin']
    
    cursor.execute("""
        SELECT estatus_entrega FROM estado_entregas 
        WHERE unidad_nombre = %s AND semana_inicio = %s AND semana_fin = %s
    """, (req.unidad_nombre, semana_ini, semana_fin))
    entrega_previa = cursor.fetchone()
    if entrega_previa and entrega_previa['estatus_entrega'] == 'Enviado a RRHH':
        raise HTTPException(status_code=400, detail="Esta Gerencia ya envió su reporte a RRHH.")

    cursor.execute("SELECT COUNT(*) as total_emp FROM mi_unidad WHERE unidad = %s", (req.unidad_nombre,))
    total_empleados = cursor.fetchone()['total_emp']
    
    if total_empleados == 0:
        raise HTTPException(status_code=400, detail="No tiene personal adscrito a su Gerencia/Unidad.")

    dias_esperados = total_empleados * 7

    cursor.execute("""
        SELECT COUNT(*) as total_registros
        FROM registro_asistencia r
        INNER JOIN mi_unidad u ON r.id_empleado = u.id_empleado
        WHERE u.unidad = %s AND r.fecha BETWEEN %s AND %s
    """, (req.unidad_nombre, semana_ini, semana_fin))
    dias_registrados = cursor.fetchone()['total_registros']

    if dias_registrados < dias_esperados:
        faltan = dias_esperados - dias_registrados
        porcentaje = (dias_registrados / dias_esperados) * 100
        raise HTTPException(status_code=403, detail=f"Operación denegada. Solo ha reportado el {porcentaje:.1f}%. Faltan {faltan} días por justificar en su plantilla.")

    cursor.execute("""
        INSERT INTO estado_entregas (unidad_nombre, semana_inicio, semana_fin, estatus_entrega, fecha_envio)
        VALUES (%s, %s, %s, 'Enviado a RRHH', NOW())
        ON CONFLICT (unidad_nombre, semana_inicio, semana_fin) DO UPDATE 
        SET estatus_entrega = 'Enviado a RRHH', fecha_envio = NOW()
    """, (req.unidad_nombre, semana_ini, semana_fin))
    
    db.commit()
    return {"success": True, "mensaje": "¡100% Completado! Reporte bloqueado y enviado a Recursos Humanos."}

# ==========================================
# RUTAS ESTÁTICAS / WEB
# ==========================================
@app.get("/")
@app.get("/index.html")
def servir_interfaz():
    ruta_html = os.path.join(os.path.dirname(os.path.dirname(__file__)), "index.html")
    if os.path.exists(ruta_html):
        return FileResponse(ruta_html)
    raise HTTPException(status_code=404, detail="Archivo index.html no encontrado.")
