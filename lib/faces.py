"""Talking-head check, visual half: is a person on camera for most of the video?

Samples frames across the whole video and runs face detection on them. On macOS that's Apple's Vision
framework (lib/faces.swift, compiled once into ~/.cache/outlier-research/); on Linux
it's OpenCV's YuNet model (lib/models/, needs opencv-python-headless). Set OUTLIER_FACE_BACKEND=vision|yunet
to force one. A video passes when a face at least `min_height` of the
frame tall shows up in at least `min_share` of the sampled frames. B-roll and photo overlays are fine as
long as the person keeps coming back; animations, AI renders and stock-footage edits fail.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

SAMPLE_FRAMES = 12
MIN_FACE_HEIGHT = 0.04  # fraction of frame height; calibrated 11-19: a wide classroom shot is ~0.05, phone talking heads 0.10-0.30
MIN_FACE_SHARE = 0.6  # share of sampled frames with such a face; leaves room for full-screen B-roll and overlays
SOURCE = Path(__file__).with_name("faces.swift")
BINARY = Path.home() / ".cache/outlier-research/faces"
YUNET_MODEL = Path(__file__).with_name("models") / "face_detection_yunet_2023mar.onnx"
# YuNet calibrated against Vision on 982 covers/stills (2026-11-19): its boxes are ~18% taller (so 0.047 matches
# Vision's 0.04). It also "finds" faces in cartoons and AI renders, mostly at confidence 0.65-0.88; real faces
# overlap that range, so the cutoff is the best match to Vision: at 0.78 they agree on 965/982 images, and the
# leftover misses are split both ways (each detector passes a few cartoons).
YUNET_MIN_SCORE = 0.78
YUNET_MIN_FACE_HEIGHT = 0.047
YUNET_MAX_SIDE = 640  # images are scaled down to this before detection; face heights are fractions, so unaffected


class FaceDetectorError(Exception):
    pass


def sample_times(duration: float, n: int = SAMPLE_FRAMES) -> List[float]:
    """n evenly spaced times inside the video, keeping clear of the first and last 3%."""
    if duration <= 0 or n <= 0:
        return []
    if n == 1:
        return [round(duration / 2, 2)]
    lo, hi = duration * 0.03, duration * 0.97
    return [round(lo + (hi - lo) * i / (n - 1), 2) for i in range(n)]


def min_height() -> float:
    """Smallest face that counts, for the active detector (their box sizes differ)."""
    return YUNET_MIN_FACE_HEIGHT if backend() == "yunet" else MIN_FACE_HEIGHT


def face_share(face_heights: Sequence[Sequence[float]], threshold: Optional[float] = None) -> float:
    """face_heights: per sampled frame, the heights of the faces found in it. Returns the share of
    frames with at least one face of `threshold` (default: the active detector's min_height()) or taller."""
    if threshold is None:
        threshold = min_height()
    if not face_heights:
        return 0.0
    hits = sum(1 for faces in face_heights if any(h >= threshold for h in faces))
    return hits / len(face_heights)


def _binary() -> Path:
    if BINARY.exists() and BINARY.stat().st_mtime >= SOURCE.stat().st_mtime:
        return BINARY
    BINARY.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["swiftc", "-O", str(SOURCE), "-o", str(BINARY)], capture_output=True, text=True,
                       timeout=600)
    if r.returncode != 0:
        raise FaceDetectorError(f"could not compile face detector: {r.stderr.strip()[-300:]}")
    return BINARY


def backend() -> str:
    forced = os.environ.get("OUTLIER_FACE_BACKEND", "").strip().lower()
    if forced in ("vision", "yunet"):
        return forced
    return "vision" if platform.system() == "Darwin" else "yunet"


def detect(images: Sequence[Path], run: Callable = subprocess.run) -> Dict[str, List[float]]:
    """Face heights per image path (as given, str), as fractions of the image height."""
    if not images:
        return {}
    if backend() == "yunet":
        return detect_yunet(images)
    r = run([str(_binary())] + [str(p) for p in images], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise FaceDetectorError(f"face detector failed: {(r.stderr or '').strip()[-300:]}")
    out = {}
    for line in (r.stdout or "").splitlines():
        if line.strip():
            d = json.loads(line)
            out[d["path"]] = [float(h) for h in d.get("faces") or []]
    return out


def yunet_heights(faces_out, image_h: int, min_score: float = YUNET_MIN_SCORE) -> List[float]:
    """YuNet output rows are [x, y, w, h, 10 landmark coords, score]; returns h / image_h per confident face."""
    if faces_out is None or image_h <= 0:
        return []
    return [round(float(f[3]) / image_h, 4) for f in faces_out if float(f[-1]) >= min_score]


def detect_yunet(images: Sequence[Path]) -> Dict[str, List[float]]:
    try:
        import cv2
    except ImportError as e:
        raise FaceDetectorError("OpenCV missing: .venv/bin/pip install opencv-python-headless") from e
    try:  # hush the harmless "setPreferableTarget ... new graph engine" warning OpenCV 5 prints per model load
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except AttributeError:
        pass
    if not YUNET_MODEL.exists():
        raise FaceDetectorError(f"face model missing: {YUNET_MODEL}")
    det = cv2.FaceDetectorYN.create(str(YUNET_MODEL), "", (320, 320), YUNET_MIN_SCORE, 0.3, 5000)
    out: Dict[str, List[float]] = {}
    for p in images:
        img = cv2.imread(str(p))
        if img is None:  # unreadable (e.g. HEIC): no faces, same as Vision on a broken file
            out[str(p)] = []
            continue
        h, w = img.shape[:2]
        scale = min(1.0, YUNET_MAX_SIDE / max(h, w))
        if scale < 1.0:
            img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))))
        ih, iw = img.shape[:2]
        det.setInputSize((iw, ih))
        _, found = det.detect(img)
        out[str(p)] = yunet_heights(found, ih)
    return out
