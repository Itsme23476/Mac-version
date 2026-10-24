"""Filect MCP server — EXPERIMENTAL.

Exposes Filect's capabilities to an MCP client (Claude Desktop/Code) WITHOUT touching the
app's code. It imports `app.core.*` and calls the same functions the app uses; it never
edits app code and is not in the PyInstaller build.

Read tools (search, file details, analyze) only read the local index and make the same paid
AI calls the app makes. Write tools organize files: apply_organization_plan MOVES files and
undo_last_organize reverses the most recent move. Nothing here DELETES a file or changes app
settings. Cloud AI calls need the saved Supabase session (sign into Filect once).

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
        "Filect: local semantic file search + AI content analysis + file organizing. Use "
        "analyze_file to understand the contents of ANY file — images, video (frames + spoken "
        "transcript), audio, PDFs, and documents (Word/PowerPoint/RTF/email/ebook/OpenDocument). "
        "search_files/get_file_details read the index; save_to_index stores your analysis so "
        "search can find the file. suggest_custom_words proposes dictation words and "
        "add_custom_words saves them. The organize tools MOVE files: generate_organization_plan "
        "previews, apply_organization_plan moves them, undo_last_organize reverses the most recent "
        "move; enable_auto_organize turns on background auto-sorting for a folder. Tools that change "
        "app settings (add_custom_words, enable_auto_organize) must be confirmed with the user "
        "first. Nothing deletes files."
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


_access_ok = False


def _require_access():
    """Gate EVERY tool on a valid Filect subscription — the SAME rule the app's paywall uses
    (get_entitlement): access is granted only while the plan is active or trialing, or a
    cancellation whose end date hasn't passed yet (a canceled sub stays 'active' until its
    period end). Returns an {'error': ...} dict to hand back to the client when NOT entitled,
    or None when access is granted. Cached per process once granted."""
    global _access_ok
    if _access_ok:
        return None
    _ensure_auth()  # restore the saved session first (loads tokens, sets provider)
    try:
        from app.core.supabase_client import supabase_auth
        ent = supabase_auth.get_entitlement() or {}
    except Exception as e:
        return {"error": f"Could not verify your Filect subscription ({e}). Open Filect, sign in, and try again."}
    if ent.get("entitled"):
        _access_ok = True
        return None
    reason = ent.get("reason") or ent.get("status") or "no active subscription"
    return {"error": "An active Filect subscription is required to use this. Your plan must be "
                     "active or trialing, or a cancellation that hasn't reached its end date yet "
                     f"(current status: {reason}). Open Filect to manage your subscription."}


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
        gate = _require_access()
        if gate:
            return [gate]
        from app.core.search import search_service
        rows = search_service.search_files(query, limit=max(1, min(limit, 100))) or []
        return [_jsonsafe(r) for r in rows]
    except Exception as e:
        return [{"error": f"search failed: {e}"}]


@server.tool(description="Get the stored index details for one file by absolute path "
                         "(tags, caption, category, dates). Read-only.")
def get_file_details(path: str) -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
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
        gate = _require_access()
        if gate:
            return gate
        from app.core import mcp_bridge
        res = mcp_bridge.call("analyze_image_backend", path=path)
        if res is None:
            res = mcp_bridge.analyze_image_backend(path)   # fallback: direct (non-protected only)
        return _jsonsafe(res) if res else {"error": "no result (unsupported file or provider off)"}
    except Exception as e:
        return {"error": f"analyze failed: {e}"}


@server.tool(description="Transcribe an AUDIO file to text via Filect's speech backend. "
                         "Makes a paid AI call. Read-only.")
def transcribe_file(path: str) -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
        from app.core import mcp_bridge
        r = mcp_bridge.call("read_content", path=path)
        if r is None:
            r = mcp_bridge.read_content(path)   # fallback: direct (non-protected only)
        if isinstance(r, dict) and r.get("mode") == "transcript":
            return {"ok": True, "text": r.get("transcript"), "duration": r.get("duration")}
        return r if isinstance(r, dict) else {"error": "transcription failed"}
    except Exception as e:
        return {"error": f"transcribe failed: {e}"}


@server.tool(description="Suggest distinctive, correctly-spelled dictation custom words for a "
                         "field/niche the user describes (e.g. 'YouTube content creation'). "
                         "Returns suggestions only — does NOT change any settings. Makes a "
                         "paid AI call.")
def suggest_custom_words(niche: str) -> list:
    try:
        gate = _require_access()
        if gate:
            return [gate]
        from app.core.vocab import suggest_terms_for_niche
        return suggest_terms_for_niche(niche)
    except Exception as e:
        return [f"error: {e}"]


@server.tool(description="Add dictation custom words to the user's Filect app (persists to "
                         "settings — case-insensitive dedup, capped at 100). Use after "
                         "suggest_custom_words once the user approves, or whenever the user asks to "
                         "add dictation vocabulary. Returns how many were actually added.")
def add_custom_words(words: list) -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
        from app.core.settings import settings
        added = settings.add_custom_terms(words or [])
        return {
            "added": added,
            "total_now": len(settings.dictation_custom_terms),
            "note": (f"Added {added} new custom word(s) to Filect's dictation vocabulary."
                     if added else "Nothing added (already present, empty, or at the 100 cap)."),
        }
    except Exception as e:
        return {"error": f"add failed: {e}"}


def _reconstruct_content(r: dict):
    """Turn the app bridge's content dict into the MCP tool response (images/video need Image blocks)."""
    mode = r.get("mode")
    if mode == "raw_text_for_client":
        return {"file": r.get("file"), "mode": mode, "text": r.get("text"),
                "note": "Text extracted — NO AI backend. You produce the label/tags/caption."}
    if mode == "image":
        import base64
        from mcp.server.mcpserver import Image as _McpImage
        note = f"image_for_client — '{r.get('file')}' below; NO AI backend. You look at it and tag it."
        return [note, _McpImage(data=base64.b64decode(r["image_b64"]), format=r.get("format", "png"))]
    if mode == "video_frames":
        import base64
        from mcp.server.mcpserver import Image as _McpImage
        fr = r.get("frames_b64", [])
        note = (f"video_frames_for_client — {len(fr)} keyframes of '{r.get('file')}'; NO AI backend. "
                "Look at them to tag. For spoken audio, call transcribe_file.")
        return [note] + [_McpImage(data=base64.b64decode(b), format="png") for b in fr]
    if mode == "transcript":
        return {"file": r.get("file"), "mode": "transcript_for_client",
                "transcript": r.get("transcript"), "duration_seconds": r.get("duration"),
                "note": "Transcript via Filect's speech backend. You produce the tags."}
    if mode == "analyzed":
        return {k: r.get(k) for k in ("file", "type", "tags", "caption", "transcript_or_text")}
    return r


