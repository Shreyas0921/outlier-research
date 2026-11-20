"""Outlier scoring.

A video's score ("multiple") is its views divided by the median views of the same creator's
recent videos, leaving the video itself out of the median so one viral hit can't raise its
own baseline.

- Creator mode: every video in the creator's recent list is scored against the rest of that list.
- Keyword / niche mode: a two-stage funnel that saves credits.
    Stage 1 (free): filter by min views and age, then rank by views ÷ followers. Instagram search
              results have no follower count, so those rank by raw views. The two rankings are
              interleaved so neither platform crowds out the other.
    Stage 2 (paid): for the top N, fetch each creator's recent videos once and compute the real
              multiple. If the candidate also appears in that list, its count from the list is
              used, so candidate and baseline are measured with the same metric.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from statistics import median
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from lib.api_client import OutOfCredits
from lib.normalize import Video

MIN_BASELINE_VIDEOS = 5


@dataclass
class Scored:
    video: Video
    views: int  # the count the multiple was computed from
    multiple: float
    baseline: float  # creator's median views (excluding this video)
    baseline_n: int  # how many videos the median came from


@dataclass
class FunnelResult:
    outliers: List[Scored] = field(default_factory=list)
    checked: List[Scored] = field(default_factory=list)  # every Stage 2 candidate that got a score
    candidates: int = 0  # unique videos received
    skipped_seen: int = 0
    passed_filters: int = 0
    stage2: int = 0
    failures: List[Tuple[str, str]] = field(default_factory=list)  # (creator key, error)
    out_of_credits: bool = False  # Stage 2 stopped early; results so far are kept
    ai_skipped: int = 0  # dropped as AI-generated (label or blocked hashtag), in either stage
    rule_skipped: Dict[str, int] = field(default_factory=dict)  # free stage-1 rule drops by reason ("not English", "#parody")


# ---------------------------------------------------------------- basics


def median_baseline(views_by_id: Dict[str, int], exclude_id: Optional[str] = None,
                    min_n: int = MIN_BASELINE_VIDEOS) -> Tuple[Optional[float], int]:
    """Median views of a creator's recent videos, leaving out `exclude_id`.
    Returns (None, n) when fewer than `min_n` videos remain."""
    others = [v for vid, v in views_by_id.items() if vid != exclude_id and v is not None]
    if len(others) < min_n:
        return None, len(others)
    return float(median(others)), len(others)


def ai_reason(v: Video, thresholds: dict) -> Optional[str]:
    """Why a video counts as AI-generated ("label" or "#tag"), or None. Off unless `exclude_ai` is set."""
    if not thresholds.get("exclude_ai"):
        return None
    if v.ai_labeled:
        return "label"
    blocked = set(thresholds.get("ai_hashtags") or [])
    for tag in re.findall(r"#(\w+)", v.caption or ""):
        if tag.lower() in blocked:
            return f"#{tag.lower()}"
    return None


def rule_reason(v: Video, thresholds: dict) -> Optional[str]:
    """Free stage-1 format rules, or None:
    - english_only: the platform's spoken-language tag says it isn't English ("not English (es)").
      Unknown language (all Instagram) passes here and gets the audio check after download.
    - skip_hashtags: a caption hashtag marks a skipped format (skits, clip/fan pages). "*edit" matches any
      hashtag ending in "edit"; listing "pov" also catches captions that start with "POV:"."""
    if thresholds.get("english_only") and v.spoken_lang and not v.spoken_lang.startswith("en"):
        return f"not English ({v.spoken_lang})"
    blocked = thresholds.get("skip_hashtags") or []
    if not blocked:
        return None
    exact = {t for t in blocked if not t.startswith("*")}
    suffixes = tuple(t[1:] for t in blocked if t.startswith("*"))
    for tag in re.findall(r"#(\w+)", v.caption or ""):
        t = tag.lower()
        if t in exact or (suffixes and t.endswith(suffixes)):
            return f"#{t}"
    if "pov" in exact and re.match(r"\s*pov\b", v.caption or "", re.I):
        return "POV caption"
    return None


def passes_filters(v: Video, thresholds: dict, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if ai_reason(v, thresholds) or rule_reason(v, thresholds):
        return False
    if v.views is None or v.views < thresholds["min_views"]:
        return False
    if v.posted is None:
        return False  # can't confirm it's recent
    if now - v.posted > timedelta(days=thresholds["max_age_days"]):
        return False
    lo, hi = thresholds.get("min_duration_s"), thresholds.get("max_duration_s")
    if lo is not None or hi is not None:  # length filter applies to candidates only, never to baselines
        if v.duration_s is None:
            return False  # can't confirm the length
        if (lo is not None and v.duration_s < lo) or (hi is not None and v.duration_s > hi):
            return False
    floor = thresholds.get(f"min_likes_{v.platform}")  # per-platform likes floor, candidates only
    if floor is not None and (v.likes is None or v.likes < floor):
        return False  # unknown or hidden likes can't confirm the floor
    return True


def count_rule(counts: Dict[str, int], reason: str) -> None:
    key = "not English" if reason.startswith("not English") else reason
    counts[key] = counts.get(key, 0) + 1


def views_map(videos: Iterable[Video]) -> Dict[str, int]:
    return {v.id: v.views for v in videos if v.views is not None}


# ----------------------------------------------------------- creator mode


def score_creator(videos: List[Video], thresholds: dict, now: Optional[datetime] = None) -> List[Scored]:
    """Score each of a creator's recent videos against the rest. Returns outliers, best first."""
    vmap = views_map(videos)
    out = []
    for v in videos:
        if not passes_filters(v, thresholds, now):
            continue
        s = _score(v, vmap)
        if s and s.multiple >= thresholds["min_multiple"]:
            out.append(s)
    return sorted(out, key=lambda s: s.multiple, reverse=True)


