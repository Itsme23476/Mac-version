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

SUPABASE_URL = "https://gsvccxhdgcshiwgjvgfi.supabase.co"
TRANSCRIBE_URL = f"{SUPABASE_URL}/functions/v1/transcribe"
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
    print("transcription self-check passed ✓")
