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

## Test
One readable script proves the logic offline (mic/network/permissions faked):
`voice_dictation_check.py` — 9 checks incl. hold-to-talk and quick-tap-latch. Run headless:
`venv\Scripts\python voice_dictation_check.py` (Windows) — the gesture checks are
platform-independent and should pass unchanged.
