from pathlib import Path

import pytest
import yaml

from lib import config as C

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


PHILOSOPHY = "niches:\n  Philosophy:\n    tiktok_creators: [jonstertruck]\n    ig_creators: [michael__mezz]\n"


def test_example_config_loads():
    cfg = C.load(EXAMPLE)
    assert cfg.thresholds["min_multiple"] == 5.0 and cfg.thresholds["min_views"] == 500_000
    assert cfg.thresholds["talking_head_only"] is True and cfg.niches == {}  # new users start with no niches


def test_niche_lookup_is_case_insensitive(tmp_path):
    n = C.load(write(tmp_path, PHILOSOPHY)).niche("philosophy")
    assert n.name == "Philosophy"
    assert n.tiktok_creators == ["jonstertruck"] and n.ig_creators == ["michael__mezz"]


def test_missing_file_gives_defaults_and_no_niches(tmp_path):
    cfg = C.load(tmp_path / "config.yaml")
    assert cfg.thresholds == C.DEFAULT_THRESHOLDS and cfg.niches == {}


def test_partial_thresholds_fill_defaults_and_cast(tmp_path):
    cfg = C.load(write(tmp_path, "thresholds:\n  min_multiple: 3\n  min_views: 5000.0\n"))
    assert cfg.thresholds["min_multiple"] == 3.0
    assert cfg.thresholds["min_views"] == 5000 and isinstance(cfg.thresholds["min_views"], int)
    assert cfg.thresholds["max_age_days"] == 730


def test_handles_are_cleaned(tmp_path):
    cfg = C.load(write(tmp_path, "niches:\n  Humor:\n    tiktok_creators: ['@SomeOne', 'https://www.tiktok.com/@other']\n"
                                 "    ig_hashtags: ['#funny', comedy]\n"))
    n = cfg.niche("Humor")
    assert n.tiktok_creators == ["someone", "other"]
    assert n.ig_hashtags == ["funny", "comedy"]
    assert n.ig_creators == [] and n.ig_keywords == []


@pytest.mark.parametrize(
    "text,msg",
    [
        ("colour: blue\n", "unknown top-level"),
        ("thresholds:\n  speed: 3\n", "unknown threshold"),
        ("thresholds:\n  min_views: -1\n", "positive number"),
        ("thresholds:\n  min_views: yes\n", "positive number"),
        ("niches:\n  A:\n    tiktok_creator: [x]\n", "unknown key"),
        ("niches:\n  A:\n    tiktok_creators: [bad handle!]\n", "valid handle"),
        ("niches:\n  A:\n    tiktok_creators: [x]\n  B:\n    tiktok_creators: ['@X']\n", "listed in both"),
        ("niches:\n  A: {}\n  a: {}\n", "listed twice"),
        ("niches:\n  A:\n    ig_creators: 5\n", "must be a list"),
    ],
)
def test_validation_errors_are_readable(tmp_path, text, msg):
    with pytest.raises(C.ConfigError, match=msg):
        C.load(write(tmp_path, text))


def test_same_handle_on_two_platforms_is_fine(tmp_path):
    cfg = C.load(write(tmp_path, "niches:\n  A:\n    tiktok_creators: [x]\n    ig_creators: [x]\n"))
    assert cfg.niche("A").ig_creators == ["x"]


def test_unknown_niche_error_lists_known_ones(tmp_path):
    cfg = C.load(write(tmp_path, PHILOSOPHY))
    with pytest.raises(C.ConfigError, match="Known niches: Philosophy"):
        cfg.niche("cooking")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("@Alex.Ma", (None, "alex.ma")),
        ("alex_ma", (None, "alex_ma")),
        ("tiktok:@x", ("tiktok", "x")),
        ("tt:x", ("tiktok", "x")),
        ("ig:x", ("ig", "x")),
        ("Instagram:x", ("ig", "x")),
        ("https://www.instagram.com/michael__mezz/", ("ig", "michael__mezz")),
        ("https://www.tiktok.com/@jonstertruck?lang=en", ("tiktok", "jonstertruck")),
    ],
)
def test_parse_handle(text, expected):
    assert C.parse_handle(text) == expected


def test_parse_handle_default_platform_and_errors():
    assert C.parse_handle("@x", default_platform="ig") == ("ig", "x")
    with pytest.raises(C.ConfigError, match="Unknown platform"):
        C.parse_handle("youtube:x")
    with pytest.raises(C.ConfigError, match="valid handle"):
        C.parse_handle("@")


def test_add_creator_round_trip(tmp_path):
    path = tmp_path / "config.yaml"
    cfg = C.load(path)
    assert C.add_creator(cfg, "Philosophy", "tiktok", "@JonsterTruck") == "added"
    assert C.add_creator(cfg, "philosophy", "ig", "michael__mezz") == "added"  # existing niche, any case
    assert C.add_creator(cfg, "Humor", "tiktok", "jonstertruck") == "exists:Philosophy"
    C.save(cfg)

    text = path.read_text()
    assert text.startswith("# Outlier research config")
    data = yaml.safe_load(text)
    assert list(data) == ["thresholds", "niches"]  # key order kept
    assert data["niches"]["Philosophy"]["tiktok_creators"] == ["jonstertruck"]

    again = C.load(path)
    assert again.niche("Philosophy").ig_creators == ["michael__mezz"]
    assert "Humor" not in again.niches
    assert list(tmp_path.glob(".config-*")) == []  # temp file cleaned up


