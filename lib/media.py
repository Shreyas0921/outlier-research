"""Media processing for one outlier: download -> 4 stills -> transcript -> cleanup (no video is kept).

Everything for a video lives in its own folder (`_media/<platform>-<id>/`). Downloaded files are
untrusted: they are only ever read by ffmpeg/ffprobe/whisper, never executed.

whisper.cpp note (verified 2026-11-14): with `--vad`, segment offsets are on the real timeline but
token offsets are on the VAD-trimmed timeline. Each segment's words are shifted by
(segment start - first token start) so the "first 5 seconds" split is correct.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import requests

from lib import faces
from lib.normalize import Video

WHISPER_MODEL = Path.home() / ".cache/whisper/ggml-large-v3-turbo.bin"
VAD_MODEL = Path.home() / ".cache/whisper/ggml-silero-v5.1.2.bin"


def whisper_threads(cores: Optional[int] = None) -> int:
    """Leave 2 cores free on a big machine (the Mac stays usable); use every core on a small one (e.g. a 2-core server)."""
    cores = cores or os.cpu_count() or 4
    return cores - 2 if cores > 4 else cores
HOOK_SECONDS = 5.0
STILL_TIMES = (0.0, 1.0, 2.5, 4.5)
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
NON_SPEECH = re.compile(r"^\s*[\[\(][^\]\)]*[\]\)]\s*$|^[\s♪♫.,!?-]*$")


class MediaError(Exception):
    pass


class Rejected(Exception):
    """The video downloaded fine but fails a content rule (not English, no person on camera, not enough
    speech). Its media folder is deleted; the caller marks it seen so it's never downloaded again."""


@dataclass
class Checks:
    """Content rules applied after download. `spoken_lang` is the platform's tag when it has one."""
    english_only: bool = False
    talking_head_only: bool = False
    min_words_per_min: float = 60
    spoken_lang: Optional[str] = None


LANG_SAMPLE_S = 30
LANG_RE = re.compile(r"auto-detected language:\s*(\w+)")


@dataclass
class Transcript:
    speech: bool
    words: List[dict] = field(default_factory=list)  # {"w", "start", "end"} in seconds, real timeline
    hook_text: str = ""  # words starting before HOOK_SECONDS
    full_text: str = ""


@dataclass
class MediaResult:
    dir: Path
    stills: List[Path]
    transcript: Transcript
    duration_s: float
    downloaded_via: str  # "direct" | "yt-dlp"


# ---------------------------------------------------------------- download


