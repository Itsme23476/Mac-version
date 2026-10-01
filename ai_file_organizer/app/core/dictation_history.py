"""Local JSON store of recent dictation transcripts.

Transcripts are personal, so they live on disk only and are never uploaded.
All file IO is swallowed: history must never break dictation.
"""
import json
import time

from app.core.settings import settings

_MAX = 100
_MAX_AGE = 30 * 86400  # entries older than 30 days are auto-purged


def _path():
    return settings.get_app_data_dir() / "dictation_history.json"


def _read() -> list[dict]:
    """Raw load of the stored list; [] on any error."""
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write(items: list[dict]) -> None:
    """Persist the list; never raises."""
    try:
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(items, f)
    except Exception:
        pass


def get_all() -> list[dict]:
    """Return entries newest-first, pruned of >30-day-old rows and capped at 100.

    Persists the pruned list whenever anything was dropped. Never raises.
    """
    items = _read()
    cutoff = time.time() - _MAX_AGE
    kept = [e for e in items if isinstance(e, dict) and e.get("ts", 0) > cutoff][:_MAX]
    if kept != items:
        _write(kept)
    return kept


def add(text: str) -> None:
    """Prepend {"text", "ts"} newest-first, pruned + capped at 100. Never raises."""
    try:
        text = (text or "").strip()
        if not text:
            return
        items = get_all()  # already pruned + capped
        items.insert(0, {"text": text, "ts": int(time.time())})
        del items[_MAX:]
        _write(items)
    except Exception:
        pass


def delete(index: int) -> None:
    """Remove the entry at `index` in newest-first (get_all) order, then persist.

    Out-of-range index is a no-op. Never raises.
    """
    try:
        items = get_all()
        if 0 <= index < len(items):
            del items[index]
            _write(items)
    except Exception:
        pass


def clear() -> None:
    """Truncate history to an empty list. Never raises."""
    _write([])


if __name__ == "__main__":
    clear()
    add("first entry")
    add("second entry")
    items = get_all()
    assert len(items) == 2 and items[0]["text"] == "second entry", items
    assert isinstance(items[0]["ts"], int), items[0]

    delete(0)
    items = get_all()
    assert len(items) == 1 and items[0]["text"] == "first entry", items

    delete(5)  # out of range → no-op
    assert len(get_all()) == 1, get_all()

    # purge: inject an entry older than 30 days, get_all must drop it
    stale = get_all()
    stale.insert(0, {"text": "ancient", "ts": int(time.time()) - 31 * 86400})
    _write(stale)
    assert all(e["text"] != "ancient" for e in get_all()), get_all()

    clear()
    assert get_all() == [], get_all()
    print("dictation_history self-check passed ✓")
