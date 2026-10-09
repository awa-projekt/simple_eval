from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean

from simple_eval.artifacts import RunArtifact, RunManifest, TypedReport


@dataclass
class Aggregate:
    label: str
    executions: int
    failures: int
    # Per assertion name, e.g. `value.article_type`.
    assertion_rates: dict[str, float] = field(default_factory=dict)
    # Per metric, pooled over its items: `value` over every `value.<item>`.
    metric_rates: dict[str, float] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    mean_duration_s: float = 0.0

    @property
    def pass_rate(self) -> float | None:
        if not self.metric_rates:
            return None
        return mean(self.metric_rates.values())


@dataclass
class CaseStability:
    target: str
    case_id: str
    trials: int
    passes: int

    @property
    def pass_rate(self) -> float:
        return self.passes / self.trials if self.trials else 0.0

    @property
    def flaky(self) -> bool:
        return 0 < self.passes < self.trials


@dataclass
class Summary:
    run_id: str
    spec_name: str
    portable: bool
    unportable_reasons: list[str]
    # One aggregate per target, in the order the spec lists them.
    targets: dict[str, Aggregate]
    # target -> metadata label -> aggregate
    groups: dict[str, dict[str, Aggregate]]
    group_key: str | None
    stability: list[CaseStability]

    @property
    def flaky_cases(self) -> list[CaseStability]:
        return [case for case in self.stability if case.flaky]


def summarize(artifact: RunArtifact, *, group_by: str | None = None) -> Summary:
    """Phase 3: aggregate a graded run, per target. Reads artifacts only."""
    manifest = artifact.read_manifest()
    report = artifact.read_evaluation()
    cases = _by_target(report.cases, manifest.targets)
    failures = _by_target(report.failures, manifest.targets)
    return Summary(
        run_id=manifest.run_id,
        spec_name=manifest.spec.name,
        portable=manifest.provenance.portable,
        unportable_reasons=manifest.provenance.unportable_reasons(),
        targets={
            target: _aggregate(target, cases[target], len(failures[target]))
            for target in manifest.targets
        },
        groups={
            target: _grouped(cases[target], failures[target], group_by)
            for target in manifest.targets
        }
        if group_by
        else {},
        group_key=group_by,
        stability=_stability(report, manifest),
    )


def _by_target(items: list, targets: list[str]) -> dict[str, list]:
    out: dict[str, list] = {target: [] for target in targets}
    for item in items:
        out.setdefault(item.inputs.target, []).append(item)
    return out


def _grouped(cases: list, failures: list, key: str) -> dict[str, Aggregate]:
    buckets: dict[str, list] = defaultdict(list)
    failure_counts: dict[str, int] = defaultdict(int)
    for case in cases:
        buckets[_label(case.metadata, key)].append(case)
    for failure in failures:
        failure_counts[_label(failure.metadata, key)] += 1
    return {
        label: _aggregate(label, grouped, failure_counts[label])
        for label, grouped in sorted(buckets.items())
    }


def _label(metadata: object, key: str) -> str:
    if isinstance(metadata, dict):
        return str(metadata.get(key, "—"))
    return "—"


def _aggregate(label: str, cases: list, failures: int) -> Aggregate:
    by_name: dict[str, list[bool]] = defaultdict(list)
    by_metric: dict[str, list[bool]] = defaultdict(list)
    score_values: dict[str, list[float]] = defaultdict(list)
    durations: list[float] = []
    for case in cases:
        for name, result in case.assertions.items():
            by_name[name].append(bool(result.value))
            by_metric[name.partition(".")[0]].append(bool(result.value))
        for name, result in case.scores.items():
            score_values[name].append(float(result.value))
        durations.append(
            float(case.attributes.get("runner_duration_s", case.task_duration))
        )
    return Aggregate(
        label=label,
        executions=len(cases),
        failures=failures,
        assertion_rates=_rates(by_name),
        metric_rates=_rates(by_metric),
        scores={name: mean(values) for name, values in sorted(score_values.items())},
        mean_duration_s=mean(durations) if durations else 0.0,
    )


def _rates(values: dict[str, list[bool]]) -> dict[str, float]:
    return {name: sum(vs) / len(vs) for name, vs in sorted(values.items())}


def _stability(report: TypedReport, manifest: RunManifest) -> list[CaseStability]:
    if manifest.trials < 2:
        return []
    trials: dict[tuple[str, str], int] = defaultdict(int)
    passes: dict[tuple[str, str], int] = defaultdict(int)
    for case in report.cases:
        key = (case.inputs.target, case.inputs.case_id)
        trials[key] += 1
        if case.assertions and all(a.value for a in case.assertions.values()):
            passes[key] += 1
    for failure in report.failures:
        trials[(failure.inputs.target, failure.inputs.case_id)] += 1
    return sorted(
        (
            CaseStability(target=target, case_id=case_id, trials=count,
                          passes=passes[(target, case_id)])
            for (target, case_id), count in trials.items()
        ),
        key=lambda case: (case.target, case.pass_rate, case.case_id),
    )


@dataclass
class Delta:
    target: str
    name: str
    baseline: float | None
    candidate: float | None

    @property
    def change(self) -> float | None:
        if self.baseline is None or self.candidate is None:
            return None
        return self.candidate - self.baseline


def compare(baseline: Summary, candidate: Summary) -> list[Delta]:
    """Every metric of every target the two runs share."""
    deltas: list[Delta] = []
    for target, now in candidate.targets.items():
        before = baseline.targets.get(target)
        if before is None:
            continue
        deltas.append(Delta(target, "pass_rate", before.pass_rate, now.pass_rate))
        for name in sorted(set(before.metric_rates) | set(now.metric_rates)):
            deltas.append(
                Delta(target, name, before.metric_rates.get(name), now.metric_rates.get(name))
            )
    return deltas
