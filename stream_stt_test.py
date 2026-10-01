#!/usr/bin/env python3
"""
Standalone xAI Grok STREAMING speech-to-text test — NO Filect app needed.

This does NOT use the Filect app. It records YOUR MICROPHONE directly, streams it
live to wss://api.x.ai/v1/stt, and prints transcripts as you speak so you can FEEL
streaming latency vs the app's batch mode (record -> upload -> paste).

Setup (one time):
    export XAI_API_KEY=xai-...            # your REAL xAI key — never hard-code it
    venv311/bin/pip install websockets    # sounddevice + numpy already installed

Run (auto-stops after 15s — no Ctrl+C needed):
    venv311/bin/python stream_stt_test.py
    venv311/bin/python stream_stt_test.py --seconds 25 --language en --keyterm Filect
    venv311/bin/python stream_stt_test.py --raw            # dump raw JSON events
    venv311/bin/python stream_stt_test.py --list-devices   # pick a mic with --device

While it runs you'll see:
    [mic] level ███·········  peak=0.42      <- proves the mic is actually hearing you
    [ 1.20s] … hello this is a               <- partial transcript, grows live
    [ 2.05s] ✓ Hello, this is a test.        <- finalized segment
A summary at the end says what happened if nothing showed (mic? key? field names?).
"""
import os
import sys
import json
import time
import asyncio
import argparse

import numpy as np
import sounddevice as sd

try:
    import websockets
except ImportError:
    sys.exit("Missing dep. Run:  venv311/bin/pip install websockets")

SAMPLE_RATE = 16000
CHUNK = SAMPLE_RATE * 100 // 1000  # 100 ms of 16 kHz mono int16


