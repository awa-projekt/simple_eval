from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.table import Table

from simple_eval import analysis, evaluation, runner
from simple_eval.analysis import Summary
from simple_eval.artifacts import RunArtifact
from simple_eval.models import CaseResult
from simple_eval.spec import load_spec, spec_json_schema

app = typer.Typer(
    help="Reproducible evals in three phases: run, evaluate, analyze.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def run(
    spec_path: Annotated[Path, typer.Argument(help="Path to an eval spec YAML file.")],
    run_id: Annotated[
        str | None, typer.Option(help="Override the generated run id.")
    ] = None,
    trials: Annotated[
        int | None, typer.Option(min=1, help="Override spec.run.trials.")
    ] = None,
    case: Annotated[
        list[str] | None, typer.Option(help="Run only this case id. Repeatable.")
    ] = None,
    limit: Annotated[
        int | None, typer.Option(min=1, help="Run at most this many cases.")
    ] = None,
    target: Annotated[
        list[str] | None, typer.Option(help="Run only this target. Repeatable.")
    ] = None,
    concurrency: Annotated[
        int | None, typer.Option(min=1, help="Override spec.run.max_concurrency.")
    ] = None,
) -> None:
    """Phase 1: stand up the system under evaluation and record raw outputs."""
    load_dotenv(find_dotenv(usecwd=True))
    spec, base_dir, spec_bytes = load_spec(spec_path)
    if trials is not None:
        spec.run.trials = trials
    if concurrency is not None:
        spec.run.max_concurrency = concurrency

    artifact = asyncio.run(
        runner.execute(
            spec,
            base_dir=base_dir,
            spec_bytes=spec_bytes,
            run_id=run_id,
            case_ids=case,
            limit=limit,
            target_names=target,
            on_result=_print_result,
        )
    )
    manifest = artifact.read_manifest()

    console.print(f"[bold]{manifest.spec.name}[/bold] → {artifact.dir}")
    console.print(
        f"  {', '.join(manifest.targets)}: {manifest.case_count} cases x "
        f"{manifest.trials} trials, {manifest.error_count} execution errors"
    )
    _print_portability(
        manifest.provenance.portable, manifest.provenance.unportable_reasons()
    )
    console.print(f"\nNext: [cyan]simple-eval evaluate {artifact.dir}[/cyan]")


@app.command()
def evaluate(
    run_dir: Annotated[Path, typer.Argument(help="A run directory produced by 'run'.")],
    spec_path: Annotated[
        Path | None,
        typer.Option(
            "--spec", help="Re-grade with the graders from this spec instead."
        ),
    ] = None,
    show_cases: Annotated[bool, typer.Option(help="Print the per-case table.")] = True,
) -> None:
    """Phase 2: grade recorded outputs. Does not re-run the system."""
    load_dotenv(find_dotenv(usecwd=True))
    artifact = RunArtifact.open(run_dir)
    graders = load_spec(spec_path)[0].graders if spec_path else None

    report = evaluation.evaluate(artifact, graders=graders)
    if show_cases:
        report.print(
            console=console,
            include_input=True,
            include_expected_output=True,
            include_output=True,
            include_reasons=True,
        )
    console.print(
        f"Graded {len(report.cases)} executions "
        f"({len(report.failures)} failures) → {artifact.evaluation_path}, "
        f"{artifact.results_path.name}"
    )
    console.print(f"\nNext: [cyan]simple-eval analyze {artifact.dir}[/cyan]")


@app.command()
def analyze(
    run_dir: Annotated[Path, typer.Argument(help="A graded run directory.")],
    by: Annotated[
        str | None, typer.Option(help="Metadata key to break results down by.")
    ] = None,
    baseline: Annotated[
        Path | None, typer.Option(help="Compare against this graded run directory.")
    ] = None,
    items: Annotated[
        bool, typer.Option(help="Also list every assertion, not just the metrics.")
    ] = False,
) -> None:
    """Phase 3: aggregate and compare graded runs."""
    summary = analysis.summarize(RunArtifact.open(run_dir), group_by=by)
    _print_summary(summary, items=items)

    if summary.groups:
        _print_groups(summary)
    if summary.flaky_cases:
        _print_flaky(summary)
    if baseline is not None:
        _print_comparison(analysis.summarize(RunArtifact.open(baseline)), summary)


