"""Voice intent router — classifies a spoken command as NEW / FOLLOW_UP / RESUME, on the user's
Claude subscription (the agent SDK uses the Claude Code login, not an API key).

Runs a WARM, long-lived ClaudeSDKClient on its own asyncio thread so each classification is a message,
not a cold `claude` subprocess spawn — measured ~2.5s warm vs ~4s cold (the spawn, not the model, is
the cost, so the model choice is latency-neutral; we default to Opus). The client is recycled every
~20 calls to bound conversation growth, and the system prompt tells it to classify EACH message
independently, so reusing one conversation doesn't bleed context (verified). ANY failure raises so the
caller can fall back safely (NEW / continue-the-focused).

Decided with the user: Opus (configurable via settings.assistant_router_model). Not nano (their
backend cost + silent auth-fail), not a word-gate (can't catch a topic shift that never says "new").
"""
from __future__ import annotations
import asyncio
import json
import logging
import threading

logger = logging.getLogger("ai_file_organizer")

_RECYCLE_EVERY = 20          # ponytail: recycle the warm client every N calls to bound context growth

_SYS = (
    "You route a short spoken command for a local coding assistant that can run SEVERAL conversations "
    "at once. Pick ONE intent: NEW (a brand-new task), FOLLOW_UP (continue the CURRENT conversation), "
    "or RESUME (switch back to a specific earlier conversation the user names). Default to FOLLOW_UP "
    "when there is a current conversation and the command reads like more work on it or a tweak. Pick "
    "NEW when it's clearly a different task, or they say 'new task', 'start over', 'separately'. Pick "
    "RESUME only when they explicitly point to a specific earlier conversation by name/topic, and set "
    "index to its number. Treat EACH message independently — classify only the command in THIS message, "
    "ignore earlier ones. "
    'Respond with ONLY compact JSON: {"intent":"NEW|FOLLOW_UP|RESUME","index":<number or null>,'
    '"task":"<the instruction to carry out, with any routing phrasing removed>"}.'
)


def _build_prompt(text: str, focused_title: str | None, candidate_titles: list[str]) -> str:
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(candidate_titles)) or "(none)"
    return (f"Current conversation: {focused_title or '(none)'}\n"
            f"Other conversations:\n{listing}\n\nSpoken command: {text!r}")


def _parse(raw: str, fallback_task: str) -> dict:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    data = json.loads(raw)
    intent = str(data.get("intent", "NEW")).upper()
    if intent not in ("NEW", "FOLLOW_UP", "RESUME"):
        intent = "NEW"
    idx = data.get("index")
    idx = idx if isinstance(idx, int) else None
    task = (data.get("task") or fallback_task).strip() or fallback_task
    return {"intent": intent, "index": idx, "task": task}


class IntentRouter:
    """Singleton warm classifier. `classify()` is synchronous (call it off the UI thread)."""
    _instance: "IntentRouter | None" = None
    _init_lock = threading.Lock()

    @classmethod
    def get(cls) -> "IntentRouter":
        with cls._init_lock:
            if cls._instance is None or cls._instance._failed:   # e.g. Claude Code installed since
                model = "opus"
                try:
                    from app.core.settings import settings as _s
                    model = getattr(_s, "assistant_router_model", "") or "opus"
                except Exception:
                    pass
                cls._instance = cls(model)
            return cls._instance

    def __init__(self, model: str):
        self._model = model
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._calls = 0
        self._failed = False                   # the warm client couldn't start (no Claude Code)
        self._ready = threading.Event()
        self._call_lock = threading.Lock()     # one warm conversation → serialize classifications
        self._thread = threading.Thread(target=self._run, daemon=True, name="filect-intent-router")
        self._thread.start()

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._open())
            self._ready.set()
            self._loop.run_forever()
        except Exception as e:                 # a dead loop just means callers fall back (Codex / NEW)
            logger.warning("[intent-router] loop failed to start: %s", e)
            self._failed = True
            self._ready.set()                  # don't make classify() wait 8s to find out

    async def _open(self):
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
        from app.core.agent_runner import use_login_path
        use_login_path()
        opts = ClaudeAgentOptions(model=self._model, setting_sources=[], max_turns=1,
                                  system_prompt=_SYS, allowed_tools=[])
        self._client = ClaudeSDKClient(options=opts)
        await self._client.__aenter__()
        logger.info("[intent-router] warm client ready (model=%s)", self._model)

    async def _reopen(self):
        try:
            await self._client.__aexit__(None, None, None)
        except Exception:
            pass
        await self._open()

    async def _classify_async(self, prompt: str) -> str:
        from claude_agent_sdk import AssistantMessage, TextBlock
        self._calls += 1
        if self._calls % _RECYCLE_EVERY == 0:          # bound conversation growth
            await self._reopen()
        await self._client.query(prompt)
        out = []
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        out.append(b.text)
        return "".join(out)

    def classify(self, text: str, focused_title: str | None,
                 candidate_titles: list[str], timeout: float = 12.0) -> dict:
        """Return {"intent": NEW|FOLLOW_UP|RESUME, "index": int|None, "task": str}.
        Raises on not-ready / timeout / parse failure — the caller falls back safely."""
        import time
        if not self._ready.wait(8.0) or self._loop is None or self._failed:
            raise RuntimeError("intent router not ready")
        prompt = _build_prompt(text, focused_title, candidate_titles)
        t0 = time.time()
        with self._call_lock:                          # serialize (single warm conversation)
            fut = asyncio.run_coroutine_threadsafe(self._classify_async(prompt), self._loop)
            raw = fut.result(timeout=timeout)
        d = _parse(raw, text)
        logger.info("[intent-router] %.2fs | heard=%r focused=%r cands=%s -> %s idx=%s task=%r",
                    time.time() - t0, text, focused_title, candidate_titles,
                    d["intent"], d["index"], d["task"])
        return d


def classify_with_codex(text: str, focused_title: str | None, candidate_titles: list[str]) -> dict:
    """The same decision made by Codex — when Claude Code isn't available. A cold `codex exec` per call
    (slower than the warm Claude client, several seconds). Raises on any failure, like classify()."""
    import time
    from app.core.codex_runner import complete
    t0 = time.time()
    d = _parse(complete(_SYS, _build_prompt(text, focused_title, candidate_titles)), text)
    logger.info("[intent-router] codex %.2fs | heard=%r -> %s idx=%s task=%r",
                time.time() - t0, text, d["intent"], d["index"], d["task"])
    return d


def warm_up():
    """Start the warm client early (e.g. at app launch) so the first voice command isn't cold."""
    try:
        IntentRouter.get()
    except Exception:
        pass
