"""Connect / disconnect the Filect MCP server in Claude Desktop's config.

Powers the "Connect to Claude" Settings card: one click writes (or removes) the Filect server
entry in Claude Desktop's config file, so the user never edits JSON. Does a SAFE merge — other
MCP servers in the config are left untouched.
"""
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SERVER_KEY = "filect"


def claude_config_path() -> Path:
    """Claude Desktop's config file on macOS."""
    return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def mcp_command():
    """(command, args, env) Claude Desktop should use to launch the Filect MCP server."""
    if getattr(sys, "frozen", False):
        # Shipped app: the Filect app binary IS the MCP server when launched with --mcp-server.
        # One signed binary, stable path; file reads are delegated to the running app's bridge,
        # so this launched instance needs no Full Disk Access of its own.
        return str(Path(sys.executable).resolve()), ["--mcp-server"], {"QT_QPA_PLATFORM": "offscreen"}
    # Running from source: the current Python interpreter + the server script.
    repo_root = Path(__file__).resolve().parents[3]
    server = repo_root / "filect_mcp" / "server.py"
    return sys.executable, [str(server)], {
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONPATH": str(repo_root),
    }


def _load_config() -> dict:
    p = claude_config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.warning(f"Claude config unreadable ({e}); treating as empty")
    return {}


def is_connected() -> bool:
    return SERVER_KEY in (_load_config().get("mcpServers") or {})


def connect():
    """Add the Filect MCP server to Claude Desktop's config (safe merge). (ok, message)."""
    try:
        p = claude_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        cfg = _load_config()
        cmd, args, env = mcp_command()
        entry = {"command": cmd}
        if args:
            entry["args"] = args
        if env:
            entry["env"] = env
        cfg.setdefault("mcpServers", {})[SERVER_KEY] = entry
        p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        logger.info("Filect MCP server added to Claude Desktop config")
        return True, "Connected. Quit and reopen Claude Desktop to finish."
    except Exception as e:
        logger.error(f"MCP connect failed: {e}")
        return False, f"Could not write the Claude config: {e}"


def disconnect():
    """Remove only the Filect server from Claude Desktop's config. (ok, message)."""
    try:
        p = claude_config_path()
        cfg = _load_config()
        servers = cfg.get("mcpServers") or {}
        if SERVER_KEY in servers:
            del servers[SERVER_KEY]
            p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return True, "Disconnected. Quit and reopen Claude Desktop to apply."
    except Exception as e:
        logger.error(f"MCP disconnect failed: {e}")
        return False, f"Could not update the Claude config: {e}"


if __name__ == "__main__":
    # offline self-check against a temp config (never touches the real one)
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "claude_desktop_config.json"
    globals()["claude_config_path"] = lambda: tmp
    assert is_connected() is False
    ok, _ = connect(); assert ok and is_connected()
    cfg = json.loads(tmp.read_text()); assert "filect" in cfg["mcpServers"]
    # a pre-existing server must survive
    cfg["mcpServers"]["other"] = {"command": "x"}; tmp.write_text(json.dumps(cfg))
    connect()  # re-connect
    cfg = json.loads(tmp.read_text()); assert "other" in cfg["mcpServers"] and "filect" in cfg["mcpServers"]
    ok, _ = disconnect(); assert ok and not is_connected()
    cfg = json.loads(tmp.read_text()); assert "other" in cfg["mcpServers"]  # only filect removed
    print("mcp_connect self-check passed")
