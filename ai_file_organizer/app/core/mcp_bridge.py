"""Local loopback bridge: lets the Filect MCP read/move files using THE APP's folder access
(macOS TCC grants), so the MCP itself never needs Full Disk Access.

Why this exists: macOS grants folder access per-program-identity. Filect.app earns Desktop/
Documents/Downloads access through the normal "Filect wants to access…" prompts. The MCP is a
different, unprivileged process. Instead of granting the MCP its own (scary) Full Disk Access,
the MCP asks the running app to do the file read/move — the app has the grant, so it just works.

Isolation guarantees (this file NEVER affects the normal app):
- start() runs only if the user has connected the MCP, and is wrapped so any failure is swallowed.
- The server runs on its OWN daemon thread — never the Qt UI thread, so it cannot lag the UI.
- It binds ONLY 127.0.0.1 (loopback — never the network) on a random port, and every request
  must carry a per-launch secret token, so no other local process can drive Filect.
"""
import base64
import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_server = None
_token = None
_port = None

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}


def _state_path() -> Path:
    from app.core.settings import settings
    return settings.get_app_data_dir() / "mcp_bridge.json"


def _ensure_auth():
    """Make sure the cloud backend is usable (for transcription / vision fallback)."""
    try:
        from app.core.settings import settings
        from app.core.supabase_client import supabase_auth
        settings.ai_provider = "openai"
        if not supabase_auth.is_authenticated and settings.has_stored_session():
            supabase_auth.restore_session(settings.auth_access_token, settings.auth_refresh_token)
    except Exception:
        pass


# ---------------- the privileged operations (run INSIDE the app) ----------------

def read_content(path: str) -> dict:
    """Read + extract a file's content. Mirrors the MCP's analyze_file routing but returns
    JSON-serializable data (b64 for images/frames). Runs where the file IS readable."""
    p = Path(path)
    if not p.exists():
        return {"error": "file not found"}
    try:
        with open(p, "rb") as f:
            f.read(16)
    except Exception as e:
        return {"error": f"Can't read this file ({e})."}
    suffix = p.suffix.lower()

    # text documents + PDF -> extracted text
    try:
        from app.core.text_extract import extract_file_text, get_supported_text_formats
        if suffix in set(get_supported_text_formats()) or suffix == ".pdf":
            text = (extract_file_text(p) or "").strip()
            if text:
                return {"mode": "raw_text_for_client", "file": p.name, "text": text[:200000]}
    except Exception:
        pass

    # image -> downscaled PNG (b64) for the client's vision
    if suffix in IMAGE_EXTS:
        try:
            import io
            from PIL import Image as _PILImage
            im = _PILImage.open(p)
            im.load()
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            im.thumbnail((1024, 1024))
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return {"mode": "image", "file": p.name, "format": "png",
                    "image_b64": base64.b64encode(buf.getvalue()).decode()}
        except Exception:
            pass

    # video -> keyframes (b64) ; audio -> transcript
    try:
        from app.core.video_analysis import VIDEO_EXTENSIONS, AUDIO_EXTENSIONS, extract_keyframes
    except Exception:
        VIDEO_EXTENSIONS, AUDIO_EXTENSIONS, extract_keyframes = set(), set(), None
    if extract_keyframes and suffix in VIDEO_EXTENSIONS:
        frames = extract_keyframes(p, n=4) or []
        if frames:
            return {"mode": "video_frames", "file": p.name,
                    "frames_b64": [base64.b64encode(f).decode() for f in frames]}
    if suffix in AUDIO_EXTENSIONS:
        _ensure_auth()
        from app.core.transcription import transcribe_audio
        tr = transcribe_audio(str(p)) or {}
        if tr.get("ok") and (tr.get("text") or "").strip():
            return {"mode": "transcript", "file": p.name, "transcript": tr["text"],
                    "duration": tr.get("duration")}

    # fallback: the backend analyzer (runs in the app, has access + auth)
    _ensure_auth()
    from app.core.search import search_service
    meta = search_service._process_single_file({"source_path": str(p), "name": p.name}, p.parent, force_ai=True)
    if meta and not meta.get("_error"):
        return {"mode": "analyzed", "file": p.name, "type": meta.get("label"),
                "tags": meta.get("tags") or [], "caption": meta.get("caption"),
                "transcript_or_text": meta.get("ocr_text") or None}
    return {"error": "could not read or analyze this file"}


