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
from typing import Optional, Dict, Any

import requests
from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)

SUPABASE_URL = "https://gsvccxhdgcshiwgjvgfi.supabase.co"
TRANSCRIBE_URL = f"{SUPABASE_URL}/functions/v1/transcribe"
DISTILL_URL = f"{SUPABASE_URL}/functions/v1/distill-query"

SAMPLE_RATE = 16000  # 16 kHz mono — plenty for speech, small payloads


def _get_auth_token() -> Optional[str]:
    """Current user's Supabase access token (same source as vision.py)."""
    try:
        from .supabase_client import supabase_auth
        if supabase_auth.is_authenticated:
            return supabase_auth._access_token
        return None
    except Exception as e:
        logger.error(f"Failed to get auth token: {e}")
        return None


def transcribe_audio(audio_path: str, language: Optional[str] = None) -> Dict[str, Any]:
    """
    Send an audio file to the transcribe proxy and return a result dict:
        {ok: True,  text: str, duration: float}
        {ok: False, error: <code>, message: <human message>}
    error codes: not_authenticated | no_subscription | rate_limited | provider | network
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


def _rms_level(chunk_int16) -> float:
    """Normalized 0..1 loudness of an int16 audio chunk (drives the animation)."""
    import numpy as np
    if chunk_int16 is None or len(chunk_int16) == 0:
        return 0.0
    x = chunk_int16.astype("float32") / 32768.0
    rms = float(np.sqrt(np.mean(x * x)))
    # Speech RMS is small; scale so normal talking lands mid-range, clamp to 1.0.
    return min(1.0, rms * 4.0)


class VoiceRecorder(QThread):
    """Records mic audio until stop_recording(), then transcribes via Grok."""
    finished = Signal(str)          # transcribed text
    error = Signal(str)             # human-readable message
    recording_stopped = Signal()    # mic released (before transcription result)
    level = Signal(float)           # live amplitude 0..1, for the animation

    def __init__(self, language: Optional[str] = None, device=None):
        super().__init__()
        self.language = language
        self.device = device  # sounddevice input device index/name; None = system default
        self.is_recording = False
        self._chunks = []

    def stop_recording(self):
        self.is_recording = False

    def run(self):
        try:
            import sounddevice as sd
            import numpy as np
            from scipy.io import wavfile
        except ImportError as e:
            self.error.emit(f"Missing audio library: {e}")
            return

        self.is_recording = True
        self._chunks = []

        def cb(indata, frames, time_info, status):
            if self.is_recording:
                self._chunks.append(indata.copy())
                try:
                    self.level.emit(_rms_level(indata))
                except Exception:
                    pass

        try:
            logger.info(f"Recording started (device={self.device if self.device is not None else 'default'})")
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                device=self.device, callback=cb):
                while self.is_recording:
                    sd.sleep(50)
        except Exception as e:
            logger.error(f"Mic capture failed: {e}")
            self.error.emit("Could not access the microphone.")
            return

        self.recording_stopped.emit()

        if not self._chunks:
            self.error.emit("No audio recorded.")
            return

        audio = np.concatenate(self._chunks, axis=0)
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
            result = transcribe_audio(tmp, language=self.language)
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
    print("transcription self-check passed ✓")
