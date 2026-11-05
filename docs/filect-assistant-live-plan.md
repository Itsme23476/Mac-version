# Filect Assistant (hands-free mode): implementation plan

Status: **Phase 1 BUILT and tested (2026-11-06)**, on branch `feat/live-assistant` (backup tag
`backup/pre-live-assistant-2026-11-05`). Phase 2 (speaking back) is not built.

What building it changed vs. this plan (all measured on the Mac):
- **Apple's voice processing filters the mic for every app while it runs.** So hands-free lets go of the
  mic whenever anything else records:
  - a Filect hotkey: instantly, via a hook that runs before the capture;
  - another app: within about 1 s, via CoreAudio's per-process "is recording" flag;
  - a locked screen.
  It comes back 3–4 s later. Dictation's audio was verified clean from its first 100 ms.
- **Echo cancellation works.** A full request spoken by the Mac through its speakers started nothing.
- **Cost:** the filter's CPU is mostly in macOS's audio service (about 8–10% of one core), so it's an
  on/off setting (on by default). Without it, the cost is about 2.5%.
- **Real room noise fooled the speech detector:** 80% of it looked like speech without the filter. So it
  now learns the room's noise level, and speech must stand clearly above it.
- The current "Assistant" tab is renamed **Agents**; the new tab is **Assistant**.
- The build needed a local PyInstaller rule (`pyinstaller_hooks/hook-webrtcvad.py`), because the
  built-in rule failed the whole build.

## What we agreed

- A **separate tab** in the app with a **Start / Stop** button. There's no wake word.
- While it's on, Filect listens all the time. When you speak and then pause for a set number of
  seconds, that speech runs exactly like an fn + ⌃ voice task: the router, then Claude or Codex, then the notch card.
- **Background noise and other people's voices should not be picked up.** This uses three layers:
  1. Apple's voice isolation, which also removes whatever the Mac itself is playing.
  2. Speech detection plus a volume threshold.
  3. An "is this actually a request?" check.
  A fourth layer, recognising your voice specifically, is **left out**.
- It **counts against the daily transcription limit**. Only the moments you're speaking are sent, so
  only those count.
- **Screenshots in this mode are left out for now.** You'll revisit them.
- The **notch card** shows the tasks, the same card as today.
- **Phase 2:** it speaks back, as an on/off setting, off by default.

---

## How it fits into what exists

```
 [Start] in the new tab
     │
 mic_helper child process ── voice-processed mic (no noise, no Mac's own sound) ──► 16 kHz audio
     │
 LiveListener (new) ── speech detection + volume threshold ── cuts speech into utterances
     │        (each utterance streams to the existing `transcribe-stream`; silence is never sent)
     ▼
 text ──► AssistantPage.run_voice_task(text, source="live")    ← the same entry as fn + ⌃
               │
          IntentRouter: NEW / FOLLOW_UP / RESUME / IGNORE (new, live mode only)
               │
          Claude / Codex run ──► the notch card (unchanged) + a row in the tab's activity list
```

Already built and reused as-is:
- the mic child process (`mic_helper.py`, built for the deadlock fix);
- streaming transcription (`StreamingTranscriber` plus the `transcribe-stream` edge function, which
  already counts usage from the audio actually sent);
- the router, the agents, the notch card, notifications, and the chat list.

---

## Phase 1: hands-free listening → tasks

### 1. Clean microphone audio (`app/core/mic_helper.py`)
- Add a **voice-processed capture mode** to the mic child. It uses AVAudioEngine with
  `inputNode.setVoiceProcessingEnabled_(True)`, Apple's FaceTime processing:
  - noise suppression (fans, keyboard, music);
  - **echo cancellation** (whatever the Mac plays, such as a YouTube video, is removed);
  - automatic gain.
  The child converts the audio to 16 kHz int16 mono and writes it to stdout, the same as today.
  `open_mic(cb, voice_processing=True)` selects this mode.
- It keeps the same process-death teardown as today (never stopped in-process), so the deadlock fix stays.
- **Ducking:** voice processing lowers other apps' volume by default. Set
  `voiceProcessingOtherAudioDuckingConfiguration` to the minimum (macOS 14+). *Verify in the prototype.*
- If voice processing fails to start (an odd device, older macOS), fall back to today's PortAudio capture
  and show a note in the tab: "Noise filtering unavailable".
