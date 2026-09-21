"""cedar-server — a self-hosted backend for the Cedar read-aloud app.

App assembly only: logging, startup migrations, the owner-key gate, routers,
and a small status page at /. Routes live in cedar/routes/.

Auth model
----------
There is one owner and one key (see cedar/auth.py). Every request except the
status page and /api/health must carry it as ``Authorization: Bearer <key>``
(or ``X-Cedar-Key``). Any device holding the key sees the same library. The
gate is deny-by-default: a path that isn't on the short open list needs the
key whether or not a route exists for it, so nothing — not the API schema,
not a 404 — tells an unauthenticated peer what is here.
"""
from __future__ import annotations

import html
import logging
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__, auth, db, dictionary, settings, translate
from .bodylimit import BodyLimitMiddleware
from .config import DATA_DIR, SERVER_NAME
from .routes import all_routers
from .textsafe import printable

# Reachable without the key: the status page (setup instructions, no secrets)
# and the health probe the app uses to recognise a server before pairing.
OPEN_PATHS = frozenset({"/", "/api/health"})


def _configure_logging() -> None:
    """Send the app's own "cedar.*" loggers through uvicorn's handler, and keep
    a rotating copy on the data volume so `docker logs` being wiped by a
    redeploy doesn't take the history with it."""
    app_log = logging.getLogger("cedar")
    app_log.setLevel(logging.INFO)
    stream = logging.getLogger("uvicorn.error").handlers or [logging.StreamHandler()]
    if not app_log.handlers:
        app_log.handlers = list(stream)
    # The generated-key banner goes to the console only, never to the file.
    banner = logging.getLogger("cedar.auth.banner")
    banner.propagate = False
    banner.handlers = list(stream)
    try:
        log_dir = DATA_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "cedar.log", maxBytes=8_000_000,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        for name in ("cedar", "uvicorn.error"):
            lg = logging.getLogger(name)
            if not any(isinstance(h, RotatingFileHandler) for h in lg.handlers):
                lg.addHandler(fh)
    except Exception:
        # A read-only or missing volume must never stop the service booting.
        logging.getLogger("cedar").warning("file logging unavailable", exc_info=True)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _configure_logging()
    db.init_db()
    settings.init_tables()
    dictionary.init_tables()
    translate.init_tables()
    auth.load_key()
    logging.getLogger("cedar").info("cedar-server %s ready (schema v%d, data in %s)",
                                    __version__, db.schema_version(), DATA_DIR)
    yield


# No interactive docs and no published schema: the API is for the app, and a
# route list is exactly what someone probing the port would like to have.
app = FastAPI(title="cedar-server", version=__version__, docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=_lifespan)

app.add_middleware(BodyLimitMiddleware)


def _path(request: Request) -> str:
    """The path the router matches on. Not `request.url.path`: that URL is
    rebuilt from the Host header, which the peer writes, so "Host: x/api/health?"
    can make it read as an open path while the router serves another one."""
    return request.scope["path"]


@app.middleware("http")
async def _gate(request: Request, call_next):
    if _path(request) not in OPEN_PATHS:
        verdict = auth.authenticate(request)
        if verdict == "throttled":
            return JSONResponse({"detail": "Too many wrong keys — try again later."},
                                status_code=429,
                                headers={"Retry-After": str(auth.THROTTLE_RETRY_S)})
        if verdict != "ok":
            return JSONResponse({"detail": "Unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
    return await call_next(request)


@app.middleware("http")
async def _headers(request: Request, call_next):
    """Browser-facing hygiene on every response. The API is JSON for an app,
    so nothing should be sniffed, framed or cached by anything in between."""
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "no-referrer")
    if _path(request) == "/":
        h.setdefault("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
    elif "cache-control" not in h:  # covers and thumbnails set their own
        h["Cache-Control"] = "no-store"
    return resp


_req_log = logging.getLogger("cedar.request")


@app.middleware("http")
async def _log_failures(request: Request, call_next):
    """One line per failed API request. Successes aren't logged: playback alone
    fires dozens of /tts calls a minute."""
    started = time.monotonic()
    is_api = _path(request).startswith("/api/")
    path = printable(_path(request), 300)
    try:
        resp = await call_next(request)
    except Exception:
        if is_api:
            _req_log.exception("%s %s raised", request.method, path)
        raise
    if resp.status_code >= 400 and is_api:
        _req_log.warning("%s %s -> %s (%.0fms)", request.method, path,
                         resp.status_code, (time.monotonic() - started) * 1000)
    return resp


for r in all_routers:
    app.include_router(r)


_STATUS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>
  body {{ margin:0; background:#23262D; color:#EFEDE7; font:17px/1.55 system-ui, sans-serif; }}
  main {{ max-width:520px; margin:0 auto; padding:56px 24px 64px; }}
  h1 {{ font-size:26px; margin:14px 0 4px; }}
  p.sub {{ color:#B9B4A9; margin:0 0 26px; }}
  code {{ background:#2E323A; padding:2px 6px; border-radius:6px; font-size:15px; }}
  ol {{ padding-left:22px; }} li {{ margin:10px 0; }}
  .note {{ color:#B9B4A9; font-size:14px; margin-top:26px; }}
</style></head><body><main>
  <div style="font-size:40px">&#127794;</div>
  <h1>{name}</h1>
  <p class="sub">cedar-server {version} is running.</p>
  <ol>
    <li>Open the Cedar app and choose <b>Use your own server</b>.</li>
    <li>Enter this address: <code>{origin}</code></li>
    <li>Paste the owner key from the server log (<code>docker compose logs cedar</code>)
        or <code>CEDAR_KEY</code> in your <code>.env</code>.</li>
  </ol>
  <p class="note">Every device given the same address and key shares one library.</p>
</main></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # The Host header is the peer's to choose; escaped like any other input.
    origin = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    return _STATUS_PAGE.format(name=html.escape(SERVER_NAME), version=html.escape(__version__),
                               origin=html.escape(printable(origin, 300)))
