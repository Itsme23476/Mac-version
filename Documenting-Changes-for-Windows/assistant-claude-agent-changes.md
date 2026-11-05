# Assistant: voice-driven local Claude Code agents (for the Windows port)

Mac branch: `feat/agent-runner`. It is **unshipped** with no version number yet, and the latest work is uncommitted.
**No backend changes.** The agents run on the user's own Claude Code login through `claude-agent-sdk`, so no
Supabase or OpenAI proxy is involved. The voice transcript still comes from the existing `transcribe` function.

## What it does (user-facing)
- **Hold fn+⌃ and speak** to start a task. A local Claude Code agent then works in `~/Filect Assistant`.
  - Saying something like "continue that one" goes back to the last conversation.
  - A **typed** task works too: there's a type-to-task field in the notch.
- **The notch** (top centre) is the live view of the conversation in focus. It shows:
  - what you said, the status and a timer;
  - the model and effort picker, Details, and Stop / "Open in Claude →";
  - Approve/Deny cards for risky tools.
- **The left rail** lists every conversation: running ones plus all past ones, grouped like Claude's sidebar.
  - It has a search box.
  - Clicking a chat drops the notch open on it.
- **Details** is a rich, Claude-style log:
  - file writes show a preview, Show full and Open;
  - edits show a diff;
  - commands show ✓/✗ with their output;
  - the done card shows Claude's summary plus the file it made.
- **Screen + draw:** while holding fn+⌃, **hold Space** to draw in red on the live screen.
  - **Releasing Space takes a snapshot.** Tapping Space takes a plain snapshot.
  - Releasing fn+⌃ sends your words plus every snapshot to Claude. Esc cancels.
- **Several agents can run at once.** A chat in the background that needs approval or finishes sends a branded notification.

## Files (Mac) and how portable each one is

| File | What it is | Windows |
|---|---|---|
| `app/core/agent_runner.py` | Engine: `AgentRunner.run(prompt, resume, bypass, model, effort, images)` and the approval gate | **Port as-is** (pure Python + SDK) |
| `app/core/agent_sessions.py` | Reads Claude Code's local session store: `list_sessions`, `load_transcript`, `find_by_name`, `generate_title` | **Port as-is** (see the path note) |
| `app/core/intent_router.py` | Warm Opus `ClaudeSDKClient` that classifies a voice command as NEW, FOLLOW_UP or RESUME | **Port as-is** |
| `app/ui/details_view.py` | Pure renderer: entries → HTML for the Details feed and the done card | **Port as-is** |
| `app/ui/assistant_page.py` | `AgentSession` registry, `AgentWorker(QThread)`, focus model, routing, notifications | **Port as-is** except `_play_chime` / `_notify` / open-in-Claude (already branch per platform; see below) |
| `app/ui/claude_overlay.py` | The notch card (`ClaudeOverlay`) | Qt is portable; **positioning and window style differ** (§2) |
| `app/ui/conversation_rail.py` | The left rail | Qt is portable; **window style differs** (§2) |
| `app/ui/notch_panel.py` | macOS NSPanel mechanics: `configure_macos_panel`, `notch_width`, `notch_inset_top`, `_ns_window` | **Mac-only.** Write a `win_panel.py` with the same function names (§2) |
| `app/ui/screen_ink.py` | Red-ink layer + screen capture | Ink layer is portable; **capture is per-platform** (§4) |
| `app/ui/dictation.py` | Gesture wiring: `assistant` mode, `_on_space`, `_on_escape`, `_take_shot` | **Port as-is** once the hook delivers the same callbacks (§3) |
| `app/ui/mac_hotkey.py` | CGEventTap: `_make_fn_callback(..., on_space, on_escape)` | **Mac-only.** Extend the Windows low-level hook (§3) |

Dependency: `claude-agent-sdk` (Mac used 0.2.174). Its wheels bundle the `claude` CLI per platform.
**Check that the Windows wheel installs and that `claude` launches** before porting anything else.

Settings keys (copy verbatim):
- `assistant_model` (`''`/`opus`/`sonnet`/`haiku`; the UI defaults to opus)
- `assistant_effort` (`''`/`low`…`max`; defaults to high)
- `assistant_router_model` (`opus`)

## 1. Engine and session store: shared logic, one path difference
- `AgentRunner.run` uses `ClaudeSDKClient(ClaudeAgentOptions(cwd, setting_sources=[], resume, max_turns, model, effort, ...))`.
  - `setting_sources=[]` is required. Without it, the user's permissive global `~/.claude` settings auto-approve
    everything and `can_use_tool` never fires.
  - `bypass=True` uses `permission_mode="bypassPermissions"`.
  - Read-only tools are auto-allowed (`DEFAULT_AUTO_ALLOW`). Everything else raises an Approve/Deny card.
- `_steps_from_message` turns SDK messages into `Step(kind="text"|"tool"|"result", ...)`.
  - It handles `AssistantMessage` (TextBlock / ToolUseBlock).
  - It also handles **`UserMessage` / `ToolResultBlock`**: that's how Details knows a write or command
    succeeded and what it printed.
