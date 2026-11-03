# Voice Dictation — porting guide (Mac → Windows)

Press a shortcut, speak, and the transcript is inserted into whatever app is focused —
Wispr-Flow style. **Mode A (Dictate) is built and working.** Modes B1 (find-a-file by
voice) and B2 (organize by voice) are designed but not built yet.

**Backend is shared** (Supabase `transcribe` edge function, Grok Voice Transcribe 2.0) —
already live for Windows, see the top-level README. Everything below is **client-side**.

## Client files (Mac names → what they do)
| File | Role | Portability |
|---|---|---|
| `app/core/transcription.py` | `transcribe_audio(path)` POSTs base64 audio to the `transcribe` endpoint; `VoiceRecorder(QThread)` records mic + emits a `level` signal | **Portable** (sounddevice/scipy/requests are cross-platform). Copy ~as-is. |
| `app/ui/dictation.py` | `VoiceDictationController`: owns the hotkey, the record→transcribe→insert flow, and the **gesture state machine** | Logic is **portable**; only the hotkey + text-insert primitives are per-platform. |
| `app/ui/dictation_overlay.py` | the floating waveform pill (non-activating, floats over fullscreen) | **Per-platform** window flags (see Overlay below). |
| `app/ui/mac_spaces.py` | CGS/SkyLight `move_window_to_active_space()` — puts the pill on the current fullscreen Space | **Mac-only.** Windows doesn't need it. |

---

## 1. Hotkey with press AND release  ← the crux for hold-to-talk

The gesture (below) needs **key-down and key-up**. 

- **Mac (done):** Carbon `RegisterEventHotKey` + `InstallEventHandler` for BOTH
  `kEventHotKeyPressed (5)` and `kEventHotKeyReleased (6)`, routed by `GetEventKind`.
  `register_global_hotkey(parent, seq, on_activated, on_released=None)` — when
  `on_released` is passed and the backend supports it, the returned handle dict has
  `'supports_release': True`. Press-only callers are unchanged.

- **❌ Windows pitfall:** `RegisterHotKey` / `WM_HOTKEY` (what `win_hotkey.py` uses today)
  is **press-only** — Windows sends `WM_HOTKEY` on key-down and gives you **no key-up**.
  You cannot implement hold-to-talk with it.

- **Windows fix:** install a **low-level keyboard hook**,
  `SetWindowsHookEx(WH_KEYBOARD_LL, ...)`. In the hook proc, track the modifier state
  and the hotkey's virtual-key; emit `on_activated()` on `WM_KEYDOWN`/`WM_SYSKEYDOWN`
  of the combo and `on_released()` on `WM_KEYUP`/`WM_SYSKEYUP`. Keep the existing
  `RegisterHotKey` path for press-only hotkeys (e.g. quick-search). Add the same
  `on_released` parameter + `supports_release` flag to the Windows
  `register_global_hotkey` so `dictation.py` is identical on both platforms.
  (Alternative: poll `GetAsyncKeyState` on a fast timer — simpler but less precise; the
  hook is preferred.)

### 1b. The shortcut is the Fn / Globe key (with system override)

The dictation key is now the **Fn (Globe) key** (`settings.dictation_shortcut = 'fn'`).

- **Mac (done):** Fn is a special modifier Carbon can't bind, so `register_global_hotkey`
  routes `'fn'` to `_register_fn_tap()` — a **CGEventTap** on `kCGEventFlagsChanged` that
  watches keycode 63 (Globe) toggling the secondary-fn flag, and **returns None to CONSUME
  the event** so it overrides macOS's Globe action (emoji/dictation/input-switch) and other
  apps. Runs its own CFRunLoop on a daemon thread; pure CGEvent getters only (no
  `NSEvent.eventWithCGEvent_`). ❌ Needs Accessibility **and possibly Input Monitoring** —
  if `CGEventTapCreate` returns NULL, that grant is missing. If the Globe menu still pops,
  the user can also set System Settings → Keyboard → "Press 🌐 key to: Do Nothing".

- **❌ Windows:** there is **no portable Fn-key event** — on most laptops Fn is handled in
  hardware/firmware and never reaches the OS, so you can't bind it. Pick a different default
  key for Windows (e.g. keep a chord like `ctrl+shift+d`, or a single key like `right ctrl`)
  and capture press+release with the `WH_KEYBOARD_LL` hook from §1. Don't try to replicate
  the Globe key.

