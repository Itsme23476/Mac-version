"""Rich Details feed: every action shows its substance, like the real Claude Code conversation.
(1) agent_runner._steps_from_message maps REAL SDK messages → text/tool/result steps (tool results were
    previously dropped, so a failed write still read "Writing…");
(2) details_view renders each action: file preview + size + Show full + Open (Open in browser for HTML,
    "moved or deleted" when gone), ✗ + reason on failure, edit diffs, commands with ✓/✗ + last output
    lines, web search queries, plan checklists, long replies previewed;
(3) deliverable / summary for the done card;
(4) the overlay: entries drive Details, Show full toggles via the real link handler, Open calls the OS,
    the done card carries the deliverable link;
(5) the live flow end-to-end through AssistantPage._on_step → notch.
Run: QT_QPA_PLATFORM=offscreen venv311/bin/python ai_file_organizer/tests/test_details_view.py
"""
import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QUrl, QPoint
app = QApplication.instance() or QApplication([])

from claude_agent_sdk import AssistantMessage, UserMessage, TextBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock
from app.core.agent_runner import _steps_from_message, Step
from app.ui import details_view as dv
from app.ui import claude_overlay as co
from app.ui import assistant_page as ap
ap._play_chime = lambda *a, **k: None
ap._notify = lambda *a, **k: None
ap.AssistantPage._show_idle_overlays = lambda self: None
ap.AssistantPage._load_recent = lambda self: None

TMP = tempfile.mkdtemp()
ESSAY = "\n".join([f"Paragraph line {i} about habits and how they compound." for i in range(1, 21)])


def _f(name, text):
    p = os.path.join(TMP, name)
    with open(p, "w") as fh:
        fh.write(text)
    return p


def test_steps_from_real_sdk_messages():
    msg = AssistantMessage(content=[ThinkingBlock("plan", "sig"), TextBlock("On it."),
                                    ToolUseBlock(id="t1", name="Write",
                                                 input={"file_path": "/x/essay.md", "content": "hi there"})],
                           model="claude")
    st = _steps_from_message(msg)
    assert [s.kind for s in st] == ["text", "tool"], st                    # thinking skipped
    assert st[1].tool_id == "t1" and st[1].input["content"] == "hi there" and st[1].detail == "essay.md"
    res = _steps_from_message(UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="File created",
                                                                   is_error=False)]))
    assert len(res) == 1 and res[0].kind == "result" and res[0].tool_id == "t1" and res[0].text == "File created"
    err = _steps_from_message(UserMessage(content=[ToolResultBlock(
        tool_use_id="t2", content=[{"type": "text", "text": "Permission denied"}], is_error=True)]))
    assert err[0].is_error and err[0].text == "Permission denied"
    assert _steps_from_message(UserMessage(content="echoed prompt")) == []  # not a tool result
    print("runner: real SDK messages → text / tool (full input) / result (✓/✗ + output)  OK")


def _entries(*steps):
    e = []
    for s in steps:
        dv.add_step(e, **s)
    return e


