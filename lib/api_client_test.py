import json
from pathlib import Path

import pytest
import requests

from lib.api_client import (
    ApiError,
    AuthError,
    NotFound,
    OutOfCredits,
    ScrapeCreatorsClient,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())["response"]


class FakeResponse:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Returns queued responses (or raises queued exceptions) and records each request."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.requests = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append({"url": url, "params": dict(params or {}), "headers": headers})
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(body=None, charged=1, remaining=50):
    b = {"success": True, "credits_charged": charged, "credits_remaining": remaining}
    b.update(body or {})
    return FakeResponse(200, b)


def client(*responses, **kw):
    sleeps = []
    session = FakeSession(*responses)
    c = ScrapeCreatorsClient("test-key", session=session, sleep=sleeps.append, **kw)
    return c, session, sleeps


# ------------------------------------------------------------------ core get


def test_success_counts_credits_and_sends_key_header():
    c, s, _ = client(ok({"x": 1}, charged=1, remaining=41))
    body = c.get("/v1/tiktok/profile", handle="abc", cache_max_age=None)
    assert body["x"] == 1
    assert c.calls == 1 and c.credits_used == 1 and c.credits_remaining == 41
    assert s.requests[0]["headers"] == {"x-api-key": "test-key"}
    assert s.requests[0]["params"] == {"handle": "abc"}  # None params dropped


def test_cache_hit_costs_zero():
    c, _, _ = client(ok(charged=0))
    c.get("/v1/tiktok/profile", handle="abc")
    assert c.credits_used == 0


def test_bool_params_sent_as_lowercase_strings():
    c, s, _ = client(ok())
    c.get("/x", include_pinned=False, trim=True)
    assert s.requests[0]["params"] == {"include_pinned": "false", "trim": "true"}


def test_retries_429_then_succeeds_with_backoff():
    c, s, sleeps = client(FakeResponse(429, {}), FakeResponse(503, {}), ok({"y": 2}))
    assert c.get("/x")["y"] == 2
    assert len(s.requests) == 3
    assert sleeps == [2.0, 4.0]


def test_gives_up_after_max_retries():
    c, s, sleeps = client(*[FakeResponse(500, {"message": "boom"}) for _ in range(4)])
    with pytest.raises(ApiError) as e:
        c.get("/x")
    assert e.value.status == 500
    assert len(s.requests) == 4  # 1 try + 3 retries
    assert len(sleeps) == 3


def test_network_error_is_retried():
    c, s, _ = client(requests.ConnectionError("down"), ok({"z": 3}))
    assert c.get("/x")["z"] == 3
    assert len(s.requests) == 2


def test_network_error_gives_up():
    c, _, _ = client(*[requests.Timeout("slow") for _ in range(4)])
    with pytest.raises(ApiError, match="network error"):
        c.get("/x")


@pytest.mark.parametrize(
    "status,exc",
    [(401, AuthError), (402, OutOfCredits), (403, NotFound), (404, NotFound), (400, ApiError)],
)
def test_client_errors_are_not_retried(status, exc):
    c, s, sleeps = client(FakeResponse(status, {"success": False, "message": "nope"}))
    with pytest.raises(exc):
        c.get("/x")
    assert len(s.requests) == 1 and sleeps == []


def test_success_false_raises():
    c, _, _ = client(FakeResponse(200, {"success": False, "message": "scrape failed"}))
    with pytest.raises(ApiError, match="scrape failed"):
        c.get("/x")


def test_non_json_raises():
    c, _, _ = client(FakeResponse(200, None, text="<html>"))
    with pytest.raises(ApiError, match="not JSON"):
        c.get("/x")


def test_run_budget_stops_before_overspending():
    c, s, _ = client(ok(charged=1), ok(charged=1), max_credits=2)
    c.get("/a")
    c.get("/b")
    with pytest.raises(OutOfCredits, match="budget of 2"):
        c.get("/c")
    assert len(s.requests) == 2  # the third call never went out


def test_empty_key_rejected():
    with pytest.raises(AuthError):
        ScrapeCreatorsClient("")