## 2. Gesture state machine  ← copy VERBATIM, it's pure Python

Both gestures at once, no setting (Wispr-Flow model). In `dictation.py` (`_on_press` /
`_on_release` / `_resolve_single_tap`, `HOLD_SEC = 0.35`, `DOUBLE_TAP_SEC = 0.30`):

- **HOLD** the key ≥ `HOLD_SEC`, then release → **push-to-talk**: records while held,
  transcribes on release.
- **DOUBLE-TAP** (2nd press within `DOUBLE_TAP_SEC`) → **latches** hands-free recording;
  a **single tap** while latched → stop + transcribe.
- **A lone quick tap** (no 2nd tap) is **discarded** — so a slightly-fast hold can't
  accidentally lock into hands-free. (`_resolve_single_tap` fires after the double-tap
  window and calls `_cancel_recording` if no 2nd tap arrived.)

Recording starts on key-**down** in every case (so a hold captures from the first moment).
State: `_state` (idle/recording/transcribing) + `_latched`, `_press_time`, `_key_down`,
`_got_second_tap`, `_ignore_next_release`. ❌ Latch on a **double**-tap, not a single tap —
single-tap-latch made fast holds lock in by accident. If the backend can't report releases
(`supports_release` false), the controller falls back to **tap-to-toggle** (`_toggle`).
This whole layer is identical on Windows — just feed it the hook's down/up events.

## 3. Overlay: float over other apps WITHOUT stealing focus

- **Mac (done):** `Qt.Tool | FramelessWindowHint | WindowStaysOnTopHint |
  WindowDoesNotAcceptFocus`, `WA_ShowWithoutActivating`; then on the NSWindow:
  `setCollectionBehavior(CanJoinAllSpaces|FullScreenAuxiliary)`, `setLevel(1000)`,
  `setHidesOnDeactivate_(False)`, `_setPreventsActivation_(True)`, `orderFrontRegardless()`,
  **and `move_window_to_active_space()` (mac_spaces.py) — this is what makes it appear on
  a fullscreen Space.** ❌ Do NOT call `raise_()` — it activates the app and steals focus.

- **Windows:** use `WS_EX_NOACTIVATE | WS_EX_TOPMOST` (via `WindowDoesNotAcceptFocus` +
  `WA_ShowWithoutActivating` + `WindowStaysOnTopHint`). Topmost floats over normal and
  borderless-fullscreen windows; there's **no CGS/Spaces concept** so `mac_spaces.py` is
  not needed. (True exclusive-fullscreen games may still cover it — acceptable.)

## 4. Inserting the text (paste) without moving focus

Because the overlay never takes focus, the user's app stays frontmost and we paste
straight into it.

- **Mac (done):** clipboard + **Cmd+V via Quartz `CGEventPost`**
  (`autofill_via_clipboard_paste`, the same primitive the quick-search popup uses). Save
  the user's clipboard first and restore it ~400ms later.
  **❌ Requires macOS Accessibility permission** — without it macOS *silently drops* the
  keystroke (clipboard is set but nothing pastes, and no error is raised). The code now
  checks `AXIsProcessTrusted()` first and, if not granted, leaves the transcript on the
  clipboard + tells the user, instead of faking success.

- **Windows:** clipboard + **Ctrl+V via `SendInput`** (VK_CONTROL + 'V'). Save/restore
  the clipboard the same way. **No "Accessibility" permission is required** for `SendInput`
  into normal apps, so all the Mac permission/signing machinery below is **not needed**.
  ❌ **UIPI pitfall:** you cannot `SendInput` into a window running at higher integrity
  (an app launched as Administrator) unless your app is also elevated — the paste will
  silently do nothing there, same visible symptom as the Mac permission issue.

## 5. Permissions & signing — **Mac-only, skip on Windows**

The long Mac saga (Accessibility grant not sticking) does **not** apply to Windows,
because `SendInput` needs no per-app grant. For the record, the Mac root causes were:
①ad-hoc signing gives a new identity each build so TCC won't persist the grant → sign
with a stable identity; ②a **dev build sharing the release app's bundle id**
(`com.filect.filesearch`) makes TCC bind the grant to the installed app, not the dev
build → the dev build uses id `com.filect.filesearch.dev` / name "Filect Dev" (via
`FILECT_DEV=1` in `Filect.spec`). Windows has no equivalent — ignore all of this.

