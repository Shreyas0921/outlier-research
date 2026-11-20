#!/usr/bin/env python3
"""Outlier research: find TikTok / Instagram outliers and save their hooks into the vault.

usage:
  run.py niche   <name>                              [--dry-run] [--max-credits N] [--vault PATH]
  run.py creator <handle | tiktok:h | ig:h | URL>    [--platform tiktok|ig] [--dry-run] ...
  run.py keyword "<term>"                            [--platform tiktok|ig|both] [--dry-run] ...
  run.py set-onscreen <note.md> [--text "..."]       (text from stdin if --text is omitted; "none" = no text)
  run.py reject <note.md> [...] --reason "skit"      (note + media to the Trash; never saved again)
  run.py add-fetch <handle> [<handle> ...]           [--platform tiktok|ig] [--max-credits N]
  run.py add-write <confirmed.json>                  (writes creators/niches to config.yaml)
  run.py niches
  run.py probe "<term>" ["<term>" ...]               [--seed URL] [--date all-time] [--repeat N] (search-term test)
  run.py harvest [<probe.json|summary.json> ...] [--niches]  (rank creators not banked yet; --niches adds niche creators)
  run.py hunt start --goal N [--budget C]            (a hunt: keep going until N new outliers are kept)
  run.py hunt status <id> [--json] | hunt list

Inside a hunt, set OUTLIER_HUNT=<id>: saved notes are tagged with it and credits are charged to it.

Every run writes runs/<timestamp>-<mode>-<slug>/summary.json, which the /outliers skill reads.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from lib import config as C  # noqa: E402
from lib import hunt as H  # noqa: E402
from lib import normalize as N  # noqa: E402
from lib import vault_writer as W  # noqa: E402
from lib.api_client import ApiError, OutOfCredits, ScrapeCreatorsClient  # noqa: E402
from lib.media import Checks, MediaError, Rejected, process as process_media  # noqa: E402
from lib.normalize import Video  # noqa: E402
from lib.scoring import (FunnelResult, Scored, ai_reason, count_rule, rule_reason, score_candidates,  # noqa: E402
                         score_creator)
from lib.state import State  # noqa: E402


def load_settings() -> Dict[str, str]:
    """Settings from .env (OUTLIERS_ENV_FILE points elsewhere; the tests point it at nothing), overridden by
    real environment variables of the same name."""
    from dotenv import dotenv_values
    env_file = Path(os.environ.get("OUTLIERS_ENV_FILE") or HERE / ".env")
    values = {k: v for k, v in (dotenv_values(env_file) if env_file.is_file() else {}).items() if v}
    keys = ("SCRAPECREATORS_API_KEY", "OUTLIERS_VAULT", "OUTLIERS_NOTES_DIR", "OUTLIERS_CREATORS_DIR")
    values.update({k: os.environ[k] for k in keys if os.environ.get(k)})
    return values


SETTINGS = load_settings()
# Where notes go: <vault>/<notes dir>. The vault is any folder (an Obsidian vault, or a plain folder);
# by default it is notes/ inside this project.
DEFAULT_VAULT = Path(SETTINGS.get("OUTLIERS_VAULT") or HERE / "notes").expanduser()
W.set_notes_dir(SETTINGS.get("OUTLIERS_NOTES_DIR") or "Outliers")
RAW_ROOT = Path.home() / ".cache/outlier-research/raw"  # raw API responses per run, outside the vault


@dataclass
class RunContext:
    client: ScrapeCreatorsClient
    cfg: C.Config
    state: State
    vault: Path
    run_dir: Path
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    process: Callable = process_media
    log: Callable[[str], None] = print
    explored: Optional[H.Explored] = None  # creators/terms tried, shared by all hunts (state/explored.json)


@dataclass
class Gathered:
    candidates: List[Video] = field(default_factory=list)
    sources: Dict[str, str] = field(default_factory=dict)  # video key -> first search that surfaced it
    failures: List[dict] = field(default_factory=list)
    out_of_credits: bool = False


# ------------------------------------------------------------------ fetching


def tiktok_date_filter(max_age_days: int) -> str:
    for limit, name in ((1, "yesterday"), (7, "this-week"), (30, "this-month"),
                        (90, "last-3-months"), (180, "last-6-months")):
        if max_age_days <= limit:
            return name
    return "all-time"


def ig_date_filter(max_age_days: int) -> str:
    return "last-week" if max_age_days <= 7 else "last-month" if max_age_days <= 30 else "last-year"


def creator_videos(ctx: RunContext, platform: str, handle: str, followers: Optional[int] = None) -> List[Video]:
    """A creator's recent videos (baseline_pages pages), normalized. Instagram reels carry no follower
    count, so one profile call fetches it unless `followers` is already known."""
    pages = ctx.cfg.thresholds["baseline_pages"]
    if platform == "tiktok":
        return N.tiktok_list(ctx.client.tiktok_profile_videos(handle, max_videos=500, max_pages=pages))
    if followers is None:
        try:
            followers = N.ig_followers(ctx.client.ig_profile(handle))  # cached by the API for 7 days
        except OutOfCredits:
            raise
        except ApiError:
            pass
    return N.ig_reels_list(ctx.client.ig_user_reels(handle=handle, max_reels=500, max_pages=pages),
                           creator=handle, followers=followers)


def _attempt(g: Gathered, label: str, fn: Callable[[], List[Video]], source: Optional[str] = None) -> List[Video]:
    if g.out_of_credits:
        return []
    try:
        videos = fn()
        g.candidates.extend(videos)
        if source:
            for v in videos:
                g.sources.setdefault(v.key, source)
        return videos
    except OutOfCredits as e:
        g.out_of_credits = True
        g.failures.append({"what": label, "error": f"stopped: {e}"})
    except Exception as e:  # one bad keyword/creator shouldn't sink the run
        g.failures.append({"what": label, "error": f"{type(e).__name__}: {e}"})
    return []


def gather_keyword(ctx: RunContext, term: str, platforms: List[str], g: Gathered) -> None:
    age = ctx.cfg.thresholds["max_age_days"]
    if "tiktok" in platforms:
        _attempt(g, f"tiktok keyword '{term}'", lambda: N.tiktok_list(
            ctx.client.tiktok_search(term, date_posted=tiktok_date_filter(age))), f'keyword "{term}"')
    if "ig" in platforms:
        _attempt(g, f"ig keyword '{term}'", lambda: N.ig_search_list(
            ctx.client.ig_reels_search(term, date_posted=ig_date_filter(age))), f'keyword "{term}"')


def gather_niche(ctx: RunContext, niche: C.Niche, g: Gathered) -> None:
    age = ctx.cfg.thresholds["max_age_days"]
    for kw in niche.tiktok_keywords:
        _attempt(g, f"tiktok keyword '{kw}'", lambda kw=kw: N.tiktok_list(
            ctx.client.tiktok_search(kw, date_posted=tiktok_date_filter(age))), f'keyword "{kw}"')
    for tag in niche.ig_hashtags:
        _attempt(g, f"ig hashtag #{tag}", lambda tag=tag: N.ig_search_list(
            ctx.client.ig_hashtag_search(tag, date_posted=ig_date_filter(age))), f"#{tag}")
    for kw in niche.ig_keywords:
        _attempt(g, f"ig keyword '{kw}'", lambda kw=kw: N.ig_search_list(
            ctx.client.ig_reels_search(kw, date_posted=ig_date_filter(age))), f'keyword "{kw}"')
    for platform in ("tiktok", "ig"):
        for handle in niche.creators(platform):
            videos = _attempt(g, f"{platform} creator @{handle}", lambda p=platform, h=handle: creator_videos(ctx, p, h),
                              f"creator @{handle}")
            if videos:  # their recent list doubles as their baseline, so Stage 2 costs nothing for them
                ctx.state.put_baseline(platform, handle, videos, ctx.now)


# --------------------------------------------------------------------- modes


def run_funnel(ctx: RunContext, g: Gathered) -> FunnelResult:
    fetcher = ctx.state.cached_fetcher(lambda p, c: creator_videos(ctx, p, c), ctx.now)
    res = score_candidates(g.candidates, ctx.cfg.thresholds, fetcher, seen=ctx.state.seen_keys(), now=ctx.now,
                           ai_labeled_ids=ctx.state.ai_ids)
    res.failures = [(f["what"], f["error"]) for f in g.failures] + res.failures
    res.out_of_credits = res.out_of_credits or g.out_of_credits
    return res


def run_creator(ctx: RunContext, platform: str, handle: str) -> FunnelResult:
    res = FunnelResult()
    try:
        videos = creator_videos(ctx, platform, handle)
    except OutOfCredits as e:
        res.out_of_credits = True
        res.failures.append((f"{platform} creator @{handle}", f"stopped: {e}"))
        return res
    except ApiError as e:
        res.failures.append((f"{platform} creator @{handle}", f"{type(e).__name__}: {e}"))
        return res
    ctx.state.put_baseline(platform, handle, videos, ctx.now)
    if ctx.explored is not None:
        views = sorted(v.views for v in videos if v.views is not None)
        ctx.explored.creator(platform, handle, banked_at=H._stamp(ctx.now), pages=ctx.cfg.thresholds["baseline_pages"],
                             videos=len(videos), median=views[len(views) // 2] if views else None,
                             niche=ctx.cfg.find_creator(platform, handle))
    res.candidates = len(videos)
    res.ai_skipped = sum(1 for v in videos if ai_reason(v, ctx.cfg.thresholds))
    for v in videos:
        if not ai_reason(v, ctx.cfg.thresholds) and rule_reason(v, ctx.cfg.thresholds):
            count_rule(res.rule_skipped, rule_reason(v, ctx.cfg.thresholds))
    scored = score_creator(videos, ctx.cfg.thresholds, ctx.now)
    seen = ctx.state.seen_keys()
    res.skipped_seen = sum(1 for s in scored if s.video.key in seen)
    res.outliers = [s for s in scored if s.video.key not in seen]
    res.passed_filters = res.stage2 = len(scored)
    res.checked = scored
    return res


# ------------------------------------------------------------------- process


def checks_for(v: Video, t: dict) -> Optional[Checks]:
    if not (t.get("english_only") or t.get("talking_head_only")):
        return None
    return Checks(english_only=bool(t.get("english_only")), talking_head_only=bool(t.get("talking_head_only")),
                  min_words_per_min=t.get("min_words_per_min") or 0, spoken_lang=v.spoken_lang)


def save_outliers(ctx: RunContext, outliers: List[Scored], found_for: Callable[[Scored], W.FoundVia]) -> tuple:
    """Returns (notes, failures, rejected). Rejected videos failed a content rule after download; they're
    marked seen (with the reason) so they're never downloaded again."""
    notes, failures, rejected = [], [], []
    W.ensure_base(ctx.vault)
    media_root = W.media_root(ctx.vault)
    for i, s in enumerate(outliers, 1):
        v = s.video
        ctx.state.refresh_seen()
        if ctx.state.is_seen(v.key):  # listed twice, or saved by another run since this one scored it
            ctx.log(f"  [{i}/{len(outliers)}] {v.platform} @{v.creator} — already saved, skipping")
            continue
        ctx.log(f"  [{i}/{len(outliers)}] {v.platform} @{v.creator} {s.multiple:.1f}x — downloading and transcribing...")
        checks = checks_for(v, ctx.cfg.thresholds)
        try:
            media = ctx.process(v, media_root, checks=checks) if checks else ctx.process(v, media_root)
            note = W.write_note(ctx.vault, s, found_for(s), media, today=ctx.now.date())
        except Rejected as e:
            ctx.log(f"      skipped: {e}")
            rejected.append({"platform": v.platform, "creator": v.creator, "url": v.url, "reason": str(e),
                             "outlier_score": round(s.multiple, 1), "views": s.views})
            ctx.state.mark_seen(v.key, "", ctx.now, rejected=str(e))
            ctx.state.save()
            continue
        except (MediaError, OSError) as e:
            failures.append({"what": f"{v.platform} {v.url}", "error": f"{type(e).__name__}: {e}"})
            continue  # not marked seen, so the next run retries it
        ctx.state.mark_seen(v.key, W.vault_rel(ctx.vault, note), ctx.now)
        ctx.state.save()  # after every note, so a crash never loses finished work
        notes.append({
            "note": W.vault_rel(ctx.vault, note),
            "note_abs": str(note),
            "stills_abs": [str(p) for p in media.stills],
            "platform": v.platform, "creator": v.creator, "url": v.url, "search": found_for(s).search,
            "outlier_score": round(s.multiple, 1), "views": s.views, "creator_median": int(s.baseline),
            "speech": media.transcript.speech, "spoken_hook": media.transcript.hook_text,
            "downloaded_via": media.downloaded_via,
        })
    return notes, failures, rejected


