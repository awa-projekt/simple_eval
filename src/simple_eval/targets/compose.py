from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from jsonpointer import resolve_pointer

from simple_eval.errors import SimpleEvalError
from simple_eval.models import CaseInput, TargetResult
from simple_eval.provenance import ImageRecord, TargetProvenance
from simple_eval.spec import ComposeTarget
from simple_eval.targets.base import BoundTarget
from simple_eval.templating import render


class ComposeError(SimpleEvalError):
    pass


class ComposeAdapter:
    def __init__(
        self,
        target: ComposeTarget,
        *,
        base_dir: Path,
        project: str,
        logs_dir: Path,
    ) -> None:
        self._target = target
        self._project = project
        self._logs_dir = logs_dir
        self._file = (
            target.file if target.file.is_absolute() else base_dir / target.file
        )
        if not self._file.is_file():
            raise FileNotFoundError(f"Compose file not found: {self._file}")

    @asynccontextmanager
    async def bind(self) -> AsyncIterator[BoundTarget]:
        await self._up()
        try:
            base_url = await self._base_url()
            await self._await_health(base_url)
            images = await self._images()
            config_hash = await self._config_hash()
            endpoint = self._target.endpoint
            url = f"{base_url}{endpoint.path}"

            async with httpx.AsyncClient(
                timeout=self._target.request_timeout_s
            ) as client:

                async def call(case: CaseInput, env: Any) -> TargetResult:
                    context = {**case.template_context(), "env": env}
                    response = await client.request(
                        endpoint.method,
                        url,
                        headers=render(endpoint.headers, context),
                        json=render(endpoint.body, context) if endpoint.body else None,
                    )
                    if response.status_code != endpoint.expect_status:
                        raise ComposeError(
                            f"{endpoint.method} {url} returned "
                            f"{response.status_code}: {response.text[:500]}"
                        )
                    return TargetResult(output=_extract(response, endpoint.output_pointer))

                yield BoundTarget(
                    name=self._target.name,
                    call=call,
                    provenance=TargetProvenance(
                        kind="compose", images=images, compose_config_sha256=config_hash
                    ),
                )
        finally:
            await self._capture_logs()
            await self._down()

    async def _up(self) -> None:
        profiles = [
            arg for profile in self._target.profiles for arg in ("--profile", profile)
        ]
        await self._compose(
            *profiles,
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            str(int(self._target.up_timeout_s)),
            check=True,
        )

    async def _down(self) -> None:
        await self._compose("down", "--volumes", "--remove-orphans", check=False)

    async def _base_url(self) -> str:
        endpoint = self._target.endpoint
        published = await self._compose(
            "port", endpoint.service, str(endpoint.port), check=False
        )
        if not published:
            raise ComposeError(
                f"Service '{endpoint.service}' does not publish port {endpoint.port}. "
                f'Add `ports: ["{endpoint.port}"]` to it in {self._file.name}.'
            )
        host, _, port = published.rpartition(":")
        if host in ("0.0.0.0", "::", "[::]", ""):
            host = "127.0.0.1"
        return f"http://{host}:{port}"

    async def _await_health(self, base_url: str) -> None:
        health = self._target.health
        if health is None:
            return
        url = f"{base_url}{health.path}"
        deadline = time.monotonic() + health.timeout_s
        last: str = "no attempt made"
        async with httpx.AsyncClient(timeout=health.interval_s * 4) as client:
            while time.monotonic() < deadline:
                try:
                    response = await client.get(url)
                    if response.status_code == health.expect_status:
                        return
                    last = f"status {response.status_code}"
                except httpx.HTTPError as error:
                    last = str(error)
                await asyncio.sleep(health.interval_s)
        raise ComposeError(
            f"{url} not healthy after {health.timeout_s}s (last: {last})."
        )

    async def _images(self) -> list[ImageRecord]:
        entries = _parse_json_output(
            await self._compose("ps", "--format", "json", check=False)
        )
        records: list[ImageRecord] = []
        for entry in entries:
            reference = entry.get("Image", "")
            image_id, repo_digest = await _inspect_image(reference)
            records.append(
                ImageRecord(
                    service=entry.get("Service") or entry.get("Name") or "?",
                    reference=reference,
                    image_id=image_id,
                    repo_digest=repo_digest,
                )
            )
        return records

    async def _config_hash(self) -> str | None:
        resolved = await self._compose("config", "--format", "json", check=False)
        if not resolved:
            return None
        return hashlib.sha256(resolved.encode()).hexdigest()

    async def _capture_logs(self) -> None:
        logs = await self._compose("logs", "--no-color", "--timestamps", check=False)
        if logs:
            self._logs_dir.mkdir(parents=True, exist_ok=True)
            (self._logs_dir / "compose.log").write_text(logs, encoding="utf-8")

    async def _compose(self, *args: str, check: bool) -> str:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "compose",
            "--file",
            str(self._file),
            "--project-name",
            self._project,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._file.parent,
        )
        stdout, stderr = await process.communicate()
        if check and process.returncode != 0:
            raise ComposeError(
                f"`docker compose {' '.join(args)}` failed "
                f"({process.returncode}): {stderr.decode().strip()}"
            )
        return stdout.decode().strip()


def _parse_json_output(raw: str) -> list[dict]:
    """Compose emits either a JSON array or one object per line, by version."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return [json.loads(line) for line in raw.splitlines() if line.strip()]
    return parsed if isinstance(parsed, list) else [parsed]


async def _inspect_image(reference: str) -> tuple[str, str | None]:
    if not reference:
        return "", None
    process = await asyncio.create_subprocess_exec(
        "docker",
        "image",
        "inspect",
        reference,
        "--format",
        "{{.Id}}\t{{json .RepoDigests}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return "", None
    image_id, _, raw_digests = stdout.decode().strip().partition("\t")
    digests = json.loads(raw_digests or "null") or []
    return image_id, digests[0].partition("@")[2] if digests else None


def _extract(response: httpx.Response, pointer: str) -> Any:
    """The JSON value at `pointer`, or the raw body when there is no pointer."""
    if not pointer:
        return response.text
    return resolve_pointer(response.json(), pointer)
