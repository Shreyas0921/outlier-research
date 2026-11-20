from datetime import datetime, timedelta, timezone

import pytest

from lib import hunt as H
from lib.state import State

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def test_start_writes_ledger_and_plan(tmp_path):
    h = H.start(tmp_path, 10, 500, now=NOW)
    assert h["id"] == "20261120-1200-10" and h["credits_used"] == 0
    assert (tmp_path / "hunts" / f"{h['id']}-plan.md").read_text().startswith("# Hunt 20261120-1200-10: 10 new")
    with pytest.raises(ValueError):
        H.start(tmp_path, 10, 500, now=NOW)  # same id twice
    with pytest.raises(ValueError):
        H.start(tmp_path, 0, None, name="x")


def test_status_counts_only_kept_notes_of_this_hunt(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "n").mkdir(parents=True)
    for name in ("a.md", "b.md"):
        (vault / "n" / name).write_text("x")
    h = H.start(tmp_path, 3, 100, name="h1", now=NOW)
    st = State(tmp_path)
    monkeypatch.setenv("OUTLIER_HUNT", "h1")
    st.mark_seen("tiktok:1", "n/a.md", now=NOW)
    st.mark_seen("tiktok:2", "n/b.md", now=NOW)
    st.mark_seen("tiktok:3", "", now=NOW, rejected="no person on camera")
    st.mark_seen("tiktok:4", "n/gone.md", now=NOW)  # note deleted since: doesn't count
    monkeypatch.delenv("OUTLIER_HUNT")
    st.mark_seen("tiktok:5", "n/a.md", now=NOW)  # not this hunt
    assert st.seen["tiktok:1"]["hunt"] == "h1" and "hunt" not in st.seen["tiktok:5"]
    H.charge(tmp_path, "h1", 40)
    H.charge(tmp_path, None, 99)  # outside a hunt: ignored
    s = H.status(tmp_path, H.load(tmp_path, "h1"), st.seen, vault, H.Explored(tmp_path))
    assert (s["kept"], s["rejected"], s["remaining"], s["credits_used"]) == (2, 1, 1, 40)
    assert not s["done"]
    H.charge(tmp_path, "h1", 60)
    s = H.status(tmp_path, H.load(tmp_path, "h1"), st.seen, vault, H.Explored(tmp_path))
    assert s["done"] and s["done_reasons"] == ["budget spent"]
    (tmp_path / "hunts" / "h1.stop").write_text("")
    s = H.status(tmp_path, H.load(tmp_path, "h1"), st.seen, vault, H.Explored(tmp_path))
    assert "stopped by hand" in s["done_reasons"]


def test_explored_remembers_banked_creators_and_probed_terms(tmp_path):
    ex = H.Explored(tmp_path)
    ex.creator("tiktok", "Maisonrickie", banked_at=H._stamp(NOW - timedelta(days=3)), median=66441)
    ex.term("tiktok", " Pierre Bourdieu ", probed_at=H._stamp(NOW - timedelta(days=40)), fits_format=11)
    ex.save()
    ex = H.Explored(tmp_path)
    assert ex.banked("tiktok", "maisonrickie", now=NOW)
    assert not ex.banked("tiktok", "maisonrickie", within_days=2, now=NOW)
    assert ex.probed("tiktok", "pierre bourdieu", now=NOW) is None  # older than 30 days
    assert ex.probed("tiktok", "pierre bourdieu", within_days=60, now=NOW)["fits_format"] == 11
    ex.creator("tiktok", "maisonrickie", median=None, notes=2)  # None never overwrites
    assert ex.creators["tiktok:maisonrickie"]["median"] == 66441


def test_harvest_ranks_unbanked_creators_by_evidence(tmp_path):
    ex = H.Explored(tmp_path)
    ex.creator("tiktok", "done", banked_at=H._stamp(NOW))
    probe = {"terms": [{"term": "habitus", "rows": [
        {"creator": "a", "fits_format": True, "fits_filters": False, "views": 900_000},
        {"creator": "b", "fits_format": True, "fits_filters": True, "views": 600_000},
        {"creator": "done", "fits_format": True, "fits_filters": True, "views": 5_000_000},
        {"creator": "c", "fits_format": False, "fits_filters": False, "views": 9_000_000}]}]}
    summary = {"value": "social class", "top_checked": [
        {"platform": "tiktok", "creator": "d", "is_outlier": True, "outlier_score": 12.0, "views": 700_000},
        {"platform": "tiktok", "creator": "e", "is_outlier": False, "outlier_score": 2.0, "views": 800_000}]}
    out = H.harvest([probe, summary], ex, {"tiktok:a": "Sociology"}, now=NOW)
    assert [e["handle"] for e in out] == ["d", "b", "a"]  # confirmed outlier, then big recent, then views
    assert out[0]["terms"] == ["social class"] and out[2]["niche"] == "Sociology"


def test_harvest_can_add_unbanked_niche_creators(tmp_path):
    ex = H.Explored(tmp_path)
    ex.creator("tiktok", "old", banked_at=H._stamp(NOW))
    out = H.harvest([], ex, {"tiktok:old": "Sociology", "tiktok:new": "Sociology"}, now=NOW, include_niches=True)
    assert [e["handle"] for e in out] == ["new"]
