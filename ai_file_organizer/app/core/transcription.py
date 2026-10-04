"""
Filect Voice — client-side dictation service.

Two pieces:
  - transcribe_audio(path): POSTs recorded audio to the `transcribe` Supabase edge
    function (Grok Voice Transcribe 2.0, server-side key, subscription-gated).
  - VoiceRecorder(QThread): records the mic, emits a live amplitude `level` signal
    (for the Phase-3 animation), and on stop transcribes + emits the text.

Kept separate from organize_page's existing VoiceRecordWorker (which still uses the
older OpenAI-whisper path) so the working Organize voice button is untouched; the
two can be merged later.
"""
import os
import base64
import logging
import tempfile
import subprocess
from typing import Optional, Dict, Any

import requests
from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)

# Dev diagnostics: when FILECT_DEV is set, the streaming path logs the full
# partial-by-partial progression + final text so transcription issues ("missing
# words", cut tails) can be pinpointed. Off in release so transcripts aren't logged.
_DEV = bool(os.environ.get("FILECT_DEV"))

SUPABASE_URL = "https://gsvccxhdgcshiwgjvgfi.supabase.co"
TRANSCRIBE_URL = f"{SUPABASE_URL}/functions/v1/transcribe"
STREAM_URL = (f"{SUPABASE_URL}/functions/v1/transcribe-stream"
              .replace("https://", "wss://"))  # WebSocket streaming proxy
DISTILL_URL = f"{SUPABASE_URL}/functions/v1/distill-query"
CLEAN_URL = f"{SUPABASE_URL}/functions/v1/clean-transcript"

SAMPLE_RATE = 16000  # 16 kHz mono — plenty for speech, small payloads


def _get_auth_token() -> Optional[str]:
    """Current user's Supabase access token — the LIVE, auto-refreshed one.

    Uses supabase_auth.get_access_token() (which pulls from the gotrue client's
    session and refreshes near expiry) rather than the cached _access_token, which
    went stale on background token rotation and caused 401 'session expired'."""
    try:
        from .supabase_client import supabase_auth
        if not supabase_auth.is_authenticated:
            return None
        return supabase_auth.get_access_token()
    except Exception as e:
        logger.error(f"Failed to get auth token: {e}")
        return None


def transcribe_audio(audio_path: str, language: Optional[str] = None,
                     terms: Optional[list] = None) -> Dict[str, Any]:
    """
    Send an audio file to the transcribe proxy and return a result dict:
        {ok: True,  text: str, duration: float}
        {ok: False, error: <code>, message: <human message>}
    error codes: not_authenticated | no_subscription | rate_limited | provider | network

    `terms` = the user's Custom Words (key-term biasing) — names/jargon to spell right.
    """
    token = _get_auth_token()
    if not token:
        return {"ok": False, "error": "not_authenticated",
                "message": "Please sign in to use voice dictation."}
    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
    except Exception as e:
        return {"ok": False, "error": "network", "message": f"Could not read audio: {e}"}

    # Send the audio as a RAW binary body (not base64-in-JSON) — base64 inflates the
    # payload ~33%, so raw bytes upload faster, which mainly helps slow connections.
    # Filename + optional language ride along in headers.
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "X-Audio-Filename": os.path.basename(audio_path),
    }
    if language:
        headers["X-Audio-Language"] = language
    if terms:
        # Custom Words → Grok key-term biasing (comma-separated header, capped).
        joined = ",".join(str(t).strip() for t in terms if str(t).strip())
        if joined:
            headers["X-Audio-Terms"] = joined[:4000]

    try:
        r = requests.post(
            TRANSCRIBE_URL,
            data=audio_bytes,
            headers=headers,
            timeout=45,
        )
    except Exception as e:
        logger.error(f"Transcription request failed: {e}")
        return {"ok": False, "error": "network", "message": "Network error — check your connection."}

    if r.status_code == 200:
        data = r.json()
        text = (data.get("text") or "").strip()
        logger.info(f"Transcription succeeded: {len(text)} chars")
        return {"ok": True, "text": text,
                "duration": float(data.get("duration") or 0)}
    if r.status_code == 401:
        return {"ok": False, "error": "not_authenticated",
                "message": "Your session expired — please sign in again."}
    if r.status_code == 403:
        return {"ok": False, "error": "no_subscription",
                "message": "An active subscription is required for voice dictation."}
    if r.status_code == 429:
        return {"ok": False, "error": "rate_limited",
                "message": "Daily voice limit reached. Try again tomorrow."}
    logger.error(f"Transcribe proxy error {r.status_code}: {r.text[:300]}")
    return {"ok": False, "error": "provider",
            "message": "Transcription failed — please try again."}