---

## ❌ Build-environment pitfall (BOTH platforms — bit us hard)

**Do not build inside a cloud-synced folder.** On Mac, the repo was under an
iCloud-synced `~/Desktop`, and every PyInstaller file op went through the iCloud File
Provider — builds went from ~90s to *30+ minutes / effectively hung*. Fix: build from a
non-synced path (we use `~/Developer/Mac_app`).

**Windows equivalent: OneDrive.** If the repo lives under a OneDrive-synced Desktop or
Documents (Known Folder Move), you'll hit the identical slowdown. Build from a
non-synced path like `C:\dev\App-interface`. Also keep ≥15–20 GB free; a near-full disk
compounds it.

## 6. Voice SEARCH (Mode B1) — find a file by talking

Built on top of dictation. Gesture: **Fn + Shift** (Shift held at Fn-down → search mode;
Fn alone → dictate). The pill stays brand-purple but shows a 🔍 in search mode (color is
kept consistent with the results popup; the icon marks the mode).

**Flow:** hold Fn+Shift → speak → transcribe → **distill the spoken sentence into keywords**
→ open the app's existing quick-search popup, **showing the spoken sentence but searching
on the distilled keywords** → user picks a result / reveal-in-Finder (all reused).

**Query distillation (two layers):**
1. **Free phrase-list cleaning** in `query_parser.parse_query` (shared, portable) strips
   conversational lead-ins/trailers ("can you", "find me", "in my machine", …). Handles the
   common cases at zero cost/latency.
2. **LLM distillation** via a **new shared edge function `distill-query`** (SHARED BACKEND —
   already deployed + the `voice_search_usage` table migration already applied; Windows just
   calls it). Client POSTs `{text}` → gets `{query, distilled}`. It: auths, gates on
   `get_entitlement`, enforces a **per-user 50,000/month cap** (`voice_search_usage` +
   `increment_voice_search` RPC), then calls OpenAI `gpt-4o-mini` (`OPENAI_API_KEY` secret).
   ❌ Over the cap or on ANY error it returns the raw text — the client always falls back to
   the raw transcript so search never breaks. Cost is negligible (~$0.00003/call).

**Display vs. search decoupling:** the quick-search overlay got a `run_voice_query(display,
search)` method — sets the box text to `display` (what was said) with signals blocked, then
runs the worker on `search` (distilled). `_run_search` prefers a one-shot `_voice_query`.
Windows' quick-search widget needs the same tiny decouple.

**Per-platform:**
- Hotkey: Fn+Shift is Mac-only. Windows has no OS-visible Fn — pick a different chord and
  detect its modifier at key-down in the `WH_KEYBOARD_LL` hook (§1).
- The distill client call + the phrase-list parser are portable Python; copy as-is.
- Shared backend (do NOT redeploy from Windows): `distill-query` function, `voice_search_usage`
  table, `increment_voice_search` RPC, `OPENAI_API_KEY` secret.

## 7. "Voice randomly stops working until restart" — two fixes (Mac, 2026-11-04)

**What happened on Mac.** About once every ~500 Fn presses, one recording froze. After that,
every press hung and showed "Transcription timed out — tap to try again" until Filect was
restarted. While fixing it, a second bug showed up: a crash during fast tapping. Both are
fixed on Mac. Neither has been checked against the Windows code from the Mac side, so
**check the Windows code and logs yourself, using the steps below.**

### Step 1 — does Windows have it? Check these
1. **Code.** Does any voice capture use an in-process `sounddevice` stream (`sd.InputStream` /
   `RawInputStream`, `with sd.InputStream(...)`), started and stopped for every recording?
   Check `StreamingTranscriber` / `VoiceRecorder` in `app/core/transcription.py`, and
   `VoiceRecordWorker` in `app/ui/organize_page.py`. If yes, the app is exposed to the same
   class of hang, so apply **7a**.
2. **Logs.** This is the Mac freeze signature in the app log: a recording logs
   `mode decided: …` but never `[STREAM] FINAL …`, and every later press logs `Fn press …` but
   never `[STREAM] connected …`, then `transcription watchdog fired`. Search the Windows log for
   the same pattern (the watchdog line especially).