- **Images:** `user_message(prompt, images)` builds one SDK user dict:
  - `{"type":"user","message":{"role":"user","content":[image blocks…, text]},"parent_tool_use_id":None}`.
  - Each image is downscaled to 1568px on the long edge and sent as PNG; it falls back to JPEG 85 when over 3.5 MB.
  - The text carries `[screenshot: <path>]` lines so Claude can reuse the files.
  - Send it with `client.query(async_iterable_yielding_that_dict)`, and **build it BEFORE connecting**,
    because `_image_block` waits up to 5s for a screenshot that's still being written.
- **Session store path:** `~/.claude/projects/<encoded cwd>/<uuid>.jsonl`.
  - Encoding = `re.sub(r"[^A-Za-z0-9]", "-", cwd)`. It works unchanged on Windows: `C:\Users\x\Filect Assistant` becomes `C--Users-x-Filect-Assistant`.
  - `~` is `%USERPROFILE%`.
  - `list_sessions(cwd=...)` has a fast path that reads only that folder (56ms vs 417ms).
- **Reading transcripts:** skip jsonl lines with **`isMeta`** (injected notes like "[Image: original…]") and
  **`isSidechain`** (sub-agent chatter), plus user text that starts with `<` or "This session is being continued".
- **Threading:** run each agent in its own `AgentWorker(QThread)` with its own asyncio loop.
  - Connect worker signals with `lambda ..., k=key:` so the handlers know their session.
  - Handlers must run on the **main** thread (`tests/test_worker_thread_affinity.py` guards this).

## 2. Notch and rail windows: the main Windows design decision
On Mac, both are **non-activating NSPanels**: level 1000, on all Spaces, and they never steal focus. Panels are
found by window title with `notch_panel._ns_window(title)`.

**Windows has no notch.** Recommended equivalent:
- Anchor the card at the **top centre** of the monitor under the cursor.
- The hotspot is an invisible ~185×8px strip at the top edge. Clicking it toggles the card open/closed.
- At rest, draw **nothing**. Damianos hated the faint pulsing hint we tried, so don't add one.
- Hover-reveal is **PARKED** on Mac (see the pitfall below), so ship click-to-toggle first.

**Window style** (write `app/ui/win_panel.py` exposing the same functions the overlay calls):
- Qt flags: `Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus`,
  plus `WA_ShowWithoutActivating` and `WA_TranslucentBackground`.
- Win32: `SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)`.
- **Click-through** (the Mac `setIgnoresMouseEvents_`) is `WS_EX_LAYERED | WS_EX_TRANSPARENT`, toggled on and off.
- **Typing into a non-activating panel** (rail search, type-to-task field). On Mac we call `makeKeyWindow` when
  the field is clicked. On Windows, drop `WS_EX_NOACTIVATE` and `activateWindow()` only on that click, then put the
  style back when the field loses focus.

**Rules that apply on both platforms (Damianos was explicit):**
- The notch and the rail are **independent**. Clicking one must NEVER open the other. He was furious when it
  did, so don't re-add any "cockpit" coupling.
- Selecting a chat in the rail **does** drop the notch open on it (`_show_in_notch` = `_focus` + `open_full`).
  That's intended VoiceOS behaviour, not the forbidden coupling.
- Clicking the notch toggles it. Clicking the rail strip toggles the rail.

**Rail grouping** (`conversation_rail._group`):
- RUNNING (status working/needs_you only), then TODAY, PREVIOUS 7 DAYS, PREVIOUS 30 DAYS.
- These are **calendar** days, not rolling 24h.
- After that, one group per month: `OCTOBER`, then `SEPTEMBER 2025` when it's not the current year.
- Search (`_matches`) is case-insensitive, and every word must match title + preview.

**❌ Qt pitfalls we hit (apply on both platforms):**
- **Children added to an already-visible parent** are shown through a *queued* event, so they measure 0 height
  and the rail collapsed to "only one chat". Call `w.show()` on each new row.
- **The outer layout caches the card's sizeHint.** Call `self._card.updateGeometry()` before
  `resize(sizeHint())`, otherwise the window lags one resize behind. `adjustSize()` never shrank the rail.
- **Unstyled scroll areas and scrollbars paint WHITE** (the app's light palette) on the dark card. Style every
  layer `background: transparent` and give scrollbars dark CSS.
- **Hover never fired.** The tab's reveal was driven by `enterEvent` on a child that sat outside the area the
  cursor actually entered. The Mac fix is a 60ms cursor poll (`_poll_hover`: `QCursor.pos()` vs `frameGeometry()`).
  It is still unverified live, so test the real event path, not a forced state.

## 3. Hotkeys: fn+⌃ voice, plus Space and Esc during it
- Mac: a CGEventTap (`kCGEventTapOptionDefault`, so it can swallow keys) sees Fn (keycode 63, flag `0x800000`) and
  ⌃ (`0x40000`). `_make_fn_callback(st, on_pressed, on_released, override, on_space, on_escape)` works like this:
  - **Space down** (not autorepeat) while fn **and** ⌃ are held → `on_space(True)`. **Space up** → `on_space(False)`.
    Space is **swallowed** the whole time, so it never types into the app in front.
  - **Esc** while held → `on_escape()`, swallowed.
  - Every other key passes through untouched.
- **Windows:** there's no Fn event, so use the voice chord the Windows dictation port already chose (see
  `voice-dictation.md` §1–2, e.g. a right-Ctrl based chord) plus a "modifier" that means *assistant mode*.
  - Extend the existing **`WH_KEYBOARD_LL`** hook. While the chord is held:
    - on `VK_SPACE` key-down (ignore repeats: the LL hook resends key-down, so track a `space` flag), call
      `on_space(True)` and **return 1** to swallow;
    - on key-up, call `on_space(False)` and return 1;
    - on `VK_ESCAPE`, call `on_escape()` and return 1.
  - Give `register_global_hotkey` the same `on_space` / `on_escape` keyword arguments so `dictation.py` stays identical.