def download(video: Video, dest_dir: Path, http_get: Callable = requests.get,
             run: Callable = subprocess.run) -> tuple:
    """Fetch the video to dest_dir/full.mp4. Tries the API's direct URL first, then yt-dlp on the
    public post URL. Returns (path, "direct" | "yt-dlp")."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / "full.mp4"
    errors = []
    if video.download_url:
        try:
            _http_download(video.download_url, out, http_get)
            probe(out)
            return out, "direct"
        except Exception as e:  # expired link, 403, not a video...
            errors.append(f"direct: {e}")
            out.unlink(missing_ok=True)
    try:
        _ytdlp_download(video.url, dest_dir, run)
        probe(out)
        return out, "yt-dlp"
    except Exception as e:
        errors.append(f"yt-dlp: {e}")
        out.unlink(missing_ok=True)
    raise MediaError("download failed (" + "; ".join(errors) + ")")


def _http_download(url: str, out: Path, http_get: Callable) -> None:
    with http_get(url, headers=UA, stream=True, timeout=60) as r:
        if r.status_code >= 400:
            raise MediaError(f"HTTP {r.status_code}")
        size = 0
        with open(out, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise MediaError("file too large")
                f.write(chunk)
    if size == 0:
        raise MediaError("empty download")


def _ytdlp_download(url: str, dest_dir: Path, run: Callable) -> None:
    ytdlp = Path(sys.executable).parent / "yt-dlp"
    exe = str(ytdlp) if ytdlp.exists() else (shutil.which("yt-dlp") or "yt-dlp")
    r = run([exe, "--no-playlist", "--quiet", "--no-warnings", "-f", "b[ext=mp4]/bv*+ba/b",
             "--merge-output-format", "mp4", "-o", str(dest_dir / "full.%(ext)s"), url],
            capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise MediaError((r.stderr or "yt-dlp failed").strip()[-300:])
    if not (dest_dir / "full.mp4").exists():
        others = list(dest_dir.glob("full.*"))
        if not others:
            raise MediaError("yt-dlp produced no file")
        others[0].rename(dest_dir / "full.mp4")


def probe(path: Path) -> float:
    """Check the file is a real video; return its duration in seconds."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise MediaError(f"not a readable video: {r.stderr.strip()[:200]}")
    info = json.loads(r.stdout or "{}")
    if not any(s.get("codec_type") == "video" for s in info.get("streams", [])):
        raise MediaError("file has no video stream")
    try:
        return float(info["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        raise MediaError("could not read video duration")


def has_audio(path: Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                        "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(r.stdout.strip())


# ------------------------------------------------------------------ stills


def make_stills(full: Path, dest_dir: Path, duration: float, times=STILL_TIMES) -> List[Path]:
    """JPGs at 0s, 1s, 2.5s and 4.5s (540px wide), skipping times past the end."""
    stills = []
    for t in times:
        if t >= duration:
            continue
        out = dest_dir / f"still-{t:.1f}s.jpg"
        _ffmpeg(["-ss", f"{t:.2f}", "-i", str(full), "-frames:v", "1", "-vf", "scale=540:-2",
                 "-q:v", "3", str(out)])
        if out.exists():
            stills.append(out)
    return stills


def _ffmpeg(args: List[str]) -> None:
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin"] + args, capture_output=True, text=True,
                       timeout=600)
    if r.returncode != 0:
        raise MediaError(f"ffmpeg failed: {r.stderr.strip()[-300:]}")


# ---------------------------------------------------------------- transcript


def transcribe(full: Path, work_dir: Path, run: Callable = subprocess.run) -> Transcript:
    """whisper.cpp with the large-v3-turbo model + VAD. Writes transcript.json next to the media."""
    if not has_audio(full):
        return Transcript(speech=False)
    wav = work_dir / "audio16k.wav"
    _ffmpeg(["-i", str(full), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
    try:
        out_base = work_dir / "transcript"
        r = run(["whisper-cli", "-m", str(WHISPER_MODEL), "-f", str(wav), "-l", "en", "-mc", "0",
                 "--vad", "-vm", str(VAD_MODEL), "-ojf", "-of", str(out_base),
                 "-t", str(whisper_threads()), "-np"],
                capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise MediaError(f"whisper failed: {(r.stderr or '').strip()[-300:]}")
        json_path = out_base.with_suffix(".json")
        if not json_path.exists():
            return Transcript(speech=False)  # VAD found no speech: whisper may write nothing
        return parse_whisper(json.loads(json_path.read_text()))
    finally:
        wav.unlink(missing_ok=True)


def parse_whisper(data: dict, hook_seconds: float = HOOK_SECONDS) -> Transcript:
    words: List[dict] = []
    for seg in data.get("transcription") or []:
        if NON_SPEECH.match(seg.get("text") or ""):
            continue  # "[Music]", "(applause)", "♪" ...
        tokens = [t for t in seg.get("tokens") or [] if not (t.get("text") or "").startswith("[_")]
        if not tokens:
            continue
        shift = (seg["offsets"]["from"] - tokens[0]["offsets"]["from"]) / 1000.0  # VAD timeline -> real
        current = None
        for t in tokens:
            text = t["text"]
            start = t["offsets"]["from"] / 1000.0 + shift
            end = t["offsets"]["to"] / 1000.0 + shift
            if current is None or text.startswith(" "):
                if current:
                    words.append(current)
                current = {"w": text.strip(), "start": round(start, 2), "end": round(end, 2)}
            else:  # continuation of the same word ("philosoph" + "y", or punctuation)
                current["w"] += text
                current["end"] = round(end, 2)
        if current:
            words.append(current)
    words = [w for w in words if w["w"]]
    if not words:
        return Transcript(speech=False)
    hook = [w["w"] for w in words if w["start"] < hook_seconds]
    return Transcript(speech=True, words=words, hook_text=" ".join(hook),
                      full_text=" ".join(w["w"] for w in words))


# ------------------------------------------------------------ content checks


def check_on_camera(full: Path, work: Path, duration: float, face_detect: Callable = faces.detect) -> float:
    """Face check on SAMPLE_FRAMES frames across the whole video. Raises Rejected below MIN_FACE_SHARE."""
    frames_dir = work / "faceframes"
    frames_dir.mkdir(exist_ok=True)
    try:
        frames = []
        for i, t in enumerate(faces.sample_times(duration)):
            out = frames_dir / f"f{i:02d}.jpg"
            _ffmpeg(["-ss", f"{t:.2f}", "-i", str(full), "-frames:v", "1", "-vf", "scale=540:-2",
                     "-q:v", "4", str(out)])
            if out.exists():
                frames.append(out)
        found = face_detect(frames)
        heights = [found.get(str(f), []) for f in frames]
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)
    share = faces.face_share(heights)
    if share < faces.MIN_FACE_SHARE:
        hits = round(share * len(heights))
        raise Rejected(f"no person on camera (face in {hits}/{len(heights)} frames)")
    return share


def detect_language(full: Path, work: Path, run: Callable = subprocess.run) -> Optional[str]:
    """Spoken language of the first LANG_SAMPLE_S seconds via whisper's language detection (~25 s on this
    Mac). Returns a code like "en", or None when there's no audio or whisper can't tell."""
    if not has_audio(full):
        return None
    wav = work / "lang16k.wav"
    try:
        _ffmpeg(["-i", str(full), "-t", str(LANG_SAMPLE_S), "-vn", "-ac", "1", "-ar", "16000",
                 "-c:a", "pcm_s16le", str(wav)])
        r = run(["whisper-cli", "-m", str(WHISPER_MODEL), "-f", str(wav), "-l", "auto", "-dl",
                 "-t", str(whisper_threads())], capture_output=True, text=True, timeout=600)
        m = LANG_RE.search((r.stdout or "") + (r.stderr or ""))
        return m.group(1).lower() if m else None
    finally:
        wav.unlink(missing_ok=True)


def check_speech_rate(transcript: Transcript, duration: float, min_wpm: float) -> float:
    """A person talking to camera says well over 100 words a minute; text-over-music videos say almost
    nothing (whisper picks up a stray lyric). Raises Rejected below min_wpm."""
    words = len(transcript.words) if transcript.speech else 0
    wpm = words / (duration / 60) if duration > 0 else 0
    if wpm < min_wpm:
        raise Rejected(f"not enough speech ({wpm:.0f} words/min)")
    return wpm


# ------------------------------------------------------------------- process

SCRATCH_ROOT = Path.home() / ".cache/outlier-research/work"


def process(video: Video, media_root: Path, http_get: Callable = requests.get,
            run: Callable = subprocess.run, checks: Optional[Checks] = None,
            face_detect: Callable = faces.detect, scratch_root: Optional[Path] = None) -> MediaResult:
    """Download, stills and transcript in media_root/<platform>-<id>/; the full video and the
    WAV are always deleted afterwards, success or failure. With `checks`, content rules run in order of
    cost (face check, then audio language check, before the slow transcription; speaking rate after it)
    and a failing video raises Rejected with its folder removed."""
    checks = checks or Checks()
    name = f"{video.platform}-{_safe(video.id)}"
    work = Path(media_root) / name
    work.mkdir(parents=True, exist_ok=True)
    # The video, WAV and check frames go in a scratch folder outside the vault, so Obsidian Sync never uploads a
    # half-processed 20-50 MB file; only the stills and transcript land in the vault.
    scratch = Path(scratch_root or SCRATCH_ROOT) / name
    scratch.mkdir(parents=True, exist_ok=True)
    full = scratch / "full.mp4"
    try:
        full, via = download(video, scratch, http_get=http_get, run=run)
        duration = probe(full)
        stills = make_stills(full, work, duration)
        if checks.talking_head_only:
            check_on_camera(full, scratch, duration, face_detect)
        if checks.english_only and not checks.spoken_lang:
            lang = detect_language(full, scratch, run=run)
            if lang and lang != "en":
                raise Rejected(f"not English ({lang}, audio check)")
        transcript = transcribe(full, scratch, run=run)
        if (scratch / "transcript.json").exists():  # word timings, kept next to the stills as before
            shutil.copy2(scratch / "transcript.json", work / "transcript.json")
        if checks.talking_head_only:
            check_speech_rate(transcript, duration, checks.min_words_per_min)
        (work / "transcript.txt").write_text(transcript.full_text + "\n" if transcript.speech else "")
        return MediaResult(dir=work, stills=stills, transcript=transcript,
                           duration_s=round(duration, 2), downloaded_via=via)
    except Rejected:
        shutil.rmtree(work, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        for leftover in list(work.glob("full.*")) + [work / "audio16k.wav"]:  # from runs before the scratch folder
            leftover.unlink(missing_ok=True)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)