def test_render_actions():
    essay = _f("essay.md", ESSAY)
    site = _f("index.html", "<html>\n<title>Filect</title>\n</html>")
    e = _entries(dict(kind="user", text="write me an essay"),
                 dict(kind="tool", tool="Write", input={"file_path": essay, "content": ESSAY}, tool_id="w1"),
                 dict(kind="result", tool_id="w1", text="ok"),
                 dict(kind="tool", tool="Write", input={"file_path": site, "content": "<html>\n<title>Filect</title>\n</html>"}, tool_id="w2"),
                 dict(kind="result", tool_id="w2", text="ok"),
                 dict(kind="tool", tool="Write", input={"file_path": "/gone/old.md", "content": "x"}, tool_id="w3"),
                 dict(kind="result", tool_id="w3", text="ok"),
                 dict(kind="tool", tool="Write", input={"file_path": "/x/README.md", "content": "y"}, tool_id="w4"),
                 dict(kind="result", tool_id="w4", text="<tool_use_error>Tool permission request failed: Stream closed</tool_use_error>", is_error=True),
                 dict(kind="tool", tool="Edit", input={"file_path": "/x/style.css", "old_string": "a{}\nb{}", "new_string": "a{color:blue}"}, tool_id="e1"),
                 dict(kind="result", tool_id="e1", text="ok"),
                 dict(kind="tool", tool="Bash", input={"command": "npm test", "description": "Run the tests"}, tool_id="b1"),
                 dict(kind="result", tool_id="b1", text="out-1\nout-2\nout-3\nout-4\nFAIL: 2 tests", is_error=True),
                 dict(kind="tool", tool="WebSearch", input={"query": "best habit books"}, tool_id="s1"),
                 dict(kind="tool", tool="TodoWrite", input={"todos": [{"content": "Outline", "status": "completed"},
                                                                       {"content": "Draft", "status": "in_progress"},
                                                                       {"content": "Polish", "status": "pending"}]}, tool_id="p1"),
                 dict(kind="text", text="Here is a long reply. " * 60))
    h = dv.render_html(e)
    doc = dv.QTextDocument(); doc.setHtml(h); text = doc.toPlainText()
    # essay.md: preview + size + Show full + Open (a real file link)
    assert "Wrote essay.md · 180 words" in text and "Paragraph line 1 " in text and "Paragraph line 15" not in text
    assert f'href="{dv.file_url(essay)}"' in h and "Open ↗" in text and "Show full ⌄" in text
    # index.html: Open in browser
    assert "Wrote index.html · 3 lines" in text and "Open in browser ↗" in text
    # file since moved/deleted: no Open link, says so
    assert "old.md" in text and "moved or deleted" in text
    # a FAILED write says so, with the reason (tags stripped) — not "Writing README.md"
    assert "Couldn't write README.md" in text and "Tool permission request failed: Stream closed" in text
    assert "tool_use_error" not in text
    # edit: +/- counts and the diff
    assert "Edited style.css · +1 −2" in text and "− b{}" in text and "+ a{color:blue}" in text
    # command: description, ✗, the command, only the LAST 3 output lines, Show full
    assert "✗" in text and "Run the tests" in text and "$ npm test" in text
    assert "FAIL: 2 tests" in text and "out-3" in text and "out-4" in text, text
    assert "out-1" not in text and "out-2" not in text, "only the LAST 3 output lines show"
    # web search query + plan checklist
    assert "Searched the web for “best habit books”" in text
    assert "☑ Outline" in text and "◐ Draft" in text and "☐ Polish" in text
    # long reply previewed
    assert text.count("Show full ⌄") >= 3
    # expanding the essay shows all of it
    full = dv.QTextDocument(); full.setHtml(dv.render_html(e, {e[1]["id"]}))
    assert "Paragraph line 20" in full.toPlainText() and "Show less ⌃" in full.toPlainText()
    print("render: previews, sizes, Show full, Open / Open in browser / moved, ✗ reasons, diff, command, search, plan  OK")


def test_deliverable():
    p = _f("plan.md", "a b c")
    e = _entries(dict(kind="user", text="first"),
                 dict(kind="tool", tool="Write", input={"file_path": p, "content": "a b c"}, tool_id="w1"),
                 dict(kind="result", tool_id="w1", text="ok"),
                 dict(kind="user", text="now fix it"),
                 dict(kind="tool", tool="Write", input={"file_path": "/x/bad.md", "content": "z"}, tool_id="w2"),
                 dict(kind="result", tool_id="w2", text="denied", is_error=True),
                 dict(kind="text", text="I **couldn't** save it.\n\nSecond paragraph."))
    assert dv.deliverable(e) is None, "only the LATEST turn's successful write counts"
    e2 = _entries(dict(kind="user", text="go"),
                  dict(kind="tool", tool="Write", input={"file_path": p, "content": "a b c"}, tool_id="w1"),
                  dict(kind="result", tool_id="w1", text="ok"))
    assert dv.deliverable(e2) == (p, "3 words")
    assert dv.final_reply(e) == "I **couldn't** save it.\n\nSecond paragraph."
    print("done card: deliverable = latest turn's successful write; final reply = Claude's last message  OK")