- `dictation.py` marshals both callbacks to the UI thread with `Signal(bool)` / `Signal()` and `Qt.QueuedConnection`.
  **The hook thread must never touch widgets.**
- **⚠️ Mac open question:** macOS's own ^Space ("select previous input source") is enabled on Damianos' Mac. Whether
  the tap sees fn+⌃+Space first is being checked live with the `[screen] space down/up` log lines. Windows has the
  same kind of risk with IME switching on Ctrl+Space. The LL hook runs before IME hotkeys, but **test with an IME
  installed**.

## 4. Screen capture + red ink
- The **ink layer** (`ScreenInk`) is plain Qt and **ports as-is**:
  - a frameless, translucent, full-screen window on the monitor under the cursor;
  - click-through except while Space is held;
  - strokes `#FF3B30` at 4.5 logical px, scaled to pixel density when composited.
  It is a "Drawing — release Space to snapshot" pill + a red edge while drawing, then a "✓ Screenshot N" toast and a shutter flash.
- **Capture must exclude our own floating windows** (ink, notch, rail, dictation pill) but keep everything else,
  including the main Filect window.
  - Mac: `CGWindowListCreateImageFromArray` over every on-screen window except ones where owner pid == ours and
    layer != 0. Full Retina resolution, ~20–90ms.
  - **Windows:** call `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)` (`0x11`, Windows 10 2004+) on the ink,
    notch, rail and pill windows once each is created. Then a normal capture leaves them out, either
    `QScreen.grabWindow(0)` or `mss` / BitBlt of the desktop DC. Check the returned size against the
    monitor's physical pixels at 150% scaling.
    Fallback for older builds: `hide()`, process events, wait ~1 frame, capture, `show()`.
  - **Windows needs no permission.** On Mac it's Screen Recording (`CGPreflightScreenCaptureAccess` /
    `CGRequestScreenCaptureAccess`); `_capture` returns `None` and the controller shows a hint.
- **Saving:** capture plus compositing happen on the UI thread, because that moment *is* the snapshot. The PNG
  write runs on a **background thread**: a ~7 MP PNG took ~0.5s and froze the screen on every Space release.
  - Each file is written as `<path>.part` and then `os.replace`d, so readers never see half a file. On Windows,
    `os.replace` is atomic on the same volume.
  - A `.thumb.png` (720px wide) goes next to it, for Details.
  - Folder: `~/Filect Assistant/Screenshots/`.
  - **❌ pitfall:** name files with a **uuid** (`screen-%Y%m%d-%H%M%S-<uuid8>.png`). A per-gesture counter repeated
    within the same second, so two save threads fought over one `.part` and a screenshot was lost.
- **❌ pitfall (first live test, Mac):** the drawing never showed. The gesture holds ⌃, and Qt on macOS turns a
  ⌃-click into a **right** click, but the ink layer only took the left button, so every stroke was dropped.
  The ink layer now draws with **any** mouse button. Keep it that way on Windows too: whatever modifier your chord
  holds, don't filter on the button. Regression test: `tests/test_ink_real_mouse.py`. It sends real NSEvents with
  fn+⌃ through Qt's own translation, then reads the window's on-screen pixels.
- **Tap or hold (changed after live testing: holding fn+⌃+Space while dragging was too hard, and fn+⌥ got
  hit by accident).** The draw key is **Space or Shift**. Shift sits right next to fn/⌃; it's watched on
  `flagsChanged` keycodes 56/60 and never swallowed.
  - **TAP** (released in under `TAP_SEC`=0.35s): drawing stays on, and the next tap takes the snapshot.
  - **HOLD:** you draw while it's held, and releasing it takes the snapshot.
  - **Re-grip:** a press within `REGRIP_SEC`=0.3s of the tap that turned drawing on counts as the start of a
    hold, not as "snapshot". Seen live: a 0.2s press, a 0.1s gap, then a 12s hold. All of this lives in
    `dictation._on_space`, which is key-agnostic, so on Windows the hook only has to call `on_space(True/False)`.
- **Gesture rules** (`dictation.py`, port as-is):
  - Snapshots are only possible while recording.
  - Holding Space forces assistant mode.
  - Releasing the chord while Space is still held takes a final snapshot.
  - No words + at least one snapshot uses a default prompt: "Look at what I've marked in red." or
    "Take a look at this screenshot."
  - Images ride along on new chats, follow-ups, and follow-ups queued during a run (`pending_images`).