def execute(ctx: RunContext, mode: str, value: str, platforms: Optional[List[str]] = None,
            dry_run: bool = False) -> dict:
    started = datetime.now(timezone.utc)
    if mode == "creator":
        platform = platforms[0]
        res = run_creator(ctx, platform, value)
        niche = ctx.cfg.find_creator(platform, value)
        found = lambda s: W.FoundVia("creator", f"@{value}", niche=niche, search=f"creator @{value}")  # noqa: E731
    elif mode == "niche":
        niche = ctx.cfg.niche(value)
        g = Gathered()
        gather_niche(ctx, niche, g)
        res = run_funnel(ctx, g)
        found = lambda s: W.FoundVia("niche", niche.name, niche=niche.name,  # noqa: E731
                                     search=g.sources.get(s.video.key))
    elif mode == "keyword":
        g = Gathered()
        gather_keyword(ctx, value, platforms, g)
        res = run_funnel(ctx, g)
        found = lambda s: W.FoundVia("keyword", value, search=f'keyword "{value}"')  # noqa: E731
    else:
        raise ValueError(mode)

    notes, media_failures, rejected = ([], [], []) if dry_run else save_outliers(ctx, res.outliers, found)
    ctx.state.save()
    if ctx.explored is not None and not dry_run:
        record_explored(ctx, mode, value, platforms, res, notes)
    summary = {
        "mode": mode, "value": value, "platforms": platforms, "dry_run": dry_run,
        "started": started.isoformat(), "finished": datetime.now(timezone.utc).isoformat(),
        "candidates": res.candidates, "skipped_seen": res.skipped_seen, "ai_skipped": res.ai_skipped,
        "rule_skipped": res.rule_skipped, "rejected": rejected,
        "passed_filters": res.passed_filters, "stage2_checked": res.stage2,
        "outliers_found": len(res.outliers), "notes_written": len(notes),
        "notes": notes,
        "top_checked": [{"platform": s.video.platform, "creator": s.video.creator, "url": s.video.url,
                         "outlier_score": round(s.multiple, 2), "views": s.views,
                         "creator_median": int(s.baseline), "baseline_videos": s.baseline_n,
                         "is_outlier": s.multiple >= ctx.cfg.thresholds["min_multiple"]}
                        for s in res.checked[:15]],
        "failures": [{"what": w, "error": e} for w, e in res.failures] + media_failures,
        "out_of_credits": res.out_of_credits,
        "credits_used": ctx.client.credits_used, "credits_remaining": ctx.client.credits_remaining,
        "api_calls": ctx.client.calls, "thresholds": ctx.cfg.thresholds,
    }
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary


def record_explored(ctx: RunContext, mode: str, value: str, platforms: Optional[List[str]], res: FunnelResult,
                    notes: List[dict]) -> None:
    """Remember what this run tried, for hunts: the creator banked, or the term searched, and every creator whose
    baseline the creator check looked at."""
    ex, t = ctx.explored, H._stamp(ctx.now)
    if mode == "creator":
        ex.creator(platforms[0], value, outliers=len(res.checked), notes=len(notes))
    else:
        for s in res.checked:
            ex.creator(s.video.platform, s.video.creator, stage2_at=t, median=int(s.baseline))
        if mode == "keyword":
            for pf in platforms or []:
                n = sum(1 for x in notes if x["platform"] == pf)
                ex.term(pf, value, keyword_run_at=t, notes=n)
    ex.save()


def print_summary(s: dict, run_dir: Path) -> None:
    what = f"{s['mode']} '{s['value']}'" + (" (dry run)" if s["dry_run"] else "")
    t = s["thresholds"]
    print(f"\n== {what}")
    print(f"filters: ≥{t['min_multiple']:g}x creator median · ≥{t['min_views']:,} views · last {t['max_age_days']} days"
          f" · length {C.fmt_duration(t.get('min_duration_s'))}–{C.fmt_duration(t.get('max_duration_s'))}"
          f" · likes ≥{C.fmt_count(t.get('min_likes_tiktok'))} TikTok / ≥{C.fmt_count(t.get('min_likes_ig'))} IG"
          f" · AI videos {'excluded' if t.get('exclude_ai') else 'allowed'}"
          f"{' · English only' if t.get('english_only') else ''}"
          f"{' · talking heads only' if t.get('talking_head_only') else ''}"
          f" · creator check on top {t['stage2_top_n']}")
    rules = sum((s.get("rule_skipped") or {}).values())
    print(f"candidates {s['candidates']} · already saved {s['skipped_seen']} · AI skipped {s.get('ai_skipped', 0)}"
          f" · format/language skipped {rules}"
          f" · passed filters "
          f"{s['passed_filters']} · creator-checked {s['stage2_checked']} · outliers {s['outliers_found']}"
          + ("" if s["dry_run"] else f" · notes written {s['notes_written']}"))
    rows = s["notes"] or [x for x in s["top_checked"] if x["is_outlier"]]
    for x in rows[:10]:
        print(f"  {x['outlier_score']:>6}x  {x['views']:>11,} views  {x['platform']:6} @{x['creator']}  "
              f"{x.get('note') or x['url']}")
    if s["dry_run"] and not rows and s["top_checked"]:
        print("  no outliers; best checked: " + ", ".join(
            f"@{x['creator']} {x['outlier_score']}x" for x in s["top_checked"][:5]))
    for r in s.get("rejected") or []:
        print(f"  - rejected @{r['creator']} ({r['platform']}): {r['reason']}")
    for f in s["failures"]:
        print(f"  ! {f['what']}: {f['error']}")
    if s["out_of_credits"]:
        print("  ! stopped early: out of credits / budget reached (results so far were kept)")
    print(f"credits used {s['credits_used']} (remaining {s['credits_remaining']}) · {s['api_calls']} API calls")
    print(f"summary: {run_dir / 'summary.json'}")