def _pick_text(ev: dict) -> str:
    for k in ("text", "transcript", "content"):
        v = ev.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--language", default="", help="force a language code, e.g. en")
    ap.add_argument("--keyterm", action="append", default=[], help="bias term (repeatable)")
    ap.add_argument("--model", default="grok-voice-transcribe-2.0")
    ap.add_argument("--seconds", type=float, default=15.0,
                    help="auto-stop after N seconds (0 = run until Ctrl+C)")
    ap.add_argument("--device", default=None, help="mic name or index (see --list-devices)")
    ap.add_argument("--raw", action="store_true", help="print raw JSON events")
    ap.add_argument("--list-devices", action="store_true", help="list input devices and exit")
    args = ap.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return

    key = os.environ.get("XAI_API_KEY", "")
    if not key or "YOUR" in key.upper():
        sys.exit("Set your REAL key first:  export XAI_API_KEY=xai-...\n"
                 "(the current value is empty or still the 'xai-YOUR_KEY' placeholder)")

    dev = args.device
    if isinstance(dev, str) and dev.isdigit():
        dev = int(dev)
    try:
        info = sd.query_devices(dev, "input")
        print(f"Mic: {info['name']}", flush=True)
    except Exception as e:
        sys.exit(f"No usable input device ({e}). List them with --list-devices.")

    params = [f"sample_rate={SAMPLE_RATE}", "encoding=pcm",
              "interim_results=true", f"model={args.model}"]
    if args.language:
        params.append(f"language={args.language}")
    for kt in args.keyterm:
        params.append(f"keyterm={kt}")
    url = "wss://api.x.ai/v1/stt?" + "&".join(params)

    loop = asyncio.get_running_loop()
    audio_q: asyncio.Queue = asyncio.Queue()
    stat = {"frames": 0, "peak_window": 0.0, "peak_all": 0.0, "events": 0}

    def cb(indata, frames, time_info, status):
        if status:
            print(f"[audio status] {status}", file=sys.stderr, flush=True)
        b = bytes(indata)
        s = np.frombuffer(b, dtype=np.int16)
        if s.size:
            pk = float(np.abs(s).max()) / 32768.0
            stat["peak_window"] = max(stat["peak_window"], pk)
            stat["peak_all"] = max(stat["peak_all"], pk)
        stat["frames"] += 1
        loop.call_soon_threadsafe(audio_q.put_nowait, b)

    # websockets renamed extra_headers -> additional_headers; support both.
    hdr = {"Authorization": f"Bearer {key}"}
    try:
        conn = websockets.connect(url, additional_headers=hdr)
    except TypeError:
        conn = websockets.connect(url, extra_headers=hdr)

    print(f"Connecting to {url.split('?')[0]} …", flush=True)
    t0 = time.time()
    try:
        ws = await conn
    except websockets.exceptions.InvalidStatus as e:
        body = ""
        try:
            body = bytes(e.response.body or b"").decode("utf-8", "replace")[:500]
        except Exception:
            pass
        sys.exit(f"Connection rejected: HTTP {e.response.status_code}\n"
                 f"xAI said: {body or '(no body)'}\n"
                 "  400 -> usually a placeholder/garbled key (export your REAL xai- key) "
                 "or a bad param.\n"
                 "  401/403 -> key valid-format but not authorized for STT.")
    except Exception as e:
        sys.exit(f"Connection failed: {e!r}")

    stop_note = f"auto-stops in {args.seconds:g}s" if args.seconds else "Ctrl+C to stop"
    print(f"Connected. Speak now — {stop_note}.\n", flush=True)
    try:

        async def sender():
            with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                   blocksize=CHUNK, device=dev, callback=cb):
                while True:
                    await ws.send(await audio_q.get())

        async def receiver():
            async for msg in ws:
                stat["events"] += 1
                if args.raw:
                    print(msg, flush=True)
                    continue
                try:
                    ev = json.loads(msg)
                except Exception:
                    print("raw:", msg, flush=True)
                    continue
                typ = ev.get("type", "?")
                dt = time.time() - t0
                txt = _pick_text(ev)
                if typ == "transcript.partial":
                    sys.stdout.write("\r\033[K" + f"[{dt:6.2f}s] … {txt}")
                    sys.stdout.flush()
                elif typ == "transcript.done":
                    sys.stdout.write("\r\033[K" + f"[{dt:6.2f}s] ✓ {txt}\n")
                    sys.stdout.flush()
                else:
                    # Show every other event too, so the screen is never silent.
                    print(f"[{dt:6.2f}s] ({typ}) {txt}".rstrip(), flush=True)

        async def meter():
            # Live mic level every 2s so you SEE the mic working before any transcript.
            while True:
                await asyncio.sleep(2.0)
                bars = int(stat["peak_window"] * 40)
                print(f"[mic] level {'█' * bars}{'·' * (40 - bars)} "
                      f"peak={stat['peak_window']:.2f}", flush=True)
                stat["peak_window"] = 0.0

        tasks = [asyncio.create_task(sender()),
                 asyncio.create_task(receiver()),
                 asyncio.create_task(meter())]
        if args.seconds:
            tasks.append(asyncio.create_task(asyncio.sleep(args.seconds)))

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, asyncio.CancelledError):
                print(f"\n[error] {exc!r}", flush=True)
    finally:
        await ws.close()

    # ---- diagnosis -----------------------------------------------------------
    print("\n--- summary ---")
    print(f"mic frames captured : {stat['frames']}   loudest peak: {stat['peak_all']:.2f}")
    print(f"events from server  : {stat['events']}")
    if stat["frames"] == 0:
        print("=> The mic delivered NO audio. Grant Microphone access to the terminal "
              "app in System Settings ▸ Privacy & Security ▸ Microphone, or pick a mic "
              "with --device (--list-devices).")
    elif stat["peak_all"] < 0.02:
        print("=> The mic was captured but heard near-silence (peak ~0). It's muted, the "
              "wrong device, or macOS is returning empty audio (grant Microphone access "
              "to the terminal app). Try --list-devices then --device <n>.")
    elif stat["events"] == 0:
        print("=> Audio went up but the server sent nothing back — likely auth or the "
              "stream params. Re-run with --raw and paste the first lines.")
    else:
        print("=> If you saw events but no words, the transcript field name differs — "
              "re-run with --raw and paste one event so I can map it.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
