"""Hunts: keep finding outliers until a goal number of new, kept notes is reached.

Two kinds of memory, both in state/ and written by the tool, never by hand:

- explored.json (shared by every hunt): each creator checked and each search term tried, with what came of
  it, so no hunt pays twice for the same creator or term.
      creators["tiktok:handle"] = {"banked_at", "pages", "median", "videos", "outliers", "notes",
                                   "stage2_at", "niche"}
      terms["tiktok:term"]      = {"probed_at", "results", "fits_format", "fits_filters", "creators",
                                   "keyword_run_at", "notes"}
- hunts/<id>.json (one per hunt): goal, budget, credits spent, start time. The count is never stored: it's
  worked out from seen.json, whose entries carry the hunt id (OUTLIER_HUNT) when a hunt saved them.

The agent's judgement (what to try next, dead ends) lives next to it in hunts/<id>-plan.md, which the
agent edits.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from lib.state import _read, _write_atomic

PLAN_TEMPLATE = """# Hunt {id}: {goal} new outliers

Written by the hunt agents. Each batch reads this first and updates it before it exits.
Facts (counts, credits, creators and terms already tried) come from `run.py hunt status {id}`, not from here.

## Creator queue (check next, best first, with why)

## Search terms to try (with why)

## Dead ends (don't retry)

## Notes for the next batch
"""


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _stamp(now: Optional[datetime] = None) -> str:
    return _now(now).isoformat(timespec="seconds")


def _age_days(iso: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    if not iso:
        return None
    return (_now(now) - datetime.fromisoformat(iso)).total_seconds() / 86400


# ------------------------------------------------------------------ explored


class Explored:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "explored.json"
        data = _read(self.path)
        self.creators: Dict[str, dict] = data.get("creators") or {}
        self.terms: Dict[str, dict] = data.get("terms") or {}

    def creator(self, platform: str, handle: str, **fields) -> dict:
        e = self.creators.setdefault(f"{platform}:{handle.lower()}", {})
        e.update({k: v for k, v in fields.items() if v is not None})
        return e

    def term(self, platform: str, term: str, **fields) -> dict:
        e = self.terms.setdefault(f"{platform}:{term.strip().lower()}", {})
        e.update({k: v for k, v in fields.items() if v is not None})
        return e

    def banked(self, platform: str, handle: str, within_days: float = 30, now: Optional[datetime] = None) -> bool:
        """Creator mode already ran on them recently (their outliers are saved)."""
        age = _age_days((self.creators.get(f"{platform}:{handle.lower()}") or {}).get("banked_at"), now)
        return age is not None and age <= within_days

    def probed(self, platform: str, term: str, within_days: float = 30, now: Optional[datetime] = None) -> Optional[dict]:
        e = self.terms.get(f"{platform}:{term.strip().lower()}") or {}
        age = _age_days(e.get("probed_at"), now)
        return e if age is not None and age <= within_days else None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(self.path, {"creators": self.creators, "terms": self.terms})


# --------------------------------------------------------------------- hunts


def hunts_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "hunts"


def new_id(goal: int, now: Optional[datetime] = None) -> str:
    return f"{_now(now).strftime('%Y%m%d-%H%M')}-{goal}"


def start(state_dir: Path, goal: int, budget: Optional[int], name: Optional[str] = None,
          now: Optional[datetime] = None) -> dict:
    if goal < 1:
        raise ValueError("goal must be at least 1")
    hid = re.sub(r"[^a-z0-9-]+", "-", (name or new_id(goal, now)).lower()).strip("-")
    d = hunts_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{hid}.json"
    if path.exists():
        raise ValueError(f"hunt {hid} already exists")
    hunt = {"id": hid, "goal": goal, "budget": budget, "credits_used": 0, "started_at": _stamp(now),
            "batches": 0}
    _write_atomic(path, hunt)
    (d / f"{hid}-plan.md").write_text(PLAN_TEMPLATE.format(id=hid, goal=goal))
    return hunt


def load(state_dir: Path, hid: str) -> dict:
    path = hunts_dir(state_dir) / f"{hid}.json"
    if not path.exists():
        raise ValueError(f"no hunt {hid}; run.py hunt list shows them")
    return json.loads(path.read_text())


def save(state_dir: Path, hunt: dict) -> None:
    _write_atomic(hunts_dir(state_dir) / f"{hunt['id']}.json", hunt)


def charge(state_dir: Path, hid: Optional[str], credits: int) -> None:
    """Add a command's credits to its hunt (no-op outside a hunt)."""
    if not hid or not credits:
        return
    try:
        hunt = load(state_dir, hid)
    except ValueError:
        return
    hunt["credits_used"] = int(hunt.get("credits_used") or 0) + int(credits)
    save(state_dir, hunt)


