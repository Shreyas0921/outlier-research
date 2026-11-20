from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

from lib import vault_writer as W
from lib.media import MediaResult, Transcript
from lib.normalize import Video
from lib.scoring import Scored

TODAY = date(2026, 11, 14)


def make_video(caption="Nobody tells you this about philosophy 🤯 #philosophy #stoic @friend", platform="tiktok",
               id="7701351936750062870", creator="ranking5_1"):
    return Video(platform=platform, id=id, url=f"https://www.tiktok.com/@{creator}/video/{id}", creator=creator,
                 followers=13080, views=187246, view_metric="plays", likes=17993, comments=109, shares=1483,
                 posted=datetime(2026, 10, 27, tzinfo=timezone.utc), duration_s=73.58, caption=caption,
                 download_url="x")


def make_media(vault: Path, video: Video, speech=True):
    d = W.media_root(vault) / f"{video.platform}-{video.id}"
    d.mkdir(parents=True)
    stills = []
    for t in ("0.0", "1.0", "2.5", "4.5"):
        p = d / f"still-{t}s.jpg"
        p.write_bytes(b"x")
        stills.append(p)
    tr = Transcript(speech=True, words=[], hook_text="nobody tells you this about philosophy.",
                    full_text="nobody tells you this about philosophy. it is about better questions.") \
        if speech else Transcript(speech=False)
    return MediaResult(dir=d, stills=stills, transcript=tr, duration_s=73.58, downloaded_via="direct")


def scored(video, multiple=12.3456, baseline=15_166.7, n=20):
    return Scored(video=video, views=video.views, multiple=multiple, baseline=baseline, baseline_n=n)


def frontmatter(text):
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n")[1])


NICHE = W.FoundVia("niche", "Philosophy", niche="Philosophy", search='keyword "stoicism"')


# ------------------------------------------------------------------- paths


def test_every_note_goes_in_the_one_outliers_folder(tmp_path):
    p = W.note_path(tmp_path, make_video())
    assert p.parent == tmp_path / "Outliers"
    assert p.name == "TikTok - ranking5_1 - Nobody tells you this about philosophy.md"


@pytest.mark.parametrize("found,niche,search", [
    (W.FoundVia("keyword", "study tips", search='keyword "study tips"'), None, 'keyword "study tips"'),
    (W.FoundVia("creator", "@x", niche="Humor", search="creator @x"), "Humor", "creator @x"),
    (W.FoundVia("niche", "Philosophy", niche="Philosophy", search="#stoic"), "Philosophy", "#stoic"),
])
def test_niche_and_search_are_properties(tmp_path, found, niche, search):
    v = make_video()
    path = W.write_note(tmp_path, scored(v), found, make_media(tmp_path, v), today=TODAY)
    fm = frontmatter(path.read_text())
    assert path.parent.name == "Outliers" and fm["niche"] == niche and fm["search"] == search


def test_title_strips_illegal_characters_and_caps_length():
    v = make_video(caption='What is "truth"? A/B: the [real] answer | #deep ' + "word " * 40, platform="ig",
                   creator="michael__mezz")
    t = W.note_title(v)
    assert t.startswith("Instagram - michael__mezz - What is truth")
    assert not any(c in t for c in '\\/:*?"<>|#^[]')
    assert len(t) <= W.MAX_NAME


def test_title_falls_back_to_id_when_caption_is_only_tags_and_emoji():
    assert W.note_title(make_video(caption="🔥🔥 #fyp #viral")) == "TikTok - ranking5_1 - 7701351936750062870"


# -------------------------------------------------------------------- note


def test_frontmatter_fields(tmp_path):
    v = make_video()
    text = W.render_note(tmp_path, scored(v), NICHE, make_media(tmp_path, v), today=TODAY)
    fm = frontmatter(text)
    assert fm["tags"] == ["outlier"]
    assert fm["platform"] == "TikTok" and fm["creator"] == "ranking5_1" and fm["url"] == v.url
    assert fm["views"] == 187246 and fm["followers"] == 13080
    assert fm["likes"] == 17993 and fm["comments"] == 109 and fm["shares"] == 1483
    assert fm["outlier_score"] == 12.3  # rounded for display
    assert fm["creator_median"] == 15167 and fm["baseline_videos"] == 20
    assert fm["view_metric"] == "plays"
    assert fm["posted"] == date(2026, 10, 27) and fm["found_on"] == TODAY
    assert fm["niche"] == "Philosophy" and fm["found_via"] == "niche: Philosophy"
    assert fm["search"] == 'keyword "stoicism"'
    assert fm["speech"] == "yes" and fm["duration_s"] == 73.58
    assert fm["cover"] == "[[Outliers/_media/tiktok-7701351936750062870/still-1.0s.jpg]]"