def analyze_image_backend(path: str) -> dict:
    _ensure_auth()
    from app.core.vision import analyze_image as _ai
    res = _ai(Path(path))
    return res if res else {"error": "no result (unsupported file or provider off)"}


def stat(path: str) -> dict:
    p = Path(path)
    try:
        return {"exists": p.exists(), "is_dir": p.is_dir(),
                "size": p.stat().st_size if p.exists() else None}
    except Exception as e:
        return {"error": str(e)}


def apply_moves(moves: list) -> dict:
    from app.core.apply import apply_moves as _apply
    success, errors, log_file, renamed = _apply(moves or [])
    return {"success": success, "moved": len(moves or []) - len(errors),
            "errors": errors[:10], "log_file": log_file, "renamed_to_avoid_collision": renamed}


_OPS = {"read_content": read_content, "analyze_image_backend": analyze_image_backend,
        "stat": stat, "apply_moves": apply_moves}


# ---------------- the loopback server ----------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep it quiet
        pass

    def do_POST(self):
        if self.headers.get("X-Filect-Token") != _token:
            self.send_response(403)
            self.end_headers()
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            op = req.pop("op", "")
            fn = _OPS.get(op)
            out = fn(**req) if fn else {"error": f"unknown op: {op}"}
        except Exception as e:
            out = {"error": f"bridge error: {e}"}
        body = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start(force: bool = False) -> bool:
    """Start the bridge on a daemon thread. By default only when the MCP is connected; force=True
    starts it regardless (used by the headless --mcp-bridge mode). Guarded — any failure is
    swallowed so the app is never affected. Returns True if it is (now) running."""
    global _server, _token, _port
    try:
        if _server is not None:
            return True
        if not force:
            try:
                from app.core import mcp_connect
                if not mcp_connect.is_connected():
                    return False
            except Exception:
                return False
        _token = secrets.token_hex(16)
        _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        _port = _server.server_address[1]
        _state_path().write_text(json.dumps({"token": _token, "port": _port}))
        threading.Thread(target=_server.serve_forever, daemon=True, name="filect-mcp-bridge").start()
        logger.info("Filect MCP bridge listening on 127.0.0.1:%s", _port)
        return True
    except Exception as e:
        logger.warning("MCP bridge did not start: %s", e)
        return False


# ---------------- the client (used by the separate MCP process) ----------------

def _do_call(op: str, timeout: float, **kwargs) -> dict:
    import urllib.request
    st = json.loads(_state_path().read_text())
    token, port = st["token"], st["port"]
    data = json.dumps({"op": op, **kwargs}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/", data=data,
        headers={"X-Filect-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _launch_background_bridge() -> bool:
    """Launch the Filect app HEADLESS + in the BACKGROUND (--mcp-bridge), so its bridge runs with
    the app's own folder grants — no window, no focus, ever. Shipped (frozen) app only; in dev the
    bridge is run separately. Returns True if a launch was attempted."""
    import os
    import sys
    import subprocess
    if not getattr(sys, "frozen", False):
        return False
    try:
        exe = str(Path(sys.executable).resolve())  # .app/Contents/MacOS/Filect (the inner binary)
        # Launch the inner binary DIRECTLY, not via `open`. `open --args` to a windowed .app drops
        # the args (PyInstaller munges argv) and reactivates the already-running GUI instead of
        # spawning a 2nd headless instance. A direct spawn inherits env, so FILECT_MODE=mcp-bridge
        # selects bridge mode — which never creates a Qt/NS app, so there is no window and no focus
        # change. stdout/stderr are discarded so this child can never corrupt the parent
        # --mcp-server's JSON-RPC stdio; start_new_session detaches it so it outlives the parent.
        subprocess.Popen(
            [exe],
            env={**os.environ, "FILECT_MODE": "mcp-bridge", "QT_QPA_PLATFORM": "offscreen"},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        pass
    return False


def call(op: str, timeout: float = 180.0, **kwargs) -> Optional[dict]:
    """MCP-side: POST an op to the running app's bridge. If the bridge isn't up, launch the app
    HEADLESS in the background (no window/focus) and retry. Returns the result dict, or None if the
    bridge still isn't reachable (the tool then falls back to a direct read)."""
    try:
        return _do_call(op, timeout, **kwargs)
    except Exception:
        pass
    # Bridge not reachable → start it headlessly (no focus) and retry for a few seconds.
    if _launch_background_bridge():
        import time
        for _ in range(10):
            time.sleep(0.75)
            try:
                return _do_call(op, timeout, **kwargs)
            except Exception:
                continue
    return None
