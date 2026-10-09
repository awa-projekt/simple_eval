from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic_core import to_json

from simple_eval.errors import SimpleEvalError
from simple_eval.models import CaseMetadata
from simple_eval.spec import CsvSource, DataSource, JsonlSource, NativeSource


@dataclass(frozen=True)
class LoadedCase:
    case_id: str
    prompt: str
    expected_output: str | None
    metadata: CaseMetadata


def load_cases(source: DataSource, base_dir: Path) -> list[LoadedCase]:
    path = source.path if source.path.is_absolute() else base_dir / source.path
    match source:
        case CsvSource():
            cases = _load_csv(path, source)
        case JsonlSource():
            cases = _load_jsonl(path, source)
        case NativeSource():
            cases = _load_native(path)
    _reject_duplicates(cases, path)
    return cases


def _reject_duplicates(cases: list[LoadedCase], path: Path) -> None:
    seen: set[str] = set()
    duplicates = sorted(
        {c.case_id for c in cases if c.case_id in seen or seen.add(c.case_id)}
    )
    if duplicates:
        raise SimpleEvalError(f"{path} has duplicate case ids: {', '.join(duplicates)}")


def _load_csv(path: Path, source: CsvSource) -> list[LoadedCase]:
    with path.open("r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if source.input_col not in (reader.fieldnames or []):
            raise SimpleEvalError(
                f"{path} is missing required column '{source.input_col}'."
            )
        return [
            LoadedCase(
                case_id=row.get(source.task_id_col) or f"case_{index}",
                prompt=_require(
                    row.get(source.input_col), path, index, source.input_col
                ),
                expected_output=row.get(source.expected_col),
                metadata=dict(row),
            )
            for index, row in enumerate(reader, start=1)
        ]


def _load_jsonl(path: Path, source: JsonlSource) -> list[LoadedCase]:
    cases: list[LoadedCase] = []
    with path.open("r", encoding="utf-8") as file:
        for index, line in enumerate(file, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            expected = row.get(source.expected_key)
            cases.append(
                LoadedCase(
                    case_id=str(row.get(source.task_id_key) or f"case_{index}"),
                    prompt=_require(
                        row.get(source.input_key), path, index, source.input_key
                    ),
                    expected_output=None if expected is None else str(expected),
                    metadata=row,
                )
            )
    return cases


def _load_native(path: Path) -> list[LoadedCase]:
    from pydantic_evals import Dataset

    dataset = Dataset[str, str, CaseMetadata].from_file(path)
    return [
        LoadedCase(
            case_id=case.name or f"case_{index}",
            prompt=case.inputs,
            expected_output=case.expected_output,
            metadata=case.metadata or {},
        )
        for index, case in enumerate(dataset.cases, start=1)
    ]


def _require(value: str | None, path: Path, index: int, column: str) -> str:
    if value is None:
        raise SimpleEvalError(f"{path} row {index} has no value for '{column}'.")
    return str(value)


def fingerprint(cases: list[LoadedCase]) -> str:
    payload = to_json(
        [
            {
                "case_id": case.case_id,
                "prompt": case.prompt,
                "expected_output": case.expected_output,
                "metadata": case.metadata,
            }
            for case in cases
        ]
    )
    return hashlib.sha256(payload).hexdigest()
