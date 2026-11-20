from datetime import datetime, timedelta, timezone

import pytest

from lib.api_client import NotFound, OutOfCredits
from lib.config import DEFAULT_THRESHOLDS
from lib.normalize import Video
from lib.scoring import ai_reason, median_baseline, passes_filters, score_candidates, score_creator, stage1_rank

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)
T = {**DEFAULT_THRESHOLDS, "max_age_days": 60}  # age edges below are written for a 60-day window


def vid(id, views, creator="c", platform="tiktok", followers=None, age_days=5):
    return Video(platform=platform, id=str(id), url="", creator=creator, followers=followers,
                 views=views, view_metric="plays", likes=None, comments=None, shares=None,
                 posted=NOW - timedelta(days=age_days), duration_s=30, caption="", download_url="x")


# ------------------------------------------------------------- median


def test_median_excludes_the_scored_video():
    vmap = {"a": 10, "b": 20, "c": 30, "d": 40, "e": 50, "viral": 10_000}
    assert median_baseline(vmap, exclude_id="viral") == (30.0, 5)
    # without exclusion the viral video would pull the median up
    assert median_baseline(vmap)[0] == 35.0


def test_median_needs_five_other_videos():
    vmap = {str(i): 100 for i in range(5)}
    assert median_baseline(vmap, exclude_id="0") == (None, 4)
    assert median_baseline(vmap) == (100.0, 5)


# ------------------------------------------------------------ filters


def test_filters_min_views_and_age_edges():
    assert passes_filters(vid(1, 100_000), T, NOW)  # exactly min_views passes
    assert not passes_filters(vid(1, 99_999), T, NOW)
    assert passes_filters(vid(1, 200_000, age_days=60), T, NOW)  # exactly max age passes
    assert not passes_filters(vid(1, 200_000, age_days=61), T, NOW)
    no_date = vid(1, 200_000)
    no_date.posted = None
    assert not passes_filters(no_date, T, NOW)
    no_views = vid(1, None)
    assert not passes_filters(no_views, T, NOW)


def test_length_filter_edges_and_unknown_length():
    t = dict(T, min_duration_s=60.0, max_duration_s=180.0)

    def at(seconds):
        v = vid(1, 200_000)
        v.duration_s = seconds
        return v

    assert passes_filters(at(60), t, NOW) and passes_filters(at(180), t, NOW)  # edges included
    assert not passes_filters(at(59.9), t, NOW) and not passes_filters(at(180.1), t, NOW)
    assert not passes_filters(at(None), t, NOW)  # can't confirm the length
    assert passes_filters(at(None), T, NOW)  # no length filter -> length doesn't matter
    assert passes_filters(at(10), dict(T, max_duration_s=180.0), NOW)  # only a max set


def test_length_filter_never_touches_the_baseline():
    t = dict(T, min_duration_s=60.0, max_duration_s=180.0)
    shorts = [vid(i, 50_000) for i in range(10)]  # 30s videos form the baseline
    long_hit = vid("hit", 500_000)
    long_hit.duration_s = 120
    out = score_creator(shorts + [long_hit], t, NOW)
    assert [s.video.id for s in out] == ["hit"] and out[0].baseline_n == 10


def test_likes_floor_is_per_platform_and_needs_known_likes():
    t = dict(T, min_likes_tiktok=50_000, min_likes_ig=10_000)

    def liked(likes, platform):
        v = vid(1, 200_000, platform=platform)
        v.likes = likes
        return v

    assert passes_filters(liked(50_000, "tiktok"), t, NOW)  # exactly the floor passes
    assert not passes_filters(liked(49_999, "tiktok"), t, NOW)
    assert passes_filters(liked(10_000, "ig"), t, NOW)  # IG uses its own, lower floor
    assert not passes_filters(liked(9_999, "ig"), t, NOW)
    assert not passes_filters(liked(None, "ig"), t, NOW)  # hidden/unknown likes can't confirm it
    assert passes_filters(liked(None, "ig"), dict(T, min_likes_tiktok=50_000), NOW)  # IG floor off


def test_likes_floor_never_touches_the_baseline():
    t = dict(T, min_likes_tiktok=50_000)
    low_liked = [vid(i, 50_000) for i in range(10)]  # likes unknown: still count toward the median
    hit = vid("hit", 500_000)
    hit.likes = 60_000
    out = score_creator(low_liked + [hit], t, NOW)
    assert [s.video.id for s in out] == ["hit"] and out[0].baseline_n == 10


AI = dict(T, exclude_ai=True, ai_hashtags=["aivideo", "brainrot"])


