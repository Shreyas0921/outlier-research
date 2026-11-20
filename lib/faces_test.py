from lib import faces


def test_sample_times_cover_whole_video():
    t = faces.sample_times(100, 12)
    assert len(t) == 12 and t[0] == 3.0 and t[-1] == 97.0
    assert faces.sample_times(0) == [] and faces.sample_times(10, 1) == [5.0]


def test_face_share_needs_a_big_enough_face():
    frames = [[0.2], [0.03], [], [0.05, 0.01]]
    assert faces.face_share(frames, 0.04) == 0.5  # 0.2 and 0.05 count; 0.03 is a background face
    assert faces.face_share([]) == 0.0


def test_yunet_heights_keep_confident_faces_only():
    rows = [[0, 0, 50, 100] + [0] * 10 + [0.9], [0, 0, 5, 8] + [0] * 10 + [0.3]]
    assert faces.yunet_heights(rows, 500) == [0.2]
    assert faces.yunet_heights(None, 500) == []


def test_backend_follows_override(monkeypatch):
    monkeypatch.setenv("OUTLIER_FACE_BACKEND", "yunet")
    assert faces.backend() == "yunet"
    monkeypatch.setenv("OUTLIER_FACE_BACKEND", "vision")
    assert faces.backend() == "vision"
