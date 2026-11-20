import json
from datetime import datetime, timedelta, timezone

import pytest

from lib.normalize import Video
from lib.state import State

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


def vid(id, views, metric="plays"):
    return Video(platform="tiktok", id=id, url="", creator="c", followers=None, views=views,
                 view_metric=metric, likes=None, comments=None, shares=None, posted=NOW,
                 duration_s=None, caption="", download_url=None)


def test_seen_round_trip(tmp_path):
    s = State(tmp_path)
    assert not s.is_seen("tiktok:1")
    s.mark_seen("tiktok:1", "Outliers/Philosophy/x.md", now=NOW)
    s.save()
    again = State(tmp_path)
    assert again.is_seen("tiktok:1") and again.seen_keys() == {"tiktok:1"}
    assert again.seen["tiktok:1"]["date"] == "2026-11-14"


def test_baseline_cache_hit_and_expiry(tmp_path):
    s = State(tmp_path)
    stored = s.put_baseline("tiktok", "JonsterTruck", [vid("a", 10), vid("b", None), vid("c", 30)], now=NOW)
    assert stored == {"a": 10, "c": 30}  # videos without a count are dropped
    s.save()
    again = State(tmp_path)
    assert again.get_baseline("tiktok", "jonstertruck", now=NOW + timedelta(days=7)) == {"a": 10, "c": 30}
    assert again.get_baseline("tiktok", "jonstertruck", now=NOW + timedelta(days=7, seconds=1)) is None
    assert again.get_baseline("ig", "jonstertruck", now=NOW) is None  # platforms kept apart


def test_cached_fetcher_only_pays_once(tmp_path):
    s = State(tmp_path)
    calls = []

    def fetch(platform, creator):
        calls.append(creator)
        return [vid("a", 100), vid("b", 200)]

    get = s.cached_fetcher(fetch, now=NOW)
    assert get("tiktok", "x") == {"a": 100, "b": 200}
    assert get("tiktok", "x") == {"a": 100, "b": 200}
    assert calls == ["x"] and get.fetched == ["tiktok:x"]


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    s = State(tmp_path / "state")
    s.mark_seen("ig:abc", "note.md", now=NOW)
    s.save()
    files = sorted(p.name for p in (tmp_path / "state").iterdir())
    assert files == ["baselines.json", "seen.json"]
    assert json.loads((tmp_path / "state" / "seen.json").read_text())["ig:abc"]["note"] == "note.md"


def test_corrupted_state_gives_clear_error(tmp_path):
    (tmp_path / "seen.json").write_text("{not json")
    with pytest.raises(ValueError, match="corrupted"):
        State(tmp_path)


def test_save_keeps_entries_another_run_wrote(tmp_path):
    a, b = State(tmp_path), State(tmp_path)
    a.mark_seen("tiktok:1", "n1.md", NOW)
    a.save()
    b.mark_seen("tiktok:2", "n2.md", NOW)
    b.save()  # b loaded before a saved; a's entry must survive
    assert set(State(tmp_path).seen) == {"tiktok:1", "tiktok:2"}
