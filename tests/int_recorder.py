import time

import pytest

from clipper.cutter import probe_duration
from clipper.recorder import Recorder, list_chunks
from media_helpers import source_cmd


@pytest.mark.slow
def test_recorder_makes_contiguous_playable_chunks(tmp_path):
    # catches wrong segment args: no splitting, unparseable names, or unplayable output
    rec = Recorder("http://x", tmp_path, 10, "best", "unused", source_cmd=source_cmd(22))
    rec.start()
    deadline = time.time() + 45
    while rec.poll() is None and time.time() < deadline:
        time.sleep(0.2)
    assert rec.poll() == 0  # source ended -> ffmpeg exited cleanly
    chunks = list_chunks(tmp_path)
    assert len(chunks) >= 2
    for (p1, t1), (p2, t2) in zip(chunks, chunks[1:]):
        assert abs((t2 - t1) - 10) <= 2
    assert all(probe_duration(p) > 0.5 for p, _ in chunks)
    total = sum(probe_duration(p) for p, _ in chunks)
    assert 20 <= total <= 24
    rec.stop()
    rec.stop()  # idempotent
    assert not rec.running


@pytest.mark.slow
def test_stop_midstream_leaves_playable_last_chunk(tmp_path):
    # catches killing ffmpeg before it flushes: last chunk truncated/unplayable; also stop() hanging
    rec = Recorder("http://x", tmp_path, 10, "best", "unused", source_cmd=source_cmd(60))
    rec.start()
    time.sleep(4)
    assert rec.running and rec.poll() is None
    t0 = time.time()
    rec.stop()
    assert time.time() - t0 < 15
    rec.stop()
    chunks = list_chunks(tmp_path)
    assert len(chunks) >= 1
    assert probe_duration(chunks[-1][0]) > 1
    assert rec.poll() is not None