def test_body_sections_in_order_with_vault_relative_embeds(tmp_path):
    v = make_video()
    text = W.render_note(tmp_path, scored(v), NICHE, make_media(tmp_path, v), today=TODAY)
    body = text.split("---\n", 2)[2]
    order = [body.index(s) for s in ("![[", "## Spoken hook", "## On-screen hook", "## Caption", "## Full transcript")]
    assert order == sorted(order)
    media = "Outliers/_media/tiktok-7701351936750062870"
    assert "hook.mp4" not in body and ".mp4" not in body  # no video clip is kept
    assert f"![[{media}/still-0.0s.jpg|160]] ![[{media}/still-1.0s.jpg|160]]" in body
    assert "> nobody tells you this about philosophy." in body
    assert W.ONSCREEN_MARKER in body
    assert "**12.3×** 187.2K plays vs this creator's usual 15.2K (median of 20 recent videos)" in body
    assert "it is about better questions." in body


def test_no_speech_note(tmp_path):
    v = make_video()
    text = W.render_note(tmp_path, scored(v), NICHE, make_media(tmp_path, v, speech=False), today=TODAY)
    assert frontmatter(text)["speech"] == "none"
    assert text.count("_No speech (music or silent)._") == 2


def test_onscreen_text_given_up_front(tmp_path):
    v = make_video()
    text = W.render_note(tmp_path, scored(v), NICHE, make_media(tmp_path, v), onscreen_text="none", today=TODAY)
    assert "_No on-screen text._" in text and W.ONSCREEN_MARKER not in text


# ------------------------------------------------------------ write/update


def test_write_never_overwrites(tmp_path):
    v = make_video()
    m = make_media(tmp_path, v)
    a = W.write_note(tmp_path, scored(v), NICHE, m, today=TODAY)
    b = W.write_note(tmp_path, scored(v), NICHE, m, today=TODAY)
    c = W.write_note(tmp_path, scored(v), NICHE, m, today=TODAY)
    assert b.name == a.stem + " (2).md" and c.name == a.stem + " (3).md"
    assert a.read_text() and b.read_text()


def test_set_onscreen_text_replaces_placeholder_once(tmp_path):
    v = make_video()
    note = W.write_note(tmp_path, scored(v), NICHE, make_media(tmp_path, v), today=TODAY)
    assert W.list_unread_onscreen(tmp_path, [note]) == [note]
    assert W.set_onscreen_text(note, "NOBODY TELLS YOU THIS\nabout philosophy")
    text = note.read_text()
    assert "> NOBODY TELLS YOU THIS\n> about philosophy" in text
    assert W.ONSCREEN_MARKER not in text
    assert W.set_onscreen_text(note, "again") is False  # already filled
    assert W.list_unread_onscreen(tmp_path, [note]) == []


def test_set_onscreen_none(tmp_path):
    v = make_video()
    note = W.write_note(tmp_path, scored(v), NICHE, make_media(tmp_path, v), today=TODAY)
    W.set_onscreen_text(note, "none")
    assert "_No on-screen text._" in note.read_text()


# -------------------------------------------------------------------- base


def test_ensure_base_creates_once_and_is_valid_yaml(tmp_path):
    p = W.ensure_base(tmp_path)
    assert p == tmp_path / "Outliers/Outliers.base"
    data = yaml.safe_load(p.read_text())
    assert 'file.inFolder("Outliers")' in data["filters"]["and"]
    assert data["views"][0]["sort"] == [{"property": "note.outlier_score", "direction": "DESC"}]
    assert data["views"][1]["image"] == "note.cover"
    p.write_text("# user edited\n")
    assert W.ensure_base(tmp_path) is None
    assert p.read_text() == "# user edited\n"  # never overwritten