@pytest.mark.parametrize("value,expected", [
    ("1:00", 60.0), ("3:00", 180.0), ("0:45", 45.0), ("1:02:03", 3723.0), (90, 90.0), ("90", 90.0),
    ("90s", 90.0), ("7.5", 7.5), (None, None), ("off", None), ("", None), (0, None),
])
def test_parse_duration(value, expected):
    assert C.parse_duration(value) == expected


@pytest.mark.parametrize("value", ["1:5", "abc", "1:00:", True, -5])
def test_parse_duration_rejects_bad_values(value):
    with pytest.raises(C.ConfigError):
        C.parse_duration(value)


def test_length_thresholds_load_and_save_as_mm_ss(tmp_path):
    p = write(tmp_path, 'thresholds:\n  min_duration_s: "1:00"\n  max_duration_s: 180\n')
    cfg = C.load(p)
    assert cfg.thresholds["min_duration_s"] == 60.0 and cfg.thresholds["max_duration_s"] == 180.0
    C.save(cfg)
    data = yaml.safe_load(p.read_text())
    assert data["thresholds"]["min_duration_s"] == "1:00" and data["thresholds"]["max_duration_s"] == "3:00"
    assert C.load(p).thresholds["max_duration_s"] == 180.0


def test_length_filters_off_by_default_and_not_saved(tmp_path):
    cfg = C.load(tmp_path / "config.yaml")
    assert cfg.thresholds["min_duration_s"] is None and cfg.thresholds["max_duration_s"] is None
    C.save(cfg)
    assert "duration" not in (tmp_path / "config.yaml").read_text()


def test_min_length_longer_than_max_is_rejected(tmp_path):
    with pytest.raises(C.ConfigError, match="longer than max"):
        C.load(write(tmp_path, 'thresholds:\n  min_duration_s: "3:00"\n  max_duration_s: "1:00"\n'))


@pytest.mark.parametrize("value,expected", [
    (50000, 50000), ("50000", 50000), ("50k", 50000), ("50K", 50000), ("1.2m", 1_200_000), ("50,000", 50000),
    ("10_000", 10000), (None, None), ("off", None), ("", None), (0, None),
])
def test_parse_count(value, expected):
    assert C.parse_count(value) == expected


@pytest.mark.parametrize("value", ["lots", "50kk", True, -5, "1.2b"])
def test_parse_count_rejects_bad_values(value):
    with pytest.raises(C.ConfigError):
        C.parse_count(value)


def test_likes_thresholds_load_save_and_default_off(tmp_path):
    assert C.load(tmp_path / "none.yaml").thresholds["min_likes_tiktok"] is None
    p = write(tmp_path, 'thresholds:\n  min_likes_tiktok: 50k\n  min_likes_ig: 10000\n')
    cfg = C.load(p)
    assert (cfg.thresholds["min_likes_tiktok"], cfg.thresholds["min_likes_ig"]) == (50000, 10000)
    cfg.thresholds["min_likes_ig"] = None
    C.save(cfg)
    data = yaml.safe_load(p.read_text())
    assert data["thresholds"]["min_likes_tiktok"] == 50000 and "min_likes_ig" not in data["thresholds"]


def test_ai_settings_load_and_validate(tmp_path):
    cfg = C.load(tmp_path / "none.yaml")
    assert cfg.thresholds["exclude_ai"] is False and cfg.thresholds["ai_hashtags"] == []
    cfg = C.load(write(tmp_path, 'thresholds:\n  exclude_ai: true\n  ai_hashtags: ["#AIvideo", sora, aivideo]\n'))
    assert cfg.thresholds["exclude_ai"] is True and cfg.thresholds["ai_hashtags"] == ["aivideo", "sora"]
    with pytest.raises(C.ConfigError, match="true or false"):
        C.load(write(tmp_path, 'thresholds:\n  exclude_ai: 1\n'))
    with pytest.raises(C.ConfigError, match="valid hashtag"):
        C.load(write(tmp_path, 'thresholds:\n  ai_hashtags: ["ai video"]\n'))


def test_fmt_count():
    assert [C.fmt_count(n) for n in (None, 50000, 10000, 1_500_000, 12345)] == ["off", "50k", "10k", "1.5m", "12,345"]


def test_add_creator_rejects_bad_platform(tmp_path):
    cfg = C.load(write(tmp_path, PHILOSOPHY))
    with pytest.raises(C.ConfigError, match="platform"):
        C.add_creator(cfg, "Philosophy", "youtube", "x")


def test_content_rule_keys(tmp_path):
    cfg = C.load(write(tmp_path, "thresholds:\n  english_only: true\n  talking_head_only: true\n"
                                 "  skip_hashtags: ['#Parody', '*edit', pov]\n  min_words_per_min: 80\n"))
    t = cfg.thresholds
    assert t["english_only"] is True and t["talking_head_only"] is True
    assert t["skip_hashtags"] == ["parody", "*edit", "pov"] and t["min_words_per_min"] == 80
    with pytest.raises(C.ConfigError, match="talking_head_only"):
        C.load(write(tmp_path, "thresholds:\n  talking_head_only: yes please\n"))
    with pytest.raises(C.ConfigError, match="isn't a valid hashtag"):
        C.load(write(tmp_path, "thresholds:\n  skip_hashtags: ['ed*it']\n"))
