from datetime import datetime, timedelta, timezone

from lib import probe as P

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)
T = {"min_views": 500_000, "max_age_days": 60, "min_duration_s": 60, "max_duration_s": 180,
     "min_likes_tiktok": 50_000, "english_only": True, "talking_head_only": True,
     "skip_hashtags": ["skit"], "exclude_ai": True, "ai_hashtags": ["aiart"]}


def aweme(i, views=1_000_000, likes=100_000, days=10, dur=120, lang="en", desc="hi"):
    return {"aweme_id": str(i), "desc": desc, "create_time": int((NOW - timedelta(days=days)).timestamp()),
            "author": {"unique_id": f"c{i}", "follower_count": 1000},
            "statistics": {"play_count": views, "digg_count": likes},
            "video": {"duration": dur * 1000, "play_addr": {"url_list": ["https://x/v.mp4"]},
                      "origin_cover": {"url_list": [f"https://x/{i}.heic", f"https://x/{i}.jpeg"]},
                      "cla_info": {"original_language_info": {"language_code": lang}}}}


def test_grades_format_and_filters_separately():
    items = [aweme(1), aweme(2, days=400), aweme(3, lang="es"), aweme(4, dur=30), aweme(5, desc="#skit"),
             aweme(6)]
    rows = P.rows_from_search(items, T, seed_id="2", now=NOW)
    P.grade(rows, T, {"1": [0.2], "2": [0.15], "3": [0.2], "4": [0.2], "5": [0.2], "6": [0.01]}, now=NOW)
    by = {r.id: r for r in rows}
    assert by["1"].fits_filters and by["1"].fits_format
    assert by["2"].fits_format and not by["2"].fits_filters and by["2"].is_seed  # too old, right format
    assert by["3"].rule == "not English (es)" and not by["3"].fits_format
    assert by["4"].rule == "length 30s"
    assert by["5"].rule == "#skit"
    assert by["6"].cover_face == 0.01 and not by["6"].fits_format  # no real face on the cover
    s = P.summarize(rows)
    assert s["seed_rank"] == 2 and s["fits_format"] == 2 and s["fits_filters"] == 1
    assert s["creators_fitting"] == ["c1", "c2"]


def test_unchecked_cover_is_not_a_talking_head():
    rows = P.rows_from_search([aweme(1)], T, now=NOW)
    P.grade(rows, T, {}, now=NOW)
    assert rows[0].cover_face is None and not rows[0].fits_format
    assert P.cover_url(aweme(7)) == "https://x/7.jpeg"  # JPEG preferred over HEIC
