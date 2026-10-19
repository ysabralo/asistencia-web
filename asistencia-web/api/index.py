from fastapi import FastAPI, HTTPException, UploadFile, File, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import pg8000.dbapi
import hashlib
import os
import io
import pandas as pd
from urllib.parse import urlparse, unquote

app = FastAPI(title="Backend Asistencia Movilnet")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# CONEXIÓN A SUPABASE (100% PYTHON PURO - INMUNE A BUGS DE VERCEL)
# ==========================================
def get_db():
    DATABASE_URL = os.environ.get("DATABASE_URL")
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="Falta DATABASE_URL")
    
    try:
        parsed = urlparse(DATABASE_URL)
        # Limpiamos caracteres especiales de la contraseña (como el %2A por el asterisco)
        pwd = unquote(parsed.password) if parsed.password else None
        
        # Conexión pura en Python
        conn = pg8000.dbapi.connect(
            user=parsed.username,
            password=pwd,
            host=parsed.hostname,
            port=parsed.port,
            database=parsed.path.lstrip("/"),
            ssl_context=True  # Supabase requiere conexión segura
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ERROR_CONEXION_PG8000: {str(e)}")
    
    try:
        yield conn
    finally:
        conn.close()

# Función mágica para devolver diccionarios (Reemplaza a RealDictCursor)
def dict_fetchone(cursor):
    row = cursor.fetchone()
    if not row:
        return None
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row))

# ==========================================
# RUTAS DE LA API (Endpoints)
# ==========================================
class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/api/login")
def login(req: LoginRequest, db = Depends(get_db)):
    cursor = db.cursor()
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    
    cursor.execute("SELECT rol FROM usuarios WHERE username = %s AND password = %s", (req.username, pwd_hash))
    user = dict_fetchone(cursor)
    
    if user:
        rol = user['rol']
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
    config = dict_fetchone(cursor)
    
    cursor.execute("SELECT COUNT(*) as total FROM mi_unidad")
    total_plantilla = dict_fetchone(cursor)['total']
    
    cursor.execute("SELECT COUNT(*) as total FROM historial_semanas")
    hay_historial = dict_fetchone(cursor)['total'] > 0

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
        empleados_completos = dict_fetchone(cursor)['emp_completos']
        faltantes = total_plantilla - empleados_completos

    return {
        "semana_activa": semana_activa,
        "semana_abierta": semana_abierta,
        "semana_cerrada": semana_cerrada,
        "fecha_inicio": config['semana_inicio'] if config else None,
        "fecha_fin": config['semana_fin'] if config else None,
        "total_plantilla": total_plantilla,
        "faltantes": faltantes,
        "hay_historial": hay_historial
    }

@app.post("/api/cargar-plantilla")
async def cargar_plantilla(file: UploadFile = File(...), db = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT estatus FROM configuracion_sistema WHERE id = 1")
    config = dict_fetchone(cursor)
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
            if dict_fetchone(cursor):
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

@app.get("/")
@app.get("/index.html")
def servir_interfaz():
    ruta_html = os.path.join(os.path.dirname(os.path.dirname(__file__)), "index.html")
    if os.path.exists(ruta_html):
        return FileResponse(ruta_html)
    raise HTTPException(status_code=404, detail="Archivo index.html no encontrado.")
