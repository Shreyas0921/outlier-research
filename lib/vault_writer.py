"""Write outlier notes into the vault and create the Outliers.base view.

Layout (vault-relative), one flat folder; niche and search are note properties, not folders.
<notes dir> is OUTLIERS_NOTES_DIR in .env (default "Outliers"):
  <notes dir>/<Platform> - <creator> - <caption>.md
  <notes dir>/_media/<platform>-<id>/   (stills + transcript)
  <notes dir>/Outliers.base             (an Obsidian Bases view; harmless outside Obsidian)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional

import yaml

from lib.media import MediaResult
from lib.normalize import Video
from lib.scoring import Scored

OUTLIERS_DIR = "Outliers"
MEDIA_DIR = f"{OUTLIERS_DIR}/_media"


def set_notes_dir(rel: str) -> None:
    """Point note writing at a vault-relative folder (run.py calls this with OUTLIERS_NOTES_DIR from .env)."""
    global OUTLIERS_DIR, MEDIA_DIR
    OUTLIERS_DIR = rel.strip().strip("/") or "Outliers"
    MEDIA_DIR = f"{OUTLIERS_DIR}/_media"
ONSCREEN_MARKER = "<!-- onscreen-hook: not read yet -->"
PLATFORM_NAME = {"tiktok": "TikTok", "ig": "Instagram"}
BAD_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]{}%~`]')
MAX_NAME = 80


@dataclass
class FoundVia:
    mode: str  # "niche" | "keyword" | "creator"
    value: str  # niche name, keyword, or @handle
    niche: Optional[str] = None  # set for niche runs and for creators that belong to a niche
    search: Optional[str] = None  # the exact search that surfaced the video: keyword "x", #tag or creator @x

    def label(self) -> str:
        return f"{self.mode}: {self.value}"


# ------------------------------------------------------------------- paths


def note_path(vault: Path, video: Video) -> Path:
    return Path(vault) / OUTLIERS_DIR / (note_title(video) + ".md")


def note_title(video: Video) -> str:
    prefix = f"{PLATFORM_NAME.get(video.platform, video.platform)} - {video.creator or 'unknown'} - "
    room = MAX_NAME - len(prefix)
    snippet = _caption_snippet(video.caption, room) or video.id
    return _clean(prefix + snippet)


def _caption_snippet(caption: str, room: int) -> str:
    text = re.sub(r"[#@]\w+", " ", caption or "")  # hashtags and mentions
    text = "".join(ch for ch in text if ch.isalnum() or ch in " '’,.!-&()")  # emoji etc.
    text = re.sub(r"\s+", " ", text).strip(" .,-")
    if len(text) <= room:
        return text
    cut = text[:room].rsplit(" ", 1)[0] if " " in text[:room] else text[:room]
    return cut.strip(" .,-")


def _clean(name: str) -> str:
    name = BAD_FILENAME_CHARS.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "untitled"


def vault_rel(vault: Path, p: Path) -> str:
    return Path(p).resolve().relative_to(Path(vault).resolve()).as_posix()


# -------------------------------------------------------------------- note


def render_note(vault: Path, scored: Scored, found: FoundVia, media: MediaResult,
                onscreen_text: Optional[str] = None, today: Optional[date] = None) -> str:
    v = scored.video
    stills = [vault_rel(vault, s) for s in media.stills]
    cover = next((s for s in stills if s.endswith("still-1.0s.jpg")), stills[0] if stills else None)
    t = media.transcript
    fm = {
        "tags": ["outlier"],
        "platform": PLATFORM_NAME.get(v.platform, v.platform),
        "url": v.url,
        "creator": v.creator,
        "followers": v.followers,
        "views": scored.views,
        "likes": v.likes,
        "comments": v.comments,
        "shares": v.shares,
        "outlier_score": round(scored.multiple, 1),  # display only; scoring used the exact value
        "creator_median": int(round(scored.baseline)),
        "baseline_videos": scored.baseline_n,
        "view_metric": v.view_metric,
        "posted": v.posted.date() if v.posted else None,
        "duration_s": media.duration_s or v.duration_s,
        "niche": found.niche,
        "found_via": found.label(),
        "search": found.search,
        "found_on": today or date.today(),
        "speech": "yes" if t.speech else "none",
        "cover": f"[[{cover}]]" if cover else None,
    }
    front = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000).strip()

    lines = [
        "---", front, "---", "",
        f"**{round(scored.multiple, 1)}×** {_views_phrase(scored.views, v.view_metric)} vs this creator's usual "
        f"{_fmt(scored.baseline)} (median of {scored.baseline_n} recent videos) · [Open original]({v.url})",
        "",
        " ".join(f"![[{s}|160]]" for s in stills),
        "",
        "## Spoken hook",
        "",
        f"> {t.hook_text}" if t.speech and t.hook_text else "_No speech in the first 5 seconds._" if t.speech
        else "_No speech (music or silent)._",
        "",
        "## On-screen hook",
        "",
        _onscreen_block(onscreen_text),
        "",
        "## Caption",
        "",
        v.caption.strip() or "_No caption._",
        "",
        "## Full transcript",
        "",
        t.full_text if t.speech else "_No speech (music or silent)._",
        "",
    ]
    return "\n".join(lines)


def _onscreen_block(text: Optional[str]) -> str:
    if text is None:
        return ONSCREEN_MARKER + "\n_Not read yet._"
    text = text.strip()
    if not text or text.lower() == "none":
        return "_No on-screen text._"
    return "\n".join("> " + line for line in text.splitlines())


def _views_phrase(n: int, metric: str) -> str:
    word = {"views": "views", "plays": "plays", "likes": "likes"}.get(metric, "views")
    return f"{_fmt(n)} {word}"


def _fmt(n: float) -> str:
    n = float(n)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(n))


def write_note(vault: Path, scored: Scored, found: FoundVia, media: MediaResult,
               onscreen_text: Optional[str] = None, today: Optional[date] = None) -> Path:
    """Write the note; never overwrite an existing one (adds ' (2)', ' (3)'...)."""
    path = note_path(vault, scored.video)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 2
    base = path
    while path.exists():
        path = base.with_name(f"{base.stem} ({n}).md")
        n += 1
    path.write_text(render_note(vault, scored, found, media, onscreen_text, today))
    return path


def set_onscreen_text(note: Path, text: str) -> bool:
    """Fill in the On-screen hook after Claude has read the stills. Returns False if already filled."""
    content = Path(note).read_text()
    block = ONSCREEN_MARKER + "\n_Not read yet._"
    if block not in content:
        return False
    Path(note).write_text(content.replace(block, _onscreen_block(text), 1))
    return True


# -------------------------------------------------------------------- base


def base_yaml() -> str:
    return f"""filters:
  and:
    - file.inFolder("{OUTLIERS_DIR}")
    - '!file.inFolder("{MEDIA_DIR}")'
    - file.ext == "md"
