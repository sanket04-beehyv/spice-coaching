"""Server-side AV splitter — produces ~2-minute audio chunks with deterministic timecodes.

The 2-minute window is short enough that downstream transcription does
not need to reason about timestamps inside the chunk; the canonical
``(start_ms, end_ms)`` for every transcript page is computed here from
the splitter parameters, never from the LLM.

Implementation notes:

- Uses ``ffmpeg`` / ``ffprobe`` via subprocess (the platform image
  installs ffmpeg in its apt layer). The Python wrapper keeps the call
  surface narrow — one ``split_into_chunks`` entry point — so tests
  mock the binary instead of running it.
- Always re-encodes to ``audio/mpeg`` (mp3) at 64 kbps mono. This makes
  per-chunk payloads small (under Gemini's inline transcription cap)
  and consistent regardless of source container/codec; the CPU cost
  is negligible at the platform's volume.
- Audio sources skip video stream selection entirely; video sources
  drop video via ``-vn`` before encoding audio.
"""

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class MediaSplitterError(RuntimeError):
    """Raised when ffmpeg/ffprobe fails or media duration cannot be determined."""


@dataclass(frozen=True)
class MediaChunk:
    """One window of an AV source with deterministic timecodes."""

    index: int  # 0-based chunk index
    start_ms: int
    end_ms: int
    payload_bytes: bytes
    mime_type: str  # always "audio/mpeg" for now


def _require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise MediaSplitterError(f"{name!r} not found on PATH; install ffmpeg in the platform image")
    return path


def _probe_duration_ms(source_path: Path) -> int:
    """Return the source's total duration in milliseconds via ffprobe."""
    ffprobe = _require_binary("ffprobe")
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(source_path),
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        raise MediaSplitterError(f"ffprobe failed for {source_path.name}: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaSplitterError(f"ffprobe timed out for {source_path.name}") from exc

    try:
        payload = json.loads(completed.stdout)
        seconds = float(payload["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise MediaSplitterError(
            f"ffprobe returned unparseable duration for {source_path.name}: {completed.stdout!r}"
        ) from exc

    return int(seconds * 1000)


def _encode_chunk(
    source_path: Path,
    *,
    start_ms: int,
    duration_ms: int,
    is_video: bool,
    dest: Path,
) -> None:
    """Encode one chunk to mp3 64 kbps mono. Raises ``MediaSplitterError`` on failure."""
    ffmpeg = _require_binary("ffmpeg")
    cmd: list[str] = [
        ffmpeg,
        "-loglevel",
        "error",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-i",
        str(source_path),
        "-t",
        f"{duration_ms / 1000:.3f}",
    ]
    if is_video:
        cmd.append("-vn")
    cmd.extend(
        [
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            "-codec:a",
            "libmp3lame",
            "-y",
            str(dest),
        ]
    )
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120)
    except subprocess.CalledProcessError as exc:
        raise MediaSplitterError(
            f"ffmpeg chunk encode failed (start_ms={start_ms}): {exc.stderr.strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaSplitterError(f"ffmpeg chunk encode timed out (start_ms={start_ms})") from exc


def split_into_chunks(
    source_path: str | Path,
    *,
    source_type: str,
    chunk_duration_ms: int = 120_000,
    overlap_ms: int = 15_000,
) -> list[MediaChunk]:
    """Split an audio/video file into mp3 chunks with deterministic timecodes.

    Sources shorter than ``chunk_duration_ms`` produce a single chunk that
    spans the whole source. The step between chunk starts is
    ``chunk_duration_ms - overlap_ms`` so a sentence straddling a boundary
    appears in both neighbours (downstream can tolerate the slight
    duplication; at this scale it is acceptable).
    """
    if chunk_duration_ms <= overlap_ms:
        raise ValueError("chunk_duration_ms must be greater than overlap_ms")
    if source_type not in {"audio", "video"}:
        raise ValueError(f"unsupported source_type for splitter: {source_type!r}")

    path = Path(source_path)
    if not path.is_file():
        raise MediaSplitterError(f"media file not found: {path}")

    total_duration_ms = _probe_duration_ms(path)
    if total_duration_ms <= 0:
        raise MediaSplitterError(f"media has non-positive duration: {path.name}")

    step_ms = chunk_duration_ms - overlap_ms
    chunks: list[MediaChunk] = []
    with tempfile.TemporaryDirectory(prefix="media_split_") as tmpdir:
        tmp_root = Path(tmpdir)
        index = 0
        start_ms = 0
        while start_ms < total_duration_ms:
            window_ms = min(chunk_duration_ms, total_duration_ms - start_ms)
            chunk_path = tmp_root / f"chunk_{index:04d}.mp3"
            _encode_chunk(
                path,
                start_ms=start_ms,
                duration_ms=window_ms,
                is_video=(source_type == "video"),
                dest=chunk_path,
            )
            payload = chunk_path.read_bytes()
            chunks.append(
                MediaChunk(
                    index=index,
                    start_ms=start_ms,
                    end_ms=start_ms + window_ms,
                    payload_bytes=payload,
                    mime_type="audio/mpeg",
                )
            )
            index += 1
            # If the window already reached the end, stop — the overlap
            # step would create a duplicate chunk past the source duration.
            if start_ms + window_ms >= total_duration_ms:
                break
            start_ms += step_ms

    logger.info(
        "Media split %s: duration=%dms chunks=%d step=%dms overlap=%dms",
        path.name,
        total_duration_ms,
        len(chunks),
        step_ms,
        overlap_ms,
    )
    return chunks
