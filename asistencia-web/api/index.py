from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional
import hashlib
import os
import io
import pandas as pd
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row

app = FastAPI(title="Asistencia Movilnet ERP v5.8")

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
    "", "Asistencia Normal", "Teletrabajo", "Reposo", "Vacaciones", 
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
    username: str
    p00: str
    fecha: str
    estatus: str
    motivo: Optional[str] = ""

class RangoAsistenciaRequest(BaseModel):
    username: str
    p00: str
    fecha_inicio: str
    fecha_fin: str
    estatus: str
    motivo: str

class EnvioRRHHRequest(BaseModel):
    unidad_nombre: str
    rol_usuario: str

class EmpleadoUnidadRequest(BaseModel):
    username: str
    p00: str

class ReversarRequest(BaseModel):
    jurisdiccion: str

def is_jurisdiction_locked(cursor, username, semana_ini, semana_fin):
    cursor.execute("SELECT rol, unidad_administrativa, gerencia_general FROM usuarios WHERE username = %s", (username,))
    usr = cursor.fetchone()
    if not usr or usr['rol'] == 'Recursos Humanos': return False
    
    query = """
        SELECT 1 FROM estado_entregas e
        JOIN usuarios u ON e.unidad_nombre = u.username
        WHERE e.semana_inicio = %s AND e.semana_fin = %s
        AND u.unidad_administrativa = %s
        AND (u.gerencia_general = '' OR u.gerencia_general IS NULL OR u.gerencia_general = %s)
    """
    cursor.execute(query, (semana_ini, semana_fin, usr['unidad_administrativa'], usr['gerencia_general']))
    return bool(cursor.fetchone())

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
        raise HTTPException(status_code=400, detail="El usuario ya existe")

@app.delete("/api/rrhh/usuarios/{username}")
def eliminar_usuario(username: str, db = Depends(get_db)):
    if username == "admin": raise HTTPException(status_code=400, detail="No se puede eliminar al administrador")
    cursor = db.cursor()
    cursor.execute("DELETE FROM usuarios WHERE username = %s", (username,))
    db.commit()
    return {"success": True, "mensaje": "Usuario eliminado"}

