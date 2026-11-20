import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib import normalize as N

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())["response"]


def day(v):
    return v.posted.date().isoformat()


# ------------------------------------------------------------------ TikTok


def test_tiktok_search_items():
    items = [i["aweme_info"] for i in fixture("tiktok_search.json")["search_item_list"]]
    videos = N.tiktok_list(items)
    assert len(videos) == len(items)
    v = videos[0]
    assert v.platform == "tiktok" and v.id == "7701351936750062870"
    assert v.key == "tiktok:7701351936750062870"
    assert v.url == "https://www.tiktok.com/@ranking5_1/video/7701351936750062870"
    assert v.creator == "ranking5_1" and v.followers == 13080
    assert v.views == 187246 and v.view_metric == "plays"
    assert v.likes == 17993 and v.comments == 109 and v.shares == 1483
    assert day(v) == "2026-10-27" and v.posted.tzinfo is not None
    assert v.duration_s == 73.58  # 73585 ms
    assert v.download_url.startswith("http")
    assert all(x.followers is not None for x in videos)


def test_tiktok_profile_videos():
    videos = N.tiktok_list(fixture("tiktok_profile_videos.json")["aweme_list"])
    assert [v.views for v in videos] == [85116, 127522, 1145651, 56337, 637364]
    assert {v.creator for v in videos} == {"jonstertruck"}
    assert videos[0].followers == 685581


def test_tiktok_followers_override():
    videos = N.tiktok_list(fixture("tiktok_profile_videos.json")["aweme_list"], followers=1)
    assert {v.followers for v in videos} == {1}


def test_tiktok_single_video():
    v = N.tiktok_aweme(fixture("tiktok_video.json")["aweme_detail"])
    assert v.views == 12_400_000 and v.duration_s == 200.0
    assert v.creator == "jonstertruck" and v.followers == 685400
    assert v.download_url  # watermark-only video still falls back to play_addr


def test_tiktok_profile_followers():
    assert N.tiktok_followers(fixture("tiktok_profile.json")) == 685500


def test_tiktok_skips_photo_carousels_and_items_without_video():
    base = fixture("tiktok_profile_videos.json")["aweme_list"][0]
    carousel = dict(base, image_post_info={"images": [{"display_image": {}}]})
    cover_only = dict(base, image_post_info={"image_post_cover": {}, "images": []})
    assert N.tiktok_aweme(cover_only) is not None  # normal video with a cover block
    no_video = dict(base, video={})
    assert N.tiktok_aweme(carousel) is None
    assert N.tiktok_aweme(no_video) is None
    assert N.tiktok_aweme({}) is None


def test_tiktok_prefers_no_watermark_url():
    base = fixture("tiktok_profile_videos.json")["aweme_list"][0]
    a = dict(base, video=dict(base["video"], download_no_watermark_addr={"url_list": ["https://nowm"]}))
    assert N.tiktok_aweme(a).download_url == "https://nowm"


# --------------------------------------------------------------- Instagram


def test_ig_user_reels_with_creator_override_for_collabs():
    media = [i["media"] for i in fixture("ig_user_reels.json")["items"]]
    raw = N.ig_reels_list(media)
    assert raw[0].creator == "climatevanguard"  # collab reel owned by the other account
    videos = N.ig_reels_list(media, creator="michael__mezz", followers=329922)
    v = videos[0]
    assert v.platform == "ig" and v.id == "Dfr6dfPgglg"
    assert v.url == "https://www.instagram.com/reel/Dfr6dfPgglg/"
    assert v.creator == "michael__mezz" and v.followers == 329922
    assert v.views == 140942 and v.view_metric == "plays"
    assert v.posted == datetime.fromtimestamp(1794582585, tz=timezone.utc)
    assert v.duration_s == 74.0
    assert "capitalism" in v.caption
    assert v.download_url.startswith("http")
    assert [x.views for x in videos] == [140942, 27593, 220509, 57105, 131861]