def test_run_dir_saves_responses_without_key(tmp_path):
    c, _, _ = client(ok({"a": 1}), run_dir=tmp_path)
    c.get("/v1/tiktok/search/keyword", query="philosophy")
    files = list(tmp_path.glob("*.json"))
    assert [f.name for f in files] == ["001-v1-tiktok-search-keyword.json"]
    text = files[0].read_text()
    assert "test-key" not in text
    assert json.loads(text)["request"]["params"] == {"query": "philosophy"}


# ------------------------------------------------------------ helpers / paging


def test_tiktok_search_parses_fixture_and_dedupes_across_pages():
    page = fixture("tiktok_search.json")
    c, s, _ = client(ok(page), ok(page))  # same page twice -> duplicates dropped
    items = c.tiktok_search("philosophy", pages=2)
    ids = [a["aweme_id"] for a in items]
    assert len(ids) == len(set(ids)) == len(page["search_item_list"])
    assert s.requests[1]["params"]["cursor"] == page["cursor"]


def test_tiktok_search_stops_on_empty_page():
    c, s, _ = client(ok({"search_item_list": [], "cursor": 30}))
    assert c.tiktok_search("x", pages=5) == []
    assert len(s.requests) == 1


def test_tiktok_profile_videos_pages_until_has_more_false():
    full = fixture("tiktok_profile_videos.json")
    p1 = dict(full, has_more=1, max_cursor=111)
    p2 = dict(full, has_more=0, max_cursor=222)
    c, s, _ = client(ok(p1), ok(p2))
    items = c.tiktok_profile_videos("jonstertruck", max_videos=100, max_pages=3)
    assert len(items) == 2 * len(full["aweme_list"])
    assert s.requests[0]["params"]["include_pinned"] == "false"
    assert s.requests[1]["params"]["max_cursor"] == 111
    assert len(s.requests) == 2


def test_tiktok_profile_videos_respects_max_videos():
    full = fixture("tiktok_profile_videos.json")
    c, s, _ = client(ok(dict(full, has_more=1, max_cursor=1)))
    items = c.tiktok_profile_videos("x", max_videos=3, max_pages=3)
    assert len(items) == 3 and len(s.requests) == 1


def test_ig_user_reels_pages_with_max_id():
    full = fixture("ig_user_reels.json")
    p1 = dict(full, paging_info={"max_id": "abc", "more_available": True})
    p2 = dict(full, paging_info={"max_id": None, "more_available": False})
    c, s, _ = client(ok(p1), ok(p2))
    reels = c.ig_user_reels(handle="michael__mezz", max_reels=100, max_pages=3)
    assert len(reels) == 2 * len(full["items"])
    assert all("play_count" in r for r in reels)
    assert s.requests[1]["params"]["max_id"] == "abc"


def test_ig_user_reels_needs_handle_or_id():
    c, _, _ = client()
    with pytest.raises(ValueError):
        c.ig_user_reels()


def test_ig_hashtag_strips_hash_and_pages_with_cursor():
    full = fixture("ig_hashtag_search.json")
    c, s, _ = client(ok(dict(full, cursor="2")), ok(dict(full, cursor=None)))
    posts = c.ig_hashtag_search("#philosophy", pages=5)
    assert len(posts) == 2 * len(full["posts"])
    assert s.requests[0]["params"]["hashtag"] == "philosophy"
    assert s.requests[0]["params"]["media_type"] == "reels"
    assert s.requests[1]["params"]["cursor"] == "2"
    assert len(s.requests) == 2


def test_ig_reels_search_uses_page_numbers_and_caps_at_11():
    full = fixture("ig_reels_search.json")
    c, s, _ = client(*[ok(full) for _ in range(11)])
    c.ig_reels_search("philosophy", pages=20)
    assert [r["params"]["page"] for r in s.requests] == list(range(1, 12))


def test_single_item_helpers_unwrap_fixture_shapes():
    c, _, _ = client(ok(fixture("tiktok_video.json")), ok(fixture("ig_post.json")))
    assert c.tiktok_video("https://www.tiktok.com/@a/video/1")["statistics"]["play_count"] > 0
    assert c.ig_post("https://www.instagram.com/reel/X/")["video_play_count"] > 0
