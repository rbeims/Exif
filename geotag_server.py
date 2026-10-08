#!/usr/bin/env python3
"""
Tiny web server for uploading photos + GPX tracks and geotagging them with exiftool.

Usage:
    python3 geotag_server.py [--host 0.0.0.0] [--port 8000] [--dir ./uploads]

Open http://<machine>:<port>/ in a browser, drop photos and .gpx files, then
press "Geotag" (or tick "auto-run" to geotag as soon as the uploads finish).

Uploaded files land in <dir>/photos and <dir>/gpx. Only the Python standard
library and the `exiftool` binary are required.

Optionally pass --immich-url and --immich-key (or IMMICH_URL / IMMICH_API_KEY)
to upload the geotagged photos to an Immich server.
"""

import argparse
import hashlib
import html
import http.client
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PHOTO_EXTS = {
    ".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff", ".png", ".webp",
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".pef", ".srw",
    ".mp4", ".mov",
}
TRACK_EXTS = {".gpx"}
CHUNK = 1024 * 1024

# Offsets like "+02:00", "-5:30", "0" for -geosync / timezone fields.
OFFSET_RE = re.compile(r"^[+-]?\d{1,2}(:\d{2}(:\d{2})?)?$")

geotag_lock = threading.Lock()
immich_lock = threading.Lock()


def safe_name(name: str) -> str:
    """Strip any directory parts and characters we don't want in a filename."""
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = re.sub(r"[^\w.\- ()]+", "_", name)
    if not name or name in {".", ".."} or name.startswith("."):
        raise ValueError("invalid filename")
    return name


def list_files(folder: Path):
    if not folder.is_dir():
        return []
    return sorted(
        p.name for p in folder.iterdir()
        if p.is_file() and not p.name.startswith(".") and not p.name.endswith("_original")
    )


def run_geotag(photo_dir: Path, gpx_dir: Path, opts: dict) -> dict:
    gpx_files = [gpx_dir / n for n in list_files(gpx_dir)]
    photos = list_files(photo_dir)
    if not gpx_files:
        return {"ok": False, "output": "No GPX files uploaded yet."}
    if not photos:
        return {"ok": False, "output": "No photos uploaded yet."}

    cmd = ["exiftool", "-P"]  # -P: preserve file modification date
    for g in gpx_files:
        cmd += ["-geotag", str(g)]

    tz = (opts.get("timezone") or "").strip()
    if tz:
        if not OFFSET_RE.match(tz):
            return {"ok": False, "output": f"Invalid timezone offset: {tz!r}"}
        if tz[0] not in "+-":
            tz = "+" + tz
        # Camera clocks store local time without a zone; tell exiftool which zone it was.
        cmd.append(f"-geotime<${{DateTimeOriginal}}{tz}")

    sync = (opts.get("geosync") or "").strip()
    if sync:
        if not OFFSET_RE.match(sync):
            return {"ok": False, "output": f"Invalid geosync offset: {sync!r}"}
        cmd.append(f"-geosync={sync}")

    if opts.get("overwrite", True):
        cmd.append("-overwrite_original")

    cmd += [str(photo_dir / n) for n in photos]

    # Never let two geotag runs touch the same files at once.
    if not geotag_lock.acquire(blocking=False):
        return {"ok": False, "output": "A geotag run is already in progress."}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return {"ok": False, "output": "exiftool not found on PATH. Install it first."}
    finally:
        geotag_lock.release()

    printable = " ".join(f"'{c}'" if " " in c or "$" in c else c for c in cmd)
    return {
        "ok": proc.returncode == 0,
        "output": f"$ {printable}\n\n{proc.stdout}{proc.stderr}",
    }


class ImmichError(Exception):
    pass