@server.tool(description="Analyze ANY file and return what YOU (the client model) need to "
                         "understand and tag it, WITHOUT Filect's paid backend: documents & text "
                         "PDFs come back as extracted TEXT; images & video keyframes come back as "
                         "IMAGES for your own vision; audio comes back as a speech transcript (via "
                         "Filect's speech backend). You then produce the label/tags/caption "
                         "yourself. Unreadable/scanned files fall back to Filect's own analyzer. "
                         "This only READS — call save_to_index afterward with your tags to make the "
                         "file searchable; it never moves or deletes the file.")
def analyze_file(path: str):
    try:
        gate = _require_access()
        if gate:
            return gate
        # Delegate the file read to the running Filect app (it has folder access). If the app
        # isn't running, _br is None and we fall through to the direct read below (which works
        # for non-protected folders and gives the Full Disk Access message for protected ones).
        try:
            from app.core import mcp_bridge
            _br = mcp_bridge.call("read_content", path=path)
        except Exception:
            _br = None
        if isinstance(_br, dict) and _br.get("error"):
            return _br
        if isinstance(_br, dict):
            return _reconstruct_content(_br)
        from pathlib import Path
        from app.core.search import search_service
        p = Path(path)
        if not p.exists():
            return {"error": "file not found"}
        # macOS TCC blocks reads in protected folders (Downloads/Desktop/Documents) unless the
        # launching app has Full Disk Access — catch that here with a clear message instead of
        # silently returning an empty analysis.
        try:
            with open(p, "rb") as _f:
                _f.read(16)
        except Exception as e:
            return {"error": f"Can't read this file ({e}). On macOS, grant the app that launched "
                             "this MCP (e.g. Claude Desktop) Full Disk Access in System Settings "
                             "> Privacy & Security, then try again."}
        # Experimental Path B — "use the client's AI, not Filect's backend": for a TEXT
        # document, hand the locally-extracted text straight back so the MCP client (Claude /
        # Codex) does the understanding on ITS OWN usage. No paid backend call, nothing persisted.
        # Images / PDFs / audio / video aren't text-extractable, so they fall through to the
        # existing backend analyzer below (Path A — the user's subscription).
        from app.core.text_extract import extract_file_text, get_supported_text_formats
        # .pdf isn't in get_supported_text_formats() but extract_file_text DOES handle it
        # (PyPDF2); a text PDF returns text here, a scanned one returns nothing → backend below.
        if p.suffix.lower() in set(get_supported_text_formats()) or p.suffix.lower() == ".pdf":
            doc_text = (extract_file_text(p) or "").strip()
            if doc_text:
                return {
                    "file": p.name,
                    "mode": "raw_text_for_client",
                    "note": ("Text extracted locally — NO AI backend was used. You (the client "
                             "model) read this text and produce the label/tags/caption yourself."),
                    "text": doc_text[:200000],
                }
            # No extractable text (e.g. a scanned PDF) → fall through to the backend analyzer.

        suffix = p.suffix.lower()

        # IMAGE → hand a downscaled image to the CLIENT's vision (Claude/Codex). NO backend call.
        if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}:
            try:
                import io
                from PIL import Image as _PILImage
                from mcp.server.mcpserver import Image as _McpImage
                im = _PILImage.open(p)
                im.load()
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGB")
                im.thumbnail((1024, 1024))   # same 1024 cap the app uses; keeps client token cost low
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                note = (f"image_for_client — downscaled '{p.name}' below; NO AI backend was used. "
                        "You (the client model) look at it and produce the label/tags/caption.")
                return [note, _McpImage(data=buf.getvalue(), format="png")]
            except Exception:
                pass  # PIL can't open it (heic/raw/etc.) → fall through to the backend analyzer

        # VIDEO → hand the keyframes (local AVFoundation extraction, NO backend) to the client.
        try:
            from app.core.video_analysis import VIDEO_EXTENSIONS, AUDIO_EXTENSIONS, extract_keyframes
        except Exception:
            VIDEO_EXTENSIONS, AUDIO_EXTENSIONS, extract_keyframes = set(), set(), None
        if extract_keyframes and suffix in VIDEO_EXTENSIONS:
            frames = extract_keyframes(p, n=4) or []
            if frames:
                from mcp.server.mcpserver import Image as _McpImage
                out = [(f"video_frames_for_client — {len(frames)} keyframes of '{p.name}' below; "
                        "NO AI backend was used. You (the client model) look at them for the "
                        "label/tags/caption. For the spoken audio, call transcribe_file on this path.")]
                out += [_McpImage(data=f, format="png") for f in frames]
                return out
            # no frames (silent/unreadable) → fall through to the backend analyzer

        # AUDIO → the ONE backend call: speech-to-text via the existing transcribe tool, returned
        # for the client to tag (the client model can't do speech-to-text itself).
        if suffix in AUDIO_EXTENSIONS:
            from app.core.transcription import transcribe_audio
            tr = transcribe_audio(str(p)) or {}
            if tr.get("ok") and (tr.get("text") or "").strip():
                return {"file": p.name, "mode": "transcript_for_client",
                        "transcript": tr["text"], "duration_seconds": tr.get("duration"),
                        "note": "Transcript via Filect's speech backend. You (the client model) "
                                "produce the label/tags/caption from it."}
            # transcription failed/empty → fall through to the backend analyzer

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
        if not out["tags"] and not out["transcript_or_text"] and not out["caption"]:
            return {"error": "Analysis produced no content — the file may be unreadable (see Full "
                             "Disk Access above), empty, or an unsupported format."}
        return _jsonsafe(out)
    except Exception as e:
        return {"error": f"analyze failed: {e}"}