views:
  - type: table
    name: By score
    order:
      - file.name
      - outlier_score
      - views
      - creator_median
      - creator
      - platform
      - niche
      - search
      - posted
      - found_on
      - speech
    sort:
      - property: note.outlier_score
        direction: DESC
  - type: cards
    name: Hooks
    image: note.cover
    imageAspectRatio: 1.78
    cardSize: 180
    imageFit: cover
    order:
      - file.name
      - outlier_score
      - views
      - platform
    sort:
      - property: note.outlier_score
        direction: DESC
  - type: table
    name: Newest finds
    order:
      - file.name
      - found_on
      - outlier_score
      - niche
      - search
    sort:
      - property: note.found_on
        direction: DESC
"""


def write_base(vault: Path) -> Path:
    """(Re)write Outliers.base with the current views."""
    path = Path(vault) / OUTLIERS_DIR / "Outliers.base"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base_yaml())
    return path


def ensure_base(vault: Path) -> Optional[Path]:
    """Create Outliers.base once. Returns its path if created, None if it already existed."""
    path = Path(vault) / OUTLIERS_DIR / "Outliers.base"
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base_yaml())
    return path


def media_root(vault: Path) -> Path:
    return Path(vault) / MEDIA_DIR


def list_unread_onscreen(vault: Path, notes: List[Path]) -> List[Path]:
    return [n for n in notes if ONSCREEN_MARKER in Path(n).read_text()]
