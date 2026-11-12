# async-music-fetch-engine

Subproyecto correspondiente a la primera etapa de una solución orientada al análisis de sonido mediante *machine learning* supervisado, consistente en un sistema de descarga física de música para la obtención y almacenamiento de archivos de audio en el formato configurado a partir de URL públicas de YouTube/YouTube Music o de playlists personales del servicio de *streaming* Spotify, mediante procesamiento por lotes y ejecución multihilo para la descarga paralela de múltiples enlaces y la reducción del tiempo de respuesta.

El acceso a cada fuente está sujeto a sus propias restricciones, por lo que no se procesa contenido *premium* o restringido mediante mecanismos de pago, a excepción de Spotify, cuyo funcionamiento requiere una cuenta Premium para la obtención del contenido; el alcance de esta integración está determinado por las limitaciones de la Spotify Web API, con acceso únicamente a las playlists propias del usuario autenticado.

## Requisitos

- Python 3.11+
- [ffmpeg](https://ffmpeg.org/) en el `PATH` (normalización de audio) (opcional)
- Node.js en el `PATH` (yt-dlp necesita un runtime de JS para resolver los challenges del reproductor de YouTube)
- Una aplicación de [Spotify Developer](https://developer.spotify.com/dashboard) (Client ID/Secret) (opcionales)
- Cuenta de **Spotify Premium** *(opcional, requerida para el uso de la Web API en modo desarrollo)*.



## Uso



### Credenciales y cookies

Las variables `SPOTIFY_CLIENT_ID` y `SPOTIFY_CLIENT_SECRET` definidas en el archivo `.env` corresponden a las credenciales de la aplicación registrada en Spotify Dashboard (opcionales si no se desea esta ruta de descarga). Su utilización recae en la generación de tokens mediante OAuth y posteriormente alojadas en DB local para su reutilización.

`POST /auth/spotify/login` acepta opcionalmente su propio `client_id`/`client_secret` en el body, para loguearse con una aplicación de Spotify Developer distinta a la del `.env`. Si no se envían (o se envían vacíos), usa los del `.env` por defecto. Las credenciales usadas en el login quedan guardadas junto al token en `spotify_tokens`, porque el refresh posterior tiene que hacerse con las mismas credenciales con las que se obtuvo — no necesariamente las del `.env`.

Para una ejecución completa del sistema se recomienda utilizar un archivo `cookies.txt` obtenido desde una sesión real de YouTube. Este archivo se utiliza en las llamadas a `yt-dlp` para permitir el acceso a contenido que requiere autenticación y mejorar la estabilidad de las descargas.

Su uso permite evitar que ciertos videos con restricción de edad sean rechazados por YouTube y reduce la probabilidad de bloqueos temporales asociados a múltiples solicitudes automatizadas. Cuando un recurso requiere autenticación y no se dispone de cookies válidas, el sistema lo identifica como `needs_auth` y continúa con el procesamiento sin realizar la descarga.

Para obtenerla:

1. Iniciar sesión en youtube.com con la cuenta que se quiera usar.
2. Abrir la consola de herramientas de desarrollador del navegador (F12) estando en esa página.
3. Pegar y ejecutar ahí el contenido de `music-extract/data/cookies/extract.js` (ya incluido en
  el repositorio), usa la CookieStore API del navegador (disponible en Chrome/Edge, no en  todos) para leer las cookies visibles y armar un archivo en formato Netscape.
4. El navegador descarga automáticamente un archivo `cookies.txt`.
5. Mover ese archivo a `music-extract/data/cookies/cookies-files/cookies.txt`.

El archivo `cookies.txt`, al ser un recurso compartido entre workers de descarga en paralelo, utiliza una copia privada temporal para cada descarga en lugar del archivo original. Esto se debe a que `yt-dlp` reescribe el archivo de cookies al cerrar cada sesión, y compartir el mismo archivo entre threads producía corrupción intermitente antes de aislarlo por descarga.

La sesión expira con el tiempo; no hay renovación automática, así que hay que repetir estos pasos periódicamente.

### Biblioteca en la nube (Cloudflare R2, opcional)

Con estas variables en `music-extract/.env` o en el `.env` de la raíz, cada canción descargada con cuenta se sube también a R2 (`app/core/nube.py`). Sin ellas no se sube nada.

```
R2_ENDPOINT=https://<id-de-cuenta>.r2.cloudflarestorage.com
R2_BUCKET=audio-fetch-downloader
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
# o, en lugar de las dos llaves, solo el token de R2:
# R2_TOKEN_VALUE=...
```

Las claves son por identificador, no por nombre: renombrar o mover carpetas solo reescribe `biblioteca.json`.

```
v1/u/{id_usuario}/t/{id_cancion}-{clave}.mp3      canción entera o set completo
v1/u/{id_usuario}/t/{id_cancion}-{clave}/NN.mp3   temas del tracklist
v1/u/{id_usuario}/biblioteca.json                 carpetas, nombres y claves
```

Borrar en la biblioteca borra también en R2. Al arrancar, el motor sube lo que falte (canciones anteriores o subidas que fallaron).

Con `NUBE_SIN_COPIA_LOCAL=1`, R2 pasa a ser la fuente: la copia en disco se borra en cuanto la canción queda subida y comprobada (mismo tamaño en R2). Las descargas de un MP3 redirigen a un enlace temporal de R2; los ZIP traen de R2 lo que falte y lo borran al terminar.

### Arquitectura de Base de datos

SQLite, con el schema administrado por SQLAlchemy y las migraciones por Alembic. Se crea y actualiza automaticamente al levantar el API. 

Cinco tablas: 

- `jobs` (una fila por pedido de descarga)
- `tracks` (una fila por URL dentro de un job) 
- `attempts` (una fila por cada candidato/cliente probado, con su score y proveedor)
- `retries` (una fila por ronda de reintento manual)
- `spotify_tokens` (la sesión OAuth, si se usó)

La verificación de recursos ya descargados se realiza exclusivamente contra estos registros, sin comprobar la existencia física del archivo en el sistema de almacenamiento. En consecuencia, si un archivo `.mp3` es eliminado manualmente pero su registro permanece en la base de datos, el recurso no se descargará nuevamente.

`jobs.elapsed_time_s` se calcula y se graba una única vez, en el momento en que el job pasa a `completed`/`completed_with_errors` — no en cada consulta. Mientras el job sigue `in_progress`, `view-download-status` lo calcula al vuelo (`ahora - creado`); una vez guardado, ese valor se reutiliza tal cual hasta que un reintento/retomado vuelva a poner el job en curso (lo que lo resetea a `null` para que vuelva a calcularse en vivo).

Ejemplo de flujo con datos reales:

```bash
curl -X POST http://localhost:8000/downloads/download-urls-manual \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://www.youtube.com/watch?v=JEdxT8yCf98"]}'
# -> {"job_id": [AUTOGENERATED_ID], "type": "manual-urls", "total": 1}

curl http://localhost:8000/stages/view-download-status?job_id=[AUTOGENERATED_ID]
# -> {"status": "completed", "successful": 1, "failed": 0, ...}
```

El track resultante queda con `title: "Mala Pata"`, `status: successful`, y un intento (`provider: yt-dlp`) registrado en `attempts`.

## Configuración (local)

```bash
cd music-extract
python -m venv .venv && source .venv/bin/activate   # o .venv\Scripts\activate en Windows
pip install -r requirements.txt
pip install -e .                 # instala musictools como paquete editable
playwright install chromium      # fallback de navegador para videos geo-restringidos
```

Registrar `http://127.0.0.1:8888/callback` como Redirect URI en la aplicación de Spotify Developer (lo requiere el flujo OAuth de `/auth/spotify/login`)

Alternativa a los comandos de arriba: `.vscode/launch.json` (config "FastAPI: music-extract API") levanta la API con F5 desde VS Code, apuntando `cwd` a `async-music-fetch-engine/music-extract/app`. Requiere el mismo `pip install -e .`/`pip install -r requirements.txt` corridos una vez contra el intérprete que declara ese archivo. No corras esta sesión local al mismo tiempo que el contenedor Docker — ambos apuntan al mismo `db_downloads.sqlite` y al mismo puerto `8888` de callback OAuth, y competirían entre sí.

### Selección y validación del candidato

Opciones de rutas de descargas

- Grupo de URLs sueltas (`download-urls-manual`, para YouTube o Spotify indistintamente)  
- URL de la playlist completa (`download-spotify-playlist`). Para un track de Spotify, los candidatos se buscan en dos fuentes independientes: YouTube Music y búsqueda plana (Youtube) combinadas con un cupo garantizado para cada una.

Cada candidato se evalúa comparando su título, artista y duración con los valores reales del track. Los resultados se ordenan de mayor a menor score, por lo que únicamente se procesa el primer candidato que logra descargarse. Si el score obtenido es igual o superior a 60 (techo mínimo por defecto), el track se clasifica como `successful`; en caso contrario, queda como `incorrect`, aunque la descarga del archivo haya sido exitosa.

- El track **"fable"** de **"a vow"** encontró como candidato **"fable"** del canal **"a vow - Topic"**, con un score de **135.0**. Al superar ampliamente el umbral, se aceptó directamente como `successful`.
- El track **"The Eyes, Chico. They Never Lie"** de **OURGRND** encontró como candidato **"Scarface - The Eyes, Chico, They Never Lie."** del canal **"OneMinute TV"**, con un score aproximado de **40**. Aunque el archivo pudo descargarse correctamente, el score quedó por debajo del umbral, por lo que el resultado final fue `incorrect`.

Un track marcado como `incorrect` indica que ningún candidato de YouTube superó el umbral de similitud requerido; la descarga puede existir, pero no corresponde a una coincidencia confiable.

### Resolución de metadata

La metadata se obtiene directamente desde la Spotify Web API y, al procesar playlists completas, se reutiliza la información obtenida durante el listado de tracks, evitando consultas repetidas. En una prueba con una playlist de 54 tracks, esta optimización redujo el tiempo promedio por canción de ~38-39 segundos (flujo anterior basado en `spotdl`) a ~6.6 segundos.

**Aclaración:** Las canciones obtenidas desde Spotify utilizan el mismo flujo y proveedor de descarga (`yt-dlp`) que las URLs directas de YouTube.

`yt-dlp` es el proveedor utilizado en el primer intento, incluyendo su rotación interna de `player_client`. El proveedor `notube` (automatización de navegador contra notube.im) se utiliza únicamente como fallback: para un candidato de Spotify, solo cuando el motivo de fallo es `geo_restricted`; para una URL directa de YouTube, además del caso `geo_restricted`, también como último recurso si `yt-dlp` agota sus intentos por cualquier otro motivo no descartado (`premium_only`, `needs_auth` y `client_blocked` se omiten sin intentar `notube`).

### Carpeta de descargas

Los archivos se almacenan en `data/music-downloaded/{carpeta}/`, donde cada carpeta corresponde a un `group_name` (descargas manuales) o al nombre de la playlist. Ambos valores se normalizan (minúsculas, sin tildes, espacios reemplazados por `_` y eliminación de caracteres inválidos). Si una descarga manual no tiene `group_name`, se almacena en la carpeta compartida `any-musics/`.  

El nombre final del archivo proviene del video descargado del mejor candidato y no del título oficial de Spotify, por lo que puede no coincidir exactamente con el formato "Artista - Canción".

### Variables configurables

Archivo `app/config/settings.py`

- `THREADS` *(Default: 3)* — cantidad de descargas en paralelo. Aumentarlo reduce tiempo de respuesta, pero también multiplica los pedidos simultáneos a YouTube, con más riesgo del bloqueo por detección de bot. 
- `FORMAT/BITRATE` *(Default: mp3/192k)* — formato y calidad del audio final. 
- `MAX_RETRIES_PER_TRACK` *(Default: 3)* — cuántas rondas de reintento se permiten por track. 
- `ETA_WINDOW` *(Default: 20)* — cuántos tracks recientes se promedian para estimar el tiempo restante.
- `STALL_TIMEOUT_S` *(Default: 60)* — segundos sin avance tras los que se corta una conversión. Reemplaza al límite fijo de 180 s por canción.
- `LONG_AUDIO_S` *(Default: 900)* — por encima de esta duración no se intenta notube.
- `TOP_N/CLIENT_CHAIN_LEN` *(Default: 5/5)* — alimentan cuántos intentos máximos recibe un track (`TOP_N` × `CLIENT_CHAIN_LEN` para Spotify, `CLIENT_CHAIN_LEN` + 1 para YouTube directo).

Archivo `musictools/download/core.py`

- `MIN_SCORE` *(Default: 60)* — el umbral de la sección anterior (Selección y validación del candidato).
- `candidates_max`/`candidates_per_source` *(Default: 15/10, con la mitad del cupo garantizada por fuente)*



### Scripts standalone

`scripts/` tiene entry points de CLI que no dependen del servidor corriendo:

- `download_from_csv.py` — descarga en batch a partir de un CSV (mismo formato que exporta `spotify-api/fetch_playlists.js`), sin pasar por el sistema de jobs/reintentos.
- `normalize_folder.py` — corre la normalización LUFS/peak sobre una carpeta ya descargada. Su salida se guarda en `data/music-normalized/{carpeta}/`, manteniendo el mismo esquema de directorios que la carpeta de origen.
- `check_boundaries.py` — el chequeo de las dos reglas de `musictools` (ver "Arquitectura"), pensado para correr en CI.
- `spotify-api/fetch_playlists.js` — corre en el host (**no** forma parte de la imagen Docker), abre un navegador real y escucha en `127.0.0.1:8888`). Exporta una de tus playlists de Spotify a un CSV:

```bash
cd spotify-api
npm install
node fetch_playlists.js --client-id <id> --client-secret <secret>
```

Abre un navegador para un login OAuth interactivo, lista tus playlists, y escribe un CSV en `playlists-fetched/`, que `scripts/download_from_csv.py` puede consumir después para una corrida de descarga batch, solo por CLI.

> **Nota:** `POST /downloads/download-spotify-playlist` (una vez completado `/auth/spotify/login`) cubre exactamente lo mismo de punta a punta — login, listar tus playlists, traer los tracks — sin ningún CSV intermedio, directo a un job con seguimiento y reintentos. Este script se mantiene como la alternativa batch/CSV para los casos en los que preferís inspeccionar o editar la lista de tracks antes de descargar cualquier cosa.



## Arquitectura

```
music-extract/
├── musictools/               # paquete independiente — no sabe nada de FastAPI ni de la DB.
│   ├── download/              #   core.py orquesta: metadata → candidatos → scoring → descarga
│   └── normalize/              #   normalización LUFS/peak vía ffmpeg
│
├── app/                        # la API. Depende de musictools, nunca al reves.
│   ├── config/                 #   unica fuente del .env y de las rutas del filesystem
│   ├── core/                   #   logica de negocio de jobs/tracks/reintentos
│   ├── infrastructure/
│   │   ├── persistence/        #   modelos SQLAlchemy + migraciones de Alembic
│   │   └── external/           #   conecta musictools + la API de Spotify al sistema de jobs
│   └── api/                    #   routers de FastAPI
│
├── scripts/                    # entry points de CLI standalone (descarga batch, normalizacion)
└── data/                       # estado de runtime: audio descargado, cookies, cache de bloqueados

spotify-api/
└── fetch_playlists.js          # CLI de OAuth interactivo para exportar una playlist a CSV
```

Dos reglas mantienen a `musictools` genuinamente reusable fuera de este proyecto:

1. `musictools` **nunca importa de** `app`**.** Verificado por `scripts/check_boundaries.py`, un
  chequeo basado en grep pensado para correr en CI, no una convención que se pudre en silencio.
2. `musictools` **nunca lee** `.env` **ni resuelve rutas desde** `__file__`**.** Cada función recibe lo
  que necesita como parámetro explícito — quien la llama (`app/` o un script) decide dónde  
   viven las cosas.



## Docker

Correr el proyecto en Docker es **una opción, no un requisito** — la alternativa es correr la API directamente en local con el intérprete del sistema (ver "Configuración (local)" más arriba, o `.vscode/launch.json` para levantarla con F5 desde VS Code). Docker resulta más cómodo para dejar la API corriendo de forma persistente o accesible desde otro dispositivo en la LAN (`0.0.0.0:8000`); en local es más directo para debuggear paso a paso.

```bash
cd async-music-fetch-engine
docker compose up -d --build
```

El audio descargado, el archivo de cookies y la base SQLite se montan como volúmenes (`music-extract/data/`, `music-extract/app/db/`) para que sobrevivan a los rebuilds.

> **`journal_mode`.** `database.py` fuerza `PRAGMA journal_mode=DELETE` en vez del `WAL` por defecto de SQLite — WAL requiere `mmap` sobre un archivo `-shm` que no es confiable a través de un bind mount de Windows en Docker Desktop (pudo causar un `disk I/O error` irrecuperable sin reiniciar la máquina). El costo de concurrencia es aceptable porque las escrituras que importan ya están serializadas con un lock propio en `job_service.py`.

> **OAuth de Spotify — de dónde sale la URL de login según cómo corras la API.** `run_authorization_flow()` (`spotify_oauth.py`) detecta si está corriendo dentro de un contenedor (`/.dockerenv`):
> - **Corriendo en Docker:** no hay navegador disponible dentro del contenedor (headless), así que la URL de autorización se **imprime en los logs** (`docker compose logs -f api` o la consola donde corriste `up`) — hay que copiarla a mano y abrirla en el navegador del host.
> - **Corriendo en local:** se abre el navegador automáticamente (`webbrowser.open`), sin pasos manuales.
>
> En ambos casos, el servidor de callback bindea a `0.0.0.0:8888` (publicado en el compose como `127.0.0.1:8888:8888` — accesible desde el host, no desde la LAN) y el `redirect_uri` es `http://127.0.0.1:8888/callback`, resuelto por el navegador del host — cumple la exigencia de Spotify de usar loopback literal sin que el contenedor deba ser alcanzable por nombre. El token queda persistido en la base SQLite bajo `app/db/` (volumen), así que la autorización interactiva se hace una sola vez y los reinicios posteriores solo refrescan. El `.env` va en `music-extract/.env`, que es donde lo busca tanto el compose como una corrida local.




### Desarrollo con Docker (sin reconstruir por cada cambio)

`docker-compose.override.yml` se carga solo con `docker compose up`: monta `app/`, `musictools/` y `scripts/` desde
la carpeta y corre uvicorn con `--reload`, así que **un cambio de código se aplica al guardar, sin `build`**.
Solo hace falta reconstruir cuando cambian las dependencias (`requirements.txt`, `pyproject.toml`) o el `Dockerfile`:

```bash
docker compose up -d                       # desarrollo: código montado y recarga automática
docker compose up -d --build               # después de cambiar dependencias o el Dockerfile
docker compose -f docker-compose.yml up -d # sin el override: el código de la imagen, como en producción
```

En el `Dockerfile`, las capas van de lo que menos cambia a lo que más cambia (sistema → dependencias → Chromium de
Playwright → `musictools` → `app`), así que un `build` por cambio de código no vuelve a instalar Chromium.

## Referencia de la API


| Método | Ruta                                                    | Descripción                                                                                                                                                                                     |
| ------ | -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST` | `/downloads/download-urls-manual`                        | Descarga una o varias URLs sueltas. Body: `{urls: [...], group_name?: str}`.                                                                                                                    |
| `POST` | `/downloads/download-spotify-playlist`                    | Descarga una playlist completa de Spotify. Body: `{playlist_name: str}` — acepta un nombre **o** una URL/URI de playlist.                                                                       |
| `GET`  | `/stages/view-download-status` (`?job_id=`)                | Estado agregado: conteos por estado, ETA, última ronda de reintento. Sin `job_id`, devuelve el job más reciente.                                                                                |
| `GET`  | `/stages/view-failed-urls` (`?job_id=`, `?format=csv`)     | Detalle de los tracks fallidos/omitidos de un job. Sin `job_id`, el más reciente.                                                                                                                |
| `GET`  | `/stages/view-all-jobs`                                    | Lista todos los jobs (incluidos los que están en curso).                                                                                                                                        |
| `POST` | `/auth/spotify/login`                                    | Login OAuth interactivo de Spotify, de una sola vez (abre un navegador o imprime la URL en logs, ver "Docker"). Body opcional `{client_id?, client_secret?}` — sin esto, usa el `.env`.          |
| `GET`  | `/auth/spotify/account-status`                                     | `{"autenticado": bool, ...}` — si hay token guardado, incluye además todos los campos de `GET /v1/me` de Spotify (id, display_name, email, product, etc.), no solo el booleano.                 |

**No existe un endpoint `/retry` separado.** Reintentar o retomar un bloque es simplemente volver a llamar a `download-urls-manual`/`download-spotify-playlist` con las mismas URLs (mismo `group_name`) o la misma playlist — el comportamiento se decide solo, según el estado real de los tracks de ese job:
- Si quedan tracks `pending`/`in_progress` (el proceso se cortó a mitad de camino) → se **retoman** tal cual (los `in_progress` se descargan de nuevo desde cero). No cuenta como ronda de reintento.
- Si el bloque ya terminó de correr del todo y solo quedaron `failed`/`skipped` → recién ahí se **reintenta** de verdad, contra el tope de `MAX_RETRIES_PER_TRACK` rondas.




### Flujo de la web (`/jobs`)

Lo usa [`async-music-fetch-engine-web`](../async-music-fetch-engine-web). Separa la descarga en dos tiempos: primero
se **resuelve** cada canción (metadatos y candidatos puntuados, sin bajar audio) y el usuario revisa la vista previa;
después se descarga solo lo que confirmó, con el % de cada fase y la opción de cancelar. Sin límite de canciones por
trabajo ni de duración por canción: una descarga se corta solo si se estanca.

| Método | Ruta | Descripción |
| --- | --- | --- |
| `POST` | `/jobs` | Crea el trabajo en vista previa. Body: `{origen: "enlaces" \| "spotify" \| "youtube", urls?, playlists?, entrada?, carpeta?, tracklist?: {cortar, guardar_completo}}`. Con `"enlaces"` (la lista de la web), `urls` mezcla canciones y playlists de Spotify, videos de YouTube y de TikTok, y `playlists` lleva nombres de playlists de Spotify; el trabajo queda con origen `mixto` si hay más de uno |
| `GET` | `/folders` (`?q=`) | Carpetas de descarga con su nombre visible y cantidad de canciones, y la carpeta por defecto |
| `POST` | `/folders` | Crea una carpeta. Body: `{nombre}`. 409 si ya existe |
| `GET` | `/jobs` · `/jobs/{id}` | Trabajos confirmados y su resumen (contadores, tiempo restante) |
| `GET` | `/jobs/{id}/preview` | Canciones con el mejor candidato y su score (Spotify) o duración y capítulos (YouTube) |
| `PATCH` | `/jobs/{id}/tracks/{track_id}` | `{accion: "mantener" \| "quitar" \| "cambiar_url" \| "tracklist", url?, cortar?, guardar_completo?}` |
| `POST` | `/jobs/{id}/confirm` | Encola las canciones aceptadas. Se baja exactamente el candidato revisado, sin volver a buscar |
| `GET` | `/jobs/{id}/tracks` · `/jobs/{id}/tracks/{track_id}` | Estado, intentos y `progreso` en vivo: `{fase: descargando \| convirtiendo \| cortando, porcentaje, …}`. El detalle suma los intentos |
| `POST` | `/jobs/{id}/tracks/{track_id}/cancel` · `/jobs/{id}/cancel` | Cancela una canción (en espera o en curso) o todo el trabajo |
| `POST` | `/jobs/{id}/tracks/{track_id}/retry` · `…/requeue` | Reintenta una fallida (gasta un intento) o vuelve a encolar una cancelada |
| `GET` · `DELETE` · `POST` | `/jobs/dashboard` · `/jobs/{id}` · `/jobs/{id}/tracks/{track_id}/recover` y `/tracks/queue` · `/tracks/rejected` · `/tracks/recent` (`?page&limit&categoria`) · `/tracks/{id}/redownload` | Tablero de Historial (con sesión): contadores y primeras filas de cada bloque; descartar una búsqueda sin confirmar; recuperar una rechazada; la cola con `posicion`; recientes paginadas; volver a bajar en la misma carpeta |

**Regla de intentos:** cada ronda de descarga suma 1 a `rondas_usadas`; bajar bien o cambiar la URL lo deja en 0;
cancelar y las cookies vencidas (`waiting_auth`) no suman. El tope es `MAX_RETRIES_PER_TRACK`.

**Progreso:** el % de descarga sale de los `progress_hooks` de yt-dlp. La conversión a MP3 ya no usa
`FFmpegExtractAudio`: corre ffmpeg con `-progress`, que informa los segundos convertidos. El progreso vive en memoria;
no se escribe en la base.

**TikTok:** se baja con yt-dlp como cualquier otro video. Necesita `curl_cffi` (en `requirements.txt`): sin él,
TikTok rechaza a yt-dlp con «Unexpected response from webpage request».

**Tracklist:** si el video trae capítulos y el usuario marcó «cortar», cada tema se guarda como un MP3 aparte
(`-c copy`, sin volver a codificar) en una subcarpeta con el nombre del video.

Cada canción baja a una carpeta temporal `.parcial-<track_id>`. Si se cancela o falla, se borra entera; si baja bien,
el MP3 (y la carpeta de temas) pasa a la carpeta definitiva. Al arrancar, la API vuelve a encolar lo que quedó a
medias por un reinicio.

### Pruebas

```bash
cd music-extract
pip install -r requirements-dev.txt
python -m pytest tests
```

Usan una base y una carpeta de descargas temporales (`MUSIC_EXTRACT_DB_PATH`, `MUSIC_DOWNLOADED_ROOT`) y un motor
falso, así que no tocan `app/db/db_downloads.sqlite` ni necesitan red. La conversión y el corte usan ffmpeg real.

## Licencia

[MIT](LICENSE)