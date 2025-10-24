import os
import subprocess
import sys
import json
import csv
import re
import logging
from datetime import datetime
from pathlib import Path
from time import sleep

import os
import subprocess
import sys
import json

TMP_PLAYWRIGHT = "/tmp/playwright"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = TMP_PLAYWRIGHT

chromium_exe = os.path.join(
    TMP_PLAYWRIGHT,
    "chromium-1187",
    "chrome-linux",
    "headless_shell"
)

# --- 🔹 Instalación garantizada de Chromium con salida controlada ---
try:
    if not os.path.exists(chromium_exe):
        print("=== Instalando Chromium con dependencias del sistema ===")
        log_path = "/tmp/playwright_install.log"

        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
            env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": TMP_PLAYWRIGHT},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,  # hasta 5 minutos por si el entorno es lento
        )

        with open(log_path, "w") as f:
            f.write(result.stdout)

        if result.returncode != 0:
            print("⚠️ Error al instalar Chromium, revisando salida...")
            raise RuntimeError(f"Fallo instalación Chromium:\n{result.stdout[:2000]}")
        else:
            print("✅ Chromium instalado correctamente")

except Exception as e:
    # --- Respuesta directa si esto se ejecuta dentro de Azure Function ---
    print("❌ Error crítico instalando Chromium:", e)
    # Crear un archivo para diagnóstico
    with open("/tmp/playwright_install_error.log", "w") as f:
        f.write(str(e))

    # Si la función está siendo llamada dentro de Azure Functions:
    try:
        import azure.functions as func
        def main(req: func.HttpRequest) -> func.HttpResponse:
            return func.HttpResponse(
                json.dumps({
                    "status": "error",
                    "mensaje": f"Fallo instalación de Chromium o dependencias: {str(e)}"
                }, ensure_ascii=False),
                mimetype="application/json",
                status_code=500
            )
    except Exception:
        raise e  # si no es Azure Function, relanzar error normal


from playwright.sync_api import sync_playwright  # <-- se importa después de la instalación

# === 🔹 Configuración de logging ===
DEBUG = os.getenv("SCRAPER_DEBUG", "").strip().lower() in ("1", "true", "yes", "on", "debug")
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fsp-scraper")

# === 🔹 Constantes generales ===
BASE_WEB = "https://www.farmaciasanpablo.com.mx"
API_HOST = "https://api.farmaciasanpablo.com.mx"
SITE_ID = "fsp"
PREFIX = "/rest/v2"
CURR = "MXN"
LANG = "es_MX"

COMMON_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "es-MX,es;q=0.9",
    "Origin": BASE_WEB,
    "Referer": BASE_WEB + "/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}

