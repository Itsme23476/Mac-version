"""Connect / disconnect the Filect MCP server in Claude Desktop and Codex.

Powers the Settings cards: one click writes (or removes) the Filect server entry so the user
never edits config by hand. Claude Desktop is a JSON config we merge directly; Codex is driven
through its own `codex mcp add/remove/get` CLI (which writes ~/.codex/config.toml — the same file
the Codex CLI *and* the ChatGPT/Codex desktop app both read). Other servers are left untouched.
"""
import json
import logging
import shutil
import subprocess
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
        # FILECT_MODE (env) selects the mode; the frozen .app drops sys.argv, so the --mcp-server
        # flag alone is unreliable. The flag is kept as a readable hint / for source parity.
        return str(Path(sys.executable).resolve()), ["--mcp-server"], {
            "QT_QPA_PLATFORM": "offscreen", "FILECT_MODE": "mcp-server"}
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


# ---------------- Codex (CLI + ChatGPT desktop app share ~/.codex/config.toml) ----------------

def _codex_bin():
    """Path to a usable `codex` executable, or None. Checks PATH, the standalone CLI install,
    then the Codex/ChatGPT desktop app's bundled binary."""
    found = shutil.which("codex")
    if found:
        return found
    for p in (Path.home() / ".local" / "bin" / "codex",
              Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")):
        if p.exists():
            return str(p)
    return None


def codex_available() -> bool:
    """True if Codex is installed (so the 'Connect to Codex' card is worth showing)."""
    return _codex_bin() is not None


def _run_codex(args, timeout: float = 20.0):
    """Run `codex <args>` headlessly. Returns the CompletedProcess, or None if codex is missing
    or couldn't be launched. Never raises; never shows UI."""
    bin_ = _codex_bin()
    if not bin_:
        return None
    try:
        return subprocess.run([bin_, *args], capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        logger.warning(f"codex invocation failed: {e}")
        return None


def codex_is_connected() -> bool:
    r = _run_codex(["mcp", "get", SERVER_KEY])
    return bool(r and r.returncode == 0)


def codex_connect():
    """Register the Filect MCP server with Codex via its own CLI. (ok, message)."""
    if not codex_available():
        return False, "Codex isn't installed."
    cmd, args, env = mcp_command()
    add = ["mcp", "add", SERVER_KEY]
    for k, v in (env or {}).items():
        add += ["--env", f"{k}={v}"]
    add += ["--", cmd, *args]
    # `mcp add` errors if the name already exists; remove first (idempotent) so re-connect works.
    _run_codex(["mcp", "remove", SERVER_KEY])
    r = _run_codex(add)
    if r and r.returncode == 0:
        return True, "Connected. Restart the Codex / ChatGPT desktop app (or start a new Codex session)."
    err = ((r.stderr or r.stdout).strip() if r else "codex not runnable") or "unknown error"
    logger.error(f"Codex connect failed: {err}")
    return False, f"Could not add Filect to Codex: {err}"


def codex_disconnect():
    """Remove the Filect MCP server from Codex. (ok, message)."""
    if not codex_available():
        return False, "Codex isn't installed."
    r = _run_codex(["mcp", "remove", SERVER_KEY])
    if r and r.returncode == 0:
        return True, "Disconnected from Codex. Restart the Codex / ChatGPT desktop app to apply."
    err = ((r.stderr or r.stdout).strip() if r else "codex not runnable") or "unknown error"
    logger.error(f"Codex disconnect failed: {err}")
    return False, f"Could not remove Filect from Codex: {err}"


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