def list_hunts(state_dir: Path) -> List[dict]:
    d = hunts_dir(state_dir)
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


def status(state_dir: Path, hunt: dict, seen: Dict[str, dict], vault: Path, explored: Explored) -> dict:
    """Progress worked out from seen.json: kept = notes this hunt saved that still exist and weren't rejected."""
    hid = hunt["id"]
    mine = {k: e for k, e in seen.items() if e.get("hunt") == hid}
    kept = sorted(k for k, e in mine.items()
                  if e.get("note") and not e.get("rejected") and (Path(vault) / e["note"]).exists())
    rejected = sorted(k for k, e in mine.items() if e.get("rejected"))
    started = hunt["started_at"]
    budget = hunt.get("budget")
    spent = int(hunt.get("credits_used") or 0)
    stop_file = hunts_dir(state_dir) / f"{hid}.stop"
    reasons = []
    if len(kept) >= hunt["goal"]:
        reasons.append("goal reached")
    if budget is not None and spent >= budget:
        reasons.append("budget spent")
    if stop_file.exists():
        reasons.append("stopped by hand")
    return {
        "id": hid, "goal": hunt["goal"], "kept": len(kept), "rejected": len(rejected),
        "remaining": max(0, hunt["goal"] - len(kept)), "credits_used": spent, "budget": budget,
        "batches": hunt.get("batches", 0), "started_at": started,
        "creators_banked_in_hunt": sum(1 for e in explored.creators.values() if (e.get("banked_at") or "") >= started),
        "terms_probed_in_hunt": sum(1 for e in explored.terms.values() if (e.get("probed_at") or "") >= started),
        "creators_banked_total": sum(1 for e in explored.creators.values() if e.get("banked_at")),
        "terms_probed_total": sum(1 for e in explored.terms.values() if e.get("probed_at")),
        "kept_notes": [seen[k]["note"] for k in kept],
        "plan": str(hunts_dir(state_dir) / f"{hid}-plan.md"),
        "done": bool(reasons), "done_reasons": reasons,
    }


# ------------------------------------------------------------------- harvest


def harvest(sources: Iterable[dict], explored: Explored, in_niche: Dict[str, str], within_days: float = 30,
            now: Optional[datetime] = None, include_niches: bool = False) -> List[dict]:
    """Rank creators found in probe.json / summary.json files that haven't been banked yet.

    Evidence per creator: rows that fit the format, rows that also clear the filters (big recent videos), the
    biggest view count among fitting rows, and outliers already confirmed by a keyword run's creator check.
    Creators already banked within `within_days` are left out; creators in a niche but never banked stay in.
    include_niches adds every unbanked niche creator, so a hunt's first batch has a queue to start from."""
    found: Dict[str, dict] = {}

    def entry(platform: str, handle: str) -> dict:
        key = f"{platform}:{handle.lower()}"
        return found.setdefault(key, {"key": key, "platform": platform, "handle": handle.lower(), "fits_format": 0,
                                      "fits_filters": 0, "best_views": 0, "confirmed_outliers": 0,
                                      "best_multiple": 0.0, "terms": set()})

    for src in sources:
        for t in src.get("terms") or []:  # probe.json
            for r in t.get("rows") or []:
                if not r.get("fits_format"):
                    continue
                e = entry("tiktok", r["creator"])
                e["fits_format"] += 1
                e["fits_filters"] += int(bool(r.get("fits_filters")))
                e["best_views"] = max(e["best_views"], r.get("views") or 0)
                e["terms"].add(t.get("term"))
        for x in src.get("top_checked") or []:  # summary.json from a keyword / niche run
            if not x.get("is_outlier"):
                continue
            e = entry(x["platform"], x["creator"])
            e["confirmed_outliers"] += 1
            e["best_multiple"] = max(e["best_multiple"], x.get("outlier_score") or 0)
            e["best_views"] = max(e["best_views"], x.get("views") or 0)
            if src.get("value"):
                e["terms"].add(src["value"])
    if include_niches:  # every niche creator not banked yet, even without evidence in these files
        for key in in_niche:
            platform, handle = key.split(":", 1)
            entry(platform, handle)
    out = []
    for e in found.values():
        if explored.banked(e["platform"], e["handle"], within_days, now):
            continue
        e["niche"] = in_niche.get(e["key"])
        e["terms"] = sorted(x for x in e["terms"] if x)
        out.append(e)
    return sorted(out, key=lambda e: (e["confirmed_outliers"], e["fits_filters"], e["best_views"], e["fits_format"]),
                  reverse=True)