# ----------------------------------------------------------- creator management

# Optional vault folder whose subfolders name niches you already use (e.g. Creators/Fitness). Missing = ignored.
CREATORS_DIR = SETTINGS.get("OUTLIERS_CREATORS_DIR") or "Creators"


def known_niches(cfg: C.Config, vault: Path) -> List[str]:
    """Niches in config.yaml plus the niche folders the user already keeps under CREATORS_DIR."""
    names = list(cfg.niches)
    folder = Path(vault) / CREATORS_DIR
    if folder.is_dir():
        for d in sorted(folder.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and d.name.lower() not in (n.lower() for n in names):
                names.append(d.name)
    return names


def add_fetch(ctx: RunContext, raw_handles: List[str], platform_flag: Optional[str]) -> dict:
    """Fetch profile + recent captions for each creator so Claude can propose a niche.
    The recent videos are also cached as the creator's baseline, so the first niche run is cheaper."""
    rows = []
    for raw in raw_handles:
        try:
            platform, handle = C.parse_handle(raw, default_platform=platform_flag)
        except C.ConfigError as e:
            rows.append({"input": raw, "error": str(e)})
            continue
        if not platform:
            listed = [p for p in ("tiktok", "ig") if ctx.cfg.find_creator(p, handle)]
            if len(listed) == 1:  # a bare handle the config already knows
                platform = listed[0]
        if not platform:
            rows.append({"input": raw, "handle": handle, "error": "needs_platform",
                         "hint": f"say tiktok:{handle} or ig:{handle}"})
            continue
        row = {"input": raw, "handle": handle, "platform": platform,
               "already_in_niche": ctx.cfg.find_creator(platform, handle)}
        if row["already_in_niche"]:
            rows.append(row)
            continue
        try:
            if platform == "tiktok":
                prof = ctx.client.tiktok_profile(handle)
                user = prof.get("user") or {}
                row.update(name=user.get("nickname"), bio=user.get("signature") or "",
                           followers=N.tiktok_followers(prof))
            else:
                prof = ctx.client.ig_profile(handle)
                user = (prof.get("data") or {}).get("user") or {}
                row.update(name=user.get("full_name"), bio=user.get("biography") or "",
                           followers=N.ig_followers(prof))
            videos = creator_videos(ctx, platform, handle, followers=row.get("followers"))
            ctx.state.put_baseline(platform, handle, videos, ctx.now)
            views = sorted(v.views for v in videos if v.views is not None)
            row.update(videos=len(videos), median_views=views[len(views) // 2] if views else None,
                       captions=[v.caption[:300] for v in videos if v.caption][:30])
            if ctx.explored is not None:
                ctx.explored.creator(platform, handle, stage2_at=H._stamp(ctx.now), median=row["median_views"],
                                     videos=len(videos))
        except OutOfCredits as e:
            row["error"] = f"stopped: {e}"
            rows.append(row)
            break
        except ApiError as e:
            row["error"] = f"{type(e).__name__}: {e}"
        rows.append(row)
    ctx.state.save()
    if ctx.explored is not None:
        ctx.explored.save()
    out = {"known_niches": known_niches(ctx.cfg, ctx.vault), "creators": rows,
           "credits_used": ctx.client.credits_used, "credits_remaining": ctx.client.credits_remaining}
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / "add_candidates.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    return out


def add_write(cfg: C.Config, data: dict) -> dict:
    """Apply confirmed niche choices: {"creators": [{handle, platform, niche}],
    "niche_searches": {niche: {tiktok_keywords: [...], ig_hashtags: [...], ig_keywords: [...]}}}."""
    results = []
    for row in data.get("creators") or []:
        status = C.add_creator(cfg, row["niche"], row["platform"], row["handle"])
        results.append({"handle": row["handle"], "platform": row["platform"], "niche": row["niche"], "result": status})
    searches = {}
    for niche, keys in (data.get("niche_searches") or {}).items():
        for key, values in (keys or {}).items():
            added = C.add_searches(cfg, niche, key, values or [])
            if added:
                searches.setdefault(niche, {})[key] = added
    path = C.save(cfg)
    return {"creators": results, "searches_added": searches, "config": str(path)}


def format_niches(cfg: C.Config, vault: Path) -> str:
    if not cfg.niches:
        folders = [n for n in known_niches(cfg, vault)]
        return ("No niches in config.yaml yet. Add creators with `/outliers add @handle`."
                + (f"\nNiche folders under {CREATORS_DIR}: {', '.join(folders)}" if folders else ""))
    lines = []
    for n in cfg.niches.values():
        lines.append(f"{n.name}")
        for label, key in (("TikTok creators", "tiktok_creators"), ("Instagram creators", "ig_creators"),
                           ("TikTok keywords", "tiktok_keywords"), ("Instagram hashtags", "ig_hashtags"),
                           ("Instagram keywords", "ig_keywords")):
            values = getattr(n, key)
            if values:
                shown = ", ".join(("#" if key == "ig_hashtags" else "@" if "creators" in key else "") + v for v in values)
                lines.append(f"  {label}: {shown}")
    return "\n".join(lines)


# ----------------------------------------------------------------------- CLI


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "run"


def resolve_creator_platform(cfg: C.Config, raw: str, flag: Optional[str]) -> tuple:
    platform, handle = C.parse_handle(raw, default_platform=flag)
    if platform:
        return platform, handle
    in_niches = [p for p in ("tiktok", "ig") if cfg.find_creator(p, handle)]
    if len(in_niches) == 1:
        return in_niches[0], handle
    raise C.ConfigError(f"Which platform is @{handle} on? Use tiktok:{handle} or ig:{handle} (or --platform).")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("niche", "niche name from config.yaml"), ("creator", "creator handle"),
                        ("keyword", "search term")):
        sp = sub.add_parser(name)
        sp.add_argument("value", help=help_)
        sp.add_argument("--dry-run", action="store_true", help="fetch and score only; download nothing")
        sp.add_argument("--max-credits", type=int, default=None, help="stop fetching after this many credits")
        sp.add_argument("--min-length", default=None, help="only videos at least this long, e.g. 1:00 (off = no limit)")
        sp.add_argument("--max-length", default=None, help="only videos at most this long, e.g. 3:00 (off = no limit)")
        sp.add_argument("--min-likes-tiktok", default=None, help="only TikToks with at least this many likes, e.g. 50k (off = no limit)")
        sp.add_argument("--min-likes-ig", default=None, help="only Instagram reels with at least this many likes, e.g. 10k (off = no limit)")
        sp.add_argument("--allow-ai", action="store_true", help="keep AI-labeled / AI-hashtagged videos this run")
        sp.add_argument("--min-views", default=None, help="only videos with at least this many views, e.g. 500k")
        sp.add_argument("--any-language", action="store_true", help="don't skip non-English videos this run")
        sp.add_argument("--any-format", action="store_true",
                        help="don't require a talking head or skip skit/clip hashtags this run")
        sp.add_argument("--top-n", type=int, default=None, help="how many candidates get the paid creator check")
        sp.add_argument("--baseline-pages", type=int, default=None,
                        help="pages of each creator's videos to fetch (~10 videos and 1 credit each; 10 reaches ~2 years)")
        sp.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
        sp.add_argument("--config", type=Path, default=HERE / "config.yaml")
        if name == "creator":
            sp.add_argument("--platform", choices=["tiktok", "ig"])
        if name == "keyword":
            sp.add_argument("--platform", choices=["tiktok", "ig", "both"], default="both")
    rj = sub.add_parser("reject", help="move notes that break the content rules to the Trash; never save them again")
    rj.add_argument("notes", type=Path, nargs="+")
    rj.add_argument("--reason", required=True)
    rj.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    so = sub.add_parser("set-onscreen")
    so.add_argument("note", type=Path)
    so.add_argument("--text", default=None)
    af = sub.add_parser("add-fetch", help="fetch profiles + captions so niches can be proposed")
    af.add_argument("handles", nargs="+")
    af.add_argument("--platform", choices=["tiktok", "ig"])
    af.add_argument("--max-credits", type=int, default=None)
    af.add_argument("--baseline-pages", type=int, default=None)
    af.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    af.add_argument("--config", type=Path, default=HERE / "config.yaml")
    aw = sub.add_parser("add-write", help="write confirmed creators/niches to config.yaml")
    aw.add_argument("json_file", type=Path)
    aw.add_argument("--config", type=Path, default=HERE / "config.yaml")
    pr = sub.add_parser("probe", help="list every TikTok search result for a term, graded; flags a seed video")
    pr.add_argument("terms", nargs="+")
    pr.add_argument("--seed", default=None, help="seed video URL or id to look for in the results")
    pr.add_argument("--date", default=None, help="TikTok date window (default: from max_age_days)",
                    choices=["yesterday", "this-week", "this-month", "last-3-months", "last-6-months", "all-time"])
    pr.add_argument("--sort", default=None, help="TikTok sort_by (default: relevance)")
    pr.add_argument("--pages", type=int, default=1, help="search pages per call (1 credit each)")
    pr.add_argument("--repeat", type=int, default=1, help="calls per term (results vary per call)")
    pr.add_argument("--quiet", action="store_true", help="summary lines only, no per-result table")
    pr.add_argument("--force", action="store_true", help="probe terms even if they were probed in the last 30 days")
    pr.add_argument("--config", type=Path, default=HERE / "config.yaml")
    hv = sub.add_parser("harvest", help="rank creators found in probe/summary files that haven't been banked yet")
    hv.add_argument("files", type=Path, nargs="*")
    hv.add_argument("--niches", action="store_true", help="also list every niche creator not banked yet")
    hv.add_argument("--top", type=int, default=25)
    hv.add_argument("--json", action="store_true")
    hv.add_argument("--config", type=Path, default=HERE / "config.yaml")
    hu = sub.add_parser("hunt", help="start a hunt or check its progress")
    hsub = hu.add_subparsers(dest="hunt_cmd", required=True)
    hs = hsub.add_parser("start")
    hs.add_argument("--goal", type=int, required=True, help="new outlier notes to keep")
    hs.add_argument("--budget", type=int, default=None, help="credits the hunt may spend")
    hs.add_argument("--name", default=None, help="hunt id (default: <date>-<time>-<goal>)")
    hst = hsub.add_parser("status")
    hst.add_argument("id")
    hst.add_argument("--json", action="store_true")
    hst.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    hb = hsub.add_parser("batch", help="count one more agent batch (used by bin/outliers-hunt.sh)")
    hb.add_argument("id")
    hsub.add_parser("list")
    ni = sub.add_parser("niches", help="list niches")
    ni.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    ni.add_argument("--config", type=Path, default=HERE / "config.yaml")
    return p


def reject_note(state: State, vault: Path, note: Path, reason: str, trash_root: Optional[Path] = None) -> dict:
    """Move a note and its media folder to the macOS Trash (recoverable), and keep the video in seen.json
    marked rejected so no run ever saves it again."""
    note = note.resolve()
    if not note.exists():
        raise FileNotFoundError(f"no such note: {note}")
    text = note.read_text()
    m = re.search(r"_media/([^/\]|]+)/", text)
    media = W.media_root(vault) / m.group(1) if m else None
    trash = (trash_root or Path.home() / ".Trash") / f"outliers-rejected-{datetime.now().strftime('%Y%m%d')}"
    (trash / "_media").mkdir(parents=True, exist_ok=True)
    rel = W.vault_rel(vault, note)
    key = next((k for k, e in state.seen.items() if e.get("note") == rel), None)
    shutil.move(str(note), str(_free_path(trash / note.name)))
    if media and media.exists():
        shutil.move(str(media), str(_free_path(trash / "_media" / media.name)))
    if key:
        state.seen[key]["rejected"] = reason
        state.seen[key]["note"] = ""
        state.save()
    return {"note": rel, "key": key, "trash": str(trash)}


def _free_path(p: Path) -> Path:
    i = 2
    out = p
    while out.exists():
        out = p.with_name(f"{p.stem} ({i}){p.suffix}")
        i += 1
    return out


def apply_run_overrides(cfg: C.Config, args: argparse.Namespace) -> None:
    """--min-length / --max-length / --min-likes-* / --min-views / --any-* / --top-n change thresholds for this run only
    (config.yaml is untouched)."""
    if args.min_length is not None:
        cfg.thresholds["min_duration_s"] = C.parse_duration(args.min_length, "--min-length")
    if args.max_length is not None:
        cfg.thresholds["max_duration_s"] = C.parse_duration(args.max_length, "--max-length")
    for flag in ("min_likes_tiktok", "min_likes_ig"):
        value = getattr(args, flag, None)
        if value is not None:
            cfg.thresholds[flag] = C.parse_count(value, "--" + flag.replace("_", "-"))
    if getattr(args, "allow_ai", False):
        cfg.thresholds["exclude_ai"] = False
    if getattr(args, "min_views", None) is not None:
        n = C.parse_count(args.min_views, "--min-views")
        if not n:
            raise C.ConfigError("--min-views must be a positive number like 500k")
        cfg.thresholds["min_views"] = n
    if getattr(args, "any_language", False):
        cfg.thresholds["english_only"] = False
    if getattr(args, "any_format", False):
        cfg.thresholds["talking_head_only"] = False
        cfg.thresholds["skip_hashtags"] = []
    if getattr(args, "baseline_pages", None) is not None:
        if args.baseline_pages < 1:
            raise C.ConfigError("--baseline-pages must be at least 1")
        cfg.thresholds["baseline_pages"] = args.baseline_pages
    if args.top_n is not None:
        if args.top_n < 1:
            raise C.ConfigError("--top-n must be at least 1")
        cfg.thresholds["stage2_top_n"] = args.top_n
    C.check_duration_range(cfg.thresholds)


def run_probe(cfg: C.Config, args: argparse.Namespace) -> int:
    from lib import faces, probe as P
    t = cfg.thresholds
    seed = None
    if args.seed:
        m = re.search(r"(\d{15,})", args.seed)
        seed = m.group(1) if m else None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = HERE / "runs" / f"{stamp}-probe-{slug(args.terms[0])}"
    client = make_client(run_dir, None)
    covers = Path.home() / ".cache/outlier-research/covers"
    args.date = args.date or tiktok_date_filter(t["max_age_days"])
    report = {"seed": seed, "date": args.date, "sort": args.sort, "terms": []}
    explored = H.Explored(HERE / "state")
    for term in args.terms:
        before = explored.probed("tiktok", term)
        if before and not args.force and not seed:
            print(f"== {term}: already probed {before['probed_at'][:10]} (fits format {before.get('fits_format')}/"
                  f"{before.get('results')}, fits filters {before.get('fits_filters')}); skipped, --force to redo")
            continue
        rows: List = []
        seen_ids = set()
        seed_ranks = []
        for _ in range(max(1, args.repeat)):
            try:
                items = client.tiktok_search(term, date_posted=args.date, sort_by=args.sort, pages=args.pages)
            except OutOfCredits:
                raise
            except ApiError as e:
                print(f"== {term}: failed ({e})")
                continue
            got = P.rows_from_search(items, t, seed)
            seed_ranks.append(next((r.rank for r in got if r.is_seed), None))
            for r in got:
                if r.id not in seen_ids:
                    seen_ids.add(r.id)
                    rows.append(r)
        imgs = P.fetch_covers(rows, covers)
        heights = faces.detect(list(imgs.values())) if imgs else {}
        P.grade(rows, t, {vid: heights.get(str(p), []) for vid, p in imgs.items()})
        summ = P.summarize(rows)
        summ["seed_ranks"] = seed_ranks
        report["terms"].append({"term": term, **summ, "rows": [P.row_dict(r) for r in rows]})
        explored.term("tiktok", term, probed_at=H._stamp(), results=summ["results"], fits_format=summ["fits_format"],
                      fits_filters=summ["fits_filters"], creators=summ["creators_fitting"][:30],
                      probe=str(run_dir / "probe.json"))
        explored.save()
        seed_txt = f" · seed rank {seed_ranks}" if seed else ""
        print(f"== {term}: {summ['results']} results · fits format {summ['fits_format']} · "
              f"fits filters {summ['fits_filters']} · face covers {summ['talking_head_covers']}{seed_txt}")
        if not args.quiet:
            for r in rows:
                mark = "SEED" if r.is_seed else ("ok  " if r.fits_filters else ("fmt " if r.fits_format else "    "))
                face = "  ?" if r.cover_face is None else f"{r.cover_face:.2f}"
                print(f"  {mark} {r.rank:>2} {r.views or 0:>11,} {r.age_days if r.age_days is not None else '?':>4}d "
                      f"{int(r.duration_s or 0):>4}s {r.lang or '?':>3} f{face} @{r.creator:<22} "
                      f"{(r.rule or '')[:18]:<18} {r.caption[:70]}")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "probe.json").write_text(json.dumps(report, indent=1, default=str))
    H.charge(HERE / "state", os.environ.get("OUTLIER_HUNT"), client.credits_used)
    print(f"credits used {client.credits_used} (remaining {client.credits_remaining})")
    print(f"probe: {run_dir / 'probe.json'}")
    return 0