@server.tool(description="Save a file you just analyzed into Filect's index so it becomes "
                         "SEARCHABLE (search_files will find it). Call AFTER analyze_file, passing "
                         "the label/tags/caption YOU produced plus any extracted text/transcript. "
                         "Writes to the local index only — never moves or changes the file. Images "
                         "and videos count against the plan's media index limit, same as the app.")
def save_to_index(path: str, label: str = "", tags: list = None, caption: str = "", text: str = "") -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
        import mimetypes
        from pathlib import Path
        from app.core.database import file_index
        from app.core.search import is_media_file
        p = Path(path)
        if not p.exists():
            return {"error": "file not found"}
        # Media (images/videos) count against the plan's monthly index limit, same as the app.
        if is_media_file(p):
            from app.core.supabase_client import supabase_auth
            chk = supabase_auth.can_index_media(1) or {}
            if not chk.get("allowed"):
                return {"error": chk.get("reason") or "media index limit reached for your plan",
                        "remaining": chk.get("remaining"), "limit": chk.get("limit")}
        file_data = {
            "source_path": str(p),
            "name": p.name,
            "extension": p.suffix.lower(),
            "size": p.stat().st_size,
            "mime_type": mimetypes.guess_type(str(p))[0] or "",
            "category": "Misc",
            "label": (label or "").strip(),
            "tags": tags or [],
            "caption": (caption or "").strip(),
            "ocr_text": (text or "").strip(),
            "has_ocr": bool((text or "").strip()),
            "ai_source": "mcp:client-model",
            "metadata": {"is_file": True, "is_dir": False},
        }
        ok = file_index.add_file(file_data)
        if ok and is_media_file(p):
            try:
                from app.core.supabase_client import supabase_auth
                supabase_auth.increment_index_usage(1)
            except Exception:
                pass
        return {"saved": bool(ok), "file": p.name,
                "note": "Indexed — search_files can find it now." if ok else "could not save to index"}
    except Exception as e:
        return {"error": f"save failed: {e}"}


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
        gate = _require_access()
        if gate:
            return gate
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
        gate = _require_access()
        if gate:
            return gate
        import json as _json
        from app.core import mcp_bridge
        pf = _mcp_plans_dir() / f"plan-{plan_id}.json"
        if not pf.exists():
            return {"error": "Unknown plan_id — generate a plan first."}
        moves = _json.loads(pf.read_text(encoding="utf-8"))
        # Delegate the actual file moves to the running app (it has folder access).
        res = mcp_bridge.call("apply_moves", moves=moves)
        if res is None:
            res = mcp_bridge.apply_moves(moves)   # fallback: direct (non-protected only)
        if isinstance(res, dict):
            res["note"] = "Call undo_last_organize() to reverse this."
        return _jsonsafe(res)
    except Exception as e:
        return {"error": f"apply failed: {e}"}