3. **Any capture that's a QThread subclass** (`StreamingTranscriber`, `VoiceRecordWorker`):
   apply **7b**. It's a Qt/PySide bug, not an OS one.

### 7a. Mic stop deadlock → move the mic into a helper process
- **Mac cause** (proven with a stack sample of the frozen app): `Pa_StopStream →
  AudioOutputUnitStop` and CoreAudio's IO thread (PortAudio's `startStopCallback`) deadlock
  on each other. That's PortAudio's macOS backend. The process's audio device stays locked,
  so every later `Pa_OpenStream` blocks forever, and nothing short of a restart recovers it.
  sounddevice 0.5.6 is the latest; there's no upstream fix.
- Windows PortAudio uses different backends (WASAPI/MME/DirectSound), so that exact deadlock
  is Mac-specific. It's unverified whether Windows can hang on stop in some other way. The fix
  below is plain cross-platform Python, and it makes **any** stop hang harmless, so apply it if
  Step 1 shows in-process capture.
- **How it works:** the mic lives in a throwaway child process (the app's own exe started with
  `FILECT_MODE=mic-helper`). It streams raw int16 PCM over stdout, never calls stop, and exits
  with `os._exit`. Process death can't hang. The parent kills a child that doesn't exit within
  1.5s. A spare child is pre-loaded (sounddevice imported, mic NOT open), so recording starts as
  fast as before (~0.2s).