# === 🔹 Funciones utilitarias ===
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def num(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        if isinstance(x, (int, float)):
            return float(x)
        m = re.search(r"([\d.,]+)", str(x))
        return float(m.group(1).replace(",", "")) if m else None
    except Exception:
        return None

def money(v):
    v = num(v)
    return "" if v is None else f"{v:.2f}"

def write_rows(rows, out_csv):
    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
    headers = ["UPC", "Precio sin promoción", "Precio con promoción", "Nombre del producto", "Fecha Scrapping"]
    new = not Path(out_csv).exists()
    with open(out_csv, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)

def clean_digits(s):
    return re.sub(r"\D", "", str(s or ""))

def safe_text(r):
    try:
        return (r.text() or "")[:300].replace("\n", " ")
    except Exception:
        return "<sin cuerpo>"

# === 🔹 Cliente API OCC ===
class OCC:
    def __init__(self, context):
        self.req = context.request

    def search(self, q):
        url = f"{API_HOST}{PREFIX}/{SITE_ID}/products/search"
        params = {
            "query": q,
            "curr": CURR,
            "lang": LANG,
            "pageSize": "24",
            "currentPage": "0",
            "fields": "products(code,name)",
        }
        r = self.req.get(url, params=params, headers=COMMON_HEADERS, timeout=15000)
        if not r.ok:
            return []
        try:
            return r.json().get("products") or []
        except Exception:
            return []

    def detail(self, code):
        url = f"{API_HOST}{PREFIX}/{SITE_ID}/products/{code}"
        params = {"fields": "FULL", "curr": CURR, "lang": LANG}
        r = self.req.get(url, params=params, headers=COMMON_HEADERS, timeout=15000)
        if not r.ok:
            return {}
        try:
            return r.json()
        except Exception:
            return {}

# === 🔹 Comparación de UPC ===
def upc_matches(detail_json, upc):
    t = clean_digits(upc)
    if not t:
        return False
    for k in ("gtin", "ean", "upc", "sku", "visualCode"):
        if clean_digits(detail_json.get(k)) == t:
            return True
    for k in ("eans", "gtins", "upcs"):
        vals = detail_json.get(k) or []
        try:
            for v in vals:
                if clean_digits(v) == t:
                    return True
        except Exception:
            pass
    for cl in detail_json.get("classifications", []) or []:
        for feat in cl.get("features", []) or []:
            for val in feat.get("featureValues", []) or []:
                if clean_digits(val.get("value")) == t:
                    return True
            if clean_digits(feat.get("value")) == t:
                return True
    return False

# === 🔹 Gestión de carrito anónimo ===
class Cart:
    def __init__(self, context):
        self.req = context.request

    def create(self):
        base = f"{API_HOST}{PREFIX}/{SITE_ID}/users/anonymous/carts"
        r = self.req.post(base, params={"lang": LANG, "curr": CURR}, headers=COMMON_HEADERS, timeout=15000)
        if not r.ok:
            return None
        try:
            j = r.json()
        except Exception:
            return None

        guid = j.get("guid") or j.get("code")
        if guid:
            return guid
        return None

    def add_entry(self, cart_id, code, qty=1):
        url = f"{API_HOST}{PREFIX}/{SITE_ID}/users/anonymous/carts/{cart_id}/entries"
        params = {"lang": LANG, "curr": CURR}
        headers = {**COMMON_HEADERS, "Content-Type": "application/json"}
        body = json.dumps({"product": {"code": code}, "quantity": qty})
        r = self.req.post(url, params=params, data=body, headers=headers, timeout=15000)
        return r.ok

    def get_prices(self, cart_id, entry_idx=0):
        fields = (
            "entries(entryNumber,product(code,name),"
            "basePrice(value,formattedValue),totalPrice(value,formattedValue))"
        )
        url = f"{API_HOST}{PREFIX}/{SITE_ID}/users/anonymous/carts/{cart_id}"
        params = {"fields": fields, "lang": LANG, "curr": CURR}
        r = self.req.get(url, params=params, headers=COMMON_HEADERS, timeout=15000)
        if not r.ok:
            return None
        try:
            j = r.json()
            ents = j.get("entries") or []
            if not ents:
                return None
            e = ents[entry_idx]
            base = num((e.get("basePrice") or {}).get("value"))
            total = num((e.get("totalPrice") or {}).get("value"))
            name = ((e.get("product") or {}).get("name") or "").strip()
            return base, total, name
        except Exception:
            return None

    def remove(self, cart_id, entry_idx=0):
        url = f"{API_HOST}{PREFIX}/{SITE_ID}/users/anonymous/carts/{cart_id}/entries/{entry_idx}"
        try:
            self.req.delete(url, headers=COMMON_HEADERS, timeout=10000)
        except Exception:
            pass

# === 🔹 Carga de UPCs ===
def load_upcs(path):
    p = Path(path)
    if not p.exists():
        logger.error(f"No existe {path}. Crea un JSON lista de UPCs.")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [str(x) for x in data]
    elif isinstance(data, dict) and "upcs" in data:
        return [str(x) for x in data["upcs"]]
    else:
        logger.error("upc_list.json debe ser lista JSON o un objeto con clave 'upcs'.")
        sys.exit(1)

# === 🔹 Función principal ===
def main(upc_path="upc_list.json", out_csv="/tmp/salida_san_pablo.csv", headed=False):
    upcs = load_upcs(upc_path)
    logger.info(f"Iniciando scraping. Archivo UPCs: {upc_path}. Total UPCs: {len(upcs)}")
    rows = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir="/tmp/user_data_cart",  # ruta temporal y escribible
            executable_path=os.path.join(
                TMP_PLAYWRIGHT,
                "chromium-1187",
                "chrome-linux",
                "headless_shell"
            ),
            headless=not headed,
            viewport={"width": 1280, "height": 800},
            locale="es-MX",
            timezone_id="America/Mexico_City",
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-extensions", "--disable-gpu"],
        )
        occ = OCC(context)
        cart = Cart(context)

        cart_id = cart.create()
        if not cart_id:
            logger.error("Error: No se pudo crear carrito.")
            sys.exit(1)

        for i, upc in enumerate(upcs, 1):
            logger.info(f"[{i}/{len(upcs)}] Procesando UPC {upc}")
            try:
                prods = occ.search(upc) or occ.search(f":relevance:freeText:{upc}")
                if not prods:
                    rows.append({"UPC": upc, "Precio sin promoción": "-", "Precio con promoción": "-", "Nombre del producto": "No encontrado", "Fecha Scrapping": now_str()})
                    continue

                picked = None
                for pdt in prods:
                    code = pdt.get("code")
                    if not code:
                        continue
                    dj = occ.detail(code)
                    if dj and upc_matches(dj, upc):
                        picked = pdt
                        break
                if not picked:
                    rows.append({"UPC": upc, "Precio sin promoción": "-", "Precio con promoción": "-", "Nombre del producto": "No encontrado", "Fecha Scrapping": now_str()})
                    continue

                code = picked.get("code")
                name = (picked.get("name") or "").strip()

                if not cart.add_entry(cart_id, code, qty=1):
                    rows.append({"UPC": upc, "Precio sin promoción": "-", "Precio con promoción": "-", "Nombre del producto": name, "Fecha Scrapping": now_str()})
                    continue

                sleep(0.3)
                got = cart.get_prices(cart_id)
                if not got:
                    rows.append({"UPC": upc, "Precio sin promoción": "-", "Precio con promoción": "-", "Nombre del producto": name, "Fecha Scrapping": now_str()})
                    continue

                base, total, name2 = got
                if name2:
                    name = name2
                promo = total if total is not None and base is not None and total < base else None

                rows.append({"UPC": upc, "Precio sin promoción": money(base), "Precio con promoción": money(promo), "Nombre del producto": name, "Fecha Scrapping": now_str()})
                cart.remove(cart_id)
            except Exception as e:
                logger.exception(f"Error procesando UPC {upc}")
                rows.append({"UPC": upc, "Precio sin promoción": "-", "Precio con promoción": "-", "Nombre del producto": f"Error general al procesar {upc}: {e}", "Fecha Scrapping": now_str()})

        context.close()

    write_rows(rows, out_csv)
    logger.info(f"Proceso completado: {len(upcs)} UPCs procesados")
    logger.info(f"Resultados guardados en: {out_csv}")