## 4b. Receive mode: several screenshots and requests in ONE message
- **Gesture:** hold fn and **double-tap ⌃** (two ⌃ presses less than `RECEIVE_TAP_SEC`=0.45s apart while fn is held).
  Damianos picked this because fn stays under one finger. Once in receive mode it's hands-free:
  - letting go of fn keeps it listening;
  - you talk and scroll;
  - **Space or Shift on its own** (nothing else held) works as the draw key, tap or hold;
  - **tap ⌃ on its own** sends everything as ONE message and ends receive mode. This was changed after a live
    test: he reached for ⌃ to finish, since he entered with ⌃. Tapping fn still sends too;
  - **Esc** drops all of it.
  - Only a **clean** ⌃ tap counts as the send tap, and the key tap decides this (`CTRL_TAP_SEC`=0.4s):
    - ⌃ goes down and up within 0.4s;
    - no key and no other modifier goes down in between, so ⌃C, ⌃→ and ⌃⇧ don't count;
    - fn isn't held, so a ⌃ while holding fn is an entry press, never the send tap.
    It's reported as `on_control("tap")`, while ⌃ presses with fn held are `on_control("press")`.
    Known edge: a ⌃+click (old-style right-click) looks like a clean tap, because the tap doesn't watch the mouse.
  A normal fn+⌃ hold still sends immediately; that never changed.
- **Key tap** (`mac_hotkey._make_fn_callback(..., on_control)`):
  - ⌃ key-down while fn is held calls `on_control()`. It's keycode 59/62 on `flagsChanged`, and it is never swallowed.
  - `set_screen_keys(hotkey, on)` sets `st['receive']`. While it's set, Space/Esc are swallowed and
    Shift is watched *without* fn+⌃.
  - ⌘Space and ⌥Space still pass through to Spotlight and the system.
  - **Windows:** in the LL hook, report Ctrl key-downs while the chord is held, and honour a receive flag the same way.
- **Controller** (`dictation._on_control` / `_enter_receive` / `_exit_receive`): receive mode is the existing
  hands-free latch plus forced assistant mode. `_on_release` returns early while latched, and a fn press
  while latched does `_stop()`, as before. The ink layer stays up, click-through, showing the banner
  "Receiving · N screenshots — tap Space or ⇧ to draw · tap fn to send · Esc cancels".
- **❌ pitfall fixed:** the 10-minute cap (`_on_capped`) never called `_stop()`, so the mode was never
  decided and a long Claude message would have been **pasted as dictation**. It now calls `_stop()`.
- **Which screenshot goes with which words:**
  - `StreamingTranscriber.words_so_far()` counts the finished utterances plus the one in progress.
    It shares `_assemble()` with the final transcript.
  - Each shot stores that count, and `dictation._with_markers` inserts `[screenshot N]` after that many words.
  - `agent_runner.user_message` puts a `[screenshot N]` text block before each image when there's more than one.
  - Titles drop the markers (`generate_title`).
  - Expect markers to land a word or so early, because the live transcript runs about 0.5s behind speech.

## 5. Rich Details (`details_view.py`, pure, port as-is)
- **Entries model:** `{user | text | tool(input, tool_id, result)}` on `AgentSession.entries`, built the same way from
  live steps (`add_step`) and from saved transcripts (`_transcript_view`). A result attaches to its tool by `tool_id`.
- **Rendering by tool:**
  - Write: preview + size (words for .md/.txt, lines otherwise) + Show full + Open ("Open in browser" for HTML; "moved or deleted" when the file is gone).
  - Edit: +/− diff.
  - Bash: description, ✓/✗, last 3 output lines.
  - WebSearch: the query. WebFetch: the link.
  - TodoWrite: ☑ ◐ ☐.
  - Read/Grep/Glob: muted.
  - `Agent`/`Task`: "Delegated".
  - Failures: ✗ + the reason.
