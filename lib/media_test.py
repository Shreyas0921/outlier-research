import functools
import http.server
import json
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib import media as M
from lib.normalize import Video

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ------------------------------------------------------------- test media


def ffmpeg_gen(out: Path, seconds: float, audio: bool = True) -> Path:
    # Durations are built into the lavfi sources, so these can never run forever.
    args = ["ffmpeg", "-v", "error", "-y", "-nostdin",
            "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x568:rate=30"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest"]
    args += (["-c:a", "aac"] if audio else []) + [str(out)]
    subprocess.run(args, check=True, capture_output=True, timeout=60)
    return out


@pytest.fixture(scope="module")
def media_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    ffmpeg_gen(d / "seven.mp4", 7)
    ffmpeg_gen(d / "three.mp4", 3)
    ffmpeg_gen(d / "silent.mp4", 6, audio=False)
    (d / "page.html").write_text("<html>not a video</html>")
    return d


@pytest.fixture(scope="module")
def server(media_dir):
    handler = functools.partial(QuietHandler, directory=str(media_dir))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def video(download_url=None, url="https://www.tiktok.com/@c/video/123"):
    return Video(platform="tiktok", id="123", url=url, creator="c", followers=1, views=1,
                 view_metric="plays", likes=None, comments=None, shares=None,
                 posted=datetime.now(timezone.utc), duration_s=7, caption="", download_url=download_url)


def ffprobe(path, entries):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", entries, "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def fake_runner(media_dir, whisper_json=None, ytdlp_ok=True, calls=None):
    """Stands in for subprocess.run: fakes yt-dlp and whisper-cli, runs everything else for real."""
    calls = calls if calls is not None else []

    def run(cmd, **kw):
        calls.append(Path(cmd[0]).name)
        if Path(cmd[0]).name == "yt-dlp":
            if not ytdlp_ok:
                return subprocess.CompletedProcess(cmd, 1, "", "ERROR: Unsupported URL")
            out = Path(cmd[cmd.index("-o") + 1].replace("%(ext)s", "mp4"))
            shutil.copy(media_dir / "seven.mp4", out)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "whisper-cli":
            if whisper_json is not None:
                base = Path(cmd[cmd.index("-of") + 1])
                base.with_suffix(".json").write_text(json.dumps(whisper_json))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.run(cmd, **kw)

    return run


WHISPER_FIXTURE = json.loads((FIXTURES / "whisper_vad.json").read_text())


# --------------------------------------------------------------- download


def test_direct_download(media_dir, server, tmp_path):
    path, via = M.download(video(f"{server}/seven.mp4"), tmp_path, run=fake_runner(media_dir))
    assert via == "direct" and path.name == "full.mp4"
    assert M.probe(path) == pytest.approx(7, abs=0.1)


def test_falls_back_to_ytdlp_when_link_is_dead(media_dir, server, tmp_path):
    calls = []
    path, via = M.download(video(f"{server}/missing.mp4"), tmp_path, run=fake_runner(media_dir, calls=calls))
    assert via == "yt-dlp" and calls == ["yt-dlp"] and path.exists()


def test_falls_back_when_link_is_not_a_video(media_dir, server, tmp_path):
    _, via = M.download(video(f"{server}/page.html"), tmp_path, run=fake_runner(media_dir))
    assert via == "yt-dlp"


def test_no_download_url_goes_straight_to_ytdlp(media_dir, tmp_path):
    _, via = M.download(video(None), tmp_path, run=fake_runner(media_dir))
    assert via == "yt-dlp"


def test_both_methods_failing_raises_and_leaves_nothing(media_dir, server, tmp_path):
    with pytest.raises(M.MediaError, match="direct: .*yt-dlp: .*Unsupported URL"):
        M.download(video(f"{server}/missing.mp4"), tmp_path, run=fake_runner(media_dir, ytdlp_ok=False))
    assert not (tmp_path / "full.mp4").exists()


# ------------------------------------------------------------------ stills


def test_four_stills_and_skips_times_past_the_end(media_dir, tmp_path):
    stills = M.make_stills(media_dir / "seven.mp4", tmp_path, duration=7.0)
    assert [p.name for p in stills] == ["still-0.0s.jpg", "still-1.0s.jpg", "still-2.5s.jpg", "still-4.5s.jpg"]
    assert ffprobe(stills[0], "stream=width")["streams"][0]["width"] == 540
    (tmp_path / "short").mkdir()
    short = M.make_stills(media_dir / "three.mp4", tmp_path / "short", duration=3.0)
    assert [p.name for p in short] == ["still-0.0s.jpg", "still-1.0s.jpg", "still-2.5s.jpg"]


# -------------------------------------------------------------- whisper JSON


def test_parse_whisper_maps_vad_timeline_back_to_real_time():
    t = M.parse_whisper(WHISPER_FIXTURE)
    assert t.speech
    first = t.words[0]
    assert first["w"] == "nobody" and first["start"] == pytest.approx(1.99, abs=0.01)
    and_word = next(w for w in t.words if w["w"] == "And")
    assert and_word["start"] == pytest.approx(7.29, abs=0.01)  # second segment shifted correctly
    assert t.hook_text.startswith("nobody tells you this about philosophy.")
    assert "everything" not in t.hook_text and t.full_text.endswith("changes everything.")
    assert all(w["start"] < 5.0 for w in t.words[: len(t.hook_text.split())])


def test_parse_whisper_joins_subword_tokens_and_punctuation():
    data = {"transcription": [{"offsets": {"from": 1000, "to": 2000}, "text": " Philosophy rocks.",
                               "tokens": [{"text": "[_BEG_]", "offsets": {"from": 0, "to": 0}},
                                          {"text": " Philosoph", "offsets": {"from": 0, "to": 300}},
                                          {"text": "y", "offsets": {"from": 300, "to": 400}},
                                          {"text": " rocks", "offsets": {"from": 500, "to": 800}},
                                          {"text": ".", "offsets": {"from": 800, "to": 850}}]}]}
    t = M.parse_whisper(data)
    assert [w["w"] for w in t.words] == ["Philosophy", "rocks."]
    assert t.words[0] == {"w": "Philosophy", "start": 1.0, "end": 1.4}


@pytest.mark.parametrize("text", [" [Music]", " (upbeat music)", " ♪ ♪", "  "])
def test_parse_whisper_music_only_is_no_speech(text):
    data = {"transcription": [{"offsets": {"from": 0, "to": 5000}, "text": text,
                               "tokens": [{"text": text, "offsets": {"from": 0, "to": 5000}}]}]}
    assert M.parse_whisper(data).speech is False


def test_parse_whisper_empty():
    assert M.parse_whisper({"transcription": []}).speech is False
    assert M.parse_whisper({}).speech is False


def test_transcribe_skips_whisper_when_video_has_no_audio(media_dir, tmp_path):
    calls = []
    t = M.transcribe(media_dir / "silent.mp4", tmp_path, run=fake_runner(media_dir, calls=calls))
    assert t.speech is False and "whisper-cli" not in calls


# ------------------------------------------------------------------ process


def test_process_end_to_end_cleans_up(media_dir, server, tmp_path):
    res = M.process(video(f"{server}/seven.mp4"), tmp_path,
                    run=fake_runner(media_dir, whisper_json=WHISPER_FIXTURE))
    assert res.dir == tmp_path / "tiktok-123"
    assert len(res.stills) == 4
    assert res.downloaded_via == "direct" and res.duration_s == pytest.approx(7, abs=0.1)
    assert res.transcript.speech and res.transcript.hook_text.startswith("nobody")
    left = sorted(p.name for p in res.dir.iterdir())
    assert "full.mp4" not in left and "audio16k.wav" not in left
    assert left == ["still-0.0s.jpg", "still-1.0s.jpg", "still-2.5s.jpg", "still-4.5s.jpg",
                    "transcript.json", "transcript.txt"]


def test_process_failure_still_deletes_full_video(media_dir, server, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise M.MediaError("ffmpeg exploded")
    monkeypatch.setattr(M, "make_stills", boom)
    with pytest.raises(M.MediaError):
        M.process(video(f"{server}/seven.mp4"), tmp_path, run=fake_runner(media_dir))
    assert not (tmp_path / "tiktok-123" / "full.mp4").exists()


# ---------------------------------------------------------- real whisper run


@pytest.mark.slow
def test_real_whisper_on_spoken_sample(tmp_path):
    if not shutil.which("whisper-cli") or not M.WHISPER_MODEL.exists():
        pytest.skip("whisper-cli or model missing")
    mp4 = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin",
                    "-f", "lavfi", "-i", "color=c=black:size=320x568:rate=30:duration=10",
                    "-i", str(FIXTURES / "speech_sample.aiff"),
                    "-af", "adelay=2000:all=1,apad=pad_dur=1", "-t", "10",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(mp4)],
                   check=True, capture_output=True, timeout=60)
    t = M.transcribe(mp4, tmp_path)
    assert t.speech
    assert t.words[0]["start"] == pytest.approx(2.0, abs=0.4)
    assert "philosophy" in t.hook_text.lower()
    assert "everything" in t.full_text.lower()
    assert not (tmp_path / "audio16k.wav").exists()


