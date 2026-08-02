import { createServer } from 'http';
import { randomBytes } from 'crypto';
import { writeFileSync, mkdirSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';
import { exec } from 'child_process';

const __dirname = dirname(fileURLToPath(import.meta.url));

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg.startsWith('--')) {
      const key = arg.slice(2);
      const value = argv[i + 1];
      args[key] = value;
      i++;
    }
  }
  return args;
}

const cliArgs = parseArgs(process.argv.slice(2));

const CLIENT_ID     = cliArgs['client-id'];
const CLIENT_SECRET = cliArgs['client-secret'];

if (!CLIENT_ID || !CLIENT_SECRET) {
  console.error('Uso: node fetch_playlists.js --client-id <id> --client-secret <secret>');
  process.exit(1);
}

const REDIRECT_URI  = 'http://127.0.0.1:8888/callback';
const PORT          = 8888;
const SCOPES        = 'playlist-read-private playlist-read-collaborative';
const TARGET_NAMES  = ['rock english', 'rock español'];
const OUT_DIR       = join(__dirname, 'playlists-fetched');

function openBrowser(url) {
  exec(`start "" "${url}"`);
}

function waitForAuthCode(state) {
  return new Promise((resolve, reject) => {
    const server = createServer((req, res) => {
      const reqUrl = new URL(req.url, `http://127.0.0.1:${PORT}`);
      // Sin esto, peticiones como /favicon.ico quedan colgadas.
      if (reqUrl.pathname !== '/callback') {
        res.writeHead(404).end();
        return;
      }

      const error    = reqUrl.searchParams.get('error');
      const code     = reqUrl.searchParams.get('code');
      const retState = reqUrl.searchParams.get('state');

      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(`<html><body style="font-family:sans-serif;padding:2rem">
        <h2>✅ Autorización completada</h2>
        <p>Puedes cerrar esta pestaña y volver a la terminal.</p>
      </body></html>`);
      server.close();

      if (error)              return reject(new Error(`Spotify auth error: ${error}`));
      if (retState !== state) return reject(new Error('State mismatch'));
      resolve(code);
    });

    server.listen(PORT, '127.0.0.1', () =>
      console.log(`Servidor local en http://127.0.0.1:${PORT} ...`)
    );
    server.on('error', reject);
  });
}

async function getAccessToken() {
  const state = randomBytes(8).toString('hex');

  const authUrl = new URL('https://accounts.spotify.com/authorize');
  authUrl.searchParams.set('response_type', 'code');
  authUrl.searchParams.set('client_id', CLIENT_ID);
  authUrl.searchParams.set('scope', SCOPES);
  authUrl.searchParams.set('redirect_uri', REDIRECT_URI);
  authUrl.searchParams.set('state', state);

  console.log('Abriendo navegador para autorización...');
  openBrowser(authUrl.toString());

  const code = await waitForAuthCode(state);
  console.log('Código recibido. Obteniendo token...');

  const res = await fetch('https://accounts.spotify.com/api/token', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      Authorization: 'Basic ' + Buffer.from(`${CLIENT_ID}:${CLIENT_SECRET}`).toString('base64'),
    },
    body: new URLSearchParams({
      grant_type:   'authorization_code',
      code,
      redirect_uri: REDIRECT_URI,
    }),
  });

  if (!res.ok) throw new Error(`Token exchange failed ${res.status}: ${await res.text()}`);
  const data = await res.json();
  console.log(`Token obtenido — scopes: ${data.scope}\n`);
  return data.access_token;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function fetchApi(endpoint, token, attempt = 0) {
  const res = await fetch(`https://api.spotify.com/${endpoint}`, {
    headers: { Authorization: `Bearer ${token}` },
  });

  // 429: Spotify indica cuántos segundos esperar en Retry-After.
  if (res.status === 429 && attempt < 5) {
    const wait = (Number(res.headers.get('retry-after')) || 2) + 1;
    console.warn(`\n  Rate limit. Esperando ${wait}s...`);
    await sleep(wait * 1000);
    return fetchApi(endpoint, token, attempt + 1);
  }

  if (!res.ok) {
    const err = await res.text();
    if (res.status === 403) {
      throw new Error(
        `Spotify API 403 [${endpoint}]\n` +
        `  GET /playlists/{id}/items solo funciona con playlists de las que eres\n` +
        `  DUEÑO o COLABORADOR. Playlists editoriales de Spotify o de terceros\n` +
        `  devuelven 403 incluso con un token de usuario válido.\n` +
        `  Respuesta: ${err}`
      );
    }
    throw new Error(`Spotify API ${res.status} [${endpoint}]: ${err}`);
  }
  return res.json();
}

