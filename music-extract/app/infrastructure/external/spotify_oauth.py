import secrets
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