@server.tool(description="Reverse the MOST RECENT organize operation — moves every file back to "
                         "where it was. Never deletes anything. Use right after an "
                         "apply_organization_plan you want to take back.")
def undo_last_organize() -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
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


@server.tool(description="Turn ON auto-organize for a folder (ongoing background sorting). There are "
                         "THREE modes and you MUST ask the user which one they want before calling — "
                         "do NOT pick a default: mode=1 'Re-organize All' (sorts ALL existing files "
                         "AND new ones), mode=2 'Organize As-Is' (sorts files only into the folder's "
                         "EXISTING subfolders), mode=3 'New files only' (leaves existing files alone, "
                         "only sorts files added from now on). Required flow: (1) ASK the user which "
                         "mode; (2) preview with generate_organization_plan and show it; (3) ask "
                         "'confirm? this will auto-organize your files based on this'; (4) only on an "
                         "explicit YES, call this. Takes effect when the Filect app next (re)starts; "
                         "removable from the app's Auto-Organize tab.")
def enable_auto_organize(folder: str, mode: int, instruction: str = "") -> dict:
    try:
        gate = _require_access()
        if gate:
            return gate
        from pathlib import Path
        from app.core.settings import settings
        if mode not in (1, 2, 3):
            return {"error": "mode must be 1 (re-organize all), 2 (organize as-is), or 3 (new files only)"}
        p = Path(folder)
        if not p.exists() or not p.is_dir():
            return {"error": "folder not found"}
        settings.add_auto_organize_folder(str(p), instruction or "")
        settings.update_auto_organize_action(str(p), mode)
        mode_name = {1: "re-organize all files", 2: "organize into existing subfolders only",
                     3: "new files only (existing untouched)"}[mode]
        return {
            "enabled": True,
            "folder": str(p),
            "mode": mode_name,
            "note": "Registered. Filect starts watching this folder when the app next launches. "
                    "Remove it anytime from the app's Auto-Organize tab.",
        }
    except Exception as e:
        return {"error": f"setup failed: {e}"}


if __name__ == "__main__":
    server.run("stdio")
