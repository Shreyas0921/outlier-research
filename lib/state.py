"""Local state that survives between runs (in state/, git-ignored).

- seen.json       videos already turned into notes, keyed "platform:id", so no video is processed twice.
- baselines.json  each creator's recent {video_id: views}, keyed "platform:creator", reused for
                  7 days so the same creator's videos aren't paid for twice. Per-video counts are kept
                  (not just the median) so the scored video can still be left out of its own baseline.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from lib.normalize import Video

BASELINE_TTL_DAYS = 7


class State:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.seen: Dict[str, dict] = _read(self.dir / "seen.json")
        self.baselines: Dict[str, dict] = _read(self.dir / "baselines.json")

    # ------------------------------------------------------------- seen

    def is_seen(self, key: str) -> bool:
        return key in self.seen

    def seen_keys(self) -> set:
        return set(self.seen)

    def refresh_seen(self) -> None:
        """Pick up entries another run (or a synced copy) wrote to disk since this one loaded."""
        on_disk = _read(self.dir / "seen.json")
        self.seen = {**on_disk, **self.seen}

    def mark_seen(self, key: str, note: str, now: Optional[datetime] = None, rejected: Optional[str] = None,
                  hunt: Optional[str] = None) -> None:
        """`rejected` = why the video failed a content rule (no note was kept); it's still never re-processed.
        `hunt` = the hunt that found it (from OUTLIER_HUNT), so a hunt can count exactly what it added."""
        t = _now(now)
        self.seen[key] = {"note": note, "date": t.date().isoformat(), "at": t.isoformat(timespec="seconds")}
        if rejected:
            self.seen[key]["rejected"] = rejected
        hunt = hunt if hunt is not None else (os.environ.get("OUTLIER_HUNT") or None)
        if hunt:
            self.seen[key]["hunt"] = hunt

    # -------------------------------------------------------- baselines

    def get_baseline(self, platform: str, creator: str, now: Optional[datetime] = None) -> Optional[Dict[str, int]]:
        entry = self.baselines.get(f"{platform}:{creator.lower()}")
        if not entry:
            return None
        fetched = datetime.fromisoformat(entry["fetched_at"])
        if _now(now) - fetched > timedelta(days=BASELINE_TTL_DAYS):
            return None
        return dict(entry["views"])

    def put_baseline(self, platform: str, creator: str, videos: List[Video],
                     now: Optional[datetime] = None) -> Dict[str, int]:
        views = {v.id: v.views for v in videos if v.views is not None}
        metrics = sorted({v.view_metric for v in videos if v.views is not None})
        self.baselines[f"{platform}:{creator.lower()}"] = {
            "views": views,
            "ai": sorted(v.id for v in videos if v.ai_labeled),
            "metric": ",".join(metrics),
            "fetched_at": _now(now).isoformat(),
        }
        return views

    def ai_ids(self, platform: str, creator: str) -> set:
        """Ids the creator's last fetched list labeled AI-generated (empty if never fetched)."""
        return set((self.baselines.get(f"{platform}:{creator.lower()}") or {}).get("ai") or [])

    def cached_fetcher(self, fetch: Callable[[str, str], List[Video]],
                       now: Optional[datetime] = None) -> Callable[[str, str], Dict[str, int]]:
        """Wrap `fetch(platform, creator) -> [Video]` so cached creators cost nothing.
        `.fetched` on the returned function lists the creators that were actually fetched."""
        fetched: List[str] = []

        def recent_videos_for(platform: str, creator: str) -> Dict[str, int]:
            hit = self.get_baseline(platform, creator, now)
            if hit is not None:
                return hit
            fetched.append(f"{platform}:{creator}")
            return self.put_baseline(platform, creator, fetch(platform, creator), now)

        recent_videos_for.fetched = fetched  # type: ignore[attr-defined]
        return recent_videos_for

    # -------------------------------------------------------------- save

    def save(self) -> None:
        """Merges with what's on disk first, so two runs (or a run and a sync) never erase each other's
        entries; for a key both have, this run's version wins."""
        self.dir.mkdir(parents=True, exist_ok=True)
        self.refresh_seen()
        self.baselines = {**_read(self.dir / "baselines.json"), **self.baselines}
        _write_atomic(self.dir / "seen.json", self.seen)
        _write_atomic(self.dir / "baselines.json", self.baselines)


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is corrupted ({e}); fix or delete it") from e
    return data if isinstance(data, dict) else {}


def _write_atomic(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