- **Done card:** `summary(final_reply)` (first paragraph, cut on a word boundary) + `deliverable_html`
  (the latest turn's last *successful* Write).
- **The widget** is a QTextBrowser with `setOpenLinks(False)`. Its `anchorClicked` handles `full:<id>` (toggle) and
  file/http links (`QDesktopServices.openUrl`, which works on Windows). Scroll position is kept when it re-renders.
- **❌ Qt pitfalls:**
  - Markdown code blocks are `nonBreakableLines`. Unset it, or a horizontal scrollbar appears.
  - `<h1>` gets a built-in 18pt that overrides span sizes. Rewrite h1–h6 to `<p>`.

## 6. Platform-specific pieces in `assistant_page.py`
- `_play_chime(ok)`: already cross-platform (Windows uses `winsound.MessageBeep`).
- `_notify(title, body)`: a branded notification through `QSystemTrayIcon.showMessage`, which becomes a Win10/11 toast.
  - The tray icon is created once at startup.
  - The osascript fallback is Mac dev-only and no-ops elsewhere.
  - Clicking a notification focuses that session (`messageClicked` → `_on_notification_clicked`).
- **"Open in Claude →"** runs `open claude://resume?session=<uuid>` on Mac. On Windows use `os.startfile(url)`,
  assuming the Claude desktop app registers `claude://` there; verify. Otherwise it falls back to the in-app Assistant tab.
- The detailed Windows notification write-up from earlier is in `~/Desktop/Filect-Assistant-Notch-for-Windows/`.

## Tests (Mac), all headless with `QT_QPA_PLATFORM=offscreen`
`tests/`:
- `test_worker_thread_affinity`
- `test_concurrent_sessions`
- `test_focus_render`
- `test_voice_candidates`
- `test_rail_recent` (includes the mutation-checked rebuild regression)
- `test_details_view` (real SDK message objects)
- `test_screen_ink` (real CGEvents through the tap; the ink snapshot; the controller gesture; `user_message`; routing + Details thumbnails; unique file names)

Self-check: `python app/ui/claude_overlay.py`.

`test_screen_ink`'s key-tap part is Mac-only. On Windows, replace it with a test that feeds synthetic
`KBDLLHOOKSTRUCT`s to the hook proc.

Live-verified on Mac:
- A real haiku run wrote a file and Details showed it ✓ with the deliverable.
- A real capture at 3456×2234 with red marks.
- A real Claude correctly named the red-circled "Share" button, which proves the images and marks reach the model.

Not yet live-verified: the fn+⌃+Space key path on the user's own machine, and notch hover (parked).

## 7. Release fixes (Mac v14.10.0, 2026-11-04)

These were found while testing the first packaged build. Check each one against the Windows code
yourself; the Mac side can't see it.

### 7a. Crash on quit while an agent runs — **BOTH platforms (Qt)**
- Bug: quitting mid-task (or in the first second after launch, while past chats load) destroyed a
  running QThread. Qt aborts with `QThread: Destroyed while thread '' is still running`, and the
  user sees an "app quit unexpectedly" crash.
- Fix in `assistant_page.py`:
  - `AssistantPage.shutdown()` is connected to `QApplication.aboutToQuit`. It calls
    `worker.cancel()` on every live session (that kills the claude subprocess), then
    `quit()` + `wait(3000)` on every session thread and on `_loader_thread`.
  - The session loader's `loaded → thread.quit` connection is now `Qt.DirectConnection`, so the
    loader thread ends as soon as loading finishes instead of waiting on the UI event loop.
- Test: `tests/test_rail_recent.py::test_quit_mid_task_stops_threads`.

### 7b. Agent couldn't start from a Finder-launched app — **Mac-only cause; check the Windows equivalent**
- Bug: a Finder-launched app only gets `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, and the PyInstaller
  build doesn't bundle the SDK's own `claude` binary. The SDK picked an old npm `claude`
  (`#!/usr/bin/env node`) that couldn't find node → exit 127 on every task. The agent's Bash tool
  also couldn't see Homebrew/npm tools.
- Mac fix: `agent_runner.use_login_path()`. Once, before launching claude (`AgentRunner.run`
  and `intent_router._open`), it merges the login-shell PATH (`$SHELL -ilc`) plus
  `~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin` into `os.environ["PATH"]`.
- **Windows:** the login-shell part doesn't apply, but the same failure can happen if the
  packaged app can't find `claude.exe`. Launch the packaged app from the Start menu (not a
  terminal) and run one agent task. If it fails with CLINotFoundError or a ProcessError, add the
  Claude install dir (e.g. `%USERPROFILE%\.local\bin`) to PATH the same way, or pass `cli_path`
  in `ClaudeAgentOptions`.
- Test: `tests/test_agent_path.py`.

### 7c. Continued chats dropped off the rail after a restart — **BOTH platforms**
- Bug: the rail's history only lists chats in the agent workspace (`list_sessions(cwd=DEFAULT_WORKSPACE)`).
  A chat CONTINUED in another project (e.g. the "Hyper3D MCP setup" chat in its own folder) lives
  in that project's folder, so after a restart nothing pointed to it.
- Fix in `agent_sessions.py`:
  - `remember_session(id, cwd)` is called from `_on_result` when `sess.cwd` isn't the workspace.
    It writes `[{id, cwd}]`, newest first and capped at 200, to `<app data dir>/assistant_sessions.json`.
  - `remembered_sessions(workspace)` returns those that still exist, and `_load_recent` merges
    them with the workspace list (deduped by id, newest first).
  - **One-time backfill** when that file doesn't exist yet: scan every transcript once for chats
    outside the workspace that have BOTH `"entrypoint":"sdk-py"` (written by Filect) AND
    `cli`/`claude-desktop` lines (a real continued chat, not a router or test leftover). That's
    ~1.3s per GB, off the UI thread. Match with `\s*` after the colon: real files are compact JSON.
- Windows: the transcript store is the same format (`%USERPROFILE%\.claude\projects`), so this
  ports as-is. Only the app data dir differs.
- Test: `tests/test_rail_recent.py::test_continued_chats_survive_restart`.