# ------------------------------------------------------------ content checks


def test_check_speech_rate():
    words = [{"w": "x", "start": 0, "end": 0}] * 150
    assert M.check_speech_rate(M.Transcript(speech=True, words=words), 60, 60) == 150
    with pytest.raises(M.Rejected, match="11 words/min"):
        M.check_speech_rate(M.Transcript(speech=True, words=words[:11]), 60, 60)
    with pytest.raises(M.Rejected, match="0 words/min"):
        M.check_speech_rate(M.Transcript(speech=False), 60, 60)


def test_check_on_camera_uses_frames_across_video(tmp_path, monkeypatch):
    made = []

    def fake_ffmpeg(args):
        out = Path(args[-1])
        out.write_bytes(b"jpg")
        made.append(float(args[1]))

    monkeypatch.setattr(M, "_ffmpeg", fake_ffmpeg)
    talking = lambda frames: {str(f): [0.2] for f in frames}  # noqa: E731
    assert M.check_on_camera(tmp_path / "full.mp4", tmp_path, 120, talking) == 1.0
    assert len(made) == 12 and made[-1] > 100  # samples reach the end of the video
    assert not (tmp_path / "faceframes").exists()  # frames are cleaned up
    sparse = lambda frames: {str(f): ([0.2] if i < 3 else []) for i, f in enumerate(frames)}  # noqa: E731
    with pytest.raises(M.Rejected, match=r"face in 3/12 frames"):
        M.check_on_camera(tmp_path / "full.mp4", tmp_path, 120, sparse)


def test_detect_language_parses_whisper(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "has_audio", lambda p: True)
    monkeypatch.setattr(M, "_ffmpeg", lambda args: Path(args[-1]).write_bytes(b"wav"))

    class R:
        returncode, stdout = 0, ""
        stderr = "whisper_full_with_state: auto-detected language: hi (p = 0.97)\n"

    assert M.detect_language(tmp_path / "full.mp4", tmp_path, run=lambda *a, **k: R()) == "hi"
    R.stderr = "nothing useful"
    assert M.detect_language(tmp_path / "full.mp4", tmp_path, run=lambda *a, **k: R()) is None
    assert not (tmp_path / "lang16k.wav").exists()


def test_whisper_threads_uses_every_core_on_small_machines():
    from lib.media import whisper_threads
    assert whisper_threads(2) == 2 and whisper_threads(4) == 4
    assert whisper_threads(10) == 8
