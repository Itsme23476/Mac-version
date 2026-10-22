"""Filect MCP server — EXPERIMENTAL, read-only v1.

Exposes Filect's existing capabilities to an MCP client (Claude Desktop/Code) WITHOUT
touching the app. It imports `app.core.*` and calls the same functions the app uses; it
never edits app code and is not in the PyInstaller build.

v1 is strictly READ-ONLY: it never moves/deletes files, never writes app settings, never
re-indexes. It reads the local index DB and the saved Supabase session, and makes the same
paid AI calls the app makes for image analysis / transcription / suggestions.

Run:  QT_QPA_PLATFORM=offscreen /path/venv311/bin/python -m filect_mcp.server
(registered in Claude via claude config — see README.md)
"""
import os
import sys
from pathlib import Path

# Make Filect's own package importable (app lives in ../ai_file_organizer).
_APP_DIR = Path(__file__).resolve().parent.parent / "ai_file_organizer"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

# Headless: we import only app.core.* (never app.ui.*), but some core modules pull in Qt
# transitively — offscreen keeps that from needing a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mcp.server.mcpserver import MCPServer

server = MCPServer(
    "filect",
    instructions=(
        "Filect: local semantic file search + AI organization. These tools find indexed "
        "files, analyze an image, transcribe an audio file, and suggest dictation custom "
        "words. READ-ONLY: nothing here moves, deletes, or re-indexes files."
    ),
    version="0.1.0",
)

_auth_ok = False


def _ensure_auth() -> bool:
    """Restore the user's saved Supabase session (same tokens the app stores). Needed for the
    cloud AI calls; local search does not need it."""
    global _auth_ok
    if _auth_ok:
        return True
    try:
        from app.core.settings import settings
        from app.core.supabase_client import supabase_auth
        if supabase_auth.is_authenticated:
            _auth_ok = True
        elif settings.has_stored_session():
            r = supabase_auth.restore_session(settings.auth_access_token,
                                              settings.auth_refresh_token)
            _auth_ok = bool(r.get("success"))
    except Exception:
        _auth_ok = False
    return _auth_ok


def _coerce(v):
    """Make a value JSON-serializable: pass natives through, stringify Path/datetime/Decimal/etc."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_coerce(x) for x in v]
    if isinstance(v, dict):
        return {k: _coerce(x) for k, x in v.items()}
    return str(v)


def _jsonsafe(d):
    """Keep a result row small + serializable: drop vector/binary fields, coerce the rest."""
    if not isinstance(d, dict):
        return _coerce(d)
    out = {}
    for k, v in d.items():
        if k in ("embedding", "vector", "thumbnail", "thumb", "blob"):
            continue
        if isinstance(v, (bytes, bytearray)):
            continue
        out[k] = _coerce(v)
    return out


@server.tool(description="Search your indexed files with a natural-language query (e.g. "
                         "'tax PDFs from last year', 'screenshots of invoices'). Returns "
                         "matching files with their path, name, type and relevance. Read-only.")
def search_files(query: str, limit: int = 20) -> list:
    try:
        from app.core.search import search_service
        rows = search_service.search_files(query, limit=max(1, min(limit, 100))) or []
        return [_jsonsafe(r) for r in rows]
    except Exception as e:
        return [{"error": f"search failed: {e}"}]


@server.tool(description="Get the stored index details for one file by absolute path "
                         "(tags, caption, category, dates). Read-only.")
def get_file_details(path: str) -> dict:
    try:
        from app.core.search import search_service
        d = search_service.get_file_details(path)
        return _jsonsafe(d) if d else {"error": "not found in index"}
    except Exception as e:
        return {"error": f"lookup failed: {e}"}


@server.tool(description="Analyze an IMAGE file (png/jpg/etc.) with the vision model and "
                         "return a label, tags and caption. Makes a paid AI call. Does NOT "
                         "work on video. Read-only.")
def analyze_image(path: str) -> dict:
    try:
        _ensure_auth()
        from app.core.vision import analyze_image as _ai
        res = _ai(Path(path))
        return _jsonsafe(res) if res else {"error": "no result (unsupported file or provider off)"}
    except Exception as e:
        return {"error": f"analyze failed: {e}"}


@server.tool(description="Transcribe an AUDIO file to text via Filect's speech backend. "
                         "Makes a paid AI call. Read-only.")
def transcribe_file(path: str) -> dict:
    try:
        _ensure_auth()
        from app.core.transcription import transcribe_audio
        return transcribe_audio(path)
    except Exception as e:
        return {"error": f"transcribe failed: {e}"}


@server.tool(description="Suggest distinctive, correctly-spelled dictation custom words for a "
                         "field/niche the user describes (e.g. 'YouTube content creation'). "
                         "Returns suggestions only — does NOT change any settings. Makes a "
                         "paid AI call.")
def suggest_custom_words(niche: str) -> list:
    try:
        _ensure_auth()
        from app.core.vocab import suggest_terms_for_niche
        return suggest_terms_for_niche(niche)
    except Exception as e:
        return [f"error: {e}"]


if __name__ == "__main__":
    server.run("stdio")
