"""Checksum-verified joblib artifacts loaded only after byte integrity checks."""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib


class ArtifactIntegrityError(ValueError):
    """Artifact bytes or trust metadata are missing or invalid."""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    path: Path
    checksum: str


def checksum_path(path: Path | str) -> Path:
    target = Path(path)
    return target.with_suffix(f"{target.suffix}.sha256")


def sha256_file(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump_verified(payload: Any, path: Path | str) -> ArtifactRef:
    """Atomically replace an artifact and publish its local checksum sidecar."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    joblib.dump(payload, temporary)
    os.replace(temporary, target)
    checksum = sha256_file(target)
    sidecar = checksum_path(target)
    sidecar_tmp = sidecar.with_suffix(f"{sidecar.suffix}.tmp")
    sidecar_tmp.write_text(f"{checksum}\n", encoding="ascii")
    os.replace(sidecar_tmp, sidecar)
    return ArtifactRef(target, checksum)


def load_verified(path: Path | str, expected_checksum: str | None = None) -> Any:
    """Verify the complete byte stream before invoking joblib deserialization."""

    target = Path(path)
    checksum = expected_checksum or _read_sidecar(target)
    normalized = checksum.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ArtifactIntegrityError("Artifact checksum must be a SHA-256 hex digest")
    payload = target.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(actual, normalized):
        raise ArtifactIntegrityError("Artifact checksum mismatch")
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Setting the shape on a NumPy array has been deprecated.*",
            category=DeprecationWarning,
            module=r"joblib\.numpy_pickle",
        )
        return joblib.load(io.BytesIO(payload))


def _read_sidecar(path: Path) -> str:
    sidecar = checksum_path(path)
    if not sidecar.exists():
        raise ArtifactIntegrityError("Artifact checksum is unavailable")
    return sidecar.read_text(encoding="ascii")
