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
        "Filect: local semantic file search + AI content analysis. Use analyze_file to "
        "understand the contents of ANY file — images, video (frames + spoken transcript), "
        "audio, PDFs, and documents (Word/PowerPoint/RTF/email/ebook/OpenDocument). Other "
        "tools search the index and suggest dictation words. READ-ONLY: nothing here moves, "
        "deletes, or re-indexes files."
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
        settings.ai_provider = "openai"   # the MCP always analyzes via the cloud backend
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


@server.tool(description="Analyze ANY supported file and return its content: works on images, "
                         "VIDEO (keyframes + spoken transcript), AUDIO, PDFs, and documents "
                         "(.docx/.pptx/.rtf/.eml/.epub/.odt). Returns the type, content tags, a "
                         "caption, and any transcript/extracted text. READ-ONLY: does NOT add the "
                         "file to the index or move it. Makes paid AI calls.")
def analyze_file(path: str) -> dict:
    try:
        _ensure_auth()
        from pathlib import Path
        from app.core.search import search_service
        p = Path(path)
        if not p.exists():
            return {"error": "file not found"}
        # Same per-file router the indexer uses (image/video/audio/pdf/doc), but we don't persist —
        # _process_single_file returns metadata; only index_single_file would write to the index.
        meta = search_service._process_single_file(
            {"source_path": str(p), "name": p.name}, p.parent, force_ai=True)
        if not meta or meta.get("_error"):
            return {"error": (meta.get("_error") if meta else "could not analyze this file")}
        out = {
            "file": p.name,
            "type": meta.get("label"),
            "tags": meta.get("tags") or [],
            "caption": meta.get("caption"),
            "ai_source": meta.get("ai_source"),
            "transcript_or_text": meta.get("ocr_text") or None,
            "duration_seconds": meta.get("duration"),
        }
        return _jsonsafe(out)
    except Exception as e:
        return {"error": f"analyze failed: {e}"}


def _mcp_plans_dir():
    from app.core.settings import settings
    d = settings.get_app_data_dir() / "mcp_plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _files_under(folder: str):
    """Indexed files whose path is under `folder`, as dicts the plan functions expect. Matches
    both the given path and its resolved form (handles symlinks like /tmp -> /private/tmp)."""
    import sqlite3
    import json as _json
    from pathlib import Path
    from app.core.database import file_index
    cands = {str(Path(folder)).rstrip("/"), str(Path(folder).resolve()).rstrip("/")}
    conn = sqlite3.connect(file_index.db_path)
    conn.row_factory = sqlite3.Row
    seen, out = set(), []
    for c in cands:
        for r in conn.execute("SELECT * FROM files WHERE file_path LIKE ?", (c + "/%",)).fetchall():
            d = dict(r)
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            try:
                d["tags"] = _json.loads(d.get("tags") or "[]")
            except Exception:
                d["tags"] = []
            try:
                d["metadata"] = _json.loads(d.get("metadata") or "{}")
            except Exception:
                d["metadata"] = {}
            out.append(d)
    return out


@server.tool(description="Preview how Filect would organize a folder. READ-ONLY — moves nothing. "
                         "Returns a plan_id and the proposed folders (which files go where). The "
                         "folder must already be indexed in Filect. Review the plan, then call "
                         "apply_organization_plan(plan_id) to actually move the files.")
def generate_organization_plan(folder: str, instructions: str = "") -> dict:
    try:
        _ensure_auth()
        import json as _json
        import uuid
        from pathlib import Path
        from app.core.ai_organizer import request_organization_plan, plan_to_moves
        files = _files_under(folder)
        if not files:
            return {"error": "No indexed files under this folder — index it in Filect first."}
        files_by_id = {f["id"]: f for f in files}
        plan = request_organization_plan(
            instructions or "Organize these files into sensible folders by content and type.", files)
        if not plan or not plan.get("folders"):
            return {"error": "Could not generate a plan."}
        moves = plan_to_moves(plan, files_by_id, Path(folder))
        if not moves:
            return {"error": "The plan produced no moves."}
        plan_id = uuid.uuid4().hex[:12]
        (_mcp_plans_dir() / f"plan-{plan_id}.json").write_text(_json.dumps(moves), encoding="utf-8")
        summary = {}
        for m in moves:
            folder_name = Path(m["destination_path"]).parent.name
            summary.setdefault(folder_name, []).append(Path(m["source_path"]).name)
        return _jsonsafe({
            "plan_id": plan_id,
            "total_files": len(moves),
            "proposed_folders": {k: v[:20] for k, v in summary.items()},
            "note": "Nothing moved yet. Call apply_organization_plan(plan_id) to apply.",
        })
    except Exception as e:
        return {"error": f"plan failed: {e}"}


@server.tool(description="Apply a plan from generate_organization_plan: MOVES the files into the "
                         "proposed folders. Reversible with undo_last_organize. Never deletes "
                         "anything. Needs the plan_id from a prior generate call.")
def apply_organization_plan(plan_id: str) -> dict:
    try:
        import json as _json
        from app.core.apply import apply_moves
        pf = _mcp_plans_dir() / f"plan-{plan_id}.json"
        if not pf.exists():
            return {"error": "Unknown plan_id — generate a plan first."}
        moves = _json.loads(pf.read_text(encoding="utf-8"))
        success, errors, log_file, renamed = apply_moves(moves)
        return _jsonsafe({
            "success": success,
            "moved": len(moves) - len(errors),
            "errors": errors[:10],
            "renamed_to_avoid_collision": renamed,
            "log_file": log_file,
            "note": "Call undo_last_organize() to reverse this.",
        })
    except Exception as e:
        return {"error": f"apply failed: {e}"}


@server.tool(description="Reverse the MOST RECENT organize operation — moves every file back to "
                         "where it was. Never deletes anything. Use right after an "
                         "apply_organization_plan you want to take back.")
def undo_last_organize() -> dict:
    try:
        import json as _json
        import shutil
        from pathlib import Path
        from app.core.settings import settings
        logs = sorted(settings.get_moves_dir().glob("moves-*.json"))
        if not logs:
            return {"error": "No organize operation to undo."}
        latest = logs[-1]
        moves = _json.loads(latest.read_text(encoding="utf-8")).get("moves", [])
        reverted, skipped = 0, []
        for m in reversed(moves):
            src = Path(m.get("to", ""))      # file is currently at the destination
            dst = Path(m.get("from", ""))    # move it back to the original
            if not src.exists():
                skipped.append(f"missing: {src.name}")
                continue
            if dst.exists():
                skipped.append(f"original occupied: {dst.name}")
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                reverted += 1
            except Exception as e:
                skipped.append(f"{src.name}: {e}")
        return {"reverted": reverted, "skipped": skipped[:10], "from_log": latest.name}
    except Exception as e:
        return {"error": f"undo failed: {e}"}


if __name__ == "__main__":
    server.run("stdio")
