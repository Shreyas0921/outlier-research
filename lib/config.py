"""Load, validate and update config.yaml (niches + scoring thresholds)."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

DEFAULT_THRESHOLDS = {
    "min_multiple": 5.0,
    "min_views": 100_000,
    "max_age_days": 730,
    "stage2_top_n": 20,
    "baseline_pages": 3,
    "min_duration_s": None,  # video length filter for candidates (None = off); config accepts "1:00" or 60
    "max_duration_s": None,
    "min_likes_tiktok": None,  # like-count floor per platform for candidates (None = off); accepts 50000 or "50k"
    "min_likes_ig": None,
    "exclude_ai": False,  # drop videos the platform labels AI-generated, or tagged with an ai_hashtags entry
    "ai_hashtags": [],
    "english_only": False,  # skip videos whose speech isn't English (platform tag, else a 30 s audio check)
    "talking_head_only": False,  # keep only videos where a person talks to camera (face check + speaking rate)
    "skip_hashtags": [],  # caption hashtags that mark a format to skip (skits, clip/fan pages); "*edit" = any tag ending in edit
    "min_words_per_min": 60,  # talking-head check: slower speech means music/text video, not a person talking
}
DURATION_KEYS = ("min_duration_s", "max_duration_s")
LIKES_KEYS = ("min_likes_tiktok", "min_likes_ig")
BOOL_KEYS = ("exclude_ai", "english_only", "talking_head_only")
HASHTAG_KEYS = ("ai_hashtags", "skip_hashtags")
NICHE_KEYS = ("tiktok_creators", "ig_creators", "tiktok_keywords", "ig_hashtags", "ig_keywords")
CREATOR_KEY = {"tiktok": "tiktok_creators", "ig": "ig_creators"}
HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
PLATFORM_ALIASES = {"tiktok": "tiktok", "tt": "tiktok", "ig": "ig", "insta": "ig", "instagram": "ig"}

HEADER = (
    "# Outlier research config. See config.example.yaml for what each setting means.\n"
    "# Edit by hand, or add creators with `/outliers add @handle`.\n\n"
)


class ConfigError(ValueError):
    pass


@dataclass
class Niche:
    name: str
    tiktok_creators: List[str] = field(default_factory=list)
    ig_creators: List[str] = field(default_factory=list)
    tiktok_keywords: List[str] = field(default_factory=list)
    ig_hashtags: List[str] = field(default_factory=list)
    ig_keywords: List[str] = field(default_factory=list)

    def creators(self, platform: str) -> List[str]:
        return getattr(self, CREATOR_KEY[platform])


@dataclass
class Config:
    thresholds: Dict[str, float]
    niches: Dict[str, Niche]
    path: Optional[Path] = None

    def niche(self, name: str) -> Niche:
        for key, n in self.niches.items():
            if key.lower() == name.lower():
                return n
        known = ", ".join(self.niches) or "(none yet)"
        raise ConfigError(f"No niche called '{name}'. Known niches: {known}")

    def find_creator(self, platform: str, handle: str) -> Optional[str]:
        """Name of the niche that already lists this creator, if any."""
        handle = handle.lower()
        for n in self.niches.values():
            if handle in (h.lower() for h in n.creators(platform)):
                return n.name
        return None


# ------------------------------------------------------------------ handles


def parse_handle(text: str, default_platform: Optional[str] = None) -> Tuple[Optional[str], str]:
    """'@x', 'x', 'tiktok:x', 'ig:@x', or a profile URL  ->  (platform or None, 'x')."""
    s = text.strip()
    platform = None
    url = re.match(r"^(?:https?://)?(?:www\.|m\.)?(tiktok|instagram)\.com/@?([A-Za-z0-9._]+)", s, re.I)
    if url:
        platform = "tiktok" if url.group(1).lower() == "tiktok" else "ig"
        s = url.group(2)
    elif ":" in s:
        prefix, _, rest = s.partition(":")
        if prefix.lower() not in PLATFORM_ALIASES:
            raise ConfigError(f"Unknown platform prefix '{prefix}:' in '{text}' (use tiktok: or ig:)")
        platform, s = PLATFORM_ALIASES[prefix.lower()], rest.strip()
    handle = s.lstrip("@").strip().rstrip("/")
    if not HANDLE_RE.match(handle):
        raise ConfigError(f"'{text}' doesn't look like a valid handle")
    return platform or default_platform, handle.lower()


# --------------------------------------------------------------- load/save


def load(path: Path) -> Config:
    path = Path(path)
    if not path.exists():
        return Config(dict(DEFAULT_THRESHOLDS), {}, path)
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: expected a mapping at the top level")
    unknown = set(raw) - {"thresholds", "niches"}
    if unknown:
        raise ConfigError(f"{path.name}: unknown top-level keys: {', '.join(sorted(unknown))}")
    return Config(_thresholds(raw.get("thresholds") or {}), _niches(raw.get("niches") or {}), path)


def _thresholds(raw: dict) -> Dict[str, float]:
    if not isinstance(raw, dict):
        raise ConfigError("thresholds must be a mapping")
    unknown = set(raw) - set(DEFAULT_THRESHOLDS)
    if unknown:
        raise ConfigError(f"unknown threshold(s): {', '.join(sorted(unknown))}")
    out = dict(DEFAULT_THRESHOLDS)
    for k, v in raw.items():
        if k in DURATION_KEYS:
            out[k] = parse_duration(v, k)
            continue
        if k in LIKES_KEYS:
            out[k] = parse_count(v, k)
            continue
        if k in BOOL_KEYS:
            if not isinstance(v, bool):
                raise ConfigError(f"'{k}' must be true or false, got {v!r}")
            out[k] = v
            continue
        if k in HASHTAG_KEYS:
            out[k] = parse_hashtags(v, k)
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise ConfigError(f"threshold '{k}' must be a positive number, got {v!r}")
        out[k] = v
    for k in ("min_views", "max_age_days", "stage2_top_n", "baseline_pages"):
        out[k] = int(out[k])
    out["min_multiple"] = float(out["min_multiple"])
    check_duration_range(out)
    return out


def parse_duration(value, name: str = "duration") -> Optional[float]:
    """'1:00' / '1:30' / '0:45' / 90 / '90' -> seconds. None, 0, '' or 'off' -> None (filter off)."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "off", "none")):
        return None
    if isinstance(value, bool):
        raise ConfigError(f"'{name}' must be a length like 1:00 or a number of seconds, got {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip()
        m = re.fullmatch(r"(?:(\d+):)?(\d+):(\d{2})|(\d+(?:\.\d+)?)s?", text)
        if not m:
            raise ConfigError(f"'{name}' must be a length like 1:00 or a number of seconds, got {value!r}")
        if m.group(4) is not None:
            seconds = float(m.group(4))
        else:
            seconds = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    if seconds < 0:
        raise ConfigError(f"'{name}' can't be negative")
    return seconds or None


def parse_count(value, name: str = "count") -> Optional[int]:
    """50000 / '50000' / '50k' / '1.2m' / '50,000' -> int. None, 0, '' or 'off' -> None (filter off)."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "off", "none")):
        return None
    if isinstance(value, bool):
        raise ConfigError(f"'{name}' must be a number like 50000 or 50k, got {value!r}")
    if isinstance(value, (int, float)):
        n = float(value)
    else:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km]?)", str(value).strip().lower().replace(",", "").replace("_", ""))
        if not m:
            raise ConfigError(f"'{name}' must be a number like 50000 or 50k, got {value!r}")
        n = float(m.group(1)) * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)]
    if n < 0:
        raise ConfigError(f"'{name}' can't be negative")
    return int(round(n)) or None


def parse_hashtags(value, name: str = "ai_hashtags") -> List[str]:
    """['#AIvideo', 'sora'] or 'aivideo, sora' -> ['aivideo', 'sora'] (lowercase, no #, no duplicates).
    A leading * ('*edit') is a suffix wildcard."""
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[,\s]+", value)
    if not isinstance(value, list):
        raise ConfigError(f"'{name}' must be a list of hashtags, got {value!r}")
    out: List[str] = []
    for item in value:
        tag = str(item).strip().lstrip("#").lower()
        if tag and not re.fullmatch(r"\*?\w+", tag):  # "*edit" = any hashtag ending in "edit"
            raise ConfigError(f"'{item}' isn't a valid hashtag")
        if tag and tag not in out:
            out.append(tag)
    return out


def fmt_count(n: Optional[int]) -> str:
    if n is None:
        return "off"
    if n >= 1_000_000 and n % 100_000 == 0:
        return f"{n / 1_000_000:g}m"
    if n >= 1_000 and n % 100 == 0:
        return f"{n / 1_000:g}k"
    return f"{n:,}"


def check_duration_range(t: dict) -> None:
    lo, hi = t.get("min_duration_s"), t.get("max_duration_s")
    if lo is not None and hi is not None and lo > hi:
        raise ConfigError(f"min length ({fmt_duration(lo)}) is longer than max length ({fmt_duration(hi)})")


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "off"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _niches(raw: dict) -> Dict[str, Niche]:
    if not isinstance(raw, dict):
        raise ConfigError("niches must be a mapping of niche name -> settings")
    niches: Dict[str, Niche] = {}
    owner: Dict[Tuple[str, str], str] = {}
    for name, body in raw.items():
        name = str(name)
        body = body or {}
        if not isinstance(body, dict):
            raise ConfigError(f"niche '{name}' must be a mapping")
        unknown = set(body) - set(NICHE_KEYS)
        if unknown:
            raise ConfigError(f"niche '{name}': unknown key(s): {', '.join(sorted(unknown))}")
        if any(name.lower() == n.lower() for n in niches):
            raise ConfigError(f"niche '{name}' is listed twice")
        n = Niche(name)
        for key in NICHE_KEYS:
            values = body.get(key) or []
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                raise ConfigError(f"niche '{name}': '{key}' must be a list")
            values = [str(v).strip() for v in values if str(v).strip()]
            if key in ("tiktok_creators", "ig_creators"):
                platform = "tiktok" if key == "tiktok_creators" else "ig"
                cleaned = []
                for v in values:
                    _, h = parse_handle(v)
                    other = owner.get((platform, h))
                    if other:
                        raise ConfigError(
                            f"creator '{h}' ({platform}) is listed in both '{other}' and '{name}'"
                        )
                    owner[(platform, h)] = name
                    cleaned.append(h)
                values = cleaned
            elif key == "ig_hashtags":
                values = [v.lstrip("#") for v in values]
            setattr(n, key, values)
        niches[name] = n
    return niches


def save(cfg: Config, path: Optional[Path] = None) -> Path:
    path = Path(path or cfg.path)
    thresholds = dict(cfg.thresholds)
    for k in DURATION_KEYS:  # save lengths the way people write them ("1:00"), or drop them when off
        if thresholds.get(k) is None:
            thresholds.pop(k, None)
        else:
            thresholds[k] = fmt_duration(thresholds[k])
    for k in LIKES_KEYS:  # likes floors: plain integers, dropped when off
        if thresholds.get(k) is None:
            thresholds.pop(k, None)
    data = {
        "thresholds": thresholds,
        "niches": {n.name: {k: list(getattr(n, k)) for k in NICHE_KEYS} for n in cfg.niches.values()},
    }
    text = HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=None, width=100)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def add_searches(cfg: Config, niche: str, key: str, values: List[str]) -> List[str]:
    """Add keywords/hashtags to a niche (creating it if new), skipping duplicates. Returns what was added."""
    if key not in ("tiktok_keywords", "ig_hashtags", "ig_keywords"):
        raise ConfigError(f"can't add searches to '{key}'")
    try:
        n = cfg.niche(niche)
    except ConfigError:
        n = Niche(niche.strip())
        cfg.niches[n.name] = n
    current = getattr(n, key)
    added = []
    for v in values:
        v = str(v).strip()
        if key == "ig_hashtags":
            v = v.lstrip("#")
        if v and v.lower() not in (c.lower() for c in current):
            current.append(v)
            added.append(v)
    return added


def add_creator(cfg: Config, niche: str, platform: str, handle: str) -> str:
    """Add a creator to a niche (creating the niche if new). Returns 'added' or 'exists:<niche>'."""
    if platform not in CREATOR_KEY:
        raise ConfigError(f"platform must be 'tiktok' or 'ig', got '{platform}'")
    _, handle = parse_handle(handle)
    existing = cfg.find_creator(platform, handle)
    if existing:
        return f"exists:{existing}"
    try:
        n = cfg.niche(niche)
    except ConfigError:
        n = Niche(niche.strip())
        cfg.niches[n.name] = n
    n.creators(platform).append(handle)
    return "added"
