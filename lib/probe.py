"""Search-term probe: what does one TikTok search actually return?

Used to test search terms against a seed video the user likes. One search call (1 credit per page) lists
every result unfiltered, flags the seed if it shows up, and grades each result in two steps:

- fits format: English, 1:00-3:00, not AI, no skipped hashtag, and a talking head on the cover (free: the
  cover image goes through the same macOS face check as the downloaded frames, just once)
- fits filters: fits format, and also clears the views / age / likes floors a real run uses

A term is good when its results are mostly "fits format", whatever the age, and some also fit the filters.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import requests

from lib import faces
from lib import normalize as N
from lib.scoring import ai_reason, passes_filters, rule_reason


@dataclass
class ProbeRow:
    rank: int
    id: str
    url: str
    creator: str
    followers: Optional[int]
    views: Optional[int]
    likes: Optional[int]
    age_days: Optional[int]
    duration_s: Optional[float]
    lang: Optional[str]
    caption: str
    cover_url: Optional[str]
    cover_face: Optional[float] = None  # tallest face on the cover, fraction of frame height; None = unchecked
    rule: Optional[str] = None  # why the format rules skip it (language, hashtag, AI, length), or None
    fits_format: bool = False
    fits_filters: bool = False
    is_seed: bool = False


def cover_url(a: dict) -> Optional[str]:
    """The cover image, preferring a JPEG link: TikTok lists HEIC first, which OpenCV (the Linux face
    check) can't read."""
    video = a.get("video") or {}
    fallback = None
    for k in ("origin_cover", "cover", "dynamic_cover"):
        urls = [u for u in ((video.get(k) or {}).get("url_list") or []) if isinstance(u, str) and u]
        for u in urls:
            if ".jpeg" in u or ".jpg" in u:
                return u
        fallback = fallback or (urls[0] if urls else None)
    return fallback


def format_reason(v: N.Video, t: dict) -> Optional[str]:
    """The free format rules a real run applies, minus views/age/likes. English is required here (unknown
    language fails), since a probe only covers TikTok, which always tags it."""
    r = ai_reason(v, t)
    if r:
        return f"AI ({r})"
    r = rule_reason(v, t)
    if r:
        return r
    if t.get("english_only") and not (v.spoken_lang or "").startswith("en"):
        return f"language {v.spoken_lang or 'unknown'}"
    lo, hi = t.get("min_duration_s"), t.get("max_duration_s")
    if v.duration_s is None or (lo and v.duration_s < lo) or (hi and v.duration_s > hi):
        return f"length {int(v.duration_s or 0)}s"
    return None


def rows_from_search(items: Sequence[dict], t: dict, seed_id: Optional[str] = None,
                     now: Optional[datetime] = None) -> List[ProbeRow]:
    now = now or datetime.now(timezone.utc)
    out = []
    for i, a in enumerate(items, 1):
        v = N.tiktok_aweme(a)
        if v is None:
            continue
        out.append(ProbeRow(
            rank=i, id=v.id, url=v.url, creator=v.creator, followers=v.followers, views=v.views, likes=v.likes,
            age_days=(now - v.posted).days if v.posted else None, duration_s=v.duration_s, lang=v.spoken_lang,
            caption=v.caption.replace("\n", " ")[:120], cover_url=cover_url(a), rule=format_reason(v, t),
            is_seed=bool(seed_id) and v.id == seed_id,
        ))
        out[-1]._video = v  # type: ignore[attr-defined]  # kept for grade(); not serialized
    return out


def grade(rows: List[ProbeRow], t: dict, faces_by_id: Dict[str, List[float]],
          now: Optional[datetime] = None) -> None:
    """Fill cover_face / fits_format / fits_filters from the cover face heights."""
    need = t.get("talking_head_only")
    for r in rows:
        hs = faces_by_id.get(r.id)
        r.cover_face = round(max(hs), 3) if hs else (0.0 if hs is not None else None)
        face_ok = (not need) or (r.cover_face is not None and r.cover_face >= faces.min_height())
        r.fits_format = r.rule is None and face_ok
        v = getattr(r, "_video", None)
        r.fits_filters = r.fits_format and v is not None and passes_filters(v, t, now)


def fetch_covers(rows: List[ProbeRow], folder: Path, get: Callable = requests.get) -> Dict[str, Path]:
    """Download each cover once (cached by video id). Failures are skipped (cover_face stays None)."""
    folder.mkdir(parents=True, exist_ok=True)

    def one(r: ProbeRow) -> Optional[Path]:
        p = folder / f"{r.id}.jpg"
        if not p.exists() and r.cover_url:
            try:
                resp = get(r.cover_url, timeout=10)
                if resp.status_code == 200 and resp.content:
                    p.write_bytes(resp.content)
            except requests.RequestException:
                return None
        return p if p.exists() else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(one, rows))
    return {r.id: p for r, p in zip(rows, paths) if p is not None}


def summarize(rows: List[ProbeRow]) -> dict:
    return {
        "results": len(rows),
        "seed_rank": next((r.rank for r in rows if r.is_seed), None),
        "fits_format": sum(r.fits_format for r in rows),
        "fits_filters": sum(r.fits_filters for r in rows),
        "talking_head_covers": sum(1 for r in rows if (r.cover_face or 0) >= faces.min_height()),
        "creators_fitting": sorted({r.creator for r in rows if r.fits_format}),
    }


def row_dict(r: ProbeRow) -> dict:
    return asdict(r)