async function getAllUserPlaylists(token) {
  const playlists = [];
  let url = 'v1/me/playlists?limit=50&offset=0';
  while (url) {
    const data = await fetchApi(url, token);
    playlists.push(...data.items.filter(Boolean));
    url = data.next ? data.next.replace('https://api.spotify.com/', '') : null;
  }
  return playlists;
}

async function getAllPlaylistTracks(playlistId, token) {
  const tracks = [];
  // OJO: /playlists/{id}/items tiene máximo 50 por página (el viejo /tracks aceptaba 100).
  let url = `v1/playlists/${playlistId}/items?limit=50&offset=0`;
  while (url) {
    const data = await fetchApi(url, token);
    tracks.push(...data.items.filter(i => i.item && i.item.id && i.item.type === 'track'));
    url = data.next ? data.next.replace('https://api.spotify.com/', '') : null;
    process.stdout.write(`\r  Canciones cargadas: ${tracks.length}`);
  }
  process.stdout.write('\n');
  return tracks;
}

function msToMMSS(ms) {
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function csvEscape(value) {
  const str = value === null || value === undefined ? '' : String(value);
  return str.includes(';') || str.includes('"') || str.includes('\n')
    ? `"${str.replace(/"/g, '""')}"`
    : str;
}

function tracksToCSV(tracks) {
  const headers = [
    'name', 'artists', 'album', 'release_date',
    'duration_ms', 'duration', 'track_number', 'disc_number',
    'isrc', 'spotify_id', 'spotify_url', 'popularity', 'explicit',
  ];

  const rows = tracks.map(({ item: t }) => [
    t.name,
    t.artists.map(a => a.name).join('; '),
    t.album.name,
    t.album.release_date,
    t.duration_ms,
    msToMMSS(t.duration_ms),
    t.track_number,
    t.disc_number,
    t.external_ids?.isrc ?? '',
    t.id,
    t.external_urls?.spotify ?? '',
    t.popularity ?? '',   // deprecado por Spotify: puede venir vacío
    t.explicit ? 'yes' : 'no',
  ]);

  return [headers.join(';'), ...rows.map(r => r.map(csvEscape).join(';'))].join('\n');
}

function slugify(name) {
  return name.toLowerCase()
    .replace(/[áà]/g, 'a').replace(/[éè]/g, 'e')
    .replace(/[íì]/g, 'i').replace(/[óò]/g, 'o')
    .replace(/[úù]/g, 'u').replace(/ñ/g, 'n')
    .replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
}

async function main() {
  const token = await getAccessToken();

  const me = await fetchApi('v1/me', token);
  console.log(`Autenticado como: ${me.display_name} (${me.id})`);

  console.log('Buscando tus playlists...');
  const allPlaylists = await getAllUserPlaylists(token);

  const byName = allPlaylists.filter(p =>
    TARGET_NAMES.some(t => p.name.toLowerCase().includes(t))
  );

  // /me/playlists incluye playlists que SIGUES pero no posees.
  // Pedir sus items da 403, así que las descartamos antes de intentarlo.
  const isReadable = p => p.owner?.id === me.id || p.collaborative === true;

  const matched = byName.filter(isReadable);
  const skipped = byName.filter(p => !isReadable(p));

  if (skipped.length > 0) {
    console.warn('\nOmitidas (no eres dueño ni colaborador → darían 403):');
    skipped.forEach(p => console.warn(`  - "${p.name}" (owner: ${p.owner?.display_name ?? '?'})`));
  }

  if (matched.length === 0) {
    console.error('\nNo se encontraron playlists tuyas que coincidan. Tus playlists:');
    allPlaylists
      .filter(isReadable)
      .forEach(p => console.log(`  - "${p.name}"`));
    process.exit(1);
  }

  mkdirSync(OUT_DIR, { recursive: true });

  for (const playlist of matched) {
    console.log(`\n── "${playlist.name}" (${playlist.id}) ──`);
    const tracks = await getAllPlaylistTracks(playlist.id, token);

    const csv      = tracksToCSV(tracks);
    const filename = `${slugify(playlist.name)}.csv`;
    writeFileSync(join(OUT_DIR, filename), '﻿' + csv, 'utf8');
    console.log(`  Guardado: playlists-fetched/${filename} (${tracks.length} canciones)`);
  }

  console.log('\n¡Listo!');
}

main().catch(err => {
  console.error('\nError:', err.message);
  process.exit(1);
});
