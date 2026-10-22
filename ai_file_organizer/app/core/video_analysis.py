"""Video content analysis for Filect.

A video has two signals Filect can already understand in isolation — pictures and speech —
so this module does NOT invent an analyzer. It extracts a few keyframes + the audio track
natively (AVFoundation, no external binary), then hands them to the EXISTING vision and
transcription paths and fills the SAME metadata fields an image/doc produces, so search and
organize treat a video like any other analyzed file.

macOS-only (AVFoundation). The Windows port swaps only `extract_keyframes`/`extract_audio`;
`analyze_video` and the AI calls are shared.
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.m4v', '.avi', '.mkv', '.webm', '.wmv', '.flv', '.mpeg', '.mpg'}


def _cgimage_to_png_bytes(cgimage):
    """CGImageRef -> PNG bytes via AppKit (no PIL needed)."""
    try:
        from AppKit import NSBitmapImageRep
        rep = NSBitmapImageRep.alloc().initWithCGImage_(cgimage)
        data = rep.representationUsingType_properties_(4, {})  # 4 == NSBitmapImageFileTypePNG
        return bytes(data) if data is not None else None
    except Exception as e:
        logger.debug(f"cgimage->png failed: {e}")
        return None


def extract_keyframes(path, n: int = 4) -> list:
    """Up to `n` PNG keyframes (bytes), evenly spaced across the video. [] if unreadable.

    Native AVFoundation: respects rotation, seeks with a little tolerance so grabbing the
    nearest keyframe is fast rather than decoding to an exact frame."""
    try:
        from Foundation import NSURL
        from AVFoundation import AVURLAsset, AVAssetImageGenerator
        from CoreMedia import CMTimeGetSeconds, CMTimeMakeWithSeconds
    except Exception as e:
        logger.warning(f"AVFoundation unavailable (no video frames): {e}")
        return []
    try:
        asset = AVURLAsset.URLAssetWithURL_options_(NSURL.fileURLWithPath_(str(path)), None)
        seconds = CMTimeGetSeconds(asset.duration())
        if not seconds or seconds <= 0:
            return []
        gen = AVAssetImageGenerator.assetImageGeneratorWithAsset_(asset)
        gen.setAppliesPreferredTrackTransform_(True)
        try:
            tol = CMTimeMakeWithSeconds(1.0, 600)
            gen.setRequestedTimeToleranceBefore_(tol)
            gen.setRequestedTimeToleranceAfter_(tol)
        except Exception:
            pass
        frames = []
        for i in range(max(1, n)):
            t = seconds * ((i + 0.5) / n)            # center of each of n equal segments
            res = gen.copyCGImageAtTime_actualTime_error_(CMTimeMakeWithSeconds(t, 600), None, None)
            cgimage = res[0] if isinstance(res, tuple) else res
            if cgimage is not None:
                png = _cgimage_to_png_bytes(cgimage)
                if png:
                    frames.append(png)
        return frames
    except Exception as e:
        logger.warning(f"keyframe extraction failed for {path}: {e}")
        return []


def extract_audio(path, max_seconds: int = 600):
    """Export the video's audio track to a temp .m4a (trimmed to max_seconds), for the
    existing transcription path. Returns the temp path, or None if there's no audio track /
    export fails. Caller deletes the temp file."""
    try:
        from Foundation import NSURL
        from AVFoundation import (AVURLAsset, AVAssetExportSession,
                                  AVAssetExportPresetAppleM4A, AVMediaTypeAudio)
        from CoreMedia import (CMTimeGetSeconds, CMTimeMakeWithSeconds,
                               CMTimeRangeMake, kCMTimeZero)
    except Exception as e:
        logger.warning(f"AVFoundation audio export unavailable: {e}")
        return None
    import os
    import tempfile
    import time
    try:
        asset = AVURLAsset.URLAssetWithURL_options_(NSURL.fileURLWithPath_(str(path)), None)
        if not asset.tracksWithMediaType_(AVMediaTypeAudio):
            return None  # silent video -> nothing to transcribe
        fd, out = tempfile.mkstemp(suffix=".m4a")
        os.close(fd)
        os.unlink(out)  # the export session requires the output file to NOT exist
        session = AVAssetExportSession.exportSessionWithAsset_presetName_(
            asset, AVAssetExportPresetAppleM4A)
        if session is None:
            return None
        session.setOutputURL_(NSURL.fileURLWithPath_(out))
        session.setOutputFileType_("com.apple.m4a-audio")
        if CMTimeGetSeconds(asset.duration()) > max_seconds:
            session.setTimeRange_(CMTimeRangeMake(kCMTimeZero,
                                                  CMTimeMakeWithSeconds(max_seconds, 600)))
        session.exportAsynchronouslyWithCompletionHandler_(lambda: None)
        deadline = time.time() + 180
        # AVAssetExportSession runs on its own dispatch queue, so just poll its status —
        # no need to spin a run loop (which busy-waits and, on the UI thread, re-enters Qt).
        while session.status() in (1, 2) and time.time() < deadline:  # waiting / exporting
            time.sleep(0.1)
        if session.status() == 3 and os.path.exists(out):  # completed
            return out
        logger.warning(f"audio export failed: status={session.status()} err={session.error()}")
        return None
    except Exception as e:
        logger.warning(f"audio extraction failed for {path}: {e}")
        return None


def _analyze_frames(frames: list, filename: str, transcript: str = "") -> dict:
    """Send the keyframes to the EXISTING vision proxy in ONE call (downscaled, same 1024px
    cap images use) and parse the same JSON shape gpt_vision_fallback returns."""
    import io
    from PIL import Image
    from . import vision as V
    b64s = []
    for png in frames:
        try:
            img = Image.open(io.BytesIO(png)).convert("RGB")
            img.thumbnail((1024, 1024))
            b64s.append(V._pil_image_to_b64(img, "PNG"))
        except Exception:
            continue
    if not b64s:
        return {}
    system = V.build_analysis_prompt(V.DETAILED_SYSTEM_PROMPT, None)
    content = [
        {"type": "text", "text": f"filename: {filename}"},
        {"type": "text", "text": f"These are {len(b64s)} frames sampled in order across a "
                                 "VIDEO. Analyze the video as a whole. Set \"type\" to a short "
                                 "content category for the video (e.g. tutorial, demo, presentation, "
                                 "gameplay, vlog, ad, screen-recording, music video, interview) — "
                                 "NOT an image type like photo or meme."},
    ]
    if transcript:
        content.append({"type": "text", "text": "Transcript of the video's audio (context):\n"
                                                 + transcript[:2000]})
    content.append({"type": "text", "text": "Return STRICT JSON only using the schema."})
    for b in b64s:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b}", "detail": "auto"}})
    resp = V._call_openai_proxy("vision", [{"role": "system", "content": system},
                                           {"role": "user", "content": content}],
                                max_tokens=600, temperature=0.2)
    content_str = ""
    if resp:
        content_str = (resp.get("choices", [{}])[0].get("message", {}).get("content") or "")
    data = V._parse_json_relaxed(content_str) if content_str else None
    if not isinstance(data, dict):
        return {}
    tags = data.get("tags") or []
    tags = [str(t).lower()[:64] for t in tags][:25] if isinstance(tags, list) else []
    desc = str(data.get("description", "")).strip()
    caption = (desc or str(data.get("caption", "")).strip())[:1200]
    return {
        "label": str(data.get("type", "video")).strip().lower() or "video",
        "tags": tags,
        "caption": caption,
        "description": desc,
        "type": data.get("type"),
        "detected_text": data.get("detected_text"),
        "ai_source": "video:frames",
    }


def analyze_video(path, n_frames: int = 4, max_audio_seconds: int = 600,
                  transcribe: bool = True) -> "dict | None":
    """Analyze a video by its keyframes (vision) + audio (transcription), returning the SAME
    metadata shape an image/doc produces so the indexer/search/organize need no changes:
    label, tags, caption, type, ocr_text (= the transcript), has_ocr, duration, ai_source.
    None only if nothing at all could be extracted."""
    import os
    frames = extract_keyframes(path, n=n_frames)
    transcript = ""
    if transcribe:
        audio = extract_audio(path, max_seconds=max_audio_seconds)
        if audio:
            try:
                from .transcription import transcribe_audio
                r = transcribe_audio(audio)
                if r.get("ok"):
                    transcript = (r.get("text") or "").strip()
            finally:
                try:
                    os.unlink(audio)
                except Exception:
                    pass
    meta = _analyze_frames(frames, Path(path).name, transcript) if frames else {}
    if not meta and not transcript:
        return None
    if transcript:
        meta["ocr_text"] = transcript[:5000]
        meta["has_ocr"] = True
        if not meta.get("caption"):
            meta["caption"] = transcript[:300]
    meta.setdefault("label", "video")
    meta["duration"] = round(video_duration_seconds(path), 1)
    meta["ai_source"] = "video:frames+transcript" if transcript else meta.get("ai_source", "video:frames")
    return meta


AUDIO_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg', '.oga', '.opus',
                    '.aiff', '.aif', '.wma', '.caf'}


def analyze_audio(path, max_seconds: int = 600) -> "dict | None":
    """Analyze a standalone AUDIO file: transcribe it, then tag it from what's said. Returns the
    same metadata shape as other files (ocr_text = transcript, label/tags from analyze_text,
    duration, ai_source). Long files are trimmed to `max_seconds`. None if nothing usable."""
    import os
    from .transcription import transcribe_audio
    src, tmp = str(path), None
    try:
        if video_duration_seconds(path) > max_seconds:
            tmp = extract_audio(path, max_seconds=max_seconds)  # trim long audio (podcasts etc.)
            if tmp:
                src = tmp
        r = transcribe_audio(src)
    except Exception as e:
        logger.warning(f"audio transcription failed for {path}: {e}")
        r = {}
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    transcript = (r.get("text") or "").strip() if isinstance(r, dict) else ""
    if not transcript:
        return None
    meta = {"label": "audio", "ocr_text": transcript[:5000], "has_ocr": True,
            "duration": round(video_duration_seconds(path), 1)}
    tinfo = _tag_transcript(transcript)
    if tinfo:
        meta.update({k: v for k, v in tinfo.items() if v})
    if not meta.get("caption"):
        meta["caption"] = transcript[:300]
    meta["ai_source"] = "audio:transcript"
    return meta


def _tag_transcript(transcript: str) -> dict:
    """Tag an audio clip from its transcript with an AUDIO-appropriate prompt (not the
    document prompt analyze_text uses, which leaks tags like 'transcript'/'caption'/the
    filename). Returns {caption, label(=category), tags} or {}."""
    try:
        from . import vision as V
        system = (
            "You label an AUDIO clip for a file-search index, given a transcript of what is "
            "said. Identify the real TOPICS, people, products, tools and themes discussed. "
            "Return a one-sentence caption summarizing the clip, a short category (e.g. "
            "'podcast', 'meeting', 'voice memo', 'lecture', 'interview', 'music'), and up to 15 "
            "topic tags. Tag only actual content — NEVER the file format, the words "
            "'audio'/'transcript'/'caption', or any filename. Return JSON only: "
            "{\"caption\": \"...\", \"category\": \"...\", \"tags\": [\"t1\", \"t2\"]}."
        )
        resp = V._call_openai_proxy(
            "chat",
            [{"role": "system", "content": system},
             {"role": "user", "content": transcript[:6000]}],
            max_tokens=400, model="gpt-5-mini",
        )
        content = (resp or {}).get("choices", [{}])[0].get("message", {}).get("content", "") if resp else ""
        data = V._parse_json_relaxed(content) if content else None
        if not isinstance(data, dict):
            return {}
        tags = data.get("tags") or []
        tags = [str(t).lower()[:64] for t in tags][:15] if isinstance(tags, list) else []
        return {
            "caption": str(data.get("caption", "")).strip()[:1200],
            "label": (str(data.get("category", "audio")).strip().lower() or "audio"),
            "tags": tags,
        }
    except Exception as e:
        logger.debug(f"transcript tagging failed: {e}")
        return {}


def video_duration_seconds(path) -> float:
    try:
        from Foundation import NSURL
        from AVFoundation import AVURLAsset
        from CoreMedia import CMTimeGetSeconds
        asset = AVURLAsset.URLAssetWithURL_options_(NSURL.fileURLWithPath_(str(path)), None)
        return float(CMTimeGetSeconds(asset.duration()) or 0.0)
    except Exception:
        return 0.0
