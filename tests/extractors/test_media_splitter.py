"""Unit tests for the 2-minute AV media splitter.

ffmpeg / ffprobe are mocked via ``subprocess.run`` so the tests run
without the binaries installed.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from platform_service.workers.extractors.media_splitter import (
    MediaSplitterError,
    split_into_chunks,
)


def _fake_run(
    duration_seconds: float,
    *,
    encode_payload: bytes = b"mp3-bytes",
) -> MagicMock:
    """A subprocess.run replacement that fakes ffprobe + ffmpeg calls."""
    chunk_writes: list[Path] = []

    def runner(cmd: list[str], **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if cmd[0].endswith("ffprobe"):
            result.stdout = f'{{"format": {{"duration": "{duration_seconds}"}}}}'
        else:
            # ffmpeg: write fake payload to the destination path (last arg).
            dest = Path(cmd[-1])
            dest.write_bytes(encode_payload)
            chunk_writes.append(dest)
            result.stdout = ""
        return result

    return MagicMock(side_effect=runner), chunk_writes


def _patch_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "platform_service.workers.extractors.media_splitter.shutil.which",
        lambda name: f"/fake/bin/{name}",
    )


def test_short_source_produces_single_chunk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_binaries(monkeypatch)
    source = tmp_path / "audio.mp3"
    source.write_bytes(b"x")
    fake, _ = _fake_run(duration_seconds=30.0)

    with patch("platform_service.workers.extractors.media_splitter.subprocess.run", fake):
        chunks = split_into_chunks(source, source_type="audio")

    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert chunks[0].start_ms == 0
    assert chunks[0].end_ms == 30_000
    assert chunks[0].mime_type == "audio/mpeg"
    assert chunks[0].payload_bytes == b"mp3-bytes"


def test_long_source_chunks_with_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_binaries(monkeypatch)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"x")
    # 5 minutes (300s) of media → with 120s chunks and 15s overlap (step=105s):
    #   chunk 0: 0–120000
    #   chunk 1: 105000–225000
    #   chunk 2: 210000–300000 (clipped to source end)
    fake, _ = _fake_run(duration_seconds=300.0)

    with patch("platform_service.workers.extractors.media_splitter.subprocess.run", fake):
        chunks = split_into_chunks(source, source_type="video")

    assert [(c.start_ms, c.end_ms) for c in chunks] == [
        (0, 120_000),
        (105_000, 225_000),
        (210_000, 300_000),
    ]
    assert [c.index for c in chunks] == [0, 1, 2]


def test_video_source_passes_vn_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Video sources must drop the video stream before encoding."""
    _patch_binaries(monkeypatch)
    source = tmp_path / "v.mp4"
    source.write_bytes(b"x")
    fake, _ = _fake_run(duration_seconds=60.0)

    with patch("platform_service.workers.extractors.media_splitter.subprocess.run", fake):
        split_into_chunks(source, source_type="video")

    # First call is ffprobe; subsequent calls are ffmpeg.
    ffmpeg_calls = [call for call in fake.call_args_list if call.args[0][0].endswith("ffmpeg")]
    assert ffmpeg_calls, "ffmpeg was not invoked"
    for call in ffmpeg_calls:
        assert "-vn" in call.args[0]


def test_audio_source_does_not_pass_vn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_binaries(monkeypatch)
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    fake, _ = _fake_run(duration_seconds=60.0)

    with patch("platform_service.workers.extractors.media_splitter.subprocess.run", fake):
        split_into_chunks(source, source_type="audio")

    ffmpeg_calls = [call for call in fake.call_args_list if call.args[0][0].endswith("ffmpeg")]
    for call in ffmpeg_calls:
        assert "-vn" not in call.args[0]


def test_missing_binary_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "platform_service.workers.extractors.media_splitter.shutil.which",
        lambda name: None,
    )
    source = tmp_path / "x.mp3"
    source.write_bytes(b"x")
    with pytest.raises(MediaSplitterError, match="not found on PATH"):
        split_into_chunks(source, source_type="audio")


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(MediaSplitterError, match="media file not found"):
        split_into_chunks(tmp_path / "nope.mp3", source_type="audio")


def test_zero_duration_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_binaries(monkeypatch)
    source = tmp_path / "x.mp3"
    source.write_bytes(b"x")
    fake, _ = _fake_run(duration_seconds=0.0)

    with patch("platform_service.workers.extractors.media_splitter.subprocess.run", fake):
        with pytest.raises(MediaSplitterError, match="non-positive duration"):
            split_into_chunks(source, source_type="audio")


def test_unsupported_source_type_raises(tmp_path: Path) -> None:
    source = tmp_path / "x.pdf"
    source.write_bytes(b"x")
    with pytest.raises(ValueError, match="unsupported source_type"):
        split_into_chunks(source, source_type="pdf")


def test_chunk_duration_must_exceed_overlap() -> None:
    with pytest.raises(ValueError, match="must be greater than overlap"):
        split_into_chunks("/tmp/x", source_type="audio", chunk_duration_ms=60_000, overlap_ms=60_000)