def run_harvest(cfg: C.Config, args: argparse.Namespace) -> int:
    sources = [json.loads(f.read_text()) for f in args.files]
    in_niche = {f"{pf}:{h.lower()}": n.name for n in cfg.niches.values() for pf in ("tiktok", "ig")
                for h in n.creators(pf)}
    ranked = H.harvest(sources, H.Explored(HERE / "state"), in_niche, include_niches=args.niches)[:args.top]
    if args.json:
        print(json.dumps(ranked, indent=1))
        return 0
    if not ranked:
        print("no unbanked creators in these files")
    for e in ranked:
        conf = f" · {e['confirmed_outliers']} confirmed outlier(s) up to {e['best_multiple']:g}x" if e["confirmed_outliers"] else ""
        print(f"  {e['platform']}:{e['handle']:<24} fits format {e['fits_format']} · big recent {e['fits_filters']}"
              f" · best {e['best_views']:,} views{conf}{' · in ' + e['niche'] if e['niche'] else ''}"
              f" · via {', '.join(e['terms'])[:60]}")
    return 0


def run_hunt(args: argparse.Namespace) -> int:
    sd = HERE / "state"
    if args.hunt_cmd == "start":
        h = H.start(sd, args.goal, args.budget, args.name)
        print(h["id"])
        return 0
    if args.hunt_cmd == "list":
        for h in H.list_hunts(sd):
            print(f"{h['id']}: goal {h['goal']}, started {h['started_at']}, credits {h.get('credits_used', 0)}"
                  f"/{h.get('budget') or 'no budget'}, batches {h.get('batches', 0)}")
        return 0
    hunt = H.load(sd, args.id)
    if args.hunt_cmd == "batch":
        hunt["batches"] = int(hunt.get("batches") or 0) + 1
        H.save(sd, hunt)
        print(hunt["batches"])
        return 0
    st = H.status(sd, hunt, State(sd).seen, args.vault.resolve(), H.Explored(sd))
    if args.json:
        print(json.dumps(st, indent=1))
        return 0
    print(f"hunt {st['id']}: {st['kept']}/{st['goal']} kept ({st['rejected']} rejected on review) · "
          f"credits {st['credits_used']}/{st['budget'] or 'no budget'} · batches {st['batches']}")
    print(f"this hunt: {st['creators_banked_in_hunt']} creators banked, {st['terms_probed_in_hunt']} terms probed · "
          f"all time: {st['creators_banked_total']} creators, {st['terms_probed_total']} terms")
    print(f"plan: {st['plan']}")
    print("DONE: " + ", ".join(st["done_reasons"]) if st["done"] else f"{st['remaining']} to go")
    return 0


