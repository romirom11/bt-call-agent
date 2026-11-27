"""Web UI: watch calls live, set the agent's task and settings, read transcripts.

Runs inside `bt-call-agent run --web`, on the same event loop as the bridge.
Every request needs the access token (~/.config/bt-call-agent/web-token): the
UI can read transcripts and replace the API key, and even on 127.0.0.1 any
web page the browser opens could otherwise reach it.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import pathlib
import re
import socket
import threading
import time
from dataclasses import dataclass, field

from aiohttp import web

from . import __version__, config, system
from .events import hub

STATIC = config.DATA / "web"
COOKIE = "bt_call_agent"
TRANSCRIPT = re.compile(r"^call-\d{8}-\d{6}\.txt$")
JSON_LIMIT = 256 * 1024


@dataclass
class Context:
    transcripts_dir: pathlib.Path
    cli_overrides: dict = field(default_factory=dict)
    cli_prompt: str | None = None
    key_file: str | None = None
    token: str = ""


CTX = web.AppKey("ctx", Context)


# --------------------------------------------------------------------------- helpers

class Problem(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def ok(data=None, **extra) -> web.Response:
    return web.json_response({"ok": True, **({"data": data} if data is not None else {}), **extra},
                             dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def body(request: web.Request) -> dict:
    if request.content_length and request.content_length > JSON_LIMIT:
        raise Problem(413, "request too large")
    try:
        data = await request.json()
    except ValueError:
        raise Problem(400, "expected a JSON body")
    if not isinstance(data, dict):
        raise Problem(400, "expected a JSON object")
    return data


def urls(host: str, port: int, token: str) -> list[str]:
    """Links to print for the user: with the token, so one click logs in."""
    def url(h):
        return f"http://{'[' + h + ']' if ':' in h else h}:{port}/?token={token}"
    if host not in ("0.0.0.0", "::", ""):
        return [url(host)]
    out = [url(socket.gethostname())]
    try:                                    # the LAN address; connect() on UDP sends nothing
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            out.append(url(s.getsockname()[0]))
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- middleware

def authorized(request: web.Request) -> bool:
    token = request.app[CTX].token
    given = request.cookies.get(COOKIE, "")
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        given = auth[7:].strip()
    return bool(given) and hmac.compare_digest(given.encode(), token.encode())


@web.middleware
async def guard(request: web.Request, handler):
    path = request.path
    if path.startswith("/assets/"):
        resp = await handler(request)
    elif path == "/" and "token" in request.query:
        # log in: keep the token in a cookie and take it out of the address bar
        if not hmac.compare_digest(request.query["token"].encode(), request.app[CTX].token.encode()):
            raise web.HTTPFound("/")
        resp = web.Response(status=302, headers={"Location": "/"})
        resp.set_cookie(COOKIE, request.app[CTX].token, max_age=365 * 86400,
                        httponly=True, samesite="Strict")
        return resp
    elif not authorized(request):
        if path.startswith("/api/"):
            resp = web.json_response({"ok": False, "error": "not signed in"}, status=401)
        else:
            resp = web.FileResponse(STATIC / "signin.html", status=401)
    else:
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] != request.host:
                return web.json_response({"ok": False, "error": "cross-site request"}, status=403)
        try:
            resp = await handler(request)
        except Problem as e:
            resp = web.json_response({"ok": False, "error": str(e)}, status=e.status)
        except ValueError as e:
            resp = web.json_response({"ok": False, "error": str(e)}, status=400)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Content-Security-Policy",
                            "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
    if path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# --------------------------------------------------------------------------- state

def settings_state(ctx: Context) -> dict:
    try:
        saved, error = config.load_settings(), None
    except ValueError as e:
        saved, error = {}, str(e)
    return {"saved": saved, "defaults": config.DEFAULTS,
            "effective": {**config.DEFAULTS, **saved, **ctx.cli_overrides},
            "locked": sorted(ctx.cli_overrides), "error": error,
            "voices": config.VOICES}


def prompt_state(ctx: Context) -> dict:
    path = config.prompt_path(ctx.cli_prompt)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        text = f"(can't read {path}: {e})"
    return {"text": text, "path": str(path), "locked": bool(ctx.cli_prompt),
            "bundled": path.parent == config.DATA}


def key_state(ctx: Context) -> dict:
    return {"source": config.key_source(ctx.key_file), "file": str(config.key_path())}


async def get_state(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    return ok({"version": __version__, "live": hub.snapshot(),
               "settings": settings_state(ctx), "prompt": prompt_state(ctx),
               "key": key_state(ctx), "transcripts_dir": str(ctx.transcripts_dir)})


async def get_events(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    q = hub.subscribe()

    async def send(event: dict):
        await resp.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())

    try:
        await send({"type": "snapshot", **hub.snapshot()})
        while True:
            try:
                await send(await asyncio.wait_for(q.get(), 15))
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        hub.unsubscribe(q)
    return resp


# --------------------------------------------------------------------------- settings, prompt, key

async def put_settings(request: web.Request) -> web.Response:
    config.save_settings(await body(request))
    return ok(settings_state(request.app[CTX]))


async def put_prompt(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    if ctx.cli_prompt:
        raise Problem(409, f"the agent was started with --prompt {ctx.cli_prompt}, "
                           "so this prompt can't be changed here")
    text = (await body(request)).get("text")
    if not isinstance(text, str):
        raise Problem(400, "text is required")
    config.save_prompt(text)
    return ok(prompt_state(ctx))


async def get_presets(request: web.Request) -> web.Response:
    return ok(config.list_presets())


async def post_preset(request: web.Request) -> web.Response:
    data = await body(request)
    name, text = data.get("name"), data.get("text")
    if not isinstance(name, str) or not isinstance(text, str):
        raise Problem(400, "name and text are required")
    if any(p["builtin"] and p["name"] == name.strip() for p in config.list_presets()):
        raise Problem(409, f"“{name}” is a bundled example: pick another name")
    config.save_preset(name, text)
    return ok(config.list_presets())


async def delete_preset(request: web.Request) -> web.Response:
    if not config.delete_preset(request.match_info["name"]):
        raise Problem(404, "no saved prompt with that name")
    return ok(config.list_presets())


async def put_key(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    key = (await body(request)).get("key")
    if not isinstance(key, str):
        raise Problem(400, "key is required")
    config.save_api_key(key)
    state = key_state(ctx)
    note = None
    if state["source"] == "env":
        note = "Saved, but GEMINI_API_KEY in the agent's environment still takes priority."
    elif state["source"] == "flag":
        note = f"Saved, but --key-file {ctx.key_file} still takes priority."
    hub.log("API key replaced from the web UI")
    return ok(state, note=note)


# --------------------------------------------------------------------------- transcripts

def transcript_file(ctx: Context, name: str) -> pathlib.Path:
    if not TRANSCRIPT.match(name):
        raise Problem(404, "no such transcript")
    path = ctx.transcripts_dir / name
    if not path.is_file():
        raise Problem(404, "no such transcript")
    return path


def _list_transcripts(folder: pathlib.Path) -> list[dict]:
    if not folder.is_dir():
        return []
    out = []
    for f in sorted(folder.glob("call-*.txt"), reverse=True)[:500]:
        if not TRANSCRIPT.match(f.name):
            continue
        st = f.stat()
        with open(f, encoding="utf-8", errors="replace") as fh:
            head = [fh.readline().strip() for _ in range(2)]
        out.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime,
                    "preview": " ".join(h for h in head if h)[:200]})
    return out


async def get_transcripts(request: web.Request) -> web.Response:
    return ok(await asyncio.to_thread(_list_transcripts, request.app[CTX].transcripts_dir))


async def get_transcript(request: web.Request) -> web.Response:
    path = transcript_file(request.app[CTX], request.match_info["name"])
    if request.query.get("download"):
        return web.FileResponse(path, headers={
            "Content-Disposition": f'attachment; filename="{path.name}"',
            "Content-Type": "text/plain; charset=utf-8"})
    return ok({"name": path.name, "text": path.read_text(encoding="utf-8", errors="replace")})


async def delete_transcript(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    path = transcript_file(ctx, request.match_info["name"])
    if hub.call and hub.call.get("transcript") == path.name and "ended" not in hub.call:
        raise Problem(409, "this call is still going on")
    path.unlink()
    return ok()


# --------------------------------------------------------------------------- call, system

async def stop_agent(request: web.Request) -> web.Response:
    if not hub.stop_agent():
        raise Problem(409, "the agent isn't in a call")
    hub.log("(agent stopped from the web UI; the call stays up)")
    return ok()


async def get_doctor(request: web.Request) -> web.Response:
    return ok(await asyncio.to_thread(system.checks))


_pair_cancel: threading.Event | None = None


async def start_pair(request: web.Request) -> web.Response:
    global _pair_cancel
    if hub.pairing and hub.pairing.get("running"):
        raise Problem(409, "pairing is already running")
    timeout = (await body(request)).get("timeout", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 10 <= timeout <= 600:
        raise Problem(400, "timeout must be 10 to 600 seconds")
    loop = asyncio.get_running_loop()
    cancel = _pair_cancel = threading.Event()
    state = {"running": True, "messages": [], "result": None,
             "started": time.time(), "timeout": timeout}
    hub.set_pairing(state)

    def say(msg: str):
        def apply():
            state["messages"].append(msg)
            hub.set_pairing(state)
        loop.call_soon_threadsafe(apply)

    def run():
        try:
            rc = system.pair(timeout, out=say, cancel=cancel)
        except Exception as e:     # bluetoothctl missing, etc.
            say(f"✗ {e}")
            rc = 1

        def done():
            state["running"], state["result"] = False, rc
            hub.set_pairing(state)
        loop.call_soon_threadsafe(done)

    threading.Thread(target=run, name="pair", daemon=True).start()
    return ok(state)


async def stop_pair(request: web.Request) -> web.Response:
    if _pair_cancel:
        _pair_cancel.set()
    return ok()


# --------------------------------------------------------------------------- app

async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


def make_app(ctx: Context) -> web.Application:
    if not ctx.token:
        ctx.token = config.web_token()
    app = web.Application(middlewares=[guard], client_max_size=JSON_LIMIT)
    app[CTX] = ctx
    app.add_routes([
        web.get("/", index),
        web.static("/assets", STATIC),
        web.get("/api/state", get_state),
        web.get("/api/events", get_events),
        web.put("/api/settings", put_settings),
        web.put("/api/prompt", put_prompt),
        web.get("/api/presets", get_presets),
        web.post("/api/presets", post_preset),
        web.delete("/api/presets/{name}", delete_preset),
        web.put("/api/key", put_key),
        web.get("/api/transcripts", get_transcripts),
        web.get("/api/transcripts/{name}", get_transcript),
        web.delete("/api/transcripts/{name}", delete_transcript),
        web.post("/api/agent/stop", stop_agent),
        web.get("/api/doctor", get_doctor),
        web.post("/api/pair", start_pair),
        web.delete("/api/pair", stop_pair),
    ])
    return app


async def start(host: str, port: int, ctx: Context) -> web.AppRunner:
    from .bridge import log
    hub.bind(asyncio.get_running_loop())
    app = make_app(ctx)
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    for u in urls(host, port, ctx.token):
        print(f"web UI: {u}", flush=True)       # not into the UI's own log: it holds the token
    log(f"web UI on {host}:{port}")
    if host not in ("127.0.0.1", "::1", "localhost"):
        log("(web UI is reachable from the network: keep it on a trusted LAN or VPN)")
    return runner
