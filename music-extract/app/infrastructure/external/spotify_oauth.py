import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

class AuthorizationDenied(Exception):
    pass

def build_authorize_url(client_id: str, redirect_uri: str, scopes: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": scopes,
        "redirect_uri": redirect_uri,
        "state": state,
    }

    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def wait_for_auth_code(port: int, expected_state: str) -> str:
    result: dict = {}

    class _Handler(BaseHTTPRequestHandler):

        def do_GET(self):
            parsed = urlparse(self.path)

            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return

            query = parse_qs(parsed.query)
            result["code"] = (query.get("code") or [None])[0]
            result["state"] = (query.get("state") or [None])[0]
            result["error"] = (query.get("error") or [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><body style='font-family:sans-serif;padding:2rem'>"
                "<h2>Autorizacion completada</h2>"
                "<p>Podes cerrar esta pestana y volver a la terminal.</p>"
                "</body></html>".encode("utf-8")
            )

        def log_message(self, format, *args):
            return

    httpd = HTTPServer(("0.0.0.0", port), _Handler)
    httpd.handle_request()
    httpd.server_close()

    if result.get("error"):
        raise AuthorizationDenied(f"Spotify devolvio un error de autorizacion: {result['error']}")

    if result.get("state") != expected_state:
        raise AuthorizationDenied("El parametro state no coincide con el enviado - posible CSRF, reintenta.")

    if not result.get("code"):
        raise AuthorizationDenied("No se recibio el codigo de autorizacion en el callback.")

    return result["code"]


def exchange_code_for_token(code: str, *, client_id: str, client_secret: str, redirect_uri: str) -> dict:
    r = requests.post(
        TOKEN_URL,
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        auth=(client_id, client_secret),
        timeout=15,
    )
    r.raise_for_status()

    return r.json()


def refresh_access_token(refresh_token: str, *, client_id: str, client_secret: str) -> dict:
    r = requests.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(client_id, client_secret),
        timeout=15,
    )
    r.raise_for_status()

    return r.json()


def run_authorization_flow(*, client_id: str, client_secret: str, redirect_uri: str, port: int, scopes: str) -> dict:
    state = secrets.token_hex(8)
    url = build_authorize_url(client_id, redirect_uri, scopes, state)

    if Path("/.dockerenv").exists():
        print(f"Autorizacion Spotify: abri esta URL en el navegador de tu maquina -> {url}", flush=True)
    else:
        webbrowser.open(url)

    code = wait_for_auth_code(port, state)

    return exchange_code_for_token(code, client_id=client_id, client_secret=client_secret, redirect_uri=redirect_uri)


# --- Flujo para la web: no bloquea la petición -----------------------------------------------------------------
# La web pide la URL, la abre en otra pestaña y consulta el estado hasta que el callback guarda el token.
_pending: dict = {}
_pending_lock = threading.Lock()


def start_authorization_flow(*, client_id: str, client_secret: str, redirect_uri: str, port: int, scopes: str, on_token) -> str:
    """Devuelve la URL de autorización y espera el callback en segundo plano. `on_token(token_data)` guarda el token.

    Si ya hay un flujo esperando (el callback escucha en un solo puerto), devuelve la misma URL.
    """
    with _pending_lock:
        thread = _pending.get("thread")
        if thread is not None and thread.is_alive():
            return _pending["url"]

        state = secrets.token_hex(8)
        url = build_authorize_url(client_id, redirect_uri, scopes, state)

        def wait():
            try:
                code = wait_for_auth_code(port, state)
                on_token(exchange_code_for_token(code, client_id=client_id, client_secret=client_secret, redirect_uri=redirect_uri))
            except Exception as e:  # noqa: BLE001 — el hilo no puede propagar: se deja registrado
                print(f"Autorizacion Spotify fallida: {e}", flush=True)

        thread = threading.Thread(target=wait, name="spotify-oauth", daemon=True)
        _pending.update(thread=thread, url=url)
        thread.start()
        return url


# --- Flujo con retorno a una ruta del motor (web publicada) -----------------------------------------------------
# Spotify vuelve a https://<web>/api/auth/spotify/callback, que llega al motor como /auth/spotify/callback.
_web_pending: dict[str, dict] = {}
WEB_FLOW_TTL_S = 600


def start_web_flow(*, client_id: str, client_secret: str, redirect_uri: str, scopes: str, on_token) -> str:
    """URL de autorización; el canje lo hace `finish_web_flow` cuando Spotify vuelve con el mismo `state`."""
    state = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _pending_lock:
        for key in [k for k, v in _web_pending.items() if now - v["at"] > WEB_FLOW_TTL_S]:
            _web_pending.pop(key, None)
        _web_pending[state] = {"client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri,
                               "on_token": on_token, "at": now}
    return build_authorize_url(client_id, redirect_uri, scopes, state)


def finish_web_flow(*, state: str | None, code: str | None, error: str | None) -> None:
    """Canjea el código por el token y lo guarda. El `state` se usa una sola vez (evita CSRF y repeticiones)."""
    with _pending_lock:
        flow = _web_pending.pop(state or "", None)
    if flow is None or time.monotonic() - flow["at"] > WEB_FLOW_TTL_S:
        raise AuthorizationDenied("La conexión con Spotify venció o no la inició owlo: vuelve a intentarlo.")
    if error:
        raise AuthorizationDenied(f"Spotify no autorizó la conexión ({error}).")
    if not code:
        raise AuthorizationDenied("Spotify no devolvió el código de autorización.")
    flow["on_token"](exchange_code_for_token(code, client_id=flow["client_id"], client_secret=flow["client_secret"],
                                             redirect_uri=flow["redirect_uri"]))
