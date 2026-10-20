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
# MODELOS DE DATOS (Las reglas estrictas)
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
# RUTAS DE LA API (Endpoints)
# ==========================================
@app.post("/api/login")
def login(req: LoginRequest, db = Depends(get_db)):
    cursor = db.cursor()
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    
    cursor.execute("SELECT rol, username FROM usuarios WHERE username = %s AND password_hash = %s", (req.username, pwd_hash))
    user = cursor.fetchone()
    
    if user:
        # En el futuro, asociaremos la unidad_nombre directamente al usuario aquí
        return {"success": True, "rol": user['rol'], "username": user['username']}
    else:
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

@app.post("/api/guardar-dia")
def guardar_dia(req: AsistenciaDiaRequest, db = Depends(get_db)):
    """ Endpoint web para que las Unidades Principales marquen 1 día """
    # 1. Filtro estricto de los 7 estatus
    if req.estatus not in ESTATUS_PERMITIDOS:
        raise HTTPException(status_code=400, detail=f"Estatus inválido. Solo se permite: {', '.join(ESTATUS_PERMITIDOS)}")
    
    # 2. Validación de motivo para faltas
    if req.estatus == "Falta Injustificada" and not req.motivo.strip():
        raise HTTPException(status_code=400, detail="Debe indicar un motivo para la Falta Injustificada.")

    cursor = db.cursor()
    
    # Validar que la semana no esté cerrada por RRHH o enviada por la Unidad
    cursor.execute("SELECT estatus FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if config and config['estatus'] == 'Cerrada':
        raise HTTPException(status_code=403, detail="La semana maestra ya fue cerrada por RRHH.")

    # Insertar o actualizar el día
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
    
    # 1. Obtener la semana actual
    cursor.execute("SELECT semana_inicio, semana_fin FROM configuracion_sistema WHERE id = 1")
    config = cursor.fetchone()
    if not config:
        raise HTTPException(status_code=400, detail="No hay una semana activa configurada por RRHH.")
        
    semana_ini, semana_fin = config['semana_inicio'], config['semana_fin']
    
    # 2. Verificar que no se haya enviado ya
    cursor.execute("""
        SELECT estatus_entrega FROM estado_entregas 
        WHERE unidad_nombre = %s AND semana_inicio = %s AND semana_fin = %s
    """, (req.unidad_nombre, semana_ini, semana_fin))
    entrega_previa = cursor.fetchone()
    if entrega_previa and entrega_previa['estatus_entrega'] == 'Enviado a RRHH':
        raise HTTPException(status_code=400, detail="Esta Gerencia ya envió su reporte a RRHH.")

    # 3. Matemática del 100%
    # Empleados en esta unidad
    cursor.execute("SELECT COUNT(*) as total_emp FROM mi_unidad WHERE unidad = %s", (req.unidad_nombre,))
    total_empleados = cursor.fetchone()['total_emp']
    
    if total_empleados == 0:
        raise HTTPException(status_code=400, detail="No tiene personal adscrito a su Gerencia/Unidad.")

    dias_esperados = total_empleados * 7

    # Registros actuales de esos empleados en la semana activa
    cursor.execute("""
        SELECT COUNT(*) as total_registros
        FROM registro_asistencia r
        INNER JOIN mi_unidad u ON r.id_empleado = u.id_empleado
        WHERE u.unidad = %s AND r.fecha BETWEEN %s AND %s
    """, (req.unidad_nombre, semana_ini, semana_fin))
    dias_registrados = cursor.fetchone()['total_registros']

    # 4. Candado de Envío
    if dias_registrados < dias_esperados:
        faltan = dias_esperados - dias_registrados
        porcentaje = (dias_registrados / dias_esperados) * 100
        raise HTTPException(
            status_code=403, 
            detail=f"Operación denegada. Solo ha reportado el {porcentaje:.1f}%. Faltan {faltan} días por justificar en su plantilla."
        )

    # 5. Si tiene el 100%, cambiar estatus a Enviado
    cursor.execute("""
        INSERT INTO estado_entregas (unidad_nombre, semana_inicio, semana_fin, estatus_entrega, fecha_envio)
        VALUES (%s, %s, %s, 'Enviado a RRHH', NOW())
        ON CONFLICT (unidad_nombre, semana_inicio, semana_fin) DO UPDATE 
        SET estatus_entrega = 'Enviado a RRHH', fecha_envio = NOW()
    """, (req.unidad_nombre, semana_ini, semana_fin))
    
    db.commit()
    return {"success": True, "mensaje": "¡100% Completado! Reporte bloqueado y enviado a Recursos Humanos."}

@app.get("/")
@app.get("/index.html")
def servir_interfaz():
    ruta_html = os.path.join(os.path.dirname(os.path.dirname(__file__)), "index.html")
    if os.path.exists(ruta_html):
        return FileResponse(ruta_html)
    raise HTTPException(status_code=404, detail="Archivo index.html no encontrado.")
