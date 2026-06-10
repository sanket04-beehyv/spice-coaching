"""Streaming file digests for upload provenance."""

import hashlib
from pathlib import Path

_CHUNK_BYTES = 1024 * 1024


def sha256_hex_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 digest of the file at ``path``."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