def test_overlay_links_and_done_card():
    essay = _f("essay2.md", ESSAY)
    opened = []
    co.QDesktopServices.openUrl = staticmethod(lambda url: opened.append(url.toLocalFile()))
    ov = co.ClaudeOverlay()
    e = _entries(dict(kind="user", text="write me an essay"),
                 dict(kind="tool", tool="Write", input={"file_path": essay, "content": ESSAY}, tool_id="w1"),
                 dict(kind="result", tool_id="w1", text="ok"),
                 dict(kind="text", text="I wrote a 200-word essay on habits to essay2.md."))
    ov.set_details(e)
    ov._append_detail("Starting…")                                       # status lines stay out of rich Details
    assert "Starting" not in ov._details_feed.toPlainText()
    assert "Paragraph line 15" not in ov._details_feed.toPlainText()
    ov._on_details_link(QUrl(f"full:{e[1]['id']}"))                      # the real link handler
    assert "Paragraph line 15" in ov._details_feed.toPlainText(), "Show full must expand in place"
    ov._on_details_link(QUrl(f"full:{e[1]['id']}"))
    assert "Paragraph line 15" not in ov._details_feed.toPlainText(), "Show less must collapse"
    ov._on_details_link(QUrl.fromLocalFile(essay))
    assert opened == [essay], opened
    ov.show_done("abcd1234", 0, "write me an essay", "")
    text, markup = ov._done_view.toPlainText(), ov._done_view.toHtml()
    assert "I wrote a 200-word essay" in text and "essay2.md" in text and "180 words" in text
    assert f'href="{dv.file_url(essay)}"' in markup and "Open ↗" in text
    print("overlay: entries drive Details, Show full/less + Open via the real link handler, done card links the file  OK")


class _MW:
    pass


def test_live_flow_through_page():
    essay = _f("essay3.md", ESSAY)
    page = ap.AssistantPage(_MW())
    sess = ap.AgentSession("s1", TMP, "write me an essay", "Essay", True, "haiku", "high")
    page._sessions = {"s1": sess}
    page._focused_key = "s1"
    sess.entries = []
    dv.add_step(sess.entries, "user", text=sess.prompt)
    page._on_step("s1", Step("tool", tool="Write", detail="essay3.md",
                             input={"file_path": essay, "content": ESSAY}, tool_id="w1"))
    ov = page._overlay
    assert "Writing essay3.md" in ov._details_feed.toPlainText(), "pending write shows while running"
    page._on_step("s1", Step("result", tool_id="w1", text="File created"))
    page._on_step("s1", Step("text", text="Done — a 200-word essay is in essay3.md."))
    d = ov._details_feed.toPlainText()
    assert "Wrote essay3.md · 180 words" in d and "Done — a 200-word essay" in d, d
    page._on_result("s1", "sid-123", 0.0)
    assert "essay3.md" in ov._done_view.toPlainText() and "Open ↗" in ov._done_view.toPlainText()
    print("live: _on_step tool → result → text builds the rich Details; done card offers Open  OK")


# The real answer from 2026-11-05 ("what files are in my Downloads?"): its first paragraph was only
# the lead-in, so the old one-paragraph summary showed nothing useful.
DOWNLOADS_REPLY = """These are the most recently modified items in your Downloads folder:

**Images from Nov 4**
- `free unlimited 2 softwaregent.png`
- `Opus 5.5 free forever b softwaregent.png`

**Folder from Nov 3**
- `crtypto_trading/` (35 items)

Most of the recent files are PNGs around 2–3 MB each. The whole folder uses about 86 GB."""


def test_done_card_shows_whole_answer_and_stays_open():
    """A voice task's card stays open while it runs and after it finishes, until YOU close it; the
    finished card shows Claude's whole answer (formatted, scrolls when long), not its first line."""
    real = co.QCursor
    co.QCursor = type("C", (), {"pos": staticmethod(lambda: QPoint(5, 900))})   # mouse far away
    try:
        page = ap.AssistantPage(_MW())
        sess = ap.AgentSession("s1", TMP, "what files are in my downloads", "Downloads", True, "opus", "high")
        page._sessions = {"s1": sess}
        page._focused_key = page._last_sent_key = "s1"         # as _start_worker marks a sent task
        ov = page._ensure_notch()
        page._notch("present"); page._notch("flash")             # what a voice task does
        ov._end_flash(); ov._maybe_collapse()                     # the old auto-tuck, mouse away
        assert not ov._minimized, "a running task's card must stay open until you close it"
        sess.entries = []
        dv.add_step(sess.entries, "user", text=sess.prompt)
        page._on_step("s1", Step("text", text=DOWNLOADS_REPLY))
        ov.minimize_panel()                                       # you tucked it away mid-run…
        page._on_result("s1", "sid-dl", 0.0)                      # …the answer still comes up
        page._on_done("s1")                                       # then the worker ends
        assert not ov._minimized and ov._stack.currentWidget() is ov._page_done
        text = ov._done_view.toPlainText()
        assert "These are the most recently" in text and "crtypto_trading/" in text and "86 GB" in text, text
        assert "**" not in text, "markdown must be rendered, not shown raw"
        h_short = ov._done_view.maximumHeight()
        assert 60 < h_short <= co.DONE_MAX_H, h_short
        ov._end_flash(); ov._maybe_collapse()
        assert not ov._minimized, "the answer stays up until you close it"
        ov.mousePressEvent(type("E", (), {"accept": lambda self: None})())   # a click on the card
        assert ov._minimized, "a click on the card closes it"
        ov = co.ClaudeOverlay(); ov.rest_idle(); ov.present(); ov.open_full()
        ov.show_done(task="x", result="line\n\n" * 120)              # a very long answer scrolls
        for _ in range(5):
            QApplication.processEvents()
        assert ov._done_view.maximumHeight() == co.DONE_MAX_H
        assert ov._done_view.verticalScrollBar().maximum() > 0, "long answers must scroll"
    finally:
        co.QCursor = real
    print("done card: whole answer (formatted), stays open while running + after done until you close it  OK")


