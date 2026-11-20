"""Turn each ScrapeCreators response shape into one common `Video` record.

Units and quirks (from the 2.0 probe, see README):
- TikTok `video.duration` is milliseconds; Instagram `video_duration` is seconds.
- TikTok `create_time` and Instagram reels/post timestamps are unix seconds; Instagram search
  `taken_at` is an ISO string.
- Instagram search results have no follower count (left as None).
- Views come from the best available metric, in order: views, plays, likes. `view_metric`
  records which one was used.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional


@dataclass
class Video:
    platform: str  # "tiktok" | "ig"
    id: str  # TikTok aweme_id, Instagram shortcode
    url: str
    creator: str
    followers: Optional[int]
    views: Optional[int]
    view_metric: str  # "views" | "plays" | "likes" | "none"
    likes: Optional[int]
    comments: Optional[int]
    shares: Optional[int]
    posted: Optional[datetime]
    duration_s: Optional[float]
    caption: str
    download_url: Optional[str]
    ai_labeled: Optional[bool] = None  # platform's own "AI-generated" label; None = the response doesn't say
    spoken_lang: Optional[str] = None  # platform's spoken-language code ("en"); None = unknown (all Instagram)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.id}"


# ------------------------------------------------------------------ TikTok


def tiktok_aweme(a: dict, followers: Optional[int] = None) -> Optional[Video]:
    """One TikTok video (`aweme_info` from search, `aweme_list[]` item, or `aweme_detail`).
    Returns None for photo carousels and items without a playable video."""
    # /v2/tiktok/video sends an `image_post_info` cover block even for normal videos, so a
    # carousel is one that actually has images.
    if not a or ((a.get("image_post_info") or {}).get("images")):
        return None
    video = a.get("video") or {}
    dl = _first_url(video.get("download_no_watermark_addr")) or _first_url(video.get("play_addr")) \
        or _first_url(video.get("download_addr"))
    if not dl or not a.get("aweme_id"):
        return None
    author = a.get("author") or {}
    handle = (author.get("unique_id") or "").lower()
    stats = a.get("statistics") or {}
    views, metric = _pick_views(views=None, plays=stats.get("play_count"), likes=stats.get("digg_count"))
    duration = video.get("duration")
    return Video(
        platform="tiktok",
        id=str(a["aweme_id"]),
        url=f"https://www.tiktok.com/@{handle}/video/{a['aweme_id']}",
        creator=handle,
        followers=_int(author.get("follower_count")) if followers is None else followers,
        views=views,
        view_metric=metric,
        likes=_int(stats.get("digg_count")),
        comments=_int(stats.get("comment_count")),
        shares=_int(stats.get("share_count")),
        posted=_ts(a.get("create_time")),
        duration_s=round(duration / 1000, 2) if isinstance(duration, (int, float)) and duration else None,
        caption=(a.get("desc") or "").strip(),
        download_url=dl,
        ai_labeled=tiktok_ai_label(a),
        spoken_lang=tiktok_spoken_lang(a),
    )


def tiktok_spoken_lang(a: dict) -> Optional[str]:
    """Spoken language from TikTok's own speech recognition: `video.cla_info.original_language_info`,
    else the original (not translated) entry in `caption_infos`. Lowercase code like "en", or None."""
    cla = (a.get("video") or {}).get("cla_info") or {}
    code = (cla.get("original_language_info") or {}).get("language_code")
    if not code:
        for c in cla.get("caption_infos") or []:
            if c.get("is_original_caption"):
                code = c.get("language_code")
                break
    return str(code).strip().lower() or None if code else None


def tiktok_ai_label(a: dict) -> Optional[bool]:
    """TikTok's AI-generated label: `aigc_info` (creator-set or auto) or `moderation_aigc_info`
    (applied by moderation). Label type 0 = not labeled; anything above 0 = labeled AI."""
    info, mod = a.get("aigc_info"), a.get("moderation_aigc_info")
    if info is None and mod is None:
        return None
    info, mod = info or {}, mod or {}
    return bool(info.get("created_by_ai") or (_int(info.get("aigc_label_type")) or 0) > 0
                or (_int(mod.get("moderation_aigc_label_type")) or 0) > 0)


def tiktok_list(items: Iterable[dict], followers: Optional[int] = None) -> List[Video]:
    return _compact(tiktok_aweme(a, followers) for a in items)


def tiktok_followers(profile: dict) -> Optional[int]:
    return _int((profile.get("stats") or {}).get("followerCount"))


# --------------------------------------------------------------- Instagram


def ig_reel_media(m: dict, creator: Optional[str] = None, followers: Optional[int] = None) -> Optional[Video]:
    """One `media` dict from /v1/instagram/user/reels. `creator` overrides the media's own user,
    since collab reels on a creator's tab can be owned by the other account."""
    if not m or not m.get("code"):
        return None
    dl = _first_url(m.get("video_versions"))
    if not dl:
        return None
    caption = m.get("caption")
    caption = caption.get("text", "") if isinstance(caption, dict) else (caption or "")
    views, metric = _pick_views(views=m.get("view_count"), plays=m.get("play_count") or m.get("ig_play_count"),
                                likes=m.get("like_count"))
    handle = (creator or (m.get("user") or {}).get("username") or "").lower()
    return Video(
        platform="ig",
        id=m["code"],
        url=f"https://www.instagram.com/reel/{m['code']}/",
        creator=handle,
        followers=followers,
        views=views,
        view_metric=metric,
        likes=_int(m.get("like_count")),
        comments=_int(m.get("comment_count")),
        shares=None,
        posted=_ts(m.get("taken_at")),
        duration_s=_float(m.get("video_duration")),
        caption=caption.strip(),
        download_url=dl,
        ai_labeled=ig_ai_label(m),
    )