- Mic changes (AirPods connecting, the default input switching): the child exits and the listener
  respawns it within about 1 s.
- Use this mode for hands-free listening only at first. Dictation stays as it is, and can adopt the mode
  later if it helps.

### 2. The listener (`app/core/live_listener.py`, new)
A `QThread` that owns one long-running capture and cuts it into utterances.

- **Speech detection:** `webrtcvad`, a new dependency `webrtcvad-wheels` (MIT, about 100 KB, Mac and
  Windows wheels). It checks 30 ms frames for speech, with smoothing so it needs about 200 ms of speech
  to start and treats short gaps as still speaking.
- **Volume threshold:** it ignores quiet voices, like someone across the room or a TV.
  - It keeps a running level of *your* accepted speech (the median of recent utterances).
  - Speech much quieter than that is ignored.
  - A **Sensitivity** setting (Low / Medium / High) adjusts the threshold, for a quiet or noisy room.
  - `ponytail:` this is a heuristic; if it misfires, replace it with a proper model (Silero VAD).
- **Utterance state machine:**
  - `listening` → speech starts → `hearing`:
    - open a `StreamingTranscriber`;
    - send about 300 ms of buffered pre-roll so the first word isn't clipped;
    - then send audio live.
  - Silence begins: **stop sending** (silence isn't billed) and start the countdown, which is your
    "send after N s" setting.
  - Speech resumes before the countdown ends: keep sending into the same utterance.
  - The countdown ends: finish the transcription and emit `utterance(text)` → back to `listening`.
  - Too short to be a request (under 0.6 s of speech or under 2 words, like "uh", a cough or "okay"):
    dropped without sending.
- **Pauses** (it ignores the mic but keeps the device open):
  - while any hotkey capture runs (fn dictation, fn + ⌃, fn + ⇧ search, fn + ⌥ organize), so the same
    speech isn't used twice;
  - while the screen is locked or the Mac sleeps (NSWorkspace notifications);
  - in Phase 2, while Filect is speaking.
- **Esc** while `hearing` or counting down cancels that utterance. Esc is only *observed*, never
  swallowed, so it still works in your apps.
- Signals: `state(str)`, `level(float)`, `countdown(float)`, `utterance(str)`, `error(str)`.

### 3. Reusing streaming transcription (`app/core/transcription.py`)
- Give `StreamingTranscriber` an optional **external audio source**. With `external=True` it doesn't open
  its own mic; the listener pushes chunks in with `feed(bytes)`.
- Everything else stays the same: the WebSocket to `transcribe-stream`, the batch fallback,
  custom words and language, and the usage count by bytes sent.
- Server: **no changes.** The daily 3-hour limit already counts only the audio received. When it's
  reached, the server returns 429. The listener then stops and the tab says "Daily voice limit reached —
  Filect Assistant stopped".

### 4. The "is this a request?" check (`app/core/intent_router.py`)
- Add an **IGNORE** intent, allowed only when `source == "live"`. The prompt adds: "IGNORE if this
  isn't an instruction for the assistant: talking to someone else, thinking out loud, a TV or video,
  filler."
- `run_voice_task(..., source="live")` changes in `assistant_page.py`:
  - **The notch doesn't pop open until the router says it's a real task.** Otherwise every ignored
    sentence would flash the card. While the router decides, only the small listening light pulses.
  - IGNORE → nothing runs; the tab's activity list shows "Ignored: not a request".
  - The router fails (Claude and Codex both unavailable) → in live mode **don't run it**. Show "Couldn't
    check this one — say it again or use fn + ⌃". This is safer than today's fallback of running it as a
    new task.
- Bypass and permissions: the same setting as notch tasks. The silence countdown is the chance to cancel.

### 5. The new tab (`app/ui/live_page.py`, new) plus `main_window.py`
- Sidebar entry with its own icon and a page in `page_stack` (index 6).
- **Top:** a big **Start Filect Assistant** / **Stop** button, a status line ("Listening", "Hearing
  you…", "Sending in 2 s… (Esc cancels)", "Paused — screen locked"), and a live mic level bar.
- **Settings card:**
  - **Send after I stop talking for** a slider, 1–10 s, default **2 s**;
  - **Ignore quiet voices** (Sensitivity) Low / Medium / High;
  - **Chime when a task starts** (on);
  - **Speak replies**: shown as "coming soon", greyed out until Phase 2.
- **Activity list:** one row per utterance, newest first:
  - the time · what it heard · what happened, e.g. "Started *Build landing page* in Claude",
    "Continued *Email drafts*", "Ignored: not a request" or "Error";
  - clicking a row opens that chat;
  - kept in memory for this session only, the last 100 rows.
- **Voice used today:** "≈ 14 min of 3 h", a local estimate from utterance length (the server is
  authoritative).
- Settings in `settings.py` (saved like the others): `live_send_after_sec` (2.0), `live_sensitivity`
  ("medium"), `live_chime` (True), `live_speak` (False).
- Live mode is **never on after a restart**. You click Start each time. "Start when Filect opens" can
  come later if you want it.

### 6. Notch, menu bar and hotkeys
- **Notch:** while listening, the resting handle shows a small pulsing violet light. It turns brighter
  while hearing you and shows the countdown. Real tasks use the normal card.
- **Menu bar** (the existing tray menu): "Stop Filect Assistant" / "Start Filect Assistant".
- **Hotkeys** keep working as today; the listener pauses while any of them is capturing.
- **Chime:** a short system sound when an utterance becomes a task (setting).

### 7. Telemetry (privacy rules as today: no transcript text)
- `live_started`, `live_stopped` (with duration), `live_task` (intent), `live_ignored`, `live_error`.

### 8. Tests
- **Listener, automated:** feed WAV files straight into the listener (bypassing the mic). Made with
  macOS `say` plus noise mixing:
  - one sentence;
  - a sentence with a 1 s pause under N (stays one utterance);
  - two sentences with a pause over N (two utterances);
  - a cough or "uh" (dropped);
  - a quiet far-away voice under a loud one (only the loud one);
  - Esc mid-utterance (cancelled);
  - check no silence bytes are sent.
- **Router:** IGNORE parsing; live failure means nothing runs; the notch stays closed for IGNORE.
- **Page:** Start/Stop, settings save (stubbed, never touching your settings.json), the activity rows,
  the menu-bar item, and pausing during a hotkey capture.
- **Real Mac checks I can do myself:**
  - CPU and battery while listening for 10 minutes (target under 3% CPU);
  - **echo cancellation:** the Mac speaks with `say` while listening, and nothing should be picked up;
  - a real Start → tab → notch run, driven by files.
- **What only you can test:** your real voice in the room, plus someone else talking or a call in the
  background. I'll ask you to run that after it's built.

### 9. Windows notes
Added to `Documenting-Changes-for-Windows`:
- the same listener, router change and tab;
- the mic needs Windows' own voice processing (the "Communications" capture mode with AEC/NS);
- `webrtcvad` has Windows wheels.

---

## Phase 2: it speaks back (on/off, off by default)

- **What it says, always short:**
  - when it starts: "On it — Claude's building the landing page";
  - when it's done: one or two sentences;
  - when an agent needs you: "Claude wants to delete 3 files, should it go ahead?". You answer by voice
    and the router gets APPROVE / DENY for the waiting card.
- **Never reads** code or long answers out loud; those stay on the card.
- **Summaries:** the router model turns the agent's final message into one spoken sentence.
- **Voice:** a cloud voice through a new `speak` edge function, so the key stays on the server.
  - It checks your subscription like transcription does, with its own daily cap.
  - Choose the provider by quality and price per sentence.
  - Falls back to the Mac's built-in voice when offline.
- **No talking to itself:** echo cancellation from Phase 1 removes its own voice. The listener also
  ignores the mic while it speaks, and you can **interrupt** it by talking (it stops speaking).

---

## Build order and checkpoints
1. **Prototype the clean mic and the listener**, with no UI, measuring CPU, ducking and echo
   cancellation. → Show you the numbers.
2. Streaming per utterance, the IGNORE check, and `run_voice_task("live")`.
3. The tab, the settings and the notch light. → **Your hands-on test.**
4. Windows notes, then commit. No release without your OK.
5. Phase 2 (speaking back), when you say go.

## Decisions still open
1. **Names.** There's already an "Assistant" tab (your chat list). My suggestion:
   - the new tab is **Assistant**, with the button "Start Filect Assistant";
   - the current one is renamed **Agents**.
   Alternative: the new tab is called **Live** and nothing is renamed.
2. **The default wait** after you stop talking: 2 s?
