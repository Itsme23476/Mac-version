"""A Finder-launched app starts with a bare PATH; the agent must widen it before launching `claude`
(else an npm `claude` can't find node → exit 127). No network, no claude run.
Run: venv311/bin/python ai_file_organizer/tests/test_agent_path.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core import agent_runner as ar  # noqa: E402


def test_bare_path_gets_login_and_common_dirs():
    old = os.environ["PATH"]
    os.environ["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"           # what Finder gives an app
    ar._login_path_done = False
    try:
        ar.use_login_path()
        p = os.environ["PATH"].split(":")
        for d in ("/usr/bin", "/bin", os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin"):
            assert d in p, d
        assert len(p) == len(set(p)), "no duplicate entries"
        before = os.environ["PATH"]
        ar.use_login_path()                                         # once only
        assert os.environ["PATH"] == before
    finally:
        os.environ["PATH"] = old


if __name__ == "__main__":
    test_bare_path_gets_login_and_common_dirs()
    print("test_agent_path OK")
