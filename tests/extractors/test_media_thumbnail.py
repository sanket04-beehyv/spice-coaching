"""Unit tests for AV thumbnail ffmpeg helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from platform_service.workers.extractors.media_thumbnail import (
    MediaThumbnailError,
    render_audio_waveform_to_png,
    render_video_frame_to_png,
)


def _patch_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "platform_service.workers.extractors.media_thumbnail.shutil.which",
        lambda name: f"/fake/bin/{name}",
    )


def test_render_video_frame_writes_png(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_binaries(monkeypatch)
    dest = tmp_path / "frame.png"
    fake_png = b"\x89PNG\r\n\x1a\n"

    def runner(cmd: list[str], **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        Path(cmd[-1]).write_bytes(fake_png)
        return result

    monkeypatch.setattr(
        "platform_service.workers.extractors.media_thumbnail.subprocess.run",
        MagicMock(side_effect=runner),
    )
    render_video_frame_to_png(tmp_path / "video.mp4", dest_path=dest)
    assert dest.read_bytes() == fake_png


def test_render_audio_waveform_writes_png(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_binaries(monkeypatch)
    dest = tmp_path / "wave.png"
    fake_png = b"\x89PNG\r\n\x1a\n"

    def runner(cmd: list[str], **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        Path(cmd[-1]).write_bytes(fake_png)
        return result

    monkeypatch.setattr(
        "platform_service.workers.extractors.media_thumbnail.subprocess.run",
        MagicMock(side_effect=runner),
    )
    render_audio_waveform_to_png(tmp_path / "audio.mp3", dest_path=dest)
    assert dest.read_bytes() == fake_png


def test_render_video_frame_raises_on_ffmpeg_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_binaries(monkeypatch)
    import subprocess

    def runner(cmd: list[str], **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(
        "platform_service.workers.extractors.media_thumbnail.subprocess.run",
        runner,
    )
    with pytest.raises(MediaThumbnailError, match="ffmpeg video frame"):
        render_video_frame_to_png(tmp_path / "video.mp4", dest_path=tmp_path / "out.png")
