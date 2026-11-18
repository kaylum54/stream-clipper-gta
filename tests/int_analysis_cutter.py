import json
import subprocess
import time

import numpy as np
import pytest

from clipper.analysis import audio_db_per_second, find_moments, laughter_and_transcript
from clipper.cutter import cut_clip, make_thumb, probe_duration
from media_helpers import make_ts

CFG = {"score_threshold": 3.0, "min_gap_seconds": 90, "max_clips_per_chunk": 3, "ffmpeg": "ffmpeg"}


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    return {
        "long": make_ts(d / "long.ts", 120, [(70, 2)]),
        "a": make_ts(d / "a.ts", 60),
        "b": make_ts(d / "b.ts", 60, [(10, 2)]),
        "dir": d,
    }


@pytest.mark.slow
def test_find_moments_locates_burst(media):
    # catches wrong second indexing or an unnormalised score that finds nothing / many moments
    t0 = 1_800_000_000.0
    t = time.time()
    moments = find_moments(media["long"], t0, [], CFG)
    print(f"find_moments 120 s chunk: {time.time() - t:.2f}s")
    assert len(moments) == 1
    assert abs(moments[0].peak_ts - (t0 + 70)) <= 2
    assert moments[0].signals["audio"] > 0 and moments[0].signals["chat"] == 0


@pytest.mark.slow
def test_cut_across_two_chunks_vertical(media, tmp_path):
    # catches wrong cross-chunk offsets, wrong size, or lost audio sync in the concat
    t0 = 1_800_000_000.0
    chunks = [(media["a"], t0, probe_duration(media["a"])), (media["b"], t0 + 60, probe_duration(media["b"]))]
    peak = t0 + 70
    out = cut_clip(chunks, peak - 40, peak + 20, tmp_path / "clip.mp4")
    assert abs(probe_duration(out) - 60) <= 1
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                                      "-of", "json", out], capture_output=True, text=True).stdout)["streams"][0]
    assert (info["width"], info["height"]) == (1080, 1920)
    db = audio_db_per_second(out)
    assert abs(int(np.argmax(db)) - 40) <= 2
    thumb = make_thumb(out, tmp_path / "t.jpg", 40)
    assert (tmp_path / "t.jpg").stat().st_size > 500 and thumb


@pytest.mark.slow
def test_cut_non_vertical_and_windows_path_chars(media, tmp_path):
    # catches path quoting failures (spaces, apostrophe) and non-vertical output changing resolution
    import shutil
    odd = tmp_path / "it's a dir"
    odd.mkdir()
    p = shutil.copy(media["a"], odd / "chunk a.ts")
    out = cut_clip([(p, 0.0, 60.0)], 10, 20, tmp_path / "c.mp4", vertical=False)
    assert abs(probe_duration(out) - 10) <= 1
    h = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height", "-of", "csv=p=0", out],
                       capture_output=True, text=True).stdout.strip()
    assert h == "180"


@pytest.mark.slow
def test_whisper_on_noise_does_not_raise(media):
    # catches whisper/ffmpeg errors escaping instead of returning (0.0, '')
    t = time.time()
    score, text = laughter_and_transcript(media["long"], 0, 10, "tiny")
    print(f"whisper tiny 10 s: {time.time() - t:.2f}s")
    assert isinstance(score, float) and 0 <= score <= 1 and isinstance(text, str)
    assert laughter_and_transcript(media["dir"] / "missing.ts", 0, 10) == (0.0, "")