def test_ai_filter_label_and_hashtags():
    labeled = vid(1, 200_000)
    labeled.ai_labeled = True
    tagged = vid(2, 200_000)
    tagged.caption = "tung tung #BrainRot #fyp"
    near_miss = vid(3, 200_000)
    near_miss.caption = "how AI is changing bots #ai #brainrotting"  # only exact tags count
    assert ai_reason(labeled, AI) == "label" and ai_reason(tagged, AI) == "#brainrot"
    assert ai_reason(near_miss, AI) is None and passes_filters(near_miss, AI, NOW)
    assert not passes_filters(labeled, AI, NOW) and not passes_filters(tagged, AI, NOW)
    assert passes_filters(labeled, T, NOW)  # exclude_ai off -> label ignored


def test_ai_filter_counts_and_skips_before_stage2():
    labeled = vid("ai", 900_000, creator="bot", followers=1000)
    labeled.ai_labeled = True
    real = vid("real", 900_000, creator="human", followers=1000)
    calls = []

    def fetch(p, c):
        calls.append(c)
        return {str(i): 50_000 for i in range(8)}

    res = score_candidates([labeled, real], AI, fetch, now=NOW)
    assert res.ai_skipped == 1 and calls == ["human"]  # no credits spent on the AI video
    assert [s.video.id for s in res.outliers] == ["real"]


def test_ai_label_found_in_creator_list_drops_ig_candidate_in_stage2():
    reel = vid("R1", 900_000, creator="cara", platform="ig")  # search result: label unknown
    assert reel.ai_labeled is None
    res = score_candidates([reel], AI, lambda p, c: {str(i): 50_000 for i in range(8)}, now=NOW,
                           ai_labeled_ids=lambda p, c: {"R1"})
    assert res.outliers == [] and res.ai_skipped == 1
    res = score_candidates([reel], dict(AI, exclude_ai=False), lambda p, c: {str(i): 50_000 for i in range(8)},
                           now=NOW, ai_labeled_ids=lambda p, c: {"R1"})
    assert [s.video.id for s in res.outliers] == ["R1"]  # filter off -> kept


# -------------------------------------------------------- creator mode


def test_score_creator_finds_the_breakout():
    videos = [vid(i, 50_000) for i in range(10)] + [vid("hit", 250_000)]
    out = score_creator(videos, T, NOW)
    assert [s.video.id for s in out] == ["hit"]
    assert out[0].multiple == 5.0  # exactly 5x counts
    assert out[0].baseline == 50_000 and out[0].baseline_n == 10


def test_score_creator_just_under_threshold_is_dropped():
    videos = [vid(i, 50_000) for i in range(10)] + [vid("almost", 249_999)]
    assert score_creator(videos, T, NOW) == []


def test_score_creator_sorts_best_first_and_skips_old():
    videos = [vid(i, 20_000) for i in range(10)] + [
        vid("6x", 120_000), vid("20x", 400_000), vid("old", 900_000, age_days=90)]
    assert [s.video.id for s in score_creator(videos, T, NOW)] == ["20x", "6x"]


def test_score_creator_too_few_videos_scores_nothing():
    videos = [vid(i, 10_000) for i in range(4)] + [vid("hit", 900_000)]
    assert score_creator(videos, T, NOW) == []


# ------------------------------------------------------------- stage 1


def test_stage1_interleaves_ratio_and_raw_view_rankings():
    tt = [vid("t_big", 1_000_000, followers=1_000_000), vid("t_small", 200_000, followers=10_000)]
    ig = [vid("i_low", 150_000, platform="ig"), vid("i_high", 900_000, platform="ig")]
    assert [v.id for v in stage1_rank(tt + ig)] == ["t_small", "i_high", "t_big", "i_low"]


# ------------------------------------------------------------- funnel


def baseline_lookup(table, calls):
    def fetch(platform, creator):
        calls.append((platform, creator))
        value = table[(platform, creator)]
        if isinstance(value, Exception):
            raise value
        return value
    return fetch


def test_funnel_end_to_end():
    candidates = [
        vid("a1", 600_000, creator="a", followers=50_000),  # a's median 100k -> 6x
        vid("a2", 300_000, creator="a", followers=50_000),  # 3x -> not an outlier
        vid("b1", 2_000_000, creator="b", followers=5_000_000),  # b's median 1M -> 2x
        vid("small", 5_000, creator="z", followers=100),  # under min_views
        vid("a1", 600_000, creator="a", followers=50_000),  # duplicate
    ]
    a_recent = {str(i): 100_000 for i in range(8)}
    b_recent = {str(i): 1_000_000 for i in range(8)}
    calls = []
    res = score_candidates(candidates, T, baseline_lookup(
        {("tiktok", "a"): a_recent, ("tiktok", "b"): b_recent}, calls), now=NOW)
    assert [s.video.id for s in res.outliers] == ["a1"]
    assert res.outliers[0].multiple == 6.0
    assert res.candidates == 4 and res.passed_filters == 3 and res.stage2 == 3
    assert calls == [("tiktok", "a"), ("tiktok", "b")]  # each creator fetched once
    assert len(res.checked) == 3