def make_client(run_dir: Path, max_credits: Optional[int]) -> ScrapeCreatorsClient:
    key = SETTINGS.get("SCRAPECREATORS_API_KEY") or ""
    # Raw API responses are debug copies the tool never reads back, and they add up to hundreds of MB, so they live
    # outside the vault (they once pushed it over the Obsidian Sync size limit). summary.json stays in runs/.
    return ScrapeCreatorsClient(key, run_dir=RAW_ROOT / run_dir.name, max_credits=max_credits)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "set-onscreen":
        text = args.text if args.text is not None else sys.stdin.read()
        ok = W.set_onscreen_text(args.note, text)
        print("updated" if ok else "already filled (no change)")
        return 0

    if args.cmd == "reject":
        state = State(HERE / "state")
        for n in args.notes:
            r = reject_note(state, args.vault.resolve(), n, args.reason)
            print(f"rejected {r['note']} ({r['key'] or 'not in seen.json'}) -> {r['trash']}")
        return 0
    if args.cmd == "hunt":
        return run_hunt(args)
    cfg = C.load(args.config)
    if args.cmd == "probe":
        return run_probe(cfg, args)
    if args.cmd == "harvest":
        return run_harvest(cfg, args)
    if args.cmd == "niches":
        print(format_niches(cfg, args.vault))
        return 0
    if args.cmd == "add-write":
        result = add_write(cfg, json.loads(args.json_file.read_text()))
        print(json.dumps(result, indent=1))
        return 0
    if args.cmd == "add-fetch":
        if args.baseline_pages:
            cfg.thresholds["baseline_pages"] = args.baseline_pages
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = HERE / "runs" / f"{stamp}-add"
        ctx = RunContext(client=make_client(run_dir, args.max_credits), cfg=cfg, state=State(HERE / "state"),
                         vault=args.vault.resolve(), run_dir=run_dir, explored=H.Explored(HERE / "state"))
        try:
            out = add_fetch(ctx, args.handles, args.platform)
        finally:
            H.charge(HERE / "state", os.environ.get("OUTLIER_HUNT"), ctx.client.credits_used)
        for r in out["creators"]:
            status = r.get("error") or (f"already in {r['already_in_niche']}" if r.get("already_in_niche")
                                        else f"{r.get('followers') or 0:,} followers, {r.get('videos', 0)} videos")
            print(f"  {r.get('platform') or '?':6} @{r.get('handle') or r['input']}: {status}")
        print(f"credits used {out['credits_used']} (remaining {out['credits_remaining']})")
        print(f"candidates: {run_dir / 'add_candidates.json'}")
        return 0
    apply_run_overrides(cfg, args)
    if args.cmd == "creator":
        platform, value = resolve_creator_platform(cfg, args.value, args.platform)
        platforms = [platform]
    elif args.cmd == "keyword":
        value = args.value.strip()
        platforms = ["tiktok", "ig"] if args.platform == "both" else [args.platform]
    else:
        value = cfg.niche(args.value).name  # fails early with the list of known niches
        platforms = None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = HERE / "runs" / f"{stamp}-{args.cmd}-{slug(value)}"
    ctx = RunContext(client=make_client(run_dir, args.max_credits), cfg=cfg, state=State(HERE / "state"),
                     vault=args.vault.resolve(), run_dir=run_dir, explored=H.Explored(HERE / "state"))
    try:
        summary = execute(ctx, args.cmd, value, platforms, dry_run=args.dry_run)
    finally:
        H.charge(HERE / "state", os.environ.get("OUTLIER_HUNT"), ctx.client.credits_used)
    print_summary(summary, run_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except C.ConfigError as e:
        sys.exit(f"config: {e}")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
