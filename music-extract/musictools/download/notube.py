import random
import re
import threading
import time
import urllib.request
import sys
import logging
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

NOTUBE_URL   = "https://notube.im/es/youtube-app-364"
TIMEOUT_NAV  = 30_000   # ms — carga inicial de pagina
TIMEOUT_CONV = 90_000   # ms (90 s) — el sitio tarda entre 10 s y 40 s segun la duracion del video

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_sem = threading.Semaphore(1)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

def download_from_notube(url: str, out_dir: Path) -> tuple[bool, str]:

    out_dir.mkdir(parents=True, exist_ok=True)

    with _sem:
        try:
            return _run(url, out_dir)

        except PWTimeout as e:
            return False, f"[notube] Timeout: el sitio tardo demasiado en procesar ({e})"

        except Exception as e:
            return False, f"[notube] Error inesperado: {e}"

def _pause(a=0.5, b=1.2):
    time.sleep(random.uniform(a, b))

def _run(url: str, out_dir: Path) -> tuple[bool, str]:

    with sync_playwright() as pw:
        
        try:
            browser = pw.chromium.launch(
                headless=True,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )

        ctx = browser.new_context(
            user_agent=_UA,
            viewport={"width": 1920, "height": 1080},
            locale="es-ES",
            timezone_id="America/Argentina/Buenos_Aires",
        )

        page = ctx.new_page()

        # Ocultar navigator.webdriver antes de cargar cualquier pagina
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # 1. Cargar notube.im
        page.goto(NOTUBE_URL, wait_until="domcontentloaded", timeout=TIMEOUT_NAV)
        _pause()

        # 2. Pegar URL en el campo de busqueda
        page.fill("#keyword", url)
        _pause(0.3, 0.7)

        # 3. Click en el boton OK
        page.click("#submit-button")

        # 4. Esperar a que aparezca el boton de descarga
        # Sin networkidle — notube tiene analytics en background que lo bloquean
        page.wait_for_selector("#downloadButton", state="visible", timeout=TIMEOUT_CONV)
        _pause(0.5, 1.0)

        # 5. Extraer el href directo del boton de descarga (sin hacer click)
        href = page.get_attribute("#downloadButton", "href") or ""

        browser.close()

    if not href or not href.startswith("http"):
        return False, "[notube] #downloadButton no tiene href descargable. El sitio puede haber cambiado."

    # 6. Descargar el MP3 con urllib
    dest = _download_file(href, out_dir)

    return True, f"Guardado: {dest.name}"

def _download_file(href: str, out_dir: Path) -> Path:

    req = urllib.request.Request(href, headers={"User-Agent": _UA})

    with urllib.request.urlopen(req, timeout=120) as resp:
        cd = resp.headers.get("Content-Disposition", "")
        filename = _filename_from_cd(cd) or _filename_from_url(href)
        dest = out_dir / filename
        dest.write_bytes(resp.read())

    return dest

def _filename_from_cd(cd: str) -> str:

    match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)', cd, re.IGNORECASE)

    if match:
        name = urllib.request.unquote(match.group(1).strip())

        return re.sub(r'[\\/*?:"<>|]', "_", name)

    return ""

def _filename_from_url(url: str) -> str:
    name = url.split("?")[0].split("/")[-1]
    name = urllib.request.unquote(name)
    name = re.sub(r'[\\/*?:"<>|]', "_", name)

    if not name.endswith(".mp3"):
        name += ".mp3"

    return name or "descarga.mp3"

if __name__ == "__main__":

    if len(sys.argv) < 3:
        logger.info("Uso: python notube.py <youtube-url> <out-dir>")

        sys.exit(1)

    out = Path(sys.argv[2])
    ok, msg = download_from_notube(sys.argv[1], out)

    logger.info(f"{'[OK]' if ok else '[FAIL]'} {msg}")

    sys.exit(0 if ok else 1)
