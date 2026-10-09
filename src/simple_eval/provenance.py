from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from pydantic import BaseModel, computed_field

# Untracked files larger than this are listed, not stored.
MAX_UNTRACKED_BYTES = 1_000_000


class GitInfo(BaseModel):
    commit: str
    branch: str | None
    dirty: bool
    remote: str | None
    # What the run's code differs from `commit` by, below the spec's `diff_paths`.
    diff: str = ""
    untracked: dict[str, str] = {}


class ImageRecord(BaseModel):
    service: str
    reference: str
    image_id: str
    repo_digest: str | None


class TargetProvenance(BaseModel):
    """What one target was: its images for compose, whatever it reports for python."""

    kind: str = "python"
    images: list[ImageRecord] = []
    compose_config_sha256: str | None = None
    details: dict[str, Any] = {}


class Provenance(BaseModel):
    simple_eval_version: str
    python_version: str
    platform: str
    git: GitInfo | None
    spec_sha256: str
    dataset_sha256: str
    dataset_file_sha256: str | None
    lockfile_sha256: str | None
    env_var_names: list[str]
    env_missing: list[str]
    env_fingerprint: str | None
    targets: dict[str, TargetProvenance] = {}
    environment: dict[str, Any] | None = None

    @property
    def images(self) -> list[ImageRecord]:
        return [image for target in self.targets.values() for image in target.images]

    @computed_field
    @property
    def portable(self) -> bool:
        """Whether this run's inputs are pinned tightly enough to reproduce elsewhere."""
        if self.git is None or self.git.dirty:
            return False
        return all(image.repo_digest for image in self.images)

    def unportable_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.git is None:
            reasons.append("not a git repository")
        elif self.git.dirty:
            reasons.append("uncommitted changes in working tree")
        for image in self.images:
            if not image.repo_digest:
                reasons.append(f"image '{image.reference}' has no registry digest")
        return reasons


async def capture(
    *,
    repo_dir: Path,
    spec_bytes: bytes,
    dataset_sha256: str,
    dataset_path: Path | None,
    env_passthrough: list[str],
    diff_paths: list[Path],
) -> Provenance:
    present = [name for name in env_passthrough if os.environ.get(name)]
    missing = [name for name in env_passthrough if not os.environ.get(name)]
    return Provenance(
        simple_eval_version=_own_version(),
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        git=await _git_info(repo_dir, diff_paths),
        spec_sha256=hashlib.sha256(spec_bytes).hexdigest(),
        dataset_sha256=dataset_sha256,
        dataset_file_sha256=_file_sha256(dataset_path),
        lockfile_sha256=_file_sha256(repo_dir / "uv.lock"),
        env_var_names=present,
        env_missing=missing,
        env_fingerprint=_env_fingerprint(present),
    )


def _own_version() -> str:
    try:
        return version("simple-eval")
    except PackageNotFoundError:
        return "unknown"


def _file_sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _env_fingerprint(names: list[str]) -> str | None:
    if not names:
        return None
    payload = "".join(f"{name}={os.environ[name]}\n" for name in sorted(names))
    return hashlib.sha256(payload.encode()).hexdigest()


async def _git_info(repo_dir: Path, diff_paths: list[Path]) -> GitInfo | None:
    commit = await _git(repo_dir, "rev-parse", "HEAD")
    if commit is None:
        return None
    status = await _git(repo_dir, "status", "--porcelain")
    branch = await _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    paths = [str(path) for path in diff_paths]
    untracked = await _git(
        repo_dir, "ls-files", "--others", "--exclude-standard", "--", *paths
    )
    return GitInfo(
        commit=commit,
        branch=None if branch in (None, "HEAD") else branch,
        dirty=bool(status),
        remote=await _git(repo_dir, "config", "--get", "remote.origin.url"),
        diff=await _git(repo_dir, "diff", "HEAD", "--", *paths) or "",
        untracked={
            path: _untracked(repo_dir / path) for path in (untracked or "").splitlines()
        },
    )


def _untracked(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_UNTRACKED_BYTES:
        return f"<{size} bytes, not stored>"
    return path.read_text(encoding="utf-8", errors="replace")


async def _git(repo_dir: Path, *args: str) -> str | None:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo_dir),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return None
    return stdout.decode().strip()