class Immich:
    """Minimal Immich API client (stdlib only). `url` is the server root, e.g. http://localhost:2283."""

    def __init__(self, url: str, key: str):
        self.url = urlparse(url.rstrip("/"))
        self.key = key
        self._needs_device_ids = None

    def _conn(self):
        cls = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
        return cls(self.url.netloc, timeout=300)

    def _path(self, path: str) -> str:
        return f"{self.url.path}/api{path}"

    def _finish(self, conn, method, path):
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if resp.status >= 400:
            raise ImmichError(f"{method} {path}: HTTP {resp.status} {data[:300].decode(errors='replace')}")
        return resp.status, (json.loads(data) if data else None)

    def request(self, method: str, path: str, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"x-api-key": self.key, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn = self._conn()
        conn.request(method, self._path(path), body=body, headers=headers)
        return self._finish(conn, method, path)[1]

    def needs_device_ids(self) -> bool:
        # Immich < 3 requires deviceAssetId/deviceId on uploads; v3 dropped them.
        if self._needs_device_ids is None:
            try:
                self._needs_device_ids = self.request("GET", "/server/version")["major"] < 3
            except Exception:
                self._needs_device_ids = True
        return self._needs_device_ids

    def upload(self, path: Path) -> dict:
        """Upload one file; returns {"id", "status": "created" | "duplicate"}."""
        sha1 = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                sha1.update(chunk)

        st = path.stat()
        mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
        fields = {"fileCreatedAt": mtime, "fileModifiedAt": mtime}
        if self.needs_device_ids():
            fields["deviceAssetId"] = f"{path.name}-{st.st_size}-{sha1.hexdigest()[:12]}"
            fields["deviceId"] = "geotag-server"

        # Build the multipart body by hand so big RAW/video files are streamed, not loaded in RAM.
        boundary = uuid.uuid4().hex
        head = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            for k, v in fields.items()
        )
        head += (f'--{boundary}\r\nContent-Disposition: form-data; name="assetData"; '
                 f'filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        tail = f"\r\n--{boundary}--\r\n".encode()

        conn = self._conn()
        conn.putrequest("POST", self._path("/assets"))
        conn.putheader("x-api-key", self.key)
        conn.putheader("Accept", "application/json")
        conn.putheader("x-immich-checksum", sha1.hexdigest())  # lets Immich skip known files
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(len(head) + st.st_size + len(tail)))
        conn.endheaders()
        conn.send(head)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                conn.send(chunk)
        conn.send(tail)
        return self._finish(conn, "POST", "/assets")[1]

    def add_to_album(self, name: str, asset_ids: list) -> str:
        albums = self.request("GET", "/albums") or []
        album = next((a for a in albums if a.get("albumName") == name), None)
        if album is None:
            self.request("POST", "/albums", {"albumName": name, "assetIds": asset_ids})
            return f"created album '{name}' with {len(asset_ids)} photo(s)"
        self.request("PUT", f"/albums/{album['id']}/assets", {"ids": asset_ids})
        return f"added {len(asset_ids)} photo(s) to album '{name}'"