def test_funnel_skips_seen_before_paying_for_stage2():
    calls = []
    res = score_candidates([vid("x", 900_000, creator="a", followers=1)], T,
                           baseline_lookup({}, calls), seen={"tiktok:x"}, now=NOW)
    assert res.skipped_seen == 1 and res.stage2 == 0 and calls == []


def test_funnel_caps_stage2_at_top_n():
    t = dict(T, stage2_top_n=2)
    cands = [vid(f"v{i}", 200_000 + i, creator=f"c{i}", followers=1000) for i in range(5)]
    calls = []
    table = {("tiktok", f"c{i}"): {str(j): 10_000 for j in range(6)} for i in range(5)}
    res = score_candidates(cands, t, baseline_lookup(table, calls), now=NOW)
    assert res.stage2 == 2 and len(calls) == 2


def test_funnel_uses_baseline_list_count_for_same_metric():
    # Instagram search said 1M views, but the creator's own reel list says 300k plays for it.
    cand = vid("r1", 1_000_000, creator="ig_c", platform="ig")
    recent = {"r1": 300_000, **{str(i): 100_000 for i in range(6)}}
    res = score_candidates([cand], T, baseline_lookup({("ig", "ig_c"): recent}, []), now=NOW)
    assert res.outliers == []  # 3x on the matching metric, not 10x
    assert res.checked[0].views == 300_000 and res.checked[0].multiple == 3.0


def test_funnel_records_creator_failures_and_continues():
    cands = [vid("p", 500_000, creator="private", followers=10),
             vid("ok", 900_000, creator="fine", followers=10)]
    table = {("tiktok", "private"): NotFound("404", 404),
             ("tiktok", "fine"): {str(i): 100_000 for i in range(6)}}
    res = score_candidates(cands, T, baseline_lookup(table, []), now=NOW)
    assert [s.video.id for s in res.outliers] == ["ok"]
    assert res.failures and res.failures[0][0] == "tiktok:private"


def test_funnel_stops_when_out_of_credits_but_keeps_earlier_outliers():
    cands = [vid("first", 900_000, creator="a", followers=10),   # ranks first, gets scored
             vid("second", 800_000, creator="b", followers=10)]  # credits run out here
    calls = []
    table = {("tiktok", "a"): {str(i): 100_000 for i in range(6)}, ("tiktok", "b"): OutOfCredits("402", 402)}
    res = score_candidates(cands, T, baseline_lookup(table, calls), now=NOW)
    assert res.out_of_credits
    assert [s.video.id for s in res.outliers] == ["first"]
    assert res.failures[-1][0] == "tiktok:b" and "stopped" in res.failures[-1][1]


# ------------------------------------------------------- content rules (stage 1)

from dataclasses import replace as _replace  # noqa: E402

from lib.scoring import rule_reason  # noqa: E402

RULES = dict(T, english_only=True, skip_hashtags=["parody", "satire", "pov", "*edit"])


def test_rule_reason_language_tag():
    assert rule_reason(_replace(vid(1, 1), spoken_lang="es"), RULES) == "not English (es)"
    assert rule_reason(_replace(vid(1, 1), spoken_lang="en"), RULES) is None
    assert rule_reason(_replace(vid(1, 1), spoken_lang=None), RULES) is None  # unknown: audio check later
    assert rule_reason(_replace(vid(1, 1), spoken_lang="es"), dict(RULES, english_only=False)) is None


def test_rule_reason_hashtags_wildcard_and_pov_caption():
    assert rule_reason(_replace(vid(1, 1), caption="job #Satire #genz"), RULES) == "#satire"
    assert rule_reason(_replace(vid(1, 1), caption="fix it #joebartolozziedit"), RULES) == "#joebartolozziedit"
    assert rule_reason(_replace(vid(1, 1), caption="POV: your barista is gen z"), RULES) == "POV caption"
    assert rule_reason(_replace(vid(1, 1), caption="how to invest #money #powerful"), RULES) is None
    assert rule_reason(_replace(vid(1, 1), caption="#satire"), dict(RULES, skip_hashtags=[])) is None


def test_funnel_counts_rule_skips_before_spending():
    vids = [_replace(vid("es", 2_000_000, creator="a"), spoken_lang="es"),
            _replace(vid("skit", 2_000_000, creator="b"), caption="#parody"),
            vid("ok", 2_000_000, creator="c")]
    asked = []

    def recent(platform, creator):
        asked.append(creator)
        return {str(i): 100_000 for i in range(10)}

    res = score_candidates(vids, RULES, recent, now=NOW)
    assert res.rule_skipped == {"not English": 1, "#parody": 1}
    assert asked == ["c"] and [s.video.id for s in res.outliers] == ["ok"]
