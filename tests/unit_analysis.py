import time

import numpy as np
import pytest

from clipper.analysis import LAUGH_RE, chat_series, pick_peaks, robust_z, score_series
from clipper.cutter import _quote, window_parts
from clipper.recorder import chunk_start_ts, list_chunks


def test_robust_z_flat_is_zeros():
    # catches divide-by-zero / NaN when the audio is perfectly flat
    z = robust_z(np.full(50, -40.0))
    assert np.all(z == 0) and not np.isnan(z).any()


def test_robust_z_spike_is_large():
    # catches a scale so wide that a real spike never stands out
    x = np.full(100, -40.0)
    x[50] = -5
    assert robust_z(x)[50] > 10


def test_chat_series_boundaries():
    # catches off-by-one at second edges, events before the chunk, and events past its end
    ev = [(100.0, 1), (100.99, 0), (101.0, 2), (99.9, 5), (110.0, 5), (109.99, 1)]
    m, h = chat_series(ev, 100.0, 10)
    assert list(m[:3]) == [2, 1, 0] and m[9] == 1 and m.sum() == 4
    assert h[0] == 1 and h[1] == 2 and h[9] == 1


def test_score_audio_only_when_chat_sparse():
    # catches a handful of chat messages switching on chat scoring and adding noise
    audio = np.full(100, -45.0)
    audio[50] = -5
    msgs = np.zeros(100)
    msgs[10:15] = 1
    s, comp = score_series(audio, msgs, np.zeros(100))
    assert comp["chat"].sum() == 0 and comp["hype"].sum() == 0
    assert int(np.argmax(s)) in (49, 50, 51)
    s2, _ = score_series(audio)
    assert np.allclose(s, s2)


def test_score_uses_chat_when_enough_messages():
    # catches chat being ignored when there is plenty of it
    audio = np.full(100, -45.0)
    msgs = np.zeros(100)
    msgs[30:35] = 8
    s, comp = score_series(audio, msgs, msgs.copy())
    assert comp["chat"].max() > 0 and 25 <= int(np.argmax(s)) <= 40


def test_pick_peaks_nms_threshold_max_n():
    # catches neighbours of one peak being picked twice, sub-threshold picks, and exceeding max_n
    sc = np.zeros(300)
    sc[50], sc[55], sc[150], sc[250] = 9, 8, 7, 2
    assert pick_peaks(sc, 3, 30, 5) == [50, 150]
    assert pick_peaks(sc, 3, 30, 1) == [50]
    assert pick_peaks(sc, 1, 30, 5) == [50, 150, 250]
    assert pick_peaks(np.zeros(10), 3, 5, 3) == []


def test_window_parts_inside_one_chunk():
    # catches wrong chunk-relative offsets
    ch = [("a", 1000.0, 600.0), ("b", 1600.0, 600.0)]
    assert window_parts(ch, 1100, 1160) == [("a", 100, 160)]


def test_window_parts_spans_two_and_three():
    # catches dropped or duplicated boundary pieces when a clip crosses chunks
    ch = [("a", 0.0, 60.0), ("b", 60.0, 60.0), ("c", 120.0, 60.0)]
    assert window_parts(ch, 50, 70) == [("a", 50, 60), ("b", 0, 10)]
    assert window_parts(ch, 50, 130) == [("a", 50, 60), ("b", 0, 60), ("c", 0, 10)]


def test_window_parts_outside_and_clamped():
    # catches negative offsets or phantom parts when the window leaves the recording
    ch = [("a", 100.0, 60.0), ("b", 160.0, 60.0)]
    assert window_parts(ch, 0, 50) == [] and window_parts(ch, 300, 400) == []
    assert window_parts(ch, 80, 120) == [("a", 0, 20)]
    assert window_parts(ch, 200, 300) == [("b", 40, 60)]
    assert window_parts(ch, 160, 160) == []


def test_window_parts_gap_between_chunks():
    # catches a gap being papered over as if the chunks were contiguous
    ch = [("a", 0.0, 60.0), ("b", 100.0, 60.0)]
    assert window_parts(ch, 50, 110) == [("a", 50, 60), ("b", 0, 10)]


def test_chunk_start_ts_round_trip(tmp_path):
    # catches UTC/local mixups: the name is local time, so mktime must invert it
    t = int(time.time()) // 10 * 10
    name = "chunk_" + time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + ".ts"
    assert chunk_start_ts(tmp_path / name) == t
    (tmp_path / name).write_bytes(b"")
    (tmp_path / "other.ts").write_bytes(b"")
    assert list_chunks(tmp_path) == [(str(tmp_path / name), t)]


@pytest.mark.parametrize("bad", ["chunk_x.ts", "video.ts", "chunk_20260101-000000.mp4", "chunk_20261301-000000.ts"])
def test_chunk_start_ts_rejects_other_names(bad):
    # catches foreign files in the folder being treated as chunks
    with pytest.raises(ValueError):
        chunk_start_ts(bad)


@pytest.mark.parametrize("text,hit", [
    ("hahaha no way", True), ("HEHE", True), ("jajaja", True), ("kkkkk", True), ("lol that's wild", True),
    ("LMAOOO", True), ("(laughs)", True), ("[Laughter]", True), ("*laughing*", True),
    ("the hahn valley", False), ("lollipop", False), ("kk ok", False), ("we need to go left", False),
])
def test_laugh_regex(text, hit):
    # catches regex that misses real laughter or fires on ordinary words
    assert bool(LAUGH_RE.search(text)) is hit


def test_concat_quote_escapes():
    # catches a Windows backslash path or an apostrophe breaking the concat list
    assert _quote(r"C:\a b\it's.ts") == "'C:/a b/it'\\''s.ts'"