def run_immich_upload(immich: Immich, photo_dir: Path, opts: dict) -> dict:
    photos = list_files(photo_dir)
    if not photos:
        return {"ok": False, "output": "No photos to upload."}
    if not immich_lock.acquire(blocking=False):
        return {"ok": False, "output": "An Immich upload is already in progress."}

    lines, ids, created, failed = [], [], 0, 0
    try:
        for n in photos:
            try:
                r = immich.upload(photo_dir / n)
                ids.append(r["id"])
                created += r["status"] == "created"
                lines.append(f"{r['status']:>9}  {n}")
            except Exception as e:
                failed += 1
                lines.append(f"   FAILED  {n}: {e}")

        album = (opts.get("album") or "").strip()
        if album and ids:
            try:
                lines.append("\n" + immich.add_to_album(album, ids))
            except Exception as e:
                failed += 1
                lines.append(f"\nalbum FAILED: {e}")
    finally:
        immich_lock.release()

    summary = f"Immich: {created} uploaded, {len(ids) - created} already there, {failed} failed"
    return {"ok": failed == 0, "output": summary + "\n\n" + "\n".join(lines)}


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Photo Geotagger</title>
<style>
  :root { color-scheme: light dark; --accent: #2b7de9; }
  body { font-family: system-ui, sans-serif; max-width: 820px; margin: 0 auto; padding: 16px; }
  h1 { font-size: 1.4rem; }
  #drop { border: 2px dashed #888; border-radius: 10px; padding: 32px; text-align: center; cursor: pointer; }
  #drop.over { border-color: var(--accent); background: rgba(43,125,233,.08); }
  .row { display: flex; gap: 16px; flex-wrap: wrap; margin: 16px 0; align-items: end; }
  label { display: flex; flex-direction: column; font-size: .85rem; gap: 4px; }
  label.inline { flex-direction: row; align-items: center; }
  input[type=text] { padding: 6px; width: 9em; }
  button { padding: 8px 16px; border-radius: 6px; border: 0; background: var(--accent); color: #fff; cursor: pointer; font-size: 1rem; }
  button.secondary { background: #777; }
  button:disabled { opacity: .5; cursor: default; }
  .cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 600px) { .cols { grid-template-columns: 1fr; } }
  ul { padding-left: 18px; max-height: 220px; overflow: auto; font-size: .9rem; }
  progress { width: 100%; }
  pre { background: rgba(127,127,127,.12); padding: 12px; border-radius: 6px; white-space: pre-wrap; word-break: break-word; max-height: 400px; overflow: auto; }
  .muted { color: #888; font-size: .85rem; }
</style>
</head>
<body>
<h1>Photo Geotagger</h1>

<div id="drop">Drop photos and .gpx files here, or click to choose<br>
  <span class="muted">Accepted: __EXTS__</span></div>
<input id="picker" type="file" multiple hidden>
<p id="status" class="muted"></p>
<progress id="bar" value="0" max="1" hidden></progress>

<div class="row">
  <label>Camera timezone (e.g. +02:00)
    <input id="timezone" type="text" placeholder="server local"></label>
  <label>Clock correction / geosync (e.g. +0:01:30)
    <input id="geosync" type="text" placeholder="none"></label>
  <label class="inline"><input id="overwrite" type="checkbox" checked>&nbsp;overwrite originals (no _original backups)</label>
  <label class="inline"><input id="auto" type="checkbox" checked>&nbsp;auto-run after upload</label>
</div>
<div class="row immich" hidden>
  <label>Immich album (optional)
    <input id="album" type="text" placeholder="no album"></label>
  <label class="inline"><input id="autoImmich" type="checkbox" checked>&nbsp;upload to Immich after geotagging</label>
</div>
<div class="row">
  <button id="run">Geotag photos</button>
  <button id="immich" class="immich" hidden>Upload to Immich</button>
  <button id="clear" class="secondary">Delete all uploads</button>
</div>

<div class="cols">
  <div><h3>Photos (<span id="np">0</span>)</h3><ul id="photos"></ul></div>
  <div><h3>GPX tracks (<span id="ng">0</span>)</h3><ul id="gpx"></ul></div>
</div>

<h3>Output</h3>
<pre id="out">(nothing yet)</pre>

<script>
const $ = id => document.getElementById(id);
const drop = $('drop'), picker = $('picker');

drop.onclick = () => picker.click();
picker.onchange = () => upload([...picker.files]);
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); upload([...e.dataTransfer.files]); };

function fill(ul, items) {
  ul.innerHTML = '';
  for (const n of items) { const li = document.createElement('li'); li.textContent = n; ul.appendChild(li); }
}

async function refresh() {
  const r = await fetch('/files'); const d = await r.json();
  fill($('photos'), d.photos); fill($('gpx'), d.gpx);
  $('np').textContent = d.photos.length; $('ng').textContent = d.gpx.length;
  document.querySelectorAll('.immich').forEach(el => el.hidden = !d.immich);
  immichEnabled = d.immich;
}
let immichEnabled = false;

function sendOne(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload?name=' + encodeURIComponent(file.name));
    xhr.upload.onprogress = e => onProgress(e.loaded);
    xhr.onload = () => xhr.status === 200 ? resolve() : reject(new Error(xhr.responseText || xhr.status));
    xhr.onerror = () => reject(new Error('network error'));
    xhr.send(file);
  });
}

async function upload(files) {
  if (!files.length) return;
  const total = files.reduce((s, f) => s + f.size, 0) || 1;
  let done = 0, failed = [];
  const bar = $('bar'); bar.hidden = false; bar.max = total; bar.value = 0;
  for (const [i, f] of files.entries()) {
    $('status').textContent = `Uploading ${i + 1}/${files.length}: ${f.name}`;
    try { await sendOne(f, loaded => bar.value = done + loaded); }
    catch (e) { failed.push(`${f.name}: ${e.message}`); }
    done += f.size; bar.value = done;
  }
  bar.hidden = true; picker.value = '';
  $('status').textContent = `Uploaded ${files.length - failed.length}/${files.length} file(s).` +
    (failed.length ? ' Failed: ' + failed.join('; ') : '');
  await refresh();
  if ($('auto').checked) geotag();
}

async function geotag() {
  $('run').disabled = true; $('out').textContent = 'Running exiftool…';
  try {
    const r = await fetch('/geotag', { method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ timezone: $('timezone').value, geosync: $('geosync').value,
                             overwrite: $('overwrite').checked }) });
    const d = await r.json(); $('out').textContent = d.output;
    if (d.ok && immichEnabled && $('autoImmich').checked) {
      $('run').disabled = false; return toImmich(d.output + '\\n\\n');
    }
  } catch (e) { $('out').textContent = 'Error: ' + e.message; }
  $('run').disabled = false; refresh();
}

async function toImmich(prefix = '') {
  $('immich').disabled = true; $('out').textContent = prefix + 'Uploading to Immich…';
  try {
    const r = await fetch('/immich', { method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ album: $('album').value }) });
    const d = await r.json(); $('out').textContent = prefix + d.output;
  } catch (e) { $('out').textContent = prefix + 'Error: ' + e.message; }
  $('immich').disabled = false;
}