# ==========================================
# 2. CARGA DE ESTRUCTURA Y NÓMINA (RRHH)
# ==========================================
@app.post("/api/rrhh/cargar-organizacion")
async def cargar_organizacion(file: UploadFile = File(...), db = Depends(get_db)):
    cursor = db.cursor()
    try:
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents))
        if 'Unidad_Administrativa' not in df.columns or 'Gerencia_General' not in df.columns:
            raise HTTPException(status_code=400, detail="Faltan columnas requeridas.")
        cargas = 0
        for _, fila in df.iterrows():
            ua = str(fila['Unidad_Administrativa']).strip()
            gg = str(fila['Gerencia_General']).strip()
            if ua and ua != 'nan' and gg and gg != 'nan':
                cursor.execute("INSERT INTO organizacion (unidad_administrativa, gerencia_general) VALUES (%s, %s) ON CONFLICT DO NOTHING", (ua, gg))
                cargas += 1
        db.commit()
        return {"success": True, "mensaje": f"Estructura Oficial Actualizada.\nGerencias registradas: {cargas}"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/rrhh/organizacion")
def obtener_organizacion(db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT unidad_administrativa, gerencia_general FROM organizacion ORDER BY unidad_administrativa ASC, gerencia_general ASC")
    estructura = {}
    for f in cursor.fetchall():
        ua = f['unidad_administrativa']
        gg = f['gerencia_general']
        if ua not in estructura: estructura[ua] = []
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
                
                cursor.execute("INSERT INTO organizacion (unidad_administrativa, gerencia_general) VALUES (%s, %s) ON CONFLICT DO NOTHING", (unidad, gerencia))
                cursor.execute("""
                    INSERT INTO empleados_maestro (p00, cedula, nombres_apellidos, fecha_ingreso, unidad_administrativa, gerencia_general, estatus)
                    VALUES (%s, %s, %s, %s, %s, %s, 'Activo')
                    ON CONFLICT (p00) DO UPDATE SET unidad_administrativa = EXCLUDED.unidad_administrativa, gerencia_general = EXCLUDED.gerencia_general, estatus = 'Activo'
                """, (p00, cedula, nombres, f_ingreso, unidad, gerencia))
                nuevos_emp += 1

        cursor.execute("DELETE FROM ausentismos_rrhh")
        cargas_aus = 0
        estatus_map = {
            "reposo": "Reposo", "vacaciones": "Vacaciones", "permiso otorgado": "Permiso otorgado",
            "permiso": "Permiso otorgado", "falta injustificada": "Falta Injustificada", "falta": "Falta Injustificada"
        }

        for _, fila in df_absentismos.iterrows():
            p00 = str(fila['P00']).strip()
            if p00 and p00 != 'nan':
                tipo_raw = str(fila['Tipo_Ausentismo']).strip()
                tipo = estatus_map.get(tipo_raw.lower(), tipo_raw.capitalize())
                f_ini_aus_dt = pd.to_datetime(fila['Fecha_Inicio'])
                f_fin_aus_dt = pd.to_datetime(fila['Fecha_Fin'])
                
                cursor.execute("INSERT INTO ausentismos_rrhh (p00, tipo_ausentismo, fecha_inicio, fecha_fin) VALUES (%s, %s, %s, %s)", 
                               (p00, tipo, f_ini_aus_dt.strftime('%Y-%m-%d'), f_fin_aus_dt.strftime('%Y-%m-%d')))
                cargas_aus += 1
                
                d_ini_sem = pd.to_datetime(f_ini); d_fin_sem = pd.to_datetime(f_fin)
                start_date = max(f_ini_aus_dt, d_ini_sem)
                end_date = min(f_fin_aus_dt, d_fin_sem)
                
                if start_date <= end_date:
                    curr_date = start_date
                    while curr_date <= end_date:
                        cursor.execute("""
                            INSERT INTO asistencia_diaria (p00, fecha, estatus, motivo) 
                            VALUES (%s, %s, %s, 'Cargado automáticamente por RRHH')
                            ON CONFLICT (p00, fecha) DO UPDATE SET estatus = EXCLUDED.estatus, motivo = EXCLUDED.motivo
                        """, (p00, curr_date.strftime('%Y-%m-%d'), tipo))
                        curr_date += timedelta(days=1)
                
        db.commit()
        return {"success": True, "mensaje": f"Semana habilitada exitosamente.\nPersonal: {nuevos_emp} | Auto-llenados: {cargas_aus}"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 3. GESTIÓN AUTÓNOMA DE PERSONAL (UNIDADES)
# ==========================================
@app.post("/api/unidad/agregar-empleado")
def agregar_empleado_unidad(req: EmpleadoUnidadRequest, db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT username FROM plantilla_local WHERE p00 = %s", (req.p00,))
    reclamado_por = cursor.fetchone()
    if reclamado_por and reclamado_por['username'] != req.username:
        raise HTTPException(status_code=400, detail=f"Bloqueado: El P00 {req.p00} ya está siendo reportado por '{reclamado_por['username']}'.")

    cursor.execute("SELECT unidad_administrativa, gerencia_general FROM usuarios WHERE username = %s", (req.username,))
    usr = cursor.fetchone()
    if not usr: raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    cursor.execute("SELECT unidad_administrativa, gerencia_general, nombres_apellidos FROM empleados_maestro WHERE p00 = %s AND estatus = 'Activo'", (req.p00,))
    emp = cursor.fetchone()
    if not emp: raise HTTPException(status_code=400, detail=f"El P00 {req.p00} no existe en RRHH o está Inactivo.")
        
    if usr['unidad_administrativa'] and emp['unidad_administrativa'] != usr['unidad_administrativa']:
        raise HTTPException(status_code=400, detail=f"Bloqueado: Pertenece a la Unidad '{emp['unidad_administrativa']}'.")
    if usr['gerencia_general'] and emp['gerencia_general'] != usr['gerencia_general']:
        raise HTTPException(status_code=400, detail=f"Bloqueado: Pertenece a la Gerencia '{emp['gerencia_general']}'.")

    try:
        cursor.execute("INSERT INTO plantilla_local (username, p00) VALUES (%s, %s) ON CONFLICT DO NOTHING", (req.username, req.p00))
        db.commit()
        return {"success": True, "mensaje": f"{emp['nombres_apellidos']} asignado a su personal a cargo."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail="Error vinculando al empleado.")

@app.post("/api/unidad/cargar-empleados-excel")
async def cargar_empleados_excel_unidad(username: str = Form(...), file: UploadFile = File(...), db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT unidad_administrativa, gerencia_general FROM usuarios WHERE username = %s", (username,))
    usr = cursor.fetchone()
    
    try:
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents))
        if 'P00' not in df.columns: raise HTTPException(status_code=400, detail="Falta columna 'P00'.")

        cargas = 0; errores = []
        for _, fila in df.iterrows():
            p00 = str(fila['P00']).strip()
            if p00 and p00 != 'nan':
                cursor.execute("SELECT username FROM plantilla_local WHERE p00 = %s", (p00,))
                reclamado_por = cursor.fetchone()
                if reclamado_por and reclamado_por['username'] != username:
                    errores.append(f"{p00} (Ya reportado por {reclamado_por['username']})")
                    continue

                cursor.execute("SELECT unidad_administrativa, gerencia_general FROM empleados_maestro WHERE p00 = %s AND estatus = 'Activo'", (p00,))
                emp = cursor.fetchone()
                
                if not emp: errores.append(f"{p00} (Inactivo/No Existe)"); continue
                if usr['unidad_administrativa'] and emp['unidad_administrativa'] != usr['unidad_administrativa']: errores.append(f"{p00} (Otra VP)"); continue
                if usr['gerencia_general'] and emp['gerencia_general'] != usr['gerencia_general']: errores.append(f"{p00} (Otra GG)"); continue
                    
                cursor.execute("INSERT INTO plantilla_local (username, p00) VALUES (%s, %s) ON CONFLICT DO NOTHING", (username, p00))
                cargas += 1
                
        db.commit()
        msg = f"Se integraron {cargas} empleados."
        if errores: msg += f"\nRechazados o Duplicados: {len(errores)}"
        return {"success": True, "mensaje": msg}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/unidad/remover-empleado/{username}/{p00}")
def remover_empleado_unidad(username: str, p00: str, db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("DELETE FROM plantilla_local WHERE username = %s AND p00 = %s", (username, p00))
    db.commit()
    return {"success": True, "mensaje": "Empleado desvinculado correctamente."}

# ==========================================
# 4. CONTROL DE ASISTENCIA DIARIA (VISTA ACTIVA)
# ==========================================
@app.get("/api/mi-asistencia/{username}")
def obtener_asistencia_unidad(username: str, db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config: return {"semana_activa": False, "empleados": []}
        
    semana_ini, semana_fin = config['semana_inicio'], config['semana_fin']

    cursor.execute("SELECT rol, unidad_administrativa, gerencia_general FROM usuarios WHERE username = %s", (username,))
    usr = cursor.fetchone()
    if not usr: raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    bloqueado = is_jurisdiction_locked(cursor, username, semana_ini, semana_fin)
    
    cursor.execute("SELECT p00 FROM plantilla_local WHERE username = %s", (username,))
    mis_p00s = {row['p00'] for row in cursor.fetchall()}

    query_emp = """
        SELECT e.p00, e.nombres_apellidos, e.cedula, e.unidad_administrativa, e.gerencia_general, a.fecha, a.estatus, a.motivo
        FROM empleados_maestro e
        LEFT JOIN asistencia_diaria a ON e.p00 = a.p00 AND a.fecha BETWEEN %s AND %s
        WHERE e.estatus = 'Activo'
    """
    params = [semana_ini, semana_fin]
    
    if usr['rol'] == 'Reportador':
        query_emp += " AND e.p00 IN (SELECT p00 FROM plantilla_local WHERE username = %s)"
        params.append(username)
    else:
        if usr['unidad_administrativa']:
            query_emp += " AND e.unidad_administrativa = %s"
            params.append(usr['unidad_administrativa'])
        if usr['gerencia_general']:
            query_emp += " AND e.gerencia_general = %s"
            params.append(usr['gerencia_general'])
        
    query_emp += " ORDER BY e.gerencia_general ASC, e.nombres_apellidos ASC, a.fecha ASC"
    
    cursor.execute(query_emp, params)
    registros = cursor.fetchall()
    
    cursor.execute("SELECT p00, tipo_ausentismo, fecha_inicio, fecha_fin FROM ausentismos_rrhh WHERE fecha_inicio <= %s AND fecha_fin >= %s", (semana_fin, semana_ini))
    ausentismos_fijos = [{"p00": a['p00'], "tipo_ausentismo": a['tipo_ausentismo'], "fecha_inicio": a['fecha_inicio'].strftime('%Y-%m-%d'), "fecha_fin": a['fecha_fin'].strftime('%Y-%m-%d')} for a in cursor.fetchall()]
    
    empleados_dict = {}
    for reg in registros:
        p00 = reg['p00']
        if p00 not in empleados_dict:
            empleados_dict[p00] = {
                "p00": p00, "nombres_apellidos": reg['nombres_apellidos'], 
                "unidad": reg['unidad_administrativa'], "gerencia": reg['gerencia_general'], 
                "editable": True if usr['rol'] == 'Reportador' else (p00 in mis_p00s),
                "asistencia": {}
            }
        if reg['fecha']: 
            fecha_str = reg['fecha'].strftime('%Y-%m-%d')
            empleados_dict[p00]["asistencia"][fecha_str] = {"estatus": reg['estatus'], "motivo": reg['motivo']}
            
    return {
        "semana_activa": True, "fecha_inicio": semana_ini.strftime('%Y-%m-%d'), "fecha_fin": semana_fin.strftime('%Y-%m-%d'),
        "bloqueado_por_envio": bloqueado, "empleados": list(empleados_dict.values()), "ausentismos_fijos": ausentismos_fijos
    }

@app.post("/api/guardar-dia")
def guardar_dia(req: AsistenciaDiaRequest, db = Depends(get_db)):
    if req.estatus not in ESTATUS_PERMITIDOS: raise HTTPException(status_code=400, detail="Estatus inválido.")
    if req.estatus in ["Falta Injustificada", "Permiso otorgado"] and not req.motivo.strip(): 
        raise HTTPException(status_code=400, detail="Debe indicar el motivo.")

    cursor = db.cursor()
    cursor.execute("SELECT semana_inicio, semana_fin, estatus FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if config and config['estatus'] == 'Cerrada': raise HTTPException(status_code=403, detail="Semana cerrada.")

    if is_jurisdiction_locked(cursor, req.username, config['semana_inicio'], config['semana_fin']):
        raise HTTPException(status_code=403, detail="Bloqueado: La Jefatura de Unidad ya certificó y cerró la asistencia.")

    cursor.execute("SELECT 1 FROM plantilla_local WHERE username = %s AND p00 = %s", (req.username, req.p00))
    if not cursor.fetchone():
        raise HTTPException(status_code=403, detail="Error: Solo puede editar personal en su plantilla local.")

    if req.estatus == "":
        cursor.execute("DELETE FROM asistencia_diaria WHERE p00 = %s AND fecha = %s", (req.p00, req.fecha))
    else:
        cursor.execute("""
            INSERT INTO asistencia_diaria (p00, fecha, estatus, motivo) VALUES (%s, %s, %s, %s)
            ON CONFLICT (p00, fecha) DO UPDATE SET estatus = EXCLUDED.estatus, motivo = EXCLUDED.motivo
        """, (req.p00, req.fecha, req.estatus, req.motivo))
        
    db.commit()
    return {"success": True}

@app.post("/api/guardar-rango")
def guardar_rango(req: RangoAsistenciaRequest, db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT semana_inicio, semana_fin, estatus FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if config and config['estatus'] == 'Cerrada': raise HTTPException(status_code=403, detail="Semana cerrada.")

    if is_jurisdiction_locked(cursor, req.username, config['semana_inicio'], config['semana_fin']):
        raise HTTPException(status_code=403, detail="Bloqueado: La Jefatura de Unidad ya certificó y cerró la asistencia.")

    cursor.execute("SELECT 1 FROM plantilla_local WHERE username = %s AND p00 = %s", (req.username, req.p00))
    if not cursor.fetchone():
        raise HTTPException(status_code=403, detail="Error: Solo puede editar personal en su plantilla local.")

    d_ini = pd.to_datetime(req.fecha_inicio)
    d_fin = pd.to_datetime(req.fecha_fin)
    sem_ini = pd.to_datetime(config['semana_inicio'])
    sem_fin = pd.to_datetime(config['semana_fin'])
    
    start_date = max(d_ini, sem_ini)
    end_date = min(d_fin, sem_fin)
    
    if start_date <= end_date:
        curr_date = start_date
        while curr_date <= end_date:
            cursor.execute("""
                INSERT INTO asistencia_diaria (p00, fecha, estatus, motivo) VALUES (%s, %s, %s, %s)
                ON CONFLICT (p00, fecha) DO UPDATE SET estatus = EXCLUDED.estatus, motivo = EXCLUDED.motivo
            """, (req.p00, curr_date.strftime('%Y-%m-%d'), req.estatus, req.motivo))
            curr_date += timedelta(days=1)
            
    db.commit()
    return {"success": True}

@app.post("/api/enviar-a-rrhh")
def enviar_a_rrhh(req: EnvioRRHHRequest, db = Depends(get_db)):
    if req.rol_usuario == "Reportador": raise HTTPException(status_code=403, detail="Perfil no autorizado para certificar.")

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
        raise HTTPException(status_code=403, detail=f"Auditoría Fallida: Reporte de Jurisdicción al {(dias_registrados/dias_esperados)*100:.1f}%.")

    cursor.execute("""
        INSERT INTO estado_entregas (unidad_nombre, semana_inicio, semana_fin, estatus_entrega, fecha_envio)
        VALUES (%s, %s, %s, 'Enviado a RRHH', NOW())
        ON CONFLICT (unidad_nombre, semana_inicio, semana_fin) DO UPDATE SET estatus_entrega = 'Enviado a RRHH', fecha_envio = NOW()
    """, (req.unidad_nombre, semana_ini, semana_fin))
    db.commit()
    return {"success": True, "mensaje": "Reporte certificado y enviado a RRHH."}

# ==========================================
# 5. HISTÓRICO DE SEMANAS CERRADAS
# ==========================================
@app.get("/api/unidad/semanas-cerradas/{username}")
def semanas_cerradas(username: str, db = Depends(get_db)):
    """ Devuelve un listado de las semanas que ya fueron certificadas por la jurisdicción del usuario """
    cursor = db.cursor()
    cursor.execute("SELECT unidad_administrativa, gerencia_general FROM usuarios WHERE username = %s", (username,))
    usr = cursor.fetchone()
    if not usr: return {"semanas": []}
    
    query = """
        SELECT DISTINCT e.semana_inicio, e.semana_fin
        FROM estado_entregas e
        JOIN usuarios u ON e.unidad_nombre = u.username
        WHERE u.unidad_administrativa = %s 
        AND (u.gerencia_general = '' OR u.gerencia_general IS NULL OR u.gerencia_general = %s)
        ORDER BY e.semana_inicio DESC
    """
    cursor.execute(query, (usr['unidad_administrativa'], usr['gerencia_general']))
    filas = cursor.fetchall()
    semanas = [{"inicio": f['semana_inicio'].strftime('%Y-%m-%d'), "fin": f['semana_fin'].strftime('%Y-%m-%d')} for f in filas]
    return {"success": True, "semanas": semanas}

@app.get("/api/unidad/historico-semana/{username}")
def historico_semana(username: str, inicio: str, fin: str, db = Depends(get_db)):
    """ Devuelve la matriz completa de una semana pasada (Modo Solo Lectura Panorámico) """
    cursor = db.cursor()
    cursor.execute("SELECT rol, unidad_administrativa, gerencia_general FROM usuarios WHERE username = %s", (username,))
    usr = cursor.fetchone()
    
    query_emp = """
        SELECT e.p00, e.nombres_apellidos, e.cedula, e.unidad_administrativa, e.gerencia_general, a.fecha, a.estatus, a.motivo
        FROM empleados_maestro e
        LEFT JOIN asistencia_diaria a ON e.p00 = a.p00 AND a.fecha BETWEEN %s AND %s
        WHERE e.estatus = 'Activo'
    """
    params = [inicio, fin]
    
    if usr['rol'] == 'Reportador':
        query_emp += " AND e.p00 IN (SELECT p00 FROM plantilla_local WHERE username = %s)"
        params.append(username)
    else:
        if usr['unidad_administrativa']:
            query_emp += " AND e.unidad_administrativa = %s"
            params.append(usr['unidad_administrativa'])
        if usr['gerencia_general']:
            query_emp += " AND e.gerencia_general = %s"
            params.append(usr['gerencia_general'])
        
    query_emp += " ORDER BY e.gerencia_general ASC, e.nombres_apellidos ASC, a.fecha ASC"
    
    cursor.execute(query_emp, params)
    registros = cursor.fetchall()
    
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
            
    return {
        "success": True, "fecha_inicio": inicio, "fecha_fin": fin,
        "empleados": list(empleados_dict.values())
    }

# ==========================================
# 6. MONITOREO RRHH (AGRUPACIÓN DINÁMICA POR ESTRUCTURA) Y DESCARGAS
# ==========================================
@app.post("/api/rrhh/reversar-cierre")
def reversar_cierre(req: ReversarRequest, db = Depends(get_db)):
    """ Elimina la firma de cierre de una jurisdicción. Esto liberará los candados y la descargá será bloqueada """
    cursor = db.cursor()
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config: raise HTTPException(status_code=400, detail="No hay semana activa.")

    query_users = """
        SELECT username FROM usuarios 
        WHERE rol = 'Cierre de Unidad' 
        AND (CASE WHEN unidad_administrativa = 'Presidencia' THEN gerencia_general ELSE unidad_administrativa END) = %s
    """
    cursor.execute(query_users, (req.jurisdiccion,))
    users = cursor.fetchall()
    
    if not users:
        raise HTTPException(status_code=404, detail="No se encontraron Jefaturas para esta Jurisdicción.")
        
    for u in users:
        cursor.execute("""
            DELETE FROM estado_entregas 
            WHERE unidad_nombre = %s AND semana_inicio = %s AND semana_fin = %s
        """, (u['username'], config['semana_inicio'], config['semana_fin']))
        
    db.commit()
    return {"success": True, "mensaje": f"Sello de cierre roto. La jurisdicción {req.jurisdiccion} vuelve a estatus PENDIENTE."}

@app.get("/api/rrhh/monitoreo-entregas")
def monitoreo_entregas(db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config: return {"semana_activa": False, "entregas": []}
    
    query = """
        WITH emp_dias AS (
            SELECT 
                e.p00, 
                e.unidad_administrativa, 
                e.gerencia_general,
                COUNT(a.fecha) as dias_reportados
            FROM empleados_maestro e
            LEFT JOIN asistencia_diaria a ON e.p00 = a.p00 AND a.fecha BETWEEN %s AND %s
            WHERE e.estatus = 'Activo'
            GROUP BY e.p00, e.unidad_administrativa, e.gerencia_general
        ),
        agrupado AS (
            SELECT 
                CASE 
                    WHEN unidad_administrativa = 'Presidencia' THEN gerencia_general
                    ELSE unidad_administrativa
                END as jurisdiccion,
                COUNT(p00) as total_empleados,
                SUM(CASE WHEN dias_reportados >= 7 THEN 1 ELSE 0 END) as completados
            FROM emp_dias
            GROUP BY 
                CASE 
                    WHEN unidad_administrativa = 'Presidencia' THEN gerencia_general
                    ELSE unidad_administrativa
                END
        ),
        certificaciones AS (
            SELECT 
                CASE 
                    WHEN u.unidad_administrativa = 'Presidencia' THEN u.gerencia_general 
                    ELSE u.unidad_administrativa 
                END as jurisdiccion,
                MAX(CASE WHEN e.estatus_entrega = 'Enviado a RRHH' THEN 1 ELSE 0 END) as esta_certificado
            FROM usuarios u
            LEFT JOIN estado_entregas e ON u.username = e.unidad_nombre AND e.semana_inicio = %s AND e.semana_fin = %s
            WHERE u.rol = 'Cierre de Unidad'
            GROUP BY 1
        )
        SELECT 
            a.jurisdiccion, 
            a.total_empleados, 
            a.completados, 
            COALESCE(c.esta_certificado, 0) as certificado
        FROM agrupado a
        LEFT JOIN certificaciones c ON a.jurisdiccion = c.jurisdiccion
        ORDER BY a.jurisdiccion ASC
    """
    
    cursor.execute(query, (config['semana_inicio'], config['semana_fin'], config['semana_inicio'], config['semana_fin']))
    resultados = cursor.fetchall()
            
    return {
        "semana_activa": True, 
        "fecha_inicio": config['semana_inicio'].strftime('%d/%m/%Y'), 
        "fecha_fin": config['semana_fin'].strftime('%d/%m/%Y'), 
        "entregas": resultados
    }

def generar_excel_agrupado(df_detalle, nombre_archivo):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        if not df_detalle.empty:
            df_detalle['fecha'] = pd.to_datetime(df_detalle['fecha']).dt.strftime('%d/%m/%Y')
            df_detalle.columns = ['Fecha', 'P00', 'Cédula', 'Nombres y Apellidos', 'Unidad Administrativa (VP)', 'Gerencia General', 'Estatus Asistencia', 'Motivo']
            
            df_detalle.to_excel(writer, index=False, sheet_name="Detalle_Asistencia")
            
            pagar_list = ["Asistencia Normal", "Vacaciones", "Dia Descanso Legal", "Dia Libre Compensatorio"]
            descontar_list = ["Teletrabajo", "Reposo", "Permiso otorgado", "Falta Injustificada"]
            
            df_resumen = df_detalle.copy()
            df_resumen['Días a Pagar'] = df_resumen['Estatus Asistencia'].apply(lambda x: 1 if x in pagar_list else 0)
            df_resumen['Días a Descontar'] = df_resumen['Estatus Asistencia'].apply(lambda x: 1 if x in descontar_list else 0)
            
            df_resumen = df_resumen.groupby(['P00', 'Cédula', 'Nombres y Apellidos', 'Unidad Administrativa (VP)', 'Gerencia General'], as_index=False).agg({
                'Días a Pagar': 'sum',
                'Días a Descontar': 'sum'
            })
            
            df_resumen.to_excel(writer, index=False, sheet_name="Resumen_Pago")
        else:
            pd.DataFrame(["No hay registros"]).to_excel(writer, index=False, sheet_name="Sin_Datos")
            
    output.seek(0)
    headers = {'Content-Disposition': f'attachment; filename="{nombre_archivo}.xlsx"', 'Access-Control-Expose-Headers': 'Content-Disposition'}
    return StreamingResponse(output, headers=headers, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.get("/api/rrhh/descargar-consolidado")
def descargar_consolidado(db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config: raise HTTPException(status_code=400, detail="No hay semana activa.")

    query_val = """
        WITH agrupado AS (
            SELECT CASE WHEN unidad_administrativa = 'Presidencia' THEN gerencia_general ELSE unidad_administrativa END as jurisdiccion
            FROM empleados_maestro WHERE estatus = 'Activo' GROUP BY 1
        ),
        certificaciones AS (
            SELECT CASE WHEN u.unidad_administrativa = 'Presidencia' THEN u.gerencia_general ELSE u.unidad_administrativa END as jurisdiccion
            FROM usuarios u JOIN estado_entregas e ON u.username = e.unidad_nombre
            WHERE e.semana_inicio = %s AND e.semana_fin = %s GROUP BY 1
        )
        SELECT a.jurisdiccion FROM agrupado a
        LEFT JOIN certificaciones c ON a.jurisdiccion = c.jurisdiccion
        WHERE c.jurisdiccion IS NULL
    """
    cursor.execute(query_val, (config['semana_inicio'], config['semana_fin']))
    if cursor.fetchall():
        raise HTTPException(status_code=403, detail="Consolidado Bloqueado: Aún hay Unidades pendientes por certificar asistencia.")
        
    cursor.execute("""
        SELECT a.fecha, e.p00, e.cedula, e.nombres_apellidos, e.unidad_administrativa, e.gerencia_general, a.estatus, a.motivo 
        FROM asistencia_diaria a INNER JOIN empleados_maestro e ON a.p00 = e.p00
        WHERE a.fecha BETWEEN %s AND %s ORDER BY e.unidad_administrativa ASC, e.gerencia_general ASC, e.p00 ASC, a.fecha ASC
    """, (config['semana_inicio'], config['semana_fin']))
    
    df_detalle = pd.DataFrame(cursor.fetchall())
    nombre_archivo = f"Nomina_Consolidada_{config['semana_inicio']}"
    
    return generar_excel_agrupado(df_detalle, nombre_archivo)

@app.get("/api/rrhh/descargar-jurisdiccion/{jurisdiccion}")
def descargar_jurisdiccion(jurisdiccion: str, db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config: raise HTTPException(status_code=400, detail="No hay semana activa.")

    query_val = """
        SELECT 1 FROM usuarios u 
        JOIN estado_entregas e ON u.username = e.unidad_nombre
        WHERE e.semana_inicio = %s AND e.semana_fin = %s 
        AND (CASE WHEN u.unidad_administrativa = 'Presidencia' THEN u.gerencia_general ELSE u.unidad_administrativa END) = %s
    """
    cursor.execute(query_val, (config['semana_inicio'], config['semana_fin'], jurisdiccion))
    if not cursor.fetchone():
        raise HTTPException(status_code=403, detail="Bloqueado: Esta jurisdicción aún no está certificada.")
        
    query = """
        SELECT a.fecha, e.p00, e.cedula, e.nombres_apellidos, e.unidad_administrativa, e.gerencia_general, a.estatus, a.motivo 
        FROM asistencia_diaria a INNER JOIN empleados_maestro e ON a.p00 = e.p00
        WHERE a.fecha BETWEEN %s AND %s 
        AND (
            CASE 
                WHEN e.unidad_administrativa = 'Presidencia' THEN e.gerencia_general
                ELSE e.unidad_administrativa
            END
        ) = %s
        ORDER BY e.unidad_administrativa ASC, e.gerencia_general ASC, e.p00 ASC, a.fecha ASC
    """
    cursor.execute(query, (config['semana_inicio'], config['semana_fin'], jurisdiccion))
    
    df_detalle = pd.DataFrame(cursor.fetchall())
    safe_name = "".join([c for c in jurisdiccion if c.isalpha() or c.isdigit() or c==' ']).rstrip()
    nombre_archivo = f"Nomina_{safe_name}"
    
    return generar_excel_agrupado(df_detalle, nombre_archivo)

@app.get("/")
@app.get("/index.html")
def servir_interfaz():
    ruta_html = os.path.join(os.path.dirname(os.path.dirname(__file__)), "index.html")
    if os.path.exists(ruta_html): return FileResponse(ruta_html)
    raise HTTPException(status_code=404, detail="Archivo HTML no encontrado.")