def distill_search_query(text: str) -> Optional[str]:
    """Turn a spoken search sentence into tight keywords via the distill-query edge
    function (LLM). Returns the cleaned query, or None on ANY failure/cap so the caller
    falls back to the raw transcript (search still works)."""
    text = (text or "").strip()
    if not text:
        return None
    token = _get_auth_token()
    if not token:
        return None
    try:
        r = requests.post(
            DISTILL_URL,
            json={"text": text},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=10,
        )
        if r.status_code == 200:
            q = (r.json().get("query") or "").strip()
            return q or None
        logger.warning(f"distill-query returned {r.status_code}")
    except Exception as e:
        logger.warning(f"distill_search_query failed: {e}")
    return None


def _normalize_peak(audio, target: float = 0.95, max_gain: float = 8.0):
    """Boost a too-quiet recording so soft speech reaches the model at a strong, even
    level — soft speech otherwise transcribes worse than loud speech.

    It's a LINEAR peak gain: it preserves the waveform (so it never changes *what* was
    said), only ever RAISES the level (never attenuates), scales the peak up to `target`
    of full scale (below clipping, so no distortion), and caps the gain so a near-silent
    clip (just room noise) isn't blown up. Already-loud/normal speech (peak within ~1%
    of target) is returned unchanged. Returns int16."""
    import numpy as np
    if audio is None or len(audio) == 0:
        return audio
    peak = int(np.max(np.abs(audio.astype(np.int32))))
    if peak <= 0:
        logger.info("[NORMALIZE] silent clip (peak=0) — unchanged")
        return audio  # silence — nothing to boost
    frac = peak / 32767.0
    raw_gain = (target * 32767.0) / peak
    if raw_gain <= 1.01:
        logger.info(f"[NORMALIZE] peak={frac:.2f} of full scale — already loud enough, unchanged")
        return audio  # already loud enough; leave normal/loud speech alone
    gain = min(raw_gain, max_gain)
    capped = " (capped)" if raw_gain > max_gain else ""
    logger.info(f"[NORMALIZE] quiet clip peak={frac:.2f} -> boosted x{gain:.1f}{capped}")
    return np.clip(audio.astype(np.float32) * gain, -32768.0, 32767.0).astype(np.int16)


def clean_transcript(text: str, level: str = "light", terms: Optional[list] = None) -> str:
    """Polishing (optional): polish a dictation transcript via the clean-transcript edge
    function (gpt-4o-mini). `level`: 'light' = strip filler/stumbles + fix punctuation;
    'polished' = light + smooth phrasing. `terms` = the user's Custom Words, sent so the
    cleanup NEVER alters them (otherwise the LLM "corrects" e.g. 'Filect' -> 'Firefox').
    Returns the cleaned text, or the ORIGINAL on any failure — polishing must never break
    dictation, so this always returns usable text."""
    text = (text or "").strip()
    if not text or level not in ("light", "polished"):
        return text
    token = _get_auth_token()
    if not token:
        return text
    payload = {"text": text, "level": level}
    if terms:
        payload["terms"] = [str(t).strip() for t in terms if str(t).strip()][:200]
    try:
        r = requests.post(
            CLEAN_URL,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=12,
        )
        if r.status_code == 200:
            cleaned = (r.json().get("text") or "").strip()
            return cleaned or text
        logger.warning(f"clean-transcript returned {r.status_code}")
    except Exception as e:
        logger.warning(f"clean_transcript failed: {e}")
    return text


def _rms_level(chunk_int16) -> float:
    """Normalized 0..1 loudness of an int16 audio chunk (drives the animation)."""
    import numpy as np
    if chunk_int16 is None or len(chunk_int16) == 0:
        return 0.0
    x = chunk_int16.astype("float32") / 32768.0
    rms = float(np.sqrt(np.mean(x * x)))
    # Speech RMS is small; scale so normal talking lands mid-range, clamp to 1.0.
    return min(1.0, rms * 4.0)