$('run').onclick = geotag;
$('immich').onclick = () => toImmich();
$('clear').onclick = async () => {
  if (!confirm('Delete all uploaded photos and GPX files on the server?')) return;
  await fetch('/clear', { method: 'POST' }); refresh(); $('out').textContent = '(cleared)';
};
refresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "GeotagServer/1.0"

    # --- helpers -------------------------------------------------------------
    def _send(self, code, body: bytes, ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=HTTPStatus.OK):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    @property
    def photo_dir(self) -> Path:
        return self.server.upload_dir / "photos"

    @property
    def gpx_dir(self) -> Path:
        return self.server.upload_dir / "gpx"

    # --- routes --------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            exts = ", ".join(sorted(PHOTO_EXTS | TRACK_EXTS))
            self._send(HTTPStatus.OK, PAGE.replace("__EXTS__", html.escape(exts)).encode(),
                       "text/html; charset=utf-8")
        elif path == "/files":
            self._json({"photos": list_files(self.photo_dir), "gpx": list_files(self.gpx_dir),
                        "immich": self.server.immich is not None})
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/upload":
            self.handle_upload(parse_qs(url.query).get("name", [""])[0])
        elif url.path == "/geotag":
            try:
                opts = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                opts = {}
            result = run_geotag(self.photo_dir, self.gpx_dir, opts)
            print(result["output"], flush=True)
            self._json(result)
        elif url.path == "/immich":
            try:
                opts = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                opts = {}
            if self.server.immich is None:
                return self._json({"ok": False, "output": "Immich is not configured."})
            result = run_immich_upload(self.server.immich, self.photo_dir, opts)
            print(result["output"], flush=True)
            self._json(result)
        elif url.path == "/clear":
            self._read_body()
            for d in (self.photo_dir, self.gpx_dir):
                shutil.rmtree(d, ignore_errors=True)
                d.mkdir(parents=True, exist_ok=True)
            self._json({"ok": True})
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found")

    def handle_upload(self, raw_name: str):
        try:
            name = safe_name(raw_name)
        except ValueError:
            self._read_body()
            return self._send(HTTPStatus.BAD_REQUEST, b"invalid filename")

        ext = Path(name).suffix.lower()
        if ext in TRACK_EXTS:
            dest_dir = self.gpx_dir
        elif ext in PHOTO_EXTS:
            dest_dir = self.photo_dir
        else:
            self._read_body()
            return self._send(HTTPStatus.BAD_REQUEST, f"unsupported file type: {ext}".encode())

        remaining = int(self.headers.get("Content-Length") or 0)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        tmp = dest_dir / f".{name}.part"
        try:
            with open(tmp, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ConnectionError("client disconnected")
                    f.write(chunk)
                    remaining -= len(chunk)
            os.replace(tmp, dest)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR, str(e).encode())

        print(f"received {dest}", flush=True)
        self._json({"ok": True, "name": name})


def main():
    ap = argparse.ArgumentParser(description="Upload photos + GPX and geotag them with exiftool.")
    ap.add_argument("--host", default="0.0.0.0", help="interface to bind (default: all)")
    ap.add_argument("--port", type=int, default=8000, help="port to listen on (default: 8000)")
    ap.add_argument("--dir", default="uploads", help="where uploads are stored (default: ./uploads)")
    ap.add_argument("--immich-url", default=os.environ.get("IMMICH_URL"),
                    help="Immich server, e.g. http://localhost:2283 (env: IMMICH_URL)")
    ap.add_argument("--immich-key", default=os.environ.get("IMMICH_API_KEY"),
                    help="Immich API key (env: IMMICH_API_KEY; prefer the env var over the flag)")
    args = ap.parse_args()

    immich = None
    if args.immich_url and args.immich_key:
        immich = Immich(args.immich_url, args.immich_key)
        try:
            me = immich.request("GET", "/users/me")
            print(f"Immich: uploading as {me.get('email')} to {args.immich_url}")
        except Exception as e:
            print(f"WARNING: could not reach Immich ({e}); uploads to it will fail.")
    elif args.immich_url or args.immich_key:
        ap.error("--immich-url and --immich-key must be given together")

    if shutil.which("exiftool") is None:
        print("WARNING: exiftool not found on PATH; uploads work but geotagging will fail.")

    upload_dir = Path(args.dir).resolve()
    (upload_dir / "photos").mkdir(parents=True, exist_ok=True)
    (upload_dir / "gpx").mkdir(parents=True, exist_ok=True)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.upload_dir = upload_dir
    httpd.immich = immich
    print(f"Serving on http://{args.host}:{args.port}/  (files -> {upload_dir})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
