"""Blob storage under DATA_DIR. Names on disk are opaque ids, never user-supplied paths."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

from .config import Settings

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_FILES_PER_RUN = 10
MAX_TOTAL_INPUT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_INPUT_BYTES = 10 * 1024 * 1024
_ID = re.compile(r"^[a-f0-9]{32}$")


def _directory(settings: Settings, *parts: str) -> Path:
    path = settings.data_dir.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID.match(value))


def input_path(settings: Settings, file_id: str) -> Path:
    if not is_id(file_id):
        raise ValueError("invalid file id")
    return _directory(settings, "inputs") / f"{file_id}.blob"


def artifact_path(settings: Settings, run_id: str, artifact_id: str) -> Path:
    if not is_id(artifact_id) or not re.fullmatch(r"[a-f0-9-]{36}", run_id):
        raise ValueError("invalid artifact id")
    return _directory(settings, "artifacts", run_id) / f"{artifact_id}.blob"


def write_exclusive(path: Path, content: bytes) -> None:
    """Create a new file; never overwrite."""
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def new_id() -> str:
    return uuid.uuid4().hex


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def read_checked(path: Path, expected_size: int, expected_sha256: str, limit: int) -> bytes:
    """Read a stored blob, refusing links, size changes and content changes."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("blob unavailable")
    stat = path.stat()
    if stat.st_nlink != 1 or stat.st_size != expected_size or stat.st_size > limit:
        raise ValueError("blob changed")
    content = path.read_bytes()
    if sha256(content) != expected_sha256:
        raise ValueError("blob integrity mismatch")
    return content