- **New file `app/core/mic_helper.py`** (Mac version, verbatim):
```python
import os
import subprocess
import sys
import threading
from pathlib import Path

SAMPLE_RATE = 16000
CHUNK = 1600           # samples per block (100 ms) — same block size the in-process stream used

_spare = None
_spare_lock = threading.Lock()


def serve():
    """Child (FILECT_MODE=mic-helper): wait for 'go', then write mic PCM to stdout until 'stop'/EOF."""
    import sounddevice as sd
    out = sys.stdout.buffer
    if not sys.stdin.readline().startswith("go"):    # parent gone / spare discarded
        os._exit(0)
    lock, done = threading.Lock(), [False]

    def cb(indata, frames, time_info, status):
        with lock:
            if done[0]:
                return
            try:
                out.write(bytes(indata))
                out.flush()
            except Exception:
                os._exit(0)

    try:
        sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                          blocksize=CHUNK, callback=cb).start()
    except Exception:
        os._exit(3)                                  # no mic → parent sees EOF with no audio
    sys.stdin.readline()                             # "stop", or EOF if the parent died
    with lock:                                       # no half-written block after this
        done[0] = True
    os._exit(0)                                      # never Pa_StopStream: that's the call that deadlocks


def _spawn():
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]                       # .app/Contents/MacOS/Filect
    else:
        cmd = [sys.executable, str(Path(__file__).resolve().parents[2] / "main.py")]
    return subprocess.Popen(cmd, env={**os.environ, "FILECT_MODE": "mic-helper"},
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)


def warm():
    """Make sure a loaded helper is waiting, so the next capture starts with no delay."""
    global _spare
    with _spare_lock:
        if _spare is None or _spare.poll() is not None:
            _spare = _spawn()


def open_mic(on_chunk):
    """Start capturing. on_chunk(bytes) runs on a reader thread for every block.
    Returns stop(): ends the capture after every block already captured has been delivered."""
    global _spare
    with _spare_lock:
        p, _spare = _spare, None
    if p is None or p.poll() is not None:
        p = _spawn()
    p.stdin.write(b"go\n")
    p.stdin.flush()

    def pump():
        while True:
            b = p.stdout.read(CHUNK * 2)             # buffered: a whole block, or the tail at EOF
            if not b:
                return
            on_chunk(b)

    t = threading.Thread(target=pump, daemon=True, name="mic-pump")
    t.start()
    warm()                                           # the next capture's helper loads meanwhile

    def stop():
        try:
            p.stdin.write(b"stop\n")
            p.stdin.flush()
        except Exception:
            pass
        t.join(1.5)
        if p.poll() is None:
            p.kill()
        p.wait()
        t.join(0.5)                                  # pump sees EOF once the child is gone
        for f in (p.stdin, p.stdout):
            try:
                f.close()
            except Exception:
                pass

    return stop
```
- **Windows deltas** (Mac-side Claude can't test these, so verify them):
  - Add `creationflags=subprocess.CREATE_NO_WINDOW` to the `Popen` in `_spawn()`, or a console
    window may flash on every recording.
  - In a `--windowed` PyInstaller exe, `sys.stdout` can be `None`. With the parent passing
    pipes it should be set, but check it. If it's `None` in the child, write to
    `os.fdopen(1, "wb")` instead of `sys.stdout.buffer`.
  - `parents[2] / "main.py"` assumes `ai_file_organizer/app/core/mic_helper.py`; adjust if the
    Windows layout differs.
- **`main.py`**: add this right after the `sys.path.insert(...)` line, BEFORE the PySide6/GUI
  imports, so the child stays small:
  ```python
  # Mic helper child (see app/core/mic_helper.py): branch before the GUI imports so it stays tiny.
  if os.environ.get("FILECT_MODE") == "mic-helper":
      from app.core.mic_helper import serve
      serve()
  ```
- **`StreamingTranscriber._session`**: replace the `sd.InputStream(...)` +
  `self._stream.start()` with `self._stream = mic_helper.open_mic(cb)`. `cb(data: bytes)` does
  `chunk = np.frombuffer(data, dtype=np.int16)` → `self._chunks.append(chunk)`,
  `self._outbox.append(data)`, `self.level.emit(_rms_level(chunk))`. Drop the old
  `if self._mic_stopped: return` check: stop() delivers the tail and joins the reader, so nothing
  arrives after it. `_stop_mic()` calls `self._stream()` (the returned stop function) instead of
  `.stop()/.close()`.
- **`VoiceRecordWorker.run`** (Organize page mic button): `stop = mic_helper.open_mic(lambda d:
  self.audio_data.append(np.frombuffer(d, dtype=np.int16)))`, then `while self.is_recording:
  time.sleep(0.1)` inside `try/finally: stop()`.
- **Warm-up**: call `mic_helper.warm()` once at startup (Mac does it in the dictation
  controller's `__init__`, regardless of whether dictation is enabled).
- **Test** `tests/test_mic_helper.py`: fake children, no mic. It checks that every block
  arrives, that a hung child is killed in under 3s, and that the spare is reused. It's
  platform-independent.
- Mac live results: two bursts of 44 rapid presses with no hang; 60 rapid open/stop cycles,
  slowest stop 0.06s; worked in the packaged app too.

### 7b. Crash: "QThread: Destroyed while thread '' is still running" → keep captures alive
- **Cause:** when a quick tap is cancelled (or the mic button is re-clicked mid-transcription),
  the app drops the last reference to a still-running capture QThread. When its `run()`
  returns, PySide deletes the QThread from inside its own thread, and Qt aborts the whole app
  (exit 134).
- The old code only survived by accident: a reference cycle
  (`self._stream (sd.InputStream) → callback closure → self`) kept the object alive. 7a removes
  that cycle, so **7a without 7b crashes.** ❌ pitfall: don't rely on that cycle.
- **Fix** (in `app/core/transcription.py`, verbatim):
  ```python
  _RUNNING = []   # capture QThreads kept alive until their thread has fully finished


  def keep_until_finished(thread):
      _RUNNING[:] = [t for t in _RUNNING if not t.isFinished()]   # pruned on the UI thread
      _RUNNING.append(thread)
  ```
  Then call `keep_until_finished(self)` as the first line of `StreamingTranscriber.start()`, and
  add the same `start()` override to `VoiceRecordWorker`:
  ```python
  def start(self):
      from app.core.transcription import keep_until_finished
      keep_until_finished(self)
      super().start()
  ```
- **Test** `tests/test_transcriber_lifetime.py`: 20 captures, each reference dropped mid-run,
  asserts no abort. It's pure Qt, so it runs on Windows unchanged. Without the fix it prints the
  exact abort message.

## Test
One readable script proves the logic offline (mic/network/permissions faked):
`voice_dictation_check.py` — 9 checks incl. hold-to-talk and quick-tap-latch. Run headless:
`venv\Scripts\python voice_dictation_check.py` (Windows) — the gesture checks are
platform-independent and should pass unchanged.
