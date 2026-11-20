"""ScrapeCreators API client: the only module that knows about the data provider.

Every call is a GET to https://api.scrapecreators.com with an `x-api-key` header and costs
1 credit (0 on a cache hit). Responses carry `credits_charged` and `credits_remaining`, which
this client tallies so each run can report what it spent.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

BASE_URL = "https://api.scrapecreators.com"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class ApiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, path: str = ""):
        super().__init__(message)
        self.status = status
        self.path = path


class AuthError(ApiError):
    """401: the API key is missing or wrong."""


class OutOfCredits(ApiError):
    """402: the account has no credits left. Stop the whole run."""


class NotFound(ApiError):
    """403/404: the post or profile is private, age-gated, deleted or doesn't exist."""


class ScrapeCreatorsClient:
    def __init__(
        self,
        api_key: str,
        session: Optional[requests.Session] = None,
        run_dir: Optional[Path] = None,
        max_retries: int = 3,
        backoff_s: float = 2.0,
        timeout_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        max_credits: Optional[int] = None,
    ):
        if not api_key:
            raise AuthError("SCRAPECREATORS_API_KEY is empty; put it in the .env file in the outlier-research folder")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.run_dir = Path(run_dir) if run_dir else None
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.timeout_s = timeout_s
        self.sleep = sleep
        self.max_credits = max_credits  # optional per-run budget; reaching it acts like running out
        self.calls = 0
        self.credits_used = 0
        self.credits_remaining: Optional[int] = None
        self.log: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ core

    def get(self, path: str, **params: Any) -> Dict[str, Any]:
        if self.max_credits is not None and self.credits_used >= self.max_credits:
            raise OutOfCredits(f"run budget of {self.max_credits} credits reached", None, path)
        params = {k: _param(v) for k, v in params.items() if v is not None}
        url = BASE_URL + path
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.get(
                    url, params=params, headers={"x-api-key": self.api_key}, timeout=self.timeout_s
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt > self.max_retries:
                    raise ApiError(f"network error after {attempt} tries: {e}", None, path) from e
                self.sleep(self.backoff_s * 2 ** (attempt - 1))
                continue

            status = resp.status_code
            if status in RETRY_STATUSES and attempt <= self.max_retries:
                self.sleep(self.backoff_s * 2 ** (attempt - 1))
                continue
            break

        body = _json_or_none(resp)
        self._account(path, params, status, body)

        if status == 401:
            raise AuthError("API key rejected (401)", status, path)
        if status == 402:
            raise OutOfCredits("out of ScrapeCreators credits (402)", status, path)
        if status in (403, 404):
            raise NotFound(f"not found or not public ({status})", status, path)
        if status >= 400:
            raise ApiError(f"HTTP {status}: {_short(body, resp)}", status, path)
        if body is None:
            raise ApiError("response was not JSON", status, path)
        if body.get("success") is False:
            raise ApiError(f"scrape failed: {_short(body, resp)}", status, path)
        return body

    def _account(self, path: str, params: Dict[str, Any], status: int, body: Optional[dict]) -> None:
        self.calls += 1
        charged = 0
        if isinstance(body, dict):
            charged = body.get("credits_charged")
            if charged is None:
                charged = 1 if status < 400 else 0
            if body.get("credits_remaining") is not None:
                self.credits_remaining = body["credits_remaining"]
        self.credits_used += int(charged or 0)
        entry = {"n": self.calls, "path": path, "params": params, "status": status, "credits": charged}
        self.log.append(entry)
        if self.run_dir is not None and body is not None:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            name = f"{self.calls:03d}-{re.sub(r'[^a-z0-9]+', '-', path.lower()).strip('-')}.json"
            (self.run_dir / name).write_text(json.dumps({"request": entry, "response": body}, indent=1))

    # ---------------------------------------------------------------- TikTok

    def tiktok_search(self, query: str, date_posted: Optional[str] = None,
                      sort_by: Optional[str] = None, pages: int = 1) -> List[dict]:
        """Keyword search. Returns `aweme_info` dicts. date_posted: yesterday | this-week |
        this-month | last-3-months | last-6-months | all-time."""
        items: List[dict] = []
        seen = set()
        cursor = None
        for _ in range(pages):
            body = self.get("/v1/tiktok/search/keyword", query=query, date_posted=date_posted,
                            sort_by=sort_by, cursor=cursor)
            page = [it["aweme_info"] for it in body.get("search_item_list") or [] if it.get("aweme_info")]
            for a in page:  # TikTok may return duplicates
                if a.get("aweme_id") not in seen:
                    seen.add(a.get("aweme_id"))
                    items.append(a)
            cursor = body.get("cursor")
            if not page or cursor is None:
                break
        return items

    def tiktok_profile(self, handle: str, cache_max_age: Optional[str] = "7d") -> dict:
        return self.get("/v1/tiktok/profile", handle=handle, cache_max_age=cache_max_age)

    def tiktok_profile_videos(self, handle: str, max_videos: int = 30, max_pages: int = 3,
                              sort_by: str = "latest") -> List[dict]:
        """A creator's recent videos (pinned ones excluded so they don't skew the median)."""
        items: List[dict] = []
        cursor = None
        for _ in range(max_pages):
            body = self.get("/v3/tiktok/profile/videos", handle=handle, sort_by=sort_by,
                            include_pinned=False, max_cursor=cursor)
            items.extend(body.get("aweme_list") or [])
            cursor = body.get("max_cursor")
            if len(items) >= max_videos or not body.get("has_more") or not cursor:
                break
        return items[:max_videos]

    def tiktok_video(self, url: str) -> dict:
        return self.get("/v2/tiktok/video", url=url).get("aweme_detail") or {}

    # ------------------------------------------------------------- Instagram

    def ig_profile(self, handle: str, cache_max_age: Optional[str] = "7d") -> dict:
        return self.get("/v1/instagram/profile", handle=handle, cache_max_age=cache_max_age)

    def ig_user_reels(self, handle: Optional[str] = None, user_id: Optional[str] = None,
                      max_reels: int = 30, max_pages: int = 3) -> List[dict]:
        """A creator's recent reels, as `media` dicts (with play_count)."""
        if not handle and not user_id:
            raise ValueError("ig_user_reels needs a handle or user_id")
        items: List[dict] = []
        max_id = None
        for _ in range(max_pages):
            body = self.get("/v1/instagram/user/reels", handle=handle, user_id=user_id, max_id=max_id)
            items.extend(it["media"] for it in body.get("items") or [] if it.get("media"))
            paging = body.get("paging_info") or {}
            max_id = paging.get("max_id")
            if len(items) >= max_reels or not paging.get("more_available") or not max_id:
                break
        return items[:max_reels]

    def ig_reels_search(self, query: str, date_posted: Optional[str] = None, pages: int = 1) -> List[dict]:
        """Google-indexed reels search (best effort). date_posted: last-week | last-month | last-year."""
        items: List[dict] = []
        for page in range(1, min(pages, 11) + 1):
            body = self.get("/v2/instagram/reels/search", query=query, date_posted=date_posted, page=page)
            batch = body.get("reels") or []
            items.extend(batch)
            if not batch:
                break
        return items

    def ig_hashtag_search(self, hashtag: str, date_posted: Optional[str] = None,
                          media_type: str = "reels", pages: int = 1) -> List[dict]:
        """Google-indexed hashtag search (best effort). date_posted: last-hour | last-day |
        last-week | last-month | last-year."""
        items: List[dict] = []
        cursor = None
        for _ in range(min(pages, 11)):
            body = self.get("/v1/instagram/search/hashtag", hashtag=hashtag.lstrip("#"),
                            date_posted=date_posted, media_type=media_type, cursor=cursor)
            batch = body.get("posts") or []
            items.extend(batch)
            cursor = body.get("cursor")
            if not batch or not cursor:
                break
        return items

    def ig_post(self, url: str) -> dict:
        body = self.get("/v1/instagram/post", url=url)
        return (body.get("data") or {}).get("xdt_shortcode_media") or {}


def _param(v: Any) -> Any:
    return ("true" if v else "false") if isinstance(v, bool) else v


def _json_or_none(resp: requests.Response) -> Optional[dict]:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else {"data": body}


def _short(body: Optional[dict], resp: requests.Response) -> str:
    if isinstance(body, dict):
        for k in ("message", "error", "detail"):
            if body.get(k):
                return str(body[k])[:200]
    return (resp.text or "")[:200]