@app.command()
def schema(
    output: Annotated[
        Path, typer.Option(help="Where to write the JSON Schema.")
    ] = Path("eval.schema.json"),
) -> None:
    """Write the eval spec JSON Schema for editor completion."""
    output.write_text(json.dumps(spec_json_schema(), indent=2) + "\n", encoding="utf-8")
    console.print(f"Wrote {output}")
    console.print(
        f"Add to the top of your spec: "
        f"[cyan]# yaml-language-server: $schema=./{output.name}[/cyan]"
    )


def _print_portability(portable: bool, reasons: list[str]) -> None:
    if portable:
        console.print("  [green]reproducible[/green]: inputs fully pinned")
        return
    console.print(
        f"  [yellow]not reproducible elsewhere[/yellow]: {'; '.join(reasons)}"
    )


def _print_result(result: CaseResult, finished: int, total: int) -> None:
    outcome = (
        f"[red]error[/red] {result.error[:160]}" if result.error else "[green]ok[/green]"
    )
    trial = f"#{result.trial}" if result.trial else ""
    console.print(
        f"  [{finished}/{total}] {result.target} {result.case_id}{trial} "
        f"{result.duration_s:.1f}s {outcome}"
    )


def _print_summary(summary: Summary, *, items: bool) -> None:
    console.print(f"[bold]{summary.spec_name}[/bold]  run {summary.run_id}")
    for target, overall in summary.targets.items():
        rate = overall.pass_rate
        console.print(
            f"  {target}: {overall.executions} executions, {overall.failures} failures, "
            f"pass rate {'n/a' if rate is None else f'{rate:.1%}'}, "
            f"{overall.mean_duration_s:.1f}s mean"
        )
    _print_portability(summary.portable, summary.unportable_reasons)

    targets = list(summary.targets.values())
    _print_rates("Metrics", {t.label: t.metric_rates for t in targets})
    if items:
        _print_rates("Assertions", {t.label: t.assertion_rates for t in targets})
    scores = {t.label: t.scores for t in targets}
    if any(scores.values()):
        _print_rates("Scores", scores, as_percent=False)


def _print_rates(
    title: str, by_target: dict[str, dict[str, float]], *, as_percent: bool = True
) -> None:
    names = sorted({name for rates in by_target.values() for name in rates})
    if not names:
        return
    table = Table(title=title, title_justify="left")
    table.add_column("")
    for target in by_target:
        table.add_column(target, justify="right")
    for name in names:
        cells = [by_target[target].get(name) for target in by_target]
        table.add_row(
            name,
            *(_percent(v) if as_percent else ("—" if v is None else f"{v:.3f}")
              for v in cells),
        )
    console.print(table)


def _print_groups(summary: Summary) -> None:
    for target, groups in summary.groups.items():
        names = sorted({name for group in groups.values() for name in group.metric_rates})
        table = Table(title=f"{target} by {summary.group_key}", title_justify="left")
        table.add_column(str(summary.group_key))
        table.add_column("n", justify="right")
        table.add_column("pass", justify="right")
        for name in names:
            table.add_column(name, justify="right")
        table.add_column("mean s", justify="right")

        for group in groups.values():
            table.add_row(
                group.label,
                str(group.executions),
                _percent(group.pass_rate),
                *(_percent(group.metric_rates.get(name)) for name in names),
                f"{group.mean_duration_s:.2f}",
            )
        console.print(table)


def _print_flaky(summary: Summary) -> None:
    table = Table(title="Unstable across trials", title_justify="left")
    table.add_column("target")
    table.add_column("case")
    table.add_column("passes", justify="right")
    for case in summary.flaky_cases:
        table.add_row(case.target, case.case_id, f"{case.passes}/{case.trials}")
    console.print(table)


def _print_comparison(baseline: Summary, candidate: Summary) -> None:
    table = Table(title=f"{baseline.run_id} → {candidate.run_id}", title_justify="left")
    table.add_column("target")
    table.add_column("metric")
    table.add_column("baseline", justify="right")
    table.add_column("candidate", justify="right")
    table.add_column("change", justify="right")
    for delta in analysis.compare(baseline, candidate):
        change = delta.change
        styled = "—" if change is None else f"[{_style(change)}]{change:+.1%}[/]"
        table.add_row(
            delta.target, delta.name, _percent(delta.baseline), _percent(delta.candidate),
            styled,
        )
    console.print(table)


def _style(change: float) -> str:
    if change > 0.0001:
        return "green"
    return "red" if change < -0.0001 else "dim"


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"