def test_ig_reels_search():
    videos = N.ig_search_list(fixture("ig_reels_search.json")["reels"])
    v = videos[0]
    assert v.id == "DeHoex2Mrk4" and v.creator == "vandanamahajanofficial"
    assert v.views == 618 and v.view_metric == "views"
    assert v.followers is None  # search results carry no follower count
    assert day(v) == "2026-10-05"
    assert v.caption.startswith("Philosophy encompasses")


def test_ig_hashtag_search():
    videos = N.ig_search_list(fixture("ig_hashtag_search.json")["posts"])
    assert [v.views for v in videos] == [1682, 119730, 970, 632838, 3074]
    assert day(videos[0]) == "2026-03-23"


def test_ig_post():
    v = N.ig_post(fixture("ig_post.json")["data"]["xdt_shortcode_media"])
    assert v.id == "DZoGZQBJ7H7" and v.creator == "michael__mezz"
    assert v.views == 316364 and v.view_metric == "plays"
    assert v.duration_s == 83.78
    assert v.posted == datetime.fromtimestamp(1781569589, tz=timezone.utc)


def test_ig_profile_followers():
    assert N.ig_followers(fixture("ig_profile.json")) == 329922


def test_ig_skips_non_video_items():
    r = dict(fixture("ig_reels_search.json")["reels"][0], is_video=False)
    assert N.ig_search_reel(r) is None
    m = dict(fixture("ig_user_reels.json")["items"][0]["media"], video_versions=[])
    assert N.ig_reel_media(m) is None


# ------------------------------------------------------------- view metric


@pytest.mark.parametrize(
    "views,plays,likes,expected",
    [
        (10, 20, 30, (10, "views")),
        (None, 20, 30, (20, "plays")),
        (None, None, 30, (30, "likes")),
        (None, None, None, (None, "none")),
        ("0", None, 5, (0, "views")),  # zero is a real value, not missing
    ],
)
def test_pick_views_order(views, plays, likes, expected):
    assert N._pick_views(views, plays, likes) == expected


def test_view_metric_falls_back_to_likes_on_instagram():
    r = dict(fixture("ig_hashtag_search.json")["posts"][0], video_view_count=None)
    v = N.ig_search_reel(r)
    assert v.view_metric == "likes" and v.views == r["like_count"]


# ---------------------------------------------------------------- AI labels


@pytest.mark.parametrize("aweme,expected", [
    ({"aigc_info": {"aigc_label_type": 2, "created_by_ai": True}}, True),  # creator/auto label
    ({"aigc_info": {"aigc_label_type": 0, "created_by_ai": False}}, False),
    ({"aigc_info": {"aigc_label_type": 0}, "moderation_aigc_info": {"moderation_aigc_label_type": 1}}, True),
    ({}, None),  # response doesn't say
])
def test_tiktok_ai_label(aweme, expected):
    assert N.tiktok_ai_label(aweme) is expected


@pytest.mark.parametrize("media,expected", [
    ({"gen_ai_detection_method": {"detection_method": "NONE"}}, False),
    ({"ai_label_info": {"gen_ai_detection_method": "CAMERA_CAPTURE"}}, False),
    ({"ai_label_info": None}, False),
    ({"gen_ai_detection_method": {"detection_method": "SELF_DISCLOSURE"}}, True),
    ({"ai_label_info": {"gen_ai_detection_method": "C2PA"}}, True),
    ({}, None),  # search results: no label at all
])
def test_ig_ai_label(media, expected):
    assert N.ig_ai_label(media) is expected


def test_tiktok_spoken_language():
    base = {"aweme_id": "1", "author": {"unique_id": "a"}, "video": {"play_addr": {"url_list": ["u"]}}}
    assert N.tiktok_aweme(base).spoken_lang is None
    orig = dict(base, video={"play_addr": {"url_list": ["u"]},
                             "cla_info": {"original_language_info": {"language_code": "EN"}}})
    assert N.tiktok_aweme(orig).spoken_lang == "en"
    caps = dict(base, video={"play_addr": {"url_list": ["u"]}, "cla_info": {"caption_infos": [
        {"language_code": "en", "is_original_caption": False}, {"language_code": "es", "is_original_caption": True}]}})
    assert N.tiktok_aweme(caps).spoken_lang == "es"  # the original track, not a translation
