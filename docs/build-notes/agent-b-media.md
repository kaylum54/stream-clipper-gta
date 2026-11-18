# Agent B notes: recorder, analysis, cutter

- streamlink 8.6.3: `--stdout` and `--stream-segment-threads` exist. `--twitch-disable-ads` does NOT exist (removed); dropped.
- Timings (this laptop, CPU only): audio analysis of a 600 s chunk (find_moments, ffmpeg decode + scoring) 0.6 to 2 s. Whisper base int8 on a 60 s window of noise: 21 s warm, 27 s cold (model load included). Cut of 60 s into 1080x1920 (veryfast, blur background): about 20 s on synthetic 320x180 input, so plan for roughly real-time-ish cut speed per clip. Synthetic 600 s chunk build: 9 s.
- BUG FOUND: faster-whisper 1.2.1 decode_audio fails with the installed PyAV ("open() got an unexpected keyword argument metadata_errors"). laughter_and_transcript therefore decodes with ffmpeg to a float32 16 kHz array and passes the array. Anyone calling WhisperModel.transcribe with a file path will hit this.
- BUG FOUND: Windows launchers (pip's streamlink.exe, Chocolatey's ffmpeg shim) spawn the real program as a child. terminate() on the launcher leaves the child holding the pipe, so ffmpeg never sees EOF and stop() hung. stop() now kills the source tree with taskkill /T /F, then waits for ffmpeg to flush.
- Deviation: the segmenting ffmpeg gets "-analyzeduration 1000000" before -i. With the 5 s default and a realtime source, the first chunk file is created (and named) about 5 s late, so chunk_start_ts for the first chunk would be wrong by that much. With 1 s the lag is about 2 s. Real HLS from streamlink bursts the buffer, so the lag there is smaller.
- Segment names have 1 s resolution: chunk_seconds must be >= 10. Test sources must run in realtime (-re) or chunks overwrite each other.
- --twitch-disable-ads does not exist in streamlink 8.6.3 and is not passed.
- Chat windows use a centered 10 s sum; score components are clipped to [0, 10] before weighting, so audio-only max is 15. A burst scoring at the clip plateau picks the first second of the plateau.