# ------------------------------------------------------ keyword / niche mode


def stage1_rank(videos: List[Video]) -> List[Video]:
    """Rank by views ÷ followers where followers are known, raw views otherwise; interleave."""
    known = sorted((v for v in videos if v.followers), key=lambda v: v.views / v.followers, reverse=True)
    unknown = sorted((v for v in videos if not v.followers), key=lambda v: v.views, reverse=True)
    ranked = []
    for i in range(max(len(known), len(unknown))):
        ranked.extend(x[i] for x in (known, unknown) if i < len(x))
    return ranked


def score_candidates(
    videos: List[Video],
    thresholds: dict,
    recent_videos_for: Callable[[str, str], Dict[str, int]],
    seen: Optional[Set[str]] = None,
    now: Optional[datetime] = None,
    ai_labeled_ids: Optional[Callable[[str, str], Set[str]]] = None,
) -> FunnelResult:
    """Two-stage funnel. `recent_videos_for(platform, creator)` returns {video_id: views} for that
    creator's recent videos (the caller handles caching); it may raise, and errors are recorded
    per creator. OutOfCredits stops Stage 2 but keeps the outliers already found (`out_of_credits`).
    `ai_labeled_ids(platform, creator)` returns the ids the creator's own list labels AI-generated; Instagram
    search results carry no AI label, so this is the first point an Instagram candidate's label is known."""
    seen = seen or set()
    res = FunnelResult()

    unique: Dict[str, Video] = {}
    for v in videos:
        unique.setdefault(v.key, v)
    res.candidates = len(unique)

    fresh = []
    for v in unique.values():
        if v.key in seen:
            res.skipped_seen += 1
        else:
            fresh.append(v)

    not_ai = []
    for v in fresh:
        if ai_reason(v, thresholds):
            res.ai_skipped += 1
        elif rule_reason(v, thresholds):
            count_rule(res.rule_skipped, rule_reason(v, thresholds))
        else:
            not_ai.append(v)
    passing = [v for v in not_ai if passes_filters(v, thresholds, now)]
    res.passed_filters = len(passing)
    top = stage1_rank(passing)[: thresholds["stage2_top_n"]]
    res.stage2 = len(top)

    baselines: Dict[Tuple[str, str], Optional[Dict[str, int]]] = {}
    for v in top:
        ck = (v.platform, v.creator)
        if ck not in baselines:
            try:
                baselines[ck] = recent_videos_for(v.platform, v.creator)
            except OutOfCredits as e:
                res.out_of_credits = True
                res.failures.append((f"{v.platform}:{v.creator}", f"stopped: {e}"))
                break
            except Exception as e:  # one creator failing shouldn't stop the run
                baselines[ck] = None
                res.failures.append((f"{v.platform}:{v.creator}", f"{type(e).__name__}: {e}"))
        vmap = baselines[ck]
        if not vmap:
            continue
        if thresholds.get("exclude_ai") and ai_labeled_ids and v.id in ai_labeled_ids(v.platform, v.creator):
            res.ai_skipped += 1
            continue
        s = _score(v, vmap)
        if s is None:
            continue
        res.checked.append(s)
        if s.multiple >= thresholds["min_multiple"]:
            res.outliers.append(s)

    res.outliers.sort(key=lambda s: s.multiple, reverse=True)
    res.checked.sort(key=lambda s: s.multiple, reverse=True)
    return res


def _score(v: Video, vmap: Dict[str, int]) -> Optional[Scored]:
    views = vmap.get(v.id, v.views)  # same metric as the baseline when the list has it
    if views is None:
        return None
    base, n = median_baseline(vmap, exclude_id=v.id)
    if not base:
        return None
    # Keep the exact ratio: rounding here would let 4.9999x pass a 5x threshold. Round only for display.
    return Scored(video=v, views=views, multiple=views / base, baseline=base, baseline_n=n)
