# Despliegue del motor para la app Android

La app Android (y la web) se sirven desde Render; el motor corre en la PC. Para que la app funcione hay que
levantar **el motor en Docker** y **Tailscale Funnel**. El front no se toca: ya está publicado en Render.

```
App Android ──► https://owlo.onrender.com          (Render, sitio estático)
                   └─ /api/*  ──►  https://<URL-DEL-FUNNEL>/*   (regla de Render)
                                      └─ Tailscale Funnel ──► http://127.0.0.1:1011
                                                                 └─ Docker: music-extract-api (:8000)
```

## 1. Requisitos (una sola vez)

- Docker y Tailscale instalados, con sus servicios activos:

  ```bash
  systemctl is-active docker tailscaled
  ```

- `music-extract/.env` con, al menos:

  ```env
  OWLO_SECRET_KEY=<clave larga y aleatoria>
  SPOTIFY_CLIENT_ID=<...>
  SPOTIFY_CLIENT_SECRET=<...>
  SPOTIFY_OAUTH_REDIRECT_URI=https://owlo.onrender.com/api/auth/spotify/callback
  ```

- `.env` en la raíz del repo con las llaves de R2 (`R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`,
  `R2_SECRET_ACCESS_KEY` o `R2_TOKEN_VALUE`).
- En el panel de **Spotify Developer**, la misma `SPOTIFY_OAUTH_REDIRECT_URI` registrada en *Redirect URIs*.
- En el panel de **Render** (sitio `owlo-web` → *Redirects/Rewrites*), en este orden:

  | Source | Destination | Action |
  | --- | --- | --- |
  | `/api/*` | `https://<URL-DEL-FUNNEL>/*` | Rewrite |
  | `/*` | `/index.html` | Rewrite |

  `<URL-DEL-FUNNEL>` es la que muestra `tailscale funnel status` (no se guarda en el repo).

## 2. Levantar

```bash
cd ~/Documentos/projects/async-music-fetch-engine

# Motor (usa docker-compose.yml + docker-compose.override.yml: código montado, recarga y perfil de Firefox)
docker compose up -d
# Solo la primera vez o si cambió requirements.txt, pyproject.toml o el Dockerfile:
# docker compose up -d --build

# Exponer el motor por https (queda activo en segundo plano y sobrevive a reinicios)
tailscale funnel --bg 1011
```

Las migraciones de la base se aplican solas al arrancar el motor (`init_db` → `alembic upgrade head`).

## 3. Verificar

```bash
# Motor en la PC (200)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:1011/docs

# Funnel apuntando al motor (debe mostrar «Funnel on» y «/ proxy http://127.0.0.1:1011»)
tailscale funnel status

# Camino completo de la app: Render → Funnel → motor (200)
curl -s -o /dev/null -w '%{http_code}\n' https://owlo.onrender.com/api/docs

# Logs del motor
docker compose logs -f api
```

Si los tres dan bien, la app ya funciona: abrir **Owlo** en el teléfono o la tablet.

## 4. Bajar

```bash
cd ~/Documentos/projects/async-music-fetch-engine
docker compose stop                  # detiene el motor (no borra la base ni los datos)
tailscale funnel --https=443 off     # deja de exponerlo a internet
```

## Notas

- El contenedor tiene `restart: "no"`: después de reiniciar la PC hay que volver a correr `docker compose up -d`.
  El Funnel (`--bg`) sí queda configurado tras reiniciar.
- La PC tiene que estar prendida y sin suspender mientras se use la app.
- Las cookies de Firefox para videos con inicio de sesión salen de `docker-compose.override.yml`
  (`YTDLP_COOKIES_FROM_BROWSER` y el montaje de `~/.mozilla/firefox`). Si cambia el perfil de Firefox, actualizar
  ahí su nombre.
- Sin el override (`docker compose -f docker-compose.yml up -d`) el motor arranca igual, pero sin recarga ni
  cookies de Firefox.
