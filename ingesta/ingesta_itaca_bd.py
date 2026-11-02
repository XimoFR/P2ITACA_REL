"""Ingesta de datos ITACA (BD relacional PostgreSQL) a la zona raw.

Extrae las cinco tablas del modelo, anonimiza los datos personales y las
guarda como JSON en data/raw/. Se ejecuta desde la raíz del proyecto:

    python ingesta/ingesta_itaca_bd.py
"""
import hashlib
import json
import logging
import os
import sys
from datetime import date

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("ingesta_itaca_bd")

TABLAS = ["alumnos", "calificaciones", "contenidos", "cursos", "grupos"]
DIR_RAW = "data/raw"

# Columnas de alumnos que identifican a la persona: se eliminan antes de guardar.
COLUMNAS_ELIMINAR_ALUMNOS = [
    "nombre", "apellido1", "apellido2", "fecha_nac", "tipo_doc", "documento",
    "telefono1", "telefono2", "telefono3", "email1", "email2",
    "sip", "expediente", "cod_postal", "municipio", "localidad", "municipio_nac",
]


# ---------------------------------------------------------------- conexión
def conectar():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )


def extraer_tabla(conexion, tabla: str) -> list[dict]:
    if tabla not in TABLAS:  # el nombre de tabla no puede venir de fuera
        raise ValueError(f"Tabla no permitida: {tabla}")
    with conexion.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT * FROM {tabla}")
        return [dict(fila) for fila in cur.fetchall()]


# ------------------------------------------------------------ anonimización
class Anonimizador:
    """Hash SHA-256 con sal. Cada NIA se calcula una sola vez y se reutiliza,
    así alumnos y calificaciones siguen pudiendo cruzarse."""

    def __init__(self, sal: str):
        self.sal = sal
        self.cache: dict[str, str] = {}

    def nia(self, nia) -> str:
        nia = str(nia).strip()
        if nia not in self.cache:
            digest = hashlib.sha256((self.sal + nia).encode()).hexdigest()
            self.cache[nia] = digest[:10]
        return self.cache[nia]

    def comprobar_colisiones(self) -> None:
        if len(set(self.cache.values())) != len(self.cache):
            raise RuntimeError("Colisión de hash: dos NIA distintos dan el mismo valor")


def anyo_de(fecha: str):
    """'28/05/1982' -> 1982 (se conserva solo el año de nacimiento)."""
    try:
        return int(str(fecha).strip().split("/")[-1])
    except (ValueError, AttributeError):
        return None


def anonimizar(datos: dict[str, list[dict]], anon: Anonimizador) -> None:
    for fila in datos["alumnos"]:
        fila["anyo_nac"] = anyo_de(fila.get("fecha_nac"))
        fila["nia"] = anon.nia(fila["nia"])
        for col in COLUMNAS_ELIMINAR_ALUMNOS:
            fila.pop(col, None)
    for fila in datos["calificaciones"]:
        fila["alumno"] = anon.nia(fila["alumno"])  # 'alumno' es el NIA
    anon.comprobar_colisiones()


# -------------------------------------------------------------- validación
def vacio(valor) -> bool:
    return valor is None or str(valor).strip() == ""


def validar(datos: dict[str, list[dict]], anon: Anonimizador) -> list[str]:
    """Devuelve la lista de errores graves. Las incidencias de calidad del
    dato se registran como avisos (WARNING) y no detienen la ingesta."""
    errores: list[str] = []

    # 1. Anonimización: sin columnas identificativas ni NIA originales
    columnas_alumnos = set(datos["alumnos"][0]) if datos["alumnos"] else set()
    sobrantes = columnas_alumnos & set(COLUMNAS_ELIMINAR_ALUMNOS)
    if sobrantes:
        errores.append(f"alumnos conserva columnas personales: {sorted(sobrantes)}")
    nia_originales = set(anon.cache.keys())
    nia_guardados = {f["nia"] for f in datos["alumnos"]} | {
        f["alumno"] for f in datos["calificaciones"]}
    if nia_originales & nia_guardados:
        errores.append("Hay NIA originales sin anonimizar en los datos")

    # 2. Integridad referencial (todas las claves incluyen el año)
    def claves(tabla, *cols):
        return {tuple(f[c] for c in cols) for f in datos[tabla]}

    cursos = claves("cursos", "codigo", "anyo")
    contenidos = claves("contenidos", "codigo", "curso", "anyo")
    grupos = claves("grupos", "codigo", "anyo")
    alumnos = claves("alumnos", "nia", "anyo")

    def huerfanos(tabla, cols, destino, ignorar_vacios=False):
        n = 0
        for f in datos[tabla]:
            if ignorar_vacios and vacio(f[cols[0]]):
                continue
            if tuple(f[c] for c in cols) not in destino:
                n += 1
        return n

    # Errores graves: la estructura del catálogo debe ser coherente
    for nombre, n in {
        "calificaciones.contenido -> contenidos":
            huerfanos("calificaciones", ("contenido", "curso", "anyo"), contenidos),
        "calificaciones.curso -> cursos":
            huerfanos("calificaciones", ("curso", "anyo"), cursos),
        "alumnos.curso -> cursos":
            huerfanos("alumnos", ("curso", "anyo"), cursos),
        "contenidos.curso -> cursos":
            huerfanos("contenidos", ("curso", "anyo"), cursos),
    }.items():
        if n:
            errores.append(f"{nombre}: {n} filas sin referencia")

    # Avisos: incidencias conocidas del dato de origen
    sin_grupo = sum(1 for f in datos["alumnos"] if vacio(f["grupo"]))
    if sin_grupo:
        logger.warning("alumnos sin grupo asignado (vacío): %d", sin_grupo)
    n = huerfanos("alumnos", ("grupo", "anyo"), grupos, ignorar_vacios=True)
    if n:
        logger.warning("alumnos.grupo -> grupos: %d filas sin referencia", n)
    filas = [f for f in datos["calificaciones"]
             if (f["alumno"], f["anyo"]) not in alumnos]
    if filas:
        logger.warning(
            "calificaciones de alumnos que no están en alumnos ese año: "
            "%d filas, %d alumnos distintos",
            len(filas), len({f["alumno"] for f in filas}))
    visto, duplicados = set(), 0
    for f in datos["alumnos"]:
        k = (f["nia"], f["anyo"], f["curso"])
        duplicados += k in visto
        visto.add(k)
    if duplicados:
        logger.warning("alumnos con (nia, anyo, curso) repetido: %d", duplicados)

    return errores


# ------------------------------------------------------------------ guardado
def guardar(datos: list[dict], nombre: str) -> None:
    fecha = date.today().isoformat()
    ruta = f"{DIR_RAW}/{nombre}_{fecha}.json"
    os.makedirs(DIR_RAW, exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2, default=str)
    logger.info("Guardados %d registros en %s", len(datos), ruta)


def main() -> int:
    sal = os.getenv("HASH_SALT")
    if not sal:
        logger.error("Falta HASH_SALT en el fichero .env")
        return 1

    conexion = conectar()
    try:
        datos = {t: extraer_tabla(conexion, t) for t in TABLAS}
    finally:
        conexion.close()
    for t, filas in datos.items():
        logger.info("Extraídas %d filas de %s", len(filas), t)

    anon = Anonimizador(sal)
    anonimizar(datos, anon)

    errores = validar(datos, anon)
    if errores:
        for e in errores:
            logger.error(e)
        logger.error("Validación fallida: no se guarda nada")
        return 1

    for t in TABLAS:
        guardar(datos[t], t)
    logger.info("Ingesta completada")
    return 0


if __name__ == "__main__":
    sys.exit(main())
