import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import run as R
from lib import config as C
from lib import normalize as N
from lib import vault_writer as W
from lib.api_client import NotFound, OutOfCredits
from lib.media import MediaError, MediaResult, Transcript
from lib.normalize import Video
from lib.scoring import Scored
from lib.state import State

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


# ----------------------------------------------------------- fake API data


def tt(id, creator, views, followers=10_000, age_days=3, caption="a tiktok"):
    return {"aweme_id": str(id), "desc": caption, "create_time": int((NOW - timedelta(days=age_days)).timestamp()),
            "author": {"unique_id": creator, "follower_count": followers},
            "statistics": {"play_count": views, "digg_count": views // 10, "comment_count": 1, "share_count": 1},
            "video": {"duration": 30_000, "play_addr": {"url_list": [f"http://cdn/{id}.mp4"]}}}


def ig_search(code, creator, views, age_days=3):
    return {"shortcode": code, "is_video": True, "video_url": f"http://cdn/{code}.mp4", "video_view_count": views,
            "like_count": views // 10, "comment_count": 1, "owner": {"username": creator},
            "taken_at": (NOW - timedelta(days=age_days)).isoformat().replace("+00:00", "Z"),
            "video_duration": 30.0, "caption": "an ig reel"}


def ig_media(code, creator, plays, age_days=3):
    return {"code": code, "play_count": plays, "video_versions": [{"url": f"http://cdn/{code}.mp4"}],
            "taken_at": int((NOW - timedelta(days=age_days)).timestamp()), "video_duration": 30.0,
            "caption": {"text": "reel"}, "user": {"username": creator}, "like_count": 1, "comment_count": 1}


def tt_baseline(creator, n=8, views=100_000, extra=()):
    return [tt(f"{creator}-{i}", creator, views) for i in range(n)] + list(extra)


class FakeClient:
    def __init__(self, search=None, profiles=None, ig_search_results=None, ig_tags=None, ig_reels=None,
                 fail=None, budget=None):
        self.search = search or {}
        self.profiles = profiles or {}
        self.ig_search_results = ig_search_results or {}
        self.ig_tags = ig_tags or {}
        self.ig_reels = ig_reels or {}
        self.fail = fail or {}
        self.budget = budget
        self.calls = 0
        self.credits_used = 0
        self.credits_remaining = 80
        self.log = []

    def _spend(self, what):
        if self.budget is not None and self.credits_used >= self.budget:
            raise OutOfCredits("budget reached")
        if what in self.fail:
            raise self.fail[what]
        self.calls += 1
        self.credits_used += 1
        self.credits_remaining -= 1
        self.log.append(what)

    def tiktok_search(self, query, date_posted=None, sort_by=None, pages=1):
        self._spend(f"tt_search:{query}")
        return self.search.get(query, [])

    def tiktok_profile_videos(self, handle, max_videos=30, max_pages=3, sort_by="latest"):
        self._spend(f"tt_profile:{handle}")
        return self.profiles.get(handle, [])

    def ig_profile(self, handle, cache_max_age="7d"):
        self._spend(f"ig_profile:{handle}")
        return {"data": {"user": {"edge_followed_by": {"count": 5000}}}}

    def ig_user_reels(self, handle=None, user_id=None, max_reels=30, max_pages=3):
        self._spend(f"ig_reels:{handle}")
        return self.ig_reels.get(handle, [])

    def ig_reels_search(self, query, date_posted=None, pages=1):
        self._spend(f"ig_search:{query}")
        return self.ig_search_results.get(query, [])

    def ig_hashtag_search(self, hashtag, date_posted=None, media_type="reels", pages=1):
        self._spend(f"ig_tag:{hashtag}")
        return self.ig_tags.get(hashtag, [])


def fake_process(fail_ids=()):
    calls = []

    def process(video, media_root):
        calls.append(video.id)
        if video.id in fail_ids:
            raise MediaError("download failed (direct: HTTP 403; yt-dlp: blocked)")
        d = Path(media_root) / f"{video.platform}-{video.id}"
        d.mkdir(parents=True, exist_ok=True)
        stills = []
        for t in ("0.0", "1.0", "2.5", "4.5"):
            (d / f"still-{t}s.jpg").write_bytes(b"x")
            stills.append(d / f"still-{t}s.jpg")
        return MediaResult(dir=d, stills=stills, downloaded_via="direct", duration_s=30.0,
                           transcript=Transcript(speech=True, hook_text="you won't believe this", full_text="you won't believe this at all"))

    process.calls = calls
    return process


def context(tmp_path, client, cfg=None, process=None):
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return R.RunContext(client=client, cfg=cfg or C.load(tmp_path / "none.yaml"), state=State(tmp_path / "state"),
                        vault=vault, run_dir=tmp_path / "runs" / "r1", now=NOW, process=process or fake_process(),
                        log=lambda *_: None)


def niche_cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("niches:\n  Philosophy:\n    tiktok_creators: [jon]\n    tiktok_keywords: [stoicism]\n"
                 "    ig_hashtags: [philosophy]\n    ig_creators: [mezz]\n")
    return C.load(p)


# ------------------------------------------------------------------- CLI


def test_parser_forms():
    p = R.build_parser()
    a = p.parse_args(["niche", "Philosophy", "--dry-run", "--max-credits", "20"])
    assert (a.cmd, a.value, a.dry_run, a.max_credits) == ("niche", "Philosophy", True, 20)
    a = p.parse_args(["creator", "@jon", "--platform", "tiktok"])
    assert (a.value, a.platform) == ("@jon", "tiktok")
    a = p.parse_args(["keyword", "study tips"])
    assert (a.value, a.platform) == ("study tips", "both")
    a = p.parse_args(["set-onscreen", "x.md", "--text", "none"])
    assert a.note == Path("x.md") and a.text == "none"
    with pytest.raises(SystemExit):
        p.parse_args(["keyword", "x", "--platform", "youtube"])


def test_run_overrides_for_length_and_top_n(tmp_path):
    cfg = C.load(tmp_path / "none.yaml")
    args = R.build_parser().parse_args(["keyword", "finance", "--min-length", "1:00", "--max-length", "3:00",
                                        "--top-n", "6"])
    R.apply_run_overrides(cfg, args)
    assert (cfg.thresholds["min_duration_s"], cfg.thresholds["max_duration_s"], cfg.thresholds["stage2_top_n"]) == (60, 180, 6)
    off = R.build_parser().parse_args(["keyword", "finance", "--max-length", "off"])
    R.apply_run_overrides(cfg, off)
    assert cfg.thresholds["max_duration_s"] is None and cfg.thresholds["min_duration_s"] == 60
    bad = R.build_parser().parse_args(["keyword", "x", "--min-length", "5:00", "--max-length", "1:00"])
    with pytest.raises(C.ConfigError, match="longer than max"):
        R.apply_run_overrides(C.load(tmp_path / "none.yaml"), bad)


def test_run_overrides_for_likes(tmp_path):
    cfg = C.load(tmp_path / "none.yaml")
    args = R.build_parser().parse_args(["keyword", "x", "--min-likes-tiktok", "50k", "--min-likes-ig", "10000"])
    R.apply_run_overrides(cfg, args)
    assert (cfg.thresholds["min_likes_tiktok"], cfg.thresholds["min_likes_ig"]) == (50000, 10000)
    R.apply_run_overrides(cfg, R.build_parser().parse_args(["keyword", "x", "--min-likes-ig", "off"]))
    assert cfg.thresholds["min_likes_ig"] is None and cfg.thresholds["min_likes_tiktok"] == 50000
    with pytest.raises(C.ConfigError, match="--min-likes-tiktok"):
        R.apply_run_overrides(cfg, R.build_parser().parse_args(["keyword", "x", "--min-likes-tiktok", "lots"]))


def test_keyword_run_applies_likes_floor_before_spending_credits(tmp_path):
    loved = tt("loved", "alice", 900_000)  # 90k likes
    meh = dict(tt("meh", "bob", 900_000))
    meh["statistics"] = dict(meh["statistics"], digg_count=20_000)
    reel = ig_search("reel1", "cara", 900_000)  # 90k likes, passes the IG floor
    client = FakeClient(search={"x": [loved, meh]}, ig_search_results={"x": [reel]},
                        profiles={"alice": tt_baseline("alice"), "bob": tt_baseline("bob")},
                        ig_reels={"cara": [ig_media(f"c{i}", "cara", 100_000) for i in range(8)]})
    cfg = C.load(tmp_path / "none.yaml")
    cfg.thresholds.update(min_likes_tiktok=50_000, min_likes_ig=10_000)
    s = R.execute(context(tmp_path, client, cfg=cfg), "keyword", "x", ["tiktok", "ig"])
    assert sorted(n["creator"] for n in s["notes"]) == ["alice", "cara"]
    assert "tt_profile:bob" not in client.log  # dropped in the free stage


def test_allow_ai_flag_turns_the_filter_off_for_one_run(tmp_path):
    cfg = C.load(tmp_path / "none.yaml")
    cfg.thresholds["exclude_ai"] = True
    R.apply_run_overrides(cfg, R.build_parser().parse_args(["keyword", "x"]))
    assert cfg.thresholds["exclude_ai"] is True
    R.apply_run_overrides(cfg, R.build_parser().parse_args(["keyword", "x", "--allow-ai"]))
    assert cfg.thresholds["exclude_ai"] is False


def test_keyword_run_skips_ai_videos_and_reports_them(tmp_path):
    labeled = dict(tt("ai1", "bot", 900_000), aigc_info={"aigc_label_type": 2, "created_by_ai": True})
    tagged = dict(tt("ai2", "spongecuh", 900_000), desc="what will he do #aistory")
    client = FakeClient(search={"x": [labeled, tagged, tt("real", "alice", 900_000)]},
                        profiles={"alice": tt_baseline("alice")})
    cfg = C.load(tmp_path / "none.yaml")
    cfg.thresholds.update(exclude_ai=True, ai_hashtags=["aistory"])
    s = R.execute(context(tmp_path, client, cfg=cfg), "keyword", "x", ["tiktok"])
    assert [n["creator"] for n in s["notes"]] == ["alice"] and s["ai_skipped"] == 2
    assert client.log == ["tt_search:x", "tt_profile:alice"]


def test_baseline_cache_remembers_ai_labels(tmp_path):
    st = State(tmp_path)
    vids = N.tiktok_list([dict(tt("a", "c", 1000), aigc_info={"aigc_label_type": 2}), tt("b", "c", 1000)])
    st.put_baseline("tiktok", "c", vids)
    assert st.ai_ids("tiktok", "c") == {"a"} and st.ai_ids("tiktok", "nobody") == set()


def test_keyword_run_applies_length_filter(tmp_path):
    short_hit = tt("short", "alice", 900_000)  # 30s
    long_hit = dict(tt("long", "bob", 900_000), video={"duration": 95_000, "play_addr": {"url_list": ["http://x"]}})
    client = FakeClient(search={"x": [short_hit, long_hit]},
                        profiles={"alice": tt_baseline("alice"), "bob": tt_baseline("bob")})
    cfg = C.load(tmp_path / "none.yaml")
    cfg.thresholds.update(min_duration_s=60.0, max_duration_s=180.0)
    s = R.execute(context(tmp_path, client, cfg=cfg), "keyword", "x", ["tiktok"])
    assert [n["creator"] for n in s["notes"]] == ["bob"]
    assert "tt_profile:alice" not in client.log  # filtered before any credits were spent on her


def test_resolve_creator_platform(tmp_path):
    cfg = niche_cfg(tmp_path)
    assert R.resolve_creator_platform(cfg, "ig:someone", None) == ("ig", "someone")
    assert R.resolve_creator_platform(cfg, "@someone", "tiktok") == ("tiktok", "someone")
    assert R.resolve_creator_platform(cfg, "@jon", None) == ("tiktok", "jon")  # known from config
    with pytest.raises(C.ConfigError, match="Which platform"):
        R.resolve_creator_platform(cfg, "@stranger", None)


@pytest.mark.parametrize("days,tt_f,ig_f", [(1, "yesterday", "last-week"), (7, "this-week", "last-week"),
                                            (60, "last-3-months", "last-year"), (365, "all-time", "last-year"),
                                            (30, "this-month", "last-month")])
def test_date_filters(days, tt_f, ig_f):
    assert R.tiktok_date_filter(days) == tt_f and R.ig_date_filter(days) == ig_f


# ------------------------------------------------------------------ keyword


def test_keyword_run_writes_notes_and_summary(tmp_path):
    client = FakeClient(
        search={"stoicism": [tt("hit", "alice", 900_000), tt("meh", "bob", 200_000), tt("tiny", "carl", 500)]},
        profiles={"alice": tt_baseline("alice"), "bob": tt_baseline("bob")})
    ctx = context(tmp_path, client)
    s = R.execute(ctx, "keyword", "stoicism", ["tiktok"])
    assert s["outliers_found"] == 1 and s["notes_written"] == 1
    note = s["notes"][0]
    assert note["note"] == "Outliers/TikTok - alice - a tiktok.md"
    assert note["search"] == 'keyword "stoicism"'
    assert note["outlier_score"] == 9.0 and len(note["stills_abs"]) == 4
    assert (ctx.vault / note["note"]).exists()
    assert (ctx.vault / "Outliers/Outliers.base").exists()
    assert State(tmp_path / "state").is_seen("tiktok:hit")
    assert s["credits_used"] == 3  # 1 search + 2 creator baselines
    assert json.loads((ctx.run_dir / "summary.json").read_text())["outliers_found"] == 1
    assert {x["creator"] for x in s["top_checked"]} == {"alice", "bob"}


def test_keyword_both_platforms(tmp_path):
    client = FakeClient(search={"x": [tt("t1", "alice", 900_000)]},
                        ig_search_results={"x": [ig_search("R1", "mezz", 2_000_000)]},
                        profiles={"alice": tt_baseline("alice")},
                        ig_reels={"mezz": [ig_media(f"m{i}", "mezz", 100_000) for i in range(8)]})
    s = R.execute(context(tmp_path, client), "keyword", "x", ["tiktok", "ig"])
    assert sorted(n["platform"] for n in s["notes"]) == ["ig", "tiktok"]


# -------------------------------------------------------------------- niche


def test_niche_run_reuses_creator_lists_as_baselines(tmp_path):
    jon = tt_baseline("jon", extra=[tt("jon-hit", "jon", 1_500_000)])
    client = FakeClient(search={"stoicism": [tt("jon-hit", "jon", 1_500_000), tt("s1", "alice", 800_000)]},
                        ig_tags={"philosophy": [ig_search("R9", "zed", 50)]},  # filtered out (too few views)
                        profiles={"jon": jon, "alice": tt_baseline("alice")},
                        ig_reels={"mezz": [ig_media(f"m{i}", "mezz", 100_000) for i in range(8)]})
    ctx = context(tmp_path, client, cfg=niche_cfg(tmp_path))
    s = R.execute(ctx, "niche", "Philosophy")
    ids = {Path(n["note"]).name for n in s["notes"]}
    assert ids == {"TikTok - jon - a tiktok.md", "TikTok - alice - a tiktok.md"}
    assert all(Path(n["note"]).parent.name == "Outliers" for n in s["notes"])
    assert {n["creator"]: n["search"] for n in s["notes"]} == {"jon": 'keyword "stoicism"', "alice": 'keyword "stoicism"'}
    fm = yaml.safe_load((ctx.vault / s["notes"][0]["note"]).read_text().split("---\n")[1])
    assert fm["niche"] == "Philosophy" and fm["search"] == 'keyword "stoicism"'
    assert client.log.count("tt_profile:jon") == 1  # niche creator list reused, not re-fetched in Stage 2
    assert s["failures"] == []


def test_niche_run_keeps_going_when_one_source_fails(tmp_path):
    client = FakeClient(search={"stoicism": [tt("s1", "alice", 800_000)]},
                        profiles={"alice": tt_baseline("alice"), "jon": tt_baseline("jon")},
                        fail={"ig_tag:philosophy": NotFound("404", 404), "ig_reels:mezz": NotFound("404", 404)})
    s = R.execute(context(tmp_path, client, cfg=niche_cfg(tmp_path)), "niche", "Philosophy")
    assert s["notes_written"] == 1
    assert {f["what"] for f in s["failures"]} == {"ig hashtag #philosophy", "ig creator @mezz"}


def test_out_of_credits_keeps_what_was_found(tmp_path):
    client = FakeClient(search={"stoicism": [tt("s1", "alice", 800_000)]},
                        profiles={"alice": tt_baseline("alice")}, budget=2)
    s = R.execute(context(tmp_path, client, cfg=niche_cfg(tmp_path)), "niche", "Philosophy")
    assert s["out_of_credits"] is True
    assert s["credits_used"] == 2


# ------------------------------------------------------------------ creator


def test_creator_run_tags_its_niche(tmp_path):
    client = FakeClient(profiles={"jon": tt_baseline("jon", extra=[tt("jon-hit", "jon", 700_000)])})
    ctx = context(tmp_path, client, cfg=niche_cfg(tmp_path))
    s = R.execute(ctx, "creator", "jon", ["tiktok"])
    assert [Path(n["note"]).parent.name for n in s["notes"]] == ["Outliers"]
    assert "niche: Philosophy" in (ctx.vault / s["notes"][0]["note"]).read_text()
    assert s["notes"][0]["search"] == "creator @jon"
    assert s["credits_used"] == 1


def test_creator_not_in_any_niche_has_no_niche(tmp_path):
    client = FakeClient(ig_reels={"mezz": [ig_media(f"m{i}", "mezz", 50_000) for i in range(8)]
                                  + [ig_media("big", "mezz", 900_000)]})
    s = R.execute(context(tmp_path, client), "creator", "mezz", ["ig"])
    assert s["notes"][0]["note"].startswith("Outliers/Instagram - mezz")
    assert client.log == ["ig_profile:mezz", "ig_reels:mezz"]


def test_creator_not_found_is_reported(tmp_path):
    client = FakeClient(fail={"tt_profile:ghost": NotFound("404", 404)})
    s = R.execute(context(tmp_path, client), "creator", "ghost", ["tiktok"])
    assert s["notes_written"] == 0 and s["failures"][0]["what"] == "tiktok creator @ghost"


# ------------------------------------------------------- repeat / failures


def test_failed_media_is_retried_next_run_and_never_duplicated(tmp_path):
    data = dict(search={"x": [tt("hit", "alice", 900_000)]}, profiles={"alice": tt_baseline("alice")})
    s1 = R.execute(context(tmp_path, FakeClient(**data), process=fake_process(fail_ids={"hit"})),
                   "keyword", "x", ["tiktok"])
    assert s1["notes_written"] == 0 and "download failed" in s1["failures"][0]["error"]
    assert not State(tmp_path / "state").is_seen("tiktok:hit")

    client2 = FakeClient(**data)
    s2 = R.execute(context(tmp_path, client2), "keyword", "x", ["tiktok"])
    assert s2["notes_written"] == 1
    assert "tt_profile:alice" not in client2.log  # baseline came from the 7-day cache

    s3 = R.execute(context(tmp_path, FakeClient(**data)), "keyword", "x", ["tiktok"])
    assert s3["notes_written"] == 0 and s3["skipped_seen"] == 1
    notes = list((tmp_path / "vault").rglob("*.md"))
    assert len(notes) == 1


def test_dry_run_writes_nothing_to_the_vault(tmp_path):
    client = FakeClient(search={"x": [tt("hit", "alice", 900_000)]}, profiles={"alice": tt_baseline("alice")})
    proc = fake_process()
    ctx = context(tmp_path, client, process=proc)
    s = R.execute(ctx, "keyword", "x", ["tiktok"], dry_run=True)
    assert s["outliers_found"] == 1 and s["notes_written"] == 0 and proc.calls == []
    assert list(ctx.vault.rglob("*")) == []
    assert not State(tmp_path / "state").is_seen("tiktok:hit")
    assert s["top_checked"][0]["is_outlier"] is True


# ------------------------------------------------------- creator management


class AddClient(FakeClient):
    def tiktok_profile(self, handle, cache_max_age="7d"):
        self._spend(f"tt_userinfo:{handle}")
        return {"user": {"nickname": handle.title(), "signature": f"{handle} talks philosophy"},
                "stats": {"followerCount": 12345}}

    def ig_profile(self, handle, cache_max_age="7d"):
        self._spend(f"ig_profile:{handle}")
        return {"data": {"user": {"full_name": "Mezz", "biography": "economics, culture",
                                  "edge_followed_by": {"count": 329922}}}}


def test_add_fetch_collects_bio_captions_and_caches_baselines(tmp_path):
    (tmp_path / "vault" / "Creators/Clothing").mkdir(parents=True)
    (tmp_path / "vault" / "Creators/Philosophy").mkdir(parents=True)
    client = AddClient(profiles={"newbie": tt_baseline("newbie")},
                       ig_reels={"newgram": [ig_media(f"m{i}", "newgram", 100_000) for i in range(8)]})
    ctx = context(tmp_path, client, cfg=niche_cfg(tmp_path))
    out = R.add_fetch(ctx, ["tiktok:newbie", "ig:newgram", "ig:mezz", "@jon", "@mystery", "tiktok:bad handle!"], None)
    rows = {r.get("handle") or r["input"]: r for r in out["creators"]}

    assert rows["newbie"]["bio"] == "newbie talks philosophy" and rows["newbie"]["followers"] == 12345
    assert rows["newbie"]["videos"] == 8 and rows["newbie"]["median_views"] == 100_000
    assert rows["newbie"]["captions"] == ["a tiktok"] * 8
    assert rows["newgram"]["followers"] == 329922 and rows["newgram"]["bio"] == "economics, culture"
    assert client.log.count("ig_profile:newgram") == 1  # profile fetched once, not twice
    assert rows["mezz"]["already_in_niche"] == "Philosophy" and "ig_profile:mezz" not in client.log
    assert rows["jon"]["already_in_niche"] == "Philosophy" and "tt_profile:jon" not in client.log
    assert rows["mystery"]["error"] == "needs_platform"
    assert "valid handle" in rows["tiktok:bad handle!"]["error"]
    assert out["known_niches"] == ["Philosophy", "Clothing"]  # config first, then new folder names
    assert ctx.state.get_baseline("tiktok", "newbie", NOW) is not None  # cached for the first niche run
    assert (ctx.run_dir / "add_candidates.json").exists()


def test_add_fetch_platform_flag_and_budget(tmp_path):
    client = AddClient(profiles={"a": tt_baseline("a"), "b": tt_baseline("b")}, budget=2)
    out = R.add_fetch(context(tmp_path, client), ["@a", "@b"], "tiktok")
    assert out["creators"][0]["videos"] == 8
    assert "stopped" in out["creators"][1]["error"] and len(out["creators"]) == 2


def test_add_write_creates_niche_and_skips_duplicates(tmp_path):
    cfg = niche_cfg(tmp_path)
    result = R.add_write(cfg, {
        "creators": [{"handle": "newbie", "platform": "tiktok", "niche": "Clothing"},
                     {"handle": "@Jon", "platform": "tiktok", "niche": "Humor"},
                     {"handle": "mezz2", "platform": "ig", "niche": "philosophy"}],
        "niche_searches": {"Clothing": {"tiktok_keywords": ["fit check", "Fit Check"], "ig_hashtags": ["#ootd"]},
                           "Philosophy": {"tiktok_keywords": ["stoicism", "nihilism"]}}})
    assert [r["result"] for r in result["creators"]] == ["added", "exists:Philosophy", "added"]
    assert result["searches_added"] == {"Clothing": {"tiktok_keywords": ["fit check"], "ig_hashtags": ["ootd"]},
                                        "Philosophy": {"tiktok_keywords": ["nihilism"]}}
    again = C.load(tmp_path / "config.yaml")
    assert again.niche("Clothing").tiktok_creators == ["newbie"]
    assert again.niche("Philosophy").ig_creators == ["mezz", "mezz2"]
    assert "Humor" not in again.niches


def test_add_write_via_cli(tmp_path, capsys):
    cfg_path = tmp_path / "config.yaml"
    j = tmp_path / "confirmed.json"
    j.write_text(json.dumps({"creators": [{"handle": "a", "platform": "ig", "niche": "Music"}]}))
    assert R.main(["add-write", str(j), "--config", str(cfg_path)]) == 0
    assert C.load(cfg_path).niche("Music").ig_creators == ["a"]
    assert R.main(["niches", "--config", str(cfg_path), "--vault", str(tmp_path)]) == 0
    assert "Music\n  Instagram creators: @a" in capsys.readouterr().out


def test_niches_listing(tmp_path):
    text = R.format_niches(niche_cfg(tmp_path), tmp_path)
    assert text.splitlines() == ["Philosophy", "  TikTok creators: @jon", "  Instagram creators: @mezz",
                                 "  TikTok keywords: stoicism", "  Instagram hashtags: #philosophy"]
    (tmp_path / "Creators/Humor").mkdir(parents=True)
    empty = R.format_niches(C.load(tmp_path / "nothing.yaml"), tmp_path)
    assert "No niches in config.yaml yet" in empty and "Humor" in empty


# --------------------------------------------------------------- set-onscreen


def test_set_onscreen_command(tmp_path, capsys):
    client = FakeClient(search={"x": [tt("hit", "alice", 900_000)]}, profiles={"alice": tt_baseline("alice")})
    s = R.execute(context(tmp_path, client), "keyword", "x", ["tiktok"])
    note = s["notes"][0]["note_abs"]
    assert R.main(["set-onscreen", note, "--text", "WAIT FOR IT"]) == 0
    assert "> WAIT FOR IT" in Path(note).read_text()
    assert R.main(["set-onscreen", note, "--text", "again"]) == 0
    assert "already filled" in capsys.readouterr().out


# ------------------------------------------------------------ content rules


def test_rejected_videos_are_marked_seen_without_a_note(tmp_path):
    from lib.media import Rejected
    calls = []

    def process(video, media_root, checks=None):
        calls.append(checks)
        raise Rejected("no person on camera (face in 2/12 frames)")

    cfg = C.load(tmp_path / "none.yaml")
    cfg.thresholds.update(talking_head_only=True, english_only=True)
    ctx = context(tmp_path, FakeClient(), cfg=cfg, process=process)
    v = Video(platform="tiktok", id="9", url="u", creator="c", followers=None, views=1, view_metric="plays",
              likes=None, comments=None, shares=None, posted=NOW, duration_s=60, caption="", download_url="x",
              spoken_lang="en")
    notes, failures, rejected = R.save_outliers(ctx, [Scored(v, 1_000_000, 50.0, 20_000, 20)],
                                                lambda s: W.FoundVia("keyword", "x", search='keyword "x"'))
    assert notes == [] and failures == [] and rejected[0]["reason"].startswith("no person")
    assert calls[0].talking_head_only and calls[0].spoken_lang == "en"
    assert ctx.state.seen["tiktok:9"]["rejected"].startswith("no person")
    assert not list((tmp_path / "vault").rglob("*.md"))


def test_reject_note_moves_to_trash_and_blocks_resave(tmp_path):
    vault = tmp_path / "vault"
    media = W.media_root(vault) / "tiktok-5"
    media.mkdir(parents=True)
    (media / "still-0.0s.jpg").write_bytes(b"x")
    note = media.parent.parent / "TikTok - c - skit.md"
    note.write_text("![[Outliers/_media/tiktok-5/still-0.0s.jpg]]")
    state = State(tmp_path / "state")
    state.mark_seen("tiktok:5", W.vault_rel(vault, note))
    out = R.reject_note(state, vault, note, "character skit", trash_root=tmp_path / "Trash")
    assert out["key"] == "tiktok:5" and not note.exists() and not media.exists()
    trash = Path(out["trash"])
    assert (trash / note.name).exists() and (trash / "_media" / "tiktok-5" / "still-0.0s.jpg").exists()
    assert State(tmp_path / "state").seen["tiktok:5"]["rejected"] == "character skit"


# ------------------------------------------------------------------- hunts


def test_runs_record_what_they_explored_and_tag_hunt_notes(tmp_path, monkeypatch):
    from lib import hunt as H
    monkeypatch.setenv("OUTLIER_HUNT", "h1")
    client = FakeClient(profiles={"jon": tt_baseline("jon", extra=[tt("jon-hit", "jon", 700_000)])},
                        search={"stoicism": [tt("hit", "alice", 900_000)]})
    client.profiles["alice"] = tt_baseline("alice")
    ctx = context(tmp_path, client, cfg=niche_cfg(tmp_path))
    ctx.explored = H.Explored(tmp_path / "state")
    R.execute(ctx, "creator", "jon", ["tiktok"])
    R.execute(ctx, "keyword", "stoicism", ["tiktok"])
    ex = H.Explored(tmp_path / "state")
    jon = ex.creators["tiktok:jon"]
    assert jon["banked_at"] and jon["notes"] == 1 and jon["outliers"] == 1 and jon["niche"] == "Philosophy"
    assert ex.creators["tiktok:alice"]["stage2_at"] and "banked_at" not in ex.creators["tiktok:alice"]
    assert ex.terms["tiktok:stoicism"]["notes"] == 1
    st = State(tmp_path / "state")
    assert st.seen["tiktok:jon-hit"]["hunt"] == "h1" and st.seen["tiktok:hit"]["hunt"] == "h1"


def test_hunt_cli_start_and_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(R, "HERE", tmp_path)
    assert R.main(["hunt", "start", "--goal", "5", "--budget", "100", "--name", "t"]) == 0
    assert capsys.readouterr().out.strip() == "t"
    assert R.main(["hunt", "batch", "t"]) == 0
    capsys.readouterr()
    assert R.main(["hunt", "status", "t", "--vault", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "0/5 kept" in out and "batches 1" in out and "5 to go" in out


def test_save_outliers_skips_a_video_listed_twice_or_saved_meanwhile(tmp_path):
    calls = []

    def process(video, media_root, checks=None):
        calls.append(video.key)
        raise Rejected("stop here")

    from lib.media import Rejected
    cfg = C.load(tmp_path / "none.yaml")
    ctx = context(tmp_path, FakeClient(), cfg=cfg, process=process)
    mk = lambda i: Video(platform="tiktok", id=i, url="u", creator="c", followers=None, views=1,
                         view_metric="plays", likes=None, comments=None, shares=None, posted=NOW, duration_s=60,
                         caption="", download_url="x", spoken_lang="en")
    other = State(ctx.state.dir)
    other.mark_seen("tiktok:8", "elsewhere.md", NOW)
    other.save()  # another run saved video 8 after this one loaded its state
    R.save_outliers(ctx, [Scored(mk("9"), 1_000_000, 50.0, 20_000, 20), Scored(mk("9"), 1_000_000, 50.0, 20_000, 20),
                          Scored(mk("8"), 1_000_000, 50.0, 20_000, 20)],
                    lambda s: W.FoundVia("keyword", "x", search='keyword "x"'))
    assert calls == ["tiktok:9"]
    assert ctx.state.seen["tiktok:8"]["note"] == "elsewhere.md"