def ig_reels_list(items: Iterable[dict], creator: Optional[str] = None,
                  followers: Optional[int] = None) -> List[Video]:
    return _compact(ig_reel_media(m, creator, followers) for m in items)


def ig_search_reel(r: dict) -> Optional[Video]:
    """One item from /v2/instagram/reels/search `reels[]` or /v1/instagram/search/hashtag `posts[]`."""
    if not r or not r.get("is_video") or not r.get("video_url") or not r.get("shortcode"):
        return None
    views, metric = _pick_views(views=r.get("video_view_count"), plays=r.get("video_play_count"),
                                likes=r.get("like_count"))
    return Video(
        platform="ig",
        id=r["shortcode"],
        url=f"https://www.instagram.com/reel/{r['shortcode']}/",
        creator=((r.get("owner") or {}).get("username") or "").lower(),
        followers=_int((r.get("owner") or {}).get("follower_count")),
        views=views,
        view_metric=metric,
        likes=_int(r.get("like_count")),
        comments=_int(r.get("comment_count")),
        shares=None,
        posted=_ts(r.get("taken_at")),
        duration_s=_float(r.get("video_duration")),
        caption=(r.get("caption") or "").strip() if isinstance(r.get("caption"), str) else "",
        download_url=r["video_url"],
        ai_labeled=ig_ai_label(r),
    )


def ig_search_list(items: Iterable[dict]) -> List[Video]:
    return _compact(ig_search_reel(r) for r in items)


def ig_post(m: dict) -> Optional[Video]:
    """`data.xdt_shortcode_media` from /v1/instagram/post."""
    if not m or not m.get("is_video") or not m.get("video_url") or not m.get("shortcode"):
        return None
    edges = (m.get("edge_media_to_caption") or {}).get("edges") or []
    caption = ((edges[0] or {}).get("node") or {}).get("text", "") if edges else ""
    views, metric = _pick_views(views=None, plays=m.get("video_play_count") or m.get("video_view_count"),
                                likes=(m.get("edge_media_preview_like") or {}).get("count"))
    return Video(
        platform="ig",
        id=m["shortcode"],
        url=f"https://www.instagram.com/reel/{m['shortcode']}/",
        creator=((m.get("owner") or {}).get("username") or "").lower(),
        followers=_int(((m.get("owner") or {}).get("edge_followed_by") or {}).get("count")),
        views=views,
        view_metric=metric,
        likes=_int((m.get("edge_media_preview_like") or {}).get("count")),
        comments=_int((m.get("edge_media_to_parent_comment") or {}).get("count")),
        shares=None,
        posted=_ts(m.get("taken_at_timestamp")),
        duration_s=_float(m.get("video_duration")),
        caption=caption.strip(),
        download_url=m["video_url"],
        ai_labeled=ig_ai_label(m),
    )


def ig_followers(profile: dict) -> Optional[int]:
    user = (profile.get("data") or {}).get("user") or {}
    return _int((user.get("edge_followed_by") or {}).get("count"))


# ----------------------------------------------------------------- helpers


# Instagram's "AI info" detection methods that mean "not AI" (CAMERA_CAPTURE = "Captured with a camera").
IG_NOT_AI = {"", "NONE", "CAMERA_CAPTURE"}


def ig_ai_label(m: dict) -> Optional[bool]:
    """Instagram's AI-info label, from `gen_ai_detection_method` (reels) or `ai_label_info` (profile/post).
    Only present on creator/post endpoints, not on search results (None there). Any detection method other
    than none/camera capture counts as labeled AI. Untested against a live AI-labeled reel (none seen yet)."""
    methods = []
    g = m.get("gen_ai_detection_method")
    if g is not None:
        methods.append(g.get("detection_method") if isinstance(g, dict) else g)
    info = m.get("ai_label_info")
    if isinstance(info, dict):
        methods.append(info.get("gen_ai_detection_method"))
    elif "ai_label_info" in m:
        methods.append("NONE")  # explicit null = no label
    if not methods:
        return None
    return any(str(x or "").upper() not in IG_NOT_AI for x in methods)


def _pick_views(views: Any, plays: Any, likes: Any):
    for value, name in ((views, "views"), (plays, "plays"), (likes, "likes")):
        v = _int(value)
        if v is not None:
            return v, name
    return None, "none"


def _first_url(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        urls = obj.get("url_list") or []
        return urls[0] if urls else None
    if isinstance(obj, list) and obj:  # Instagram video_versions: [{url, width, height}, ...]
        first = obj[0]
        return first.get("url") if isinstance(first, dict) else None
    return None


def _ts(value: Any) -> Optional[datetime]:
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> Optional[float]:
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def _compact(videos: Iterable[Optional[Video]]) -> List[Video]:
    return [v for v in videos if v is not None]