### 7d. Left rail: VoiceOS-style drawer + hover cue + New chat (Mac, 2026-11-04) — **design applies to BOTH**
These follow Damianos' calls after comparing against VoiceOS. The behavior is platform-neutral Qt.
- **At rest:** a slim dark see-through sliver on the left edge (4px wide, ~58% of the drawer height,
  black alpha 115, white hairline alpha 28). **Hover:** it slides out a little (7 → 16px) and the sliver
  grows (8px, 66%, alpha 150). **Hover never opens it; only a click does.** A chat that starts running
  shows the same cue for 2.6s. It never opens by itself.
- **Hover detection = cursor POLL** (`QCursor.pos()` vs `frameGeometry()` every 60ms), not enter/leave.
  macOS never delivered enter/leave to the inactive app's panel. On Windows, enter/leave may work,
  but the poll is harmless and consistent.
- **Drawer:**
  - Size and position: ~224px wide, fixed height 50% of the screen, vertically centered.
  - Colour and shape: near-black (#0B0B0D, alpha 248), painted with a QPainterPath. Corners curve INTO
    the screen edge (top-left/bottom-left, radius 18), right corners rounded 20.
  - Contents: a pill search with a magnifier, a round **lock** button, and a "New chat" row. Group
    headers are sentence case and grey. Rows are one line: an 18px round icon (Claude logo if
    bundled, else lavender) plus the title. Status is a small badge on the icon; detail is in the
    tooltip.
- **Close rules:** click an empty part to close. Unlocked, it tucks away 0.45s after the mouse leaves,
  but not while the search box has TEXT. Don't use focus for this: Qt focuses the search as soon as the
  drawer shows. The lock keeps it open.
- **"＋ New chat":** `_on_rail_new_chat` calls `_clear_resume()`, then `ClaudeOverlay.show_new_chat()`,
  which opens the notch on the blank Ready card with the type box focused. Before this it only reset
  hidden Assistant-tab state, so nothing visible happened.
- Tests: `tests/test_rail_recent.py`: `test_hover_cues_click_opens`, `test_drawer_leave_lock_and_search`,
  `test_new_chat_opens_blank_notch`, `test_idle_rail_shown_before_any_request`.

### 7e. Notch idle hover = VoiceOS-style bar (Mac, 2026-11-04). Mac notch only; Windows uses its own top widget
- This is the idle state only, with no task running. Rest the cursor on the notch for 220ms and it grows
  into a black bar. Its width is 2.1× the notch, clamped to 330–420px (`_hover_w`), and the body is 56px
  (`HOVER_BODY_H`). The bar has shoulders curving into the top edge, rounded bottom corners, and fades to
  alpha 95 at the bottom. It holds a 16px lavender orb beside the notch and a "Type or hold fn ⌃ control
  to speak" input under it (14px).
- `_HoverBar` is a separate child widget layered over the resting strip. The card and its layout are
  untouched. Enter → `submit_text` (same path as the card's type field). Leaving tucks it back after
  280ms, or 4s if you clicked into the input; it stays while text is typed. Esc tucks it away.
  `set_state(non-idle)` and `_apply_minimized_chrome` close it.
- **Click (updated in §8a):** the first click puts the cursor in the bar's own input. It does NOT switch
  to the old card. A second click with the input empty closes the bar (`close_requested`). With a draft
  typed, a click only places the cursor, so nothing typed is lost.
- A live task (working/needs_you/done/error) doesn't get this bar; see §8d for its gray lip.
- If the Windows app has a top-center widget, the same widget + rules port as-is. Test: `tests/test_notch_hover.py`.

## 8. Notch + card polish (Mac v14.10.1, 2026-11-05)

Everything here was asked for by Damianos after testing 14.10.0 live. Apart from §8b, it's Qt-level and
applies to the Windows top widget if it reuses `claude_overlay.py`. As always: check the Windows code
yourself, the Mac side can't see it.

### 8a. Which chat the card opens on (the "last chat" rule): **BOTH platforms**
- His rule: before you've sent any request, opening the card shows the blank default. Once you've sent
  one, closing and reopening shows your LAST chat, not whatever you clicked in the rail.
- `assistant_page.py`: `_last_sent_key` is set in `_start_worker` (before `_focus(fresh=True)`) and in
  `run_task`'s queued follow-up path. `ClaudeOverlay` emits `collapsed` at the end of `minimize_panel`,
  connected to `_on_notch_collapsed`:
  ```python
  sess = self._focused
  if sess is None or sess.running or sess.key == self._last_sent_key: return
  last = self._last_sent_key if self._last_sent_key in self._sessions else None
  self._resume_target = None
  if last: self._focused_key = last; self._render_focused()
  else: self._focused_key = None; self._notch("rest_idle")
  self._refresh_rail()
  ```
- Test: `tests/test_rail_recent.py`.

### 8b. Notch window sat BELOW the menu bar: **Mac-only cause; one Qt flag matters for BOTH**
- Bug: "nothing pops up at the start". `NSPanel.setFloatingPanel_(True)` silently resets the window
  level to 3. At level 3, AppKit kept the window under the menu bar (y=33) while Qt still believed y=0,
  so hovering the notch never reached it.
- Mac fix (`notch_panel.py`): re-apply level 1000 right after `setFloatingPanel_`, then `place_native()`.
  This sets the NSWindow frame directly from Qt's rect, and is called after configure, `_reanchor` and
  every slide.
- **Qt 6.11, BOTH platforms:** `ClaudeOverlay` sets `WA_ContentsMarginsRespectsSafeArea` to False. With
  it on, Qt adds the screen's safe-area inset (32px under the notch) to the layout and pushes the card
  down. Windows has no notch inset, so the flag is harmless there; keep it so the code stays the same.
- Test (real macOS only): `tests/test_notch_top_edge.py`.

### 8c. Open card restyled to match the hover bar (visual only): **design applies to BOTH**
- Palette: surface #1C1C1F, border #2A2A2E, card #000000, accent lavender #A78BFA (light #C4B5FD).
  This replaced the Claude-orange accent.
- The card frame is transparent with no drop shadow. `ClaudeOverlay.paintEvent` paints the card itself:
  pure black, shoulders curving into the top edge, bottom radius 22, and a fade to alpha 205 over the last
  22px. Skip this when minimized.
- Header row sits IN the notch band: `[orb][status][timer] … [lock][close]`, inserted at index 0 of the
  card's column. The expanded margins are `(16, max(4,(nb-TOPBAR_H)//2), 16, 12)`. On Windows, set `nb`
  to your top widget's band height (or 0).
- Pills:
  - Transcript: radius 16, 13px font.
  - Model: height 26, radius 13, no border.
  - Stop / Open in Claude: dark, radius 13.
  - Approve / Deny: radius 17; Deny is dark with red text.
- Type field: surface background, radius 16, focus border `rgba(167,139,250,0.45)`, and the same hint as
  the hover bar via `_TypeField.set_hint(_SPEAK_HINT)`. The separate footer hint row is gone (`_fit`
  no longer counts it).

### 8d. Visible lock / close buttons + gray lip under the notch: **design applies to BOTH**
- Bug: the two header buttons were text glyphs (⇣/⇡ and ✕, 11px muted grey) on near-black circles and
  read as blank.
- Fix: the icons are now painted. `_glyph()` moved from `conversation_rail.py` into `claude_overlay.py`
  (the rail imports it from there), gained a `'close'` kind and a `color=` override.
  - Pin button = the rail's **lock** icon (lavender and shut = kept open, grey and open = tucks away on
    leave).
  - Close = a grey-white X.
  - Both are 13px on 22px circles: `#2C2C31`, hover `#3A3A41`, `padding: 0`.
- Bug: while a task was working or finished, a colored pill floated 2px under the notch (green = done)
  and read as a stray green bar.
- Fix (`_Handle.paintEvent`, live states only; idle draws nothing): a GRAY lip hanging flush from the
  notch, drawn from y=-6 so its top is flat and attached.
  - Size: 50% of the strip wide and 4px thick; on hover it grows to 62% and 8px.
  - Colour: `QColor(160,160,168)`, alpha 150, rising to 220 on hover. Translucent black was tried
    first and vanished over dark windows.
  - Working: the alpha breathes. An approval waiting adds the amber dot to its right.
  - `_STATE_COLOR` and `_Handle._color()` are deleted.

### 8e. After 14.10.1 (Mac, 2026-11-05): lock removed, ⌃ timings 1s, quit logging
- **Lock button removed from BOTH the notch card and the left drawer** (Damianos: unclear, and
  VoiceOS has none).
  - Card: `_pin_btn`, `_toggle_pin` and `_sync_pin_btn` are deleted. `_pinned` STAYS: clicking the
    notch still opens the card pinned (it stays until you click again, press X or Esc). A card that
    opened by itself still closes after 3.5s unless the mouse is on it.
  - Drawer: `_lock_btn`, `_toggle_lock`, `_sync_lock` and `_pinned` are deleted. It tucks away 0.45s
    after the mouse leaves, except while the search box has text.
  - `_glyph` lost its `'lock'` kind and `on` parameter.
  - Test: `test_rail_recent.py::test_drawer_leave_and_search`.
- **⌃ timings widened to 1 second** (both were too tight live):
  - `dictation.RECEIVE_TAP_SEC` 0.45 → 1.0: the 2nd ⌃ press while fn is held enters receive mode.
    Press, let go, press again within 1s counts.
  - `mac_hotkey.CTRL_TAP_SEC` 0.4 → 1.0: in receive mode, a lone ⌃ released within 1s sends.
  - On Windows, if receive mode exists, use the same values. Tests: `test_screen_ink.py`. A 0.7s tap
    and a 0.7s ⌃…⌃ now pass; checked that they fail with the old values.
- **"App quit by itself" diagnostics: BOTH platforms.** Closing the main window quits the whole app.
  Its `closeEvent` stops voice, and Qt's quit-on-last-window-closed is on. Telemetry (`session_ended`)
  showed every "random" quit was that window closing.
  - New `[quit]` log line in `MainWindow.closeEvent` via `_close_trigger()`. It records the macOS event
    being handled (red button = mouse, ⌘W/⌘Q = key + which key, quit request = type 13), the frontmost
    app, whether Filect was active, and the Python caller.
  - `main.py` also logs `lastWindowClosed` and `aboutToQuit`.
  - On Windows, log the same in `closeEvent` (the caller + `event.spontaneous()` part is portable).

### 8f. Closing the window keeps Filect running (Mac, 2026-11-05): **BOTH platforms (Qt)**
- Damianos: closing the main window must HIDE it and keep Filect running in the background. The same
  bug exists on any platform where `closeEvent` accepts and Qt's quit-on-last-window is on.
- `main.py`: `app.setQuitOnLastWindowClosed(False)`, set after the main window is created. Only an
  explicit quit ends the app.
- `FilectApplication.event`: on `QEvent.Quit`, set `self._quitting = True`. ⌘Q, a macOS quit request,
  `QApplication.quit()` from the tray or the updater all arrive as `QEvent.Quit`; verified on Qt 6.11.
  Initialise `_quitting = False` in `__init__`.
- `MainWindow.closeEvent`:
  - If not `app._quitting`: `event.ignore()`, `self.hide()`, then switch back to a menu-bar app.
    Voice, the notch and the drawer stay alive.
  - If quitting: the old shutdown runs (session_ended telemetry and voice cleanup).
- The tray icon (`assistant_page._get_or_create_tray`) got a menu with "Open Filect" (calls
  `app._bring_to_front()`) and "Quit Filect" (`QApplication.quit`), the way back in and out. On Windows
  the tray menu shows on right-click.
- Test (real macOS): `tests/test_hide_on_close_real.py`. NSWindow `performClose:` (the red button)
  hides the window with the app still running; "Open Filect" shows it; "Quit Filect" exits 0. On
  Windows, test with the title-bar X and the tray menu.

### 8g. The card stays open + shows the WHOLE answer (Mac, 2026-11-05): **BOTH platforms (Qt)**
- **Bug 1:** a voice task's card tucked itself away about 3.5s after the last step (the flash timer),
  usually BEFORE the answer arrived, and a finished task didn't bring it back. You never saw the answer.
  - Fix: `ClaudeOverlay.present()` sets `_pinned = True`, so the card stays open while the task runs
    and after it's done, until YOU close it (click the card, ✕ or Esc). `_end_flash` and
    `_maybe_collapse` already respect `_pinned`.
  - `assistant_page._on_result` (when the task is focused and not stopped) and `_on_error` now call
    `_notch("open_full")` after showing the result. The answer comes up even if you tucked the card
    away mid-run.
- **Bug 2:** the done card showed only the FIRST PARAGRAPH of Claude's reply (`details_view.summary`).
  For "list some files" that was just "These are the most recent items in your Downloads folder:"
  with the list cut off, so you had to open Details.
  - Fix: the done page's QLabel is replaced by a QTextBrowser, `_done_view`. It shows Claude's whole
    final reply through `details_view._md` (markdown with lists and bold), plus the deliverable's
    Open link and a muted "✓ Finished" line.
  - Its height is measured per answer (`document().setTextWidth(DONE_TEXT_W)`; the ✓ column has a
    fixed 16px width so the measurement is exact). It's clamped to `DONE_MAX_H` (280) and scrolls
    beyond that.
  - `_content_height()` uses the measured height for "done". Lists use `setIndentWidth(14)`, because
    Qt's 40px default wastes the narrow card.
  - `details_view.summary()` is deleted (it had no other users). The dark scrollbar CSS is shared as
    `_SCROLLBAR_CSS`.
- Test: `tests/test_details_view.py::test_done_card_shows_whole_answer_and_stays_open`. It uses the real
  Downloads answer and checks: the card survives the flash timer with the mouse away; it reopens on the
  result after being tucked; the whole list shows with no raw `**`; a click closes it; a long answer
  scrolls. Checked that it fails with the old `present()`.

### 8h. The card and Details never show the same thing twice (Mac, 2026-11-05): **BOTH platforms (Qt)**
- Taken from Damianos' VoiceOS recordings: VoiceOS's top says WHAT, Details says HOW, and nothing
  repeats. Filect's Details was repeating the final answer that the done card already showed.
- **Top bubble = what you asked:** `ClaudeOverlay.set_task(prompt, title)`, which replaces the old
  `set_title(prompt)` + `set_title(title)` pair in `assistant_page`. `_set_bubble` cuts the prompt to the
  bubble's 2 lines at a word with "…" (measured with QFontMetrics at 13px over `CARD_W-62`); the full
  text is the tooltip. `set_title` is kept only for short cues ("Routing…", "↳ continuing …").
- **Bold line = the task's short name:** new `_title_label` (14px semibold, `TITLE_H` 20, elided) under
  the bubble, VoiceOS's "Build coffee brand website". `present()` clears it, then `set_task` /
  `load_state(title, task=prompt)` set it. It's hidden at idle / New chat; `_fit` counts it.
- **Details = the rest:** `details_view.visible_entries(entries, hide_final, hide_prompt)`. It hides the
  latest turn's final reply once the done card shows it (`show_done` re-renders Details). It hides the
  latest prompt when the bubble shows it in full, unless that prompt carries screenshots. Earlier
  turns stay. If nothing is left: "Claude answered directly — no other steps."
- Tests: `test_details_view.py::test_card_and_details_never_repeat` (checked that it fails without the
  filter); `test_rail_recent.py`: a past chat shows its latest request in the bubble and its name bold.