def test_card_and_details_never_repeat():
    """VoiceOS split: bubble = what you asked (2 lines, hover for all), bold line = the task's name,
    the done card = the answer; Details = how it got there, without repeating any of those."""
    e = _entries(dict(kind="user", text="what files are in my downloads"),
                 dict(kind="tool", tool="Bash", input={"command": "ls ~/Downloads"}, tool_id="b1"),
                 dict(kind="result", tool_id="b1", text="a.png\nb.png"),
                 dict(kind="text", text="Checking the newest ones."),
                 dict(kind="text", text=DOWNLOADS_REPLY))
    v = dv.visible_entries(e, hide_final=True, hide_prompt=True)
    kinds = [x["kind"] for x in v]
    assert kinds == ["tool", "text"] and v[1]["text"] == "Checking the newest ones.", v
    shot = _entries(dict(kind="user", text="what is this", images=["/tmp/x.png"]))
    assert dv.visible_entries(shot, hide_prompt=True) == shot, "a prompt with screenshots stays in Details"

    ov = co.ClaudeOverlay(); ov.rest_idle(); ov.present(); ov.open_full()
    ov.set_task("what files are in my downloads", "List Downloads Files")
    assert ov._transcript.text() == "what files are in my downloads" and ov._transcript.toolTip() == ""
    assert ov._title_label.text() == "List Downloads Files"
    ov.set_details(e)
    ov.show_done("sid", 0.0, "what files are in my downloads", "")
    details, card = ov._details_feed.toPlainText(), ov._done_view.toPlainText()
    assert "crtypto_trading" in card, "the answer is on the card"
    assert "crtypto_trading" not in details, "…and not repeated in Details"
    assert "what files are in my downloads" not in details, "the prompt is in the bubble, not repeated"
    assert "ls ~/Downloads" in details and "Checking the newest ones." in details, "the steps stay"

    long_prompt = "Build a polished responsive one-page website for an invented coffee brand named " \
                  "Ember & Oak with a hero, a brand story, featured coffees, a visit section and a footer."
    ov2 = co.ClaudeOverlay(); ov2.rest_idle(); ov2.present(); ov2.open_full()
    ov2.set_task(long_prompt, "Build coffee brand website")
    assert ov2._transcript.text().endswith("…") and ov2._transcript.toolTip() == long_prompt
    ov2.set_details(_entries(dict(kind="user", text=long_prompt), dict(kind="text", text="Done.")))
    ov2.show_done("sid", 0.0, long_prompt, "")
    assert "Ember & Oak with a hero" in ov2._details_feed.toPlainText(), "cut-off prompt → Details keeps it"

    ov3 = co.ClaudeOverlay(); ov3.rest_idle(); ov3.present(); ov3.open_full()
    ov3.set_task("hello", "Say Hello")
    ov3.set_details(_entries(dict(kind="user", text="hello"), dict(kind="text", text="Hey Damianos!")))
    ov3.show_done("sid", 0.0, "hello", "")
    assert "answered directly" in ov3._details_feed.toPlainText(), ov3._details_feed.toPlainText()
    print("card vs Details: bubble = ask (cut + tooltip), bold = name, card = answer, Details = only the rest  OK")


if __name__ == "__main__":
    test_steps_from_real_sdk_messages()
    test_render_actions()
    test_deliverable()
    test_overlay_links_and_done_card()
    test_live_flow_through_page()
    test_done_card_shows_whole_answer_and_stays_open()
    test_card_and_details_never_repeat()
    print("test_details_view OK")