def _pick_text(ev: dict) -> str:
    """Pull the transcript string out of an xAI STT event regardless of field name."""
    for k in ("text", "transcript", "content"):
        v = ev.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _output_muted():
    """Current macOS system-output muted state (True/False), or None if it can't be read."""
    try:
        r = subprocess.run(["osascript", "-e", "output muted of (get volume settings)"],
                           capture_output=True, text=True, timeout=2)
        return r.stdout.strip().lower() == "true"
    except Exception:
        return None


def _set_output_muted(muted: bool):
    try:
        subprocess.run(["osascript", "-e",
                        "set volume output muted " + ("true" if muted else "false")],
                       capture_output=True, timeout=2)
    except Exception:
        pass


class VoiceRecorder(QThread):
    """Records mic audio until stop_recording(), then transcribes via Grok."""
    finished = Signal(str)          # transcribed text
    error = Signal(str)             # human-readable message
    recording_stopped = Signal()    # mic released (before transcription result)
    level = Signal(float)           # live amplitude 0..1, for the animation

    def __init__(self, language: Optional[str] = None, device=None, terms=None):
        super().__init__()
        self.language = language
        self.terms = terms  # Custom Words (key-term biasing) for this capture
        self.device = device  # sounddevice input device index/name; None = system default
        self.is_recording = False
        self._chunks = []

    def start(self):
        # Set the flag BEFORE the thread runs. Otherwise a very fast stop_recording()
        # (rapid back-to-back dictations) could land before run()'s own assignment, which
        # would then re-set it True — leaving the capture loop running forever and the pill
        # stuck on "transcribing". See run().
        self.is_recording = True
        super().start()

    def stop_recording(self):
        self.is_recording = False

    def _mute_output_if_enabled(self):
        """Mute system output while recording (Voice setting, on by default) so a background
        video/music doesn't bleed into the mic. Remembers the prior state to restore it."""
        self._prior_muted = None
        try:
            from app.core.settings import settings as _s
            if not getattr(_s, "dictation_mute_while_recording", True):
                return
            self._prior_muted = _output_muted()
            if self._prior_muted is not None:
                _set_output_muted(True)
        except Exception:
            self._prior_muted = None

    def _restore_output(self):
        try:
            if getattr(self, "_prior_muted", None) is not None:
                _set_output_muted(self._prior_muted)
        except Exception:
            pass
        self._prior_muted = None

    def run(self):
        try:
            import sounddevice as sd
            import numpy as np
            from scipy.io import wavfile
            import time
        except ImportError as e:
            self.error.emit(f"Missing audio library: {e}")
            return

        # NOTE: is_recording is set True in start() (before this thread runs) — do NOT set it
        # here or we reintroduce the start/stop race.
        self._chunks = []

        def cb(indata, frames, time_info, status):
            if self.is_recording:
                self._chunks.append(indata.copy())
                try:
                    self.level.emit(_rms_level(indata))
                except Exception:
                    pass

        self._mute_output_if_enabled()   # mute system audio so a bg video doesn't bleed into the mic
        try:
            logger.info(f"Recording started (device={self.device if self.device is not None else 'default'})")
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                device=self.device, callback=cb):
                _t0 = time.monotonic()
                while self.is_recording:
                    sd.sleep(50)
                    if time.monotonic() - _t0 > 300:   # 5-min hard cap so it can never hang
                        logger.warning("recorder: hit max-duration cap; stopping")
                        break
        except Exception as e:
            logger.error(f"Mic capture failed: {e}")
            self.error.emit("Could not access the microphone.")
            return
        finally:
            self._restore_output()       # unmute the instant recording stops (before transcription)

        logger.info(f"recorder: capture loop ended ({len(self._chunks)} chunks)")
        self.recording_stopped.emit()

        if not self._chunks:
            self.error.emit("No audio recorded.")
            return

        audio = np.concatenate(self._chunks, axis=0)
        # Even out volume so soft speech isn't transcribed worse than loud speech.
        audio = _normalize_peak(audio)
        logger.info(f"Captured {len(audio) / SAMPLE_RATE:.1f}s of audio; transcribing…")
        tmp = None
        try:
            # Encode FLAC: lossless (identical transcripts — zero accuracy cost) and
            # ~2-3x smaller than WAV, so a smaller upload. Mainly helps users on slow
            # connections; no effect on quality. Falls back to WAV if FLAC is missing.
            try:
                import soundfile as sf
                with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
                    tmp = f.name
                sf.write(tmp, audio, SAMPLE_RATE, format="FLAC", subtype="PCM_16")
            except Exception as e:
                logger.warning(f"FLAC encode unavailable ({e}); sending WAV instead")
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    tmp = f.name
                    wavfile.write(tmp, SAMPLE_RATE, audio)
            result = transcribe_audio(tmp, language=self.language, terms=self.terms)
            if result.get("ok"):
                self.finished.emit(result.get("text", ""))
            else:
                self.error.emit(result.get("message", "Transcription failed."))
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass


class StreamingTranscriber(QThread):
    """Drop-in replacement for VoiceRecorder that STREAMS audio to the
    `transcribe-stream` edge function while you speak, so the final transcript is
    ready the instant you release the key (no post-release whole-clip upload).

    Same signals + constructor + start()/stop_recording() as VoiceRecorder, so
    dictation.py needs no other changes. "Type final once": we surface only the
    final text (via `finished`), never live partials.

    Non-breaking by design: the mic is ALWAYS buffered locally, and on ANY
    streaming problem (ws lib missing, connect fails, mid-stream drop, no events,
    auth) it falls back to the exact batch path (`transcribe_audio`) — i.e. today's
    behaviour. Streaming is a pure latency optimization layered on top.
    """
    finished = Signal(str)          # final transcribed text
    error = Signal(str)             # human-readable message
    recording_stopped = Signal()    # mic released (before transcription result)
    level = Signal(float)           # live amplitude 0..1, for the animation
    capped = Signal()               # hit the max-duration safety cap (text is still saved)

    # On key-release we send {"type":"audio.done"} and wait for xAI's final, fully-stitched
    # transcript.done — authoritative and complete across multiple sentences (partials
    # reset per-utterance, so they can't be concatenated reliably). Breaks the instant
    # transcript.done arrives; falls back to batch if it never does.
    _DONE_WAIT = 3.0
    _MAX_RECORD_SEC = 600   # 10-min hard cap (runaway / stuck-hotkey safety)

    def __init__(self, language: Optional[str] = None, device=None, terms=None):
        super().__init__()
        self.language = language
        self.terms = terms
        self.device = device
        self.is_recording = False
        self._chunks = []            # full int16 audio (for the batch fallback)
        self._outbox = []            # raw PCM byte chunks queued for streaming
        self._stream = None          # sounddevice InputStream
        self._mic_stopped = False
        self._stopped_emitted = False
        self._prior_muted = None

    def start(self):
        # Set the flag BEFORE the thread runs (same start/stop race guard as
        # VoiceRecorder — rapid back-to-back dictations).
        self.is_recording = True
        super().start()

    def stop_recording(self):
        self.is_recording = False

    # --- system-output mute while recording (mirrors VoiceRecorder) -----------
    def _mute_output_if_enabled(self):
        self._prior_muted = None
        try:
            from app.core.settings import settings as _s
            if not getattr(_s, "dictation_mute_while_recording", True):
                return
            self._prior_muted = _output_muted()
            if self._prior_muted is not None:
                _set_output_muted(True)
        except Exception:
            self._prior_muted = None

    def _restore_output(self):
        try:
            if getattr(self, "_prior_muted", None) is not None:
                _set_output_muted(self._prior_muted)
        except Exception:
            pass
        self._prior_muted = None

    def _emit_stopped_once(self):
        if not self._stopped_emitted:
            self._stopped_emitted = True
            self.recording_stopped.emit()

    def _stop_mic(self):
        if self._mic_stopped:
            return
        self._mic_stopped = True
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass

    def _build_url(self) -> str:
        from urllib.parse import quote
        params = []
        if self.language:
            params.append(f"language={quote(str(self.language))}")
        if self.terms:
            joined = ",".join(str(t).strip() for t in self.terms if str(t).strip())
            if joined:
                params.append(f"terms={quote(joined[:4000])}")
        q = ("?" + "&".join(params)) if params else ""
        return f"{STREAM_URL}{q}"

    def run(self):
        try:
            import sounddevice as sd  # noqa: F401
            import numpy as np        # noqa: F401
        except ImportError as e:
            self.error.emit(f"Missing audio library: {e}")
            return

        # NOTE: muting is deliberately NOT done here — it runs osascript (slow) and would
        # delay the mic start, losing the opening words. _session() starts the mic FIRST,
        # then mutes while the mic is already capturing. _restore_output() stays idempotent.
        stream_text, stream_ok = None, False
        try:
            import asyncio
            stream_text, stream_ok = asyncio.run(self._session())
        except Exception as e:
            logger.warning(f"[STREAM] session crashed ({e}); using batch fallback")
        finally:
            self._stop_mic()
            self._restore_output()
            self._emit_stopped_once()

        if stream_ok and stream_text is not None:
            logger.info(f"[STREAM] final via streaming: {len(stream_text)} chars")
            self.finished.emit(stream_text)
            return
        # Fallback → exactly today's behaviour on the buffered audio.
        self._finish_via_batch()

    async def _session(self):
        """Returns (final_text, ok). ok=False means 'use the batch fallback'.
        Keeps the mic buffering in every failure mode so no speech is lost."""
        import asyncio
        import sounddevice as sd

        def cb(indata, frames, time_info, status):
            # Capture until the mic is actually STOPPED, not the instant the key is
            # released. At release ~100ms of already-spoken audio is still buffered in
            # PortAudio; dropping it would clip the tail of the last word. This is the
            # symmetric counterpart to starting the mic first for the opening words.
            if self._mic_stopped:
                return
            self._chunks.append(indata.copy())     # for the batch fallback
            self._outbox.append(indata.tobytes())  # for streaming
            try:
                self.level.emit(_rms_level(indata))
            except Exception:
                pass

        # Start the mic FIRST — before muting, auth, or connecting — so not one opening
        # word is lost. The mic runs on PortAudio's own thread, so it keeps capturing into
        # _outbox/_chunks while the (slow) mute + auth + WS connect happen below; the
        # backlog is flushed to xAI the instant it opens.
        try:
            self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                          dtype="int16", blocksize=1600,
                                          device=self.device, callback=cb)
            self._stream.start()
        except Exception as e:
            logger.error(f"[STREAM] mic start failed: {e}")
            self.error.emit("Could not access the microphone.")
            self._emit_stopped_once()
            # Mic never started → nothing to batch either; signal handled.
            return "", True

        # Mic is capturing now — do the slow setup (mute, auth, connect) while it buffers.
        self._mute_output_if_enabled()

        # Resolve streaming prerequisites. If a prerequisite is missing, DON'T bail — leave
        # ws=None so the mic keeps recording the full clip and batch transcribes on release
        # (exactly the non-streaming behaviour), instead of truncating mid-sentence.
        ws = None
        t_conn0 = asyncio.get_event_loop().time()
        token = _get_auth_token()
        try:
            import websockets
        except ImportError as e:
            logger.warning(f"[STREAM] websockets missing ({e}); batch fallback")
            websockets = None
        if token and websockets is not None:
            try:
                ws = await asyncio.wait_for(
                    websockets.connect(self._build_url(),
                                       additional_headers={"Authorization": f"Bearer {token}"},
                                       max_size=None),
                    timeout=8)
                logger.info(f"[STREAM] connected in {asyncio.get_event_loop().time()-t_conn0:.2f}s "
                            f"({len(self._outbox)} chunks buffered while connecting)")
            except Exception as e:
                logger.warning(f"[STREAM] connect failed ({e}); batch fallback")
                ws = None
        else:
            logger.info(f"[STREAM] prerequisites missing (token={bool(token)}, "
                        f"ws_lib={websockets is not None}); batch fallback")

        # xAI STT protocol (verified): transcript.partial events carry is_final/speech_final
        # flags. Each spoken utterance (separated by a pause) is independent — its interim
        # partials are cumulative WITHIN that utterance, and when the speaker pauses xAI emits
        # a `speech_final: true` partial = that utterance's complete text. transcript.done's
        # text is EMPTY (terminal marker only). So the full transcript = the speech_final
        # texts concatenated, in order, + any still-interim trailing utterance.
        utterances = []                # finalized utterance texts (speech_final=true)
        cur = {"t": ""}                # current in-progress utterance (latest interim/chunk-final)
        got_event = {"v": False}
        done_flag = {"v": False}       # transcript.done terminal marker seen
        n_part = {"v": 0}

        async def receiver():
            try:
                async for msg in ws:
                    got_event["v"] = True
                    try:
                        ev = __import__("json").loads(msg)
                    except Exception:
                        continue
                    typ = ev.get("type")
                    if typ == "transcript.partial":
                        txt = _pick_text(ev)
                        n_part["v"] += 1
                        if ev.get("speech_final"):
                            if txt:
                                utterances.append(txt)     # this utterance is complete
                            cur["t"] = ""                  # next partial begins a new utterance
                        else:
                            cur["t"] = txt                 # interim / chunk-final for current utterance
                        if _DEV:
                            logger.info(f"[STREAM] partial #{n_part['v']} "
                                        f"is_final={ev.get('is_final')} speech_final={ev.get('speech_final')}: {txt!r}")
                    elif typ == "transcript.done":
                        done_flag["v"] = True
                        if _DEV:
                            logger.info("[STREAM] transcript.done (terminal marker)")
                    elif typ == "error":
                        logger.warning(f"[STREAM] upstream error event: {ev}")
                    elif _DEV:
                        logger.info(f"[STREAM] event {typ}: {str(ev)[:120]}")
            except Exception as e:
                logger.info(f"[STREAM] receiver ended: {e}")

        streaming = ws is not None
        rx = asyncio.create_task(receiver()) if streaming else None
        cursor = 0

        # Pump audio up while the key is held. The MAX-duration cap is a runaway guard
        # (stuck hotkey / absurdly long hold) matching VoiceRecorder's — on hit we just
        # finalize what we have, same as a release.
        t_rec0 = asyncio.get_event_loop().time()
        while self.is_recording:
            if asyncio.get_event_loop().time() - t_rec0 > self._MAX_RECORD_SEC:
                logger.warning(f"[STREAM] hit {self._MAX_RECORD_SEC}s max-duration cap; finalizing")
                try:
                    self.capped.emit()   # tell the UI (text spoken so far is still finalized/saved)
                except Exception:
                    pass
                break
            if streaming:
                while cursor < len(self._outbox):
                    try:
                        await ws.send(self._outbox[cursor])
                        cursor += 1
                    except Exception as e:
                        logger.warning(f"[STREAM] send failed ({e}); batch fallback")
                        streaming = False
                        if rx:
                            rx.cancel()
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        break
            await asyncio.sleep(0.02)

        # --- released: capture the final in-flight tail, THEN stop the mic --------
        # PortAudio buffers ~100ms, so at release the tail of the last word is still in
        # flight. Let it arrive (the cb captures until _stop_mic) before stopping — the
        # symmetric counterpart to starting the mic first, so a tight release never clips
        # the last word. Then unmute + signal the UI.
        await asyncio.sleep(0.12)
        self._stop_mic()
        self._restore_output()
        self._emit_stopped_once()

        if not streaming:
            logger.info(f"[STREAM] streaming dropped mid-record → batch fallback "
                        f"(sent {cursor}/{len(self._outbox)} chunks, captured {len(self._chunks)})")
            return None, False   # batch fallback on the full buffer

        # Flush remaining audio, then tell xAI we're done. audio.done makes it flush its
        # buffer and emit the final, fully-stitched transcript.done — authoritative and
        # complete across every sentence (partials reset per-utterance, so concatenating
        # them drops earlier sentences; transcript.done never does).
        while cursor < len(self._outbox):
            try:
                await ws.send(self._outbox[cursor])
                cursor += 1
            except Exception:
                break
        try:
            await ws.send(__import__("json").dumps({"type": "audio.done"}))
        except Exception as e:
            logger.warning(f"[STREAM] audio.done send failed ({e})")

        # Wait for transcript.done — the terminal marker that fires once xAI has emitted
        # the final speech_final for every utterance (incl. the last, flushed by audio.done).
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._DONE_WAIT
        while loop.time() < deadline:
            await asyncio.sleep(0.02)
            if done_flag["v"]:
                break
        if rx:
            rx.cancel()
        try:
            await ws.close()
        except Exception:
            pass

        audio_s = cursor * 1600 / SAMPLE_RATE  # 1600-sample (100ms) chunks
        # Assemble: finalized utterances + the trailing in-progress one (if the last
        # utterance didn't get speech_final before we stopped waiting). Guard against a
        # cumulative trailing interim that restates everything already committed, which
        # would otherwise duplicate earlier utterances.
        parts = list(utterances)
        tail = cur["t"].strip()
        if tail:
            joined = " ".join(parts).strip()
            if joined and tail.startswith(joined):
                parts = [tail]                       # cumulative → it already contains all of it
            elif not parts or tail != parts[-1]:     # skip an exact-duplicate restatement
                parts.append(tail)
        final = " ".join(parts).strip()
        logger.info(f"[STREAM] FINAL via streaming: sent={cursor}/{len(self._outbox)} (~{audio_s:.1f}s), "
                    f"utterances={len(utterances)}, partials={n_part['v']}, done={done_flag['v']}, chars={len(final)}")
        if _DEV:
            logger.info(f"[STREAM] FINAL text: {final!r}")
        if final:
            return final, True
        # Nothing usable came back → batch fallback (correct, complete transcript).
        logger.info(f"[STREAM] empty streaming result → batch fallback ({n_part['v']} partials)")
        return None, False

    def _finish_via_batch(self):
        """Exactly VoiceRecorder's post-capture path: encode the buffered audio and
        transcribe it in one request. Guarantees streaming failures degrade to today."""
        import numpy as np
        if not self._chunks:
            self.error.emit("No audio recorded.")
            return
        audio = np.concatenate(self._chunks, axis=0)
        audio = _normalize_peak(audio)
        logger.info(f"[STREAM] batch fallback on {len(audio)/SAMPLE_RATE:.1f}s of audio")
        tmp = None
        try:
            try:
                import soundfile as sf
                with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
                    tmp = f.name
                sf.write(tmp, audio, SAMPLE_RATE, format="FLAC", subtype="PCM_16")
            except Exception as e:
                logger.warning(f"FLAC encode unavailable ({e}); sending WAV instead")
                from scipy.io import wavfile
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    tmp = f.name
                    wavfile.write(tmp, SAMPLE_RATE, audio)
            result = transcribe_audio(tmp, language=self.language, terms=self.terms)
            if result.get("ok"):
                txt = result.get("text", "")
                logger.info(f"[STREAM] batch fallback FINAL: chars={len(txt)}")
                if _DEV:
                    logger.info(f"[STREAM] batch FINAL text: {txt!r}")
                self.finished.emit(txt)
            else:
                self.error.emit(result.get("message", "Transcription failed."))
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass


if __name__ == "__main__":
    # Self-check for the one bit of non-trivial logic (amplitude normalization).
    import numpy as np
    assert _rms_level(np.zeros(1000, dtype="int16")) == 0.0
    loud = (np.ones(1000) * 20000).astype("int16")
    assert _rms_level(loud) == 1.0            # clamps
    quiet = (np.ones(1000) * 1000).astype("int16")
    assert 0.0 < _rms_level(quiet) < 1.0
    # normalization: quiet clip boosted, loud/normal clip untouched, silence stays silent, no clipping
    q = (np.ones(1000) * 2000).astype("int16")
    b = _normalize_peak(q)
    assert int(np.max(np.abs(b.astype("int32")))) > 2000                     # quiet got louder
    assert int(np.max(np.abs(b.astype("int32")))) <= 32767                    # never clips
    loud = (np.ones(1000) * 31500).astype("int16")
    assert np.array_equal(_normalize_peak(loud), loud)                        # normal/loud unchanged
    assert np.array_equal(_normalize_peak(np.zeros(1000, dtype="int16")),
                          np.zeros(1000, dtype="int16"))                      # silence unchanged

    # _pick_text: find the transcript field whatever it's called (used for both
    # transcript.partial and the authoritative transcript.done payloads)
    assert _pick_text({"type": "transcript.partial", "text": "hi"}) == "hi"
    assert _pick_text({"type": "transcript.done", "text": "Hello there. General Kenobi."}) == "Hello there. General Kenobi."
    assert _pick_text({"transcript": "there"}) == "there"
    assert _pick_text({"type": "x"}) == ""
    print("transcription self-check passed ✓")
