# simple-eval

[Deutsch](README.md) | English

A reproducible eval harness for LLM systems, built on
[Pydantic Evals](https://ai.pydantic.dev/evals/).

An eval is defined once in YAML — how the system under evaluation is started, where
its inputs come from, and how its outputs are graded — and runs in three independent
phases:

| Phase | Command | Reads | Writes | Touches the system? |
| --- | --- | --- | --- | --- |
| **1. Run** | `simple-eval run spec.yaml` | spec + dataset | `outputs.jsonl`, `manifest.json` | yes |
| **2. Evaluate** | `simple-eval evaluate <run>` | run artifact | `evaluation.json`, `results.jsonl` | no |
| **3. Analyze** | `simple-eval analyze <run>` | run artifact | stdout | no |

Splitting them matters because the expensive, non-deterministic phase is the one you
want to run *least* often. Once outputs are recorded, changing a rubric and re-grading
costs nothing and cannot perturb the results.

Pydantic Evals still owns grading, scoring, reports, and telemetry. This project owns
the layer it deliberately leaves out: standing the system up, recording what happened,
and proving the setup can be recreated.

## Install

Prerequisites:

- Python 3.13 or newer and [uv](https://docs.astral.sh/uv/)
- git, to record the commit a run was made from; runs outside a repository work but
  are marked not reproducible
- Docker with Compose v2, only for `compose` targets
- a provider API key, only for targets and graders that call an LLM

From a clone of this repository:

```bash
uv sync
uv run simple-eval --help
```

`run` and `evaluate` load a `.env` file from the current directory or a parent, so
provider keys can live there. `.env` is git-ignored.

## Quickstart

`eval.echo.yaml` evaluates a canned-answer agent (`src/examples/echo_agent.py`) on
`data.csv`. It needs no API key and no Docker:

```bash
uv run simple-eval run eval.echo.yaml --run-id demo
uv run simple-eval evaluate runs/demo
uv run simple-eval analyze runs/demo --by category
```

The agent knows three of the fifteen answers, so most cases fail, and the per-category
table shows which. `runs/demo/` holds everything that was recorded.

## The spec

```yaml
# yaml-language-server: $schema=./eval.schema.json
version: 1
name: support-agent

targets:
  - name: support-agent       # every target runs the same cases
    kind: compose
    file: docker-compose.eval.yaml
    endpoint:
      service: agent          # compose service name
      port: 8080              # container-internal port
      path: /chat
      body:
        prompt: "{{ input }}"
        category: "{{ metadata.category }}"
      output_pointer: /answer  # RFC 6901 JSON Pointer into the response
    health: { path: /health, timeout_s: 60 }

dataset:
  kind: csv
  path: data.csv
  task_id_col: id
  input_col: question
  expected_col: expected_answer

graders:
  - uses: contains_expected
  - uses: rubric_judge
    when: { category: reasoning }     # only cases whose metadata matches
    args:
      model: openai:gpt-5-mini
      rubric: "Logically correct and at most five words."

run:
  trials: 3
  max_concurrency: 8

env_passthrough: [OPENAI_API_KEY]
```

Top-level fields:

| Field | Default | |
| --- | --- | --- |
| `version` | `1` | spec format version |
| `name` | required | shown in reports |
| `targets` | required | systems under evaluation, see [Targets](#targets) |
| `dataset` | required | see [Datasets](#datasets) |
| `environment` | none | a fresh environment per execution, see [Environments](#environments) |
| `graders` | `[{uses: exact_match}]` | see [Grading](#grading) |
| `run` | `{trials: 1, max_concurrency: 4}` | |
| `artifacts_dir` | `runs` | where run directories are created |
| `env_passthrough` | `[]` | environment variables whose presence is recorded, see [Reproducibility](#reproducibility) |
| `pythonpath` | `[]` | directories put on `sys.path` |
| `diff_paths` | `[.]` | code whose uncommitted changes the manifest records |

Relative paths resolve against the spec's directory. Unknown fields are rejected.

Generate the schema for editor completion and validation:

```bash
uv run simple-eval schema
```

## Dependencies are Docker Compose's problem

There is no bespoke service or probe configuration. Compose already does ordering,
health checks, env injection, networking, and teardown, so the harness runs
`docker compose up --wait` and delegates:

```yaml
services:
  agent:
    build: ./src/examples/service
    ports: ["8080"]                    # no host port — see below
    depends_on:
      vectordb: { condition: service_healthy }
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]
      interval: 2s
      retries: 15
```

Publish the port **without** a fixed host port. Each target of each run gets its own
compose project (`simple-eval-<run_id>-<target>`), and the harness discovers the assigned host port via
`docker compose port`. That is what makes concurrent runs and shared CI runners safe.

The `health` block in the spec is a second, harness-side check. It exists because
compose `healthcheck` runs *inside* the container and distroless or scratch images
cannot define one — and because "compose says healthy" is not "my endpoint answers".

Services are always torn down, including when a run fails, and compose logs land in
`<run>/logs/<target>/compose.log`.

## Targets

`targets` lists the systems under evaluation, each with a unique `name`. Every target
runs on the same cases, so their results are comparable. Two kinds:

```yaml
targets:
  - { name: service, kind: compose, file: docker-compose.eval.yaml, endpoint: {...} }
  - { name: agent, kind: python, entrypoint: my_pkg.agent:run, args: { model: small } }
```

`python` runs in-process. It exists so iterating on a Python agent does not cost an image
rebuild. Use it for the inner loop and for fast CI; use `compose` for the reproducible run
of record and for systems not written in Python. The entrypoint is either

- a **function**, called per case as `fn(case, env, **args)`, sync or async, or
- a **class**, built once as `cls(**args)` before the first case, so a broken config fails
  before anything runs. The instance is called per case as `instance(case, env)`. An
  optional `provenance()` returns what the manifest should record about it (a resolved
  config, prompt texts); an optional `close()`, sync or async, runs after the last case.

`case` is a `CaseInput`: `case_id`, `trial`, `prompt`, the case's `metadata` and the
`run_id`. `env` is the case's environment, or `None` without one. `pythonpath` in the spec
lists directories, relative to the spec, to put on `sys.path` so project entrypoints and
graders import.

### Outputs

A target returns any JSON value, which graders see as `ctx.output`, or a
`TargetResult(output, attributes)`. `attributes` are recorded in `outputs.jsonl` next to
the output (traces, token use, intermediate state) but never graded. A compose target's
output is the JSON value at `output_pointer`. The built-in string graders compare a
structured output as its JSON text.

## Environments

A system that keeps state, such as a database it writes to, cannot run two cases against
one copy of it. An `environment` gives every execution its own:

```yaml
environment:
  entrypoint: my_pkg.env:Sandbox   # async context manager, per (target, case, trial)
  args: { template_db: data/template.db }
  setup_timeout_s: 120
  keep_workdir: failed             # failed | always | never
```

Like a target, the entrypoint is a function called as `fn(case, workdir, **args)` or a
class built once as `cls(**args)` and called as `instance(case, workdir)`, with the same
optional `provenance()` and `close()`. The call returns an async context manager.
simple-eval enters it before the target runs, hands its value to the target as `env`,
and leaves it afterwards, also when the target fails:

```python
@asynccontextmanager
async def sandbox(case, workdir, template_db):
    db = workdir / "state.db"
    shutil.copy(template_db, db)
    server = await start_server(db)
    try:
        yield {"url": server.url}
    finally:
        await server.stop()
```

`workdir` is `<run>/envs/<target>/<case>-<trial>/`, empty and the environment's own. It is
removed after a successful case and kept after a failed one. `run.max_concurrency` is
how many environments are alive at once, so stateful cases run in parallel without
sharing anything. A failed setup is recorded as the case's error and the target is not
called.

## Reproducibility

Every run records provenance in `manifest.json` before anything starts: git commit,
dirty flag, and the diff plus untracked files below the spec's `diff_paths` (default: the
spec's directory), `uv.lock` hash, spec hash, dataset content hash, resolved compose config
hash, and **image digests rather than tags**. A run is reported as portable only when
its inputs are pinned tightly enough to recreate elsewhere:

```
support-agent → runs/20260901T161205-a3f9c1
  15 cases x 3 trials, 0 execution errors
  not reproducible elsewhere: uncommitted changes in working tree
```

Secrets are never stored. Only the *names* of `env_passthrough` variables are recorded,
plus a hash of their values so drift between runs is detectable. The manifest does
contain the git remote URL, absolute paths, the platform string, and the uncommitted diff
and untracked files below `diff_paths`, so review a run directory before sharing it.

Two things this does not claim. Identical *results* are not reproducible against a live
LLM — providers rotate model snapshots, and temperature 0 still drifts. And a locally
built image has no registry digest, so runs using one are marked portable-locally-only.

## Grading

Graders are declarative and resolved from a registry, so no code is needed for the
common cases. `when` scopes a grader to cases whose metadata matches.

| `uses` | |
| --- | --- |
| `exact_match` | case-insensitive, whitespace-stripped by default |
| `contains_expected` | expected output appears in the actual output |
| `regex` | pattern match against the output |
| `rubric_judge` | LLM judge given the full case context; emits a score and a pass |
| `equals`, `equals_expected`, `contains`, `is_instance`, `llm_judge` | native Pydantic Evals evaluators |
| `my_pkg.graders:MyGrader` | any `Evaluator` subclass, by dotted path |

Custom graders receive the complete `EvaluatorContext`. Every dataset column is kept in
`ctx.metadata`, so per-case rubrics, categories, and references need no second lookup.
A grader that judges several items of one output names its results
`<metric>.<item>`, e.g. `value.price` and `value.currency`: analysis pools a metric over
its items, and `results.jsonl` splits the name into `metric` and `item`.

```python
from dataclasses import dataclass

from pydantic_evals.evaluators import EvaluationReason, Evaluator

from simple_eval import EvalContext


@dataclass(repr=False)
class NumericGrader(Evaluator):
    tolerance: float = 0.01
    evaluation_name: str = "numeric"

    def evaluate(self, ctx: EvalContext) -> EvaluationReason:
        try:
            close = abs(float(ctx.output) - float(ctx.expected_output)) <= self.tolerance
        except (TypeError, ValueError):
            return EvaluationReason(value=False, reason="Output is not numeric.")
        return EvaluationReason(value=close, reason=f"Within {self.tolerance}.")
```

```yaml
graders:
  - uses: my_pkg.graders:NumericGrader
    args: { tolerance: 0.5 }
    when: { category: math }
```

Grader names and arguments are validated in phase 1, before the run starts, so a typo
never costs you an expensive execution.

In the evaluation phase, `ctx.inputs` is a `CaseRef` (`target`, `case_id`, `trial`,
`prompt`) rather than a bare string; this is how a recorded execution is identified for
replay.
It renders as the prompt in reports, and `ctx.attributes["runner_duration_s"]` carries
the real latency measured during phase 1.

## Analysis

```bash
uv run simple-eval analyze runs/<run_id> --by category --baseline runs/<other_run_id>
```

Reports per target: pass rates per metric (`--items` lists every assertion), per-group
rates, mean latency, cases that are unstable across trials, and a signed diff against a
baseline run for the targets both runs share.

`run` takes `--target NAME` and `--case ID` (both repeatable), `--limit N`,
`--concurrency N` and `--trials N`, and prints a line per finished execution.

## Re-grading without re-running

```bash
uv run simple-eval run eval.compose.yaml            # expensive, once
uv run simple-eval evaluate runs/<run_id>           # graders from the spec
uv run simple-eval evaluate runs/<run_id> --spec stricter.yaml   # free, no re-execution
uv run simple-eval analyze runs/<run_id>
```

## Run artifact

```
runs/<run_id>/
  manifest.json      # spec as run + full provenance, written before the first case
  outputs.jsonl      # one recorded execution per line (phase 1)
  evaluation.json    # native Pydantic Evals EvaluationReport (phase 2)
  results.jsonl      # the same results, one row per assertion, score or label (phase 2)
  envs/<target>/     # environment workdirs kept from failed cases
  logs/<target>/     # compose output
```

`results.jsonl` rows carry `target`, `case_id`, `trial`, `kind` (`assertion`, `score`,
`label`, `grader_error`, `execution_error`), `grader`, `name`, `metric`, `item`, `value`
and `reason`, so any aggregate is a group-by.

`outputs.jsonl` is the contract between the phases. Everything downstream is derived
from it, and nothing downstream can change it.

## Datasets

| `kind` | Fields (defaults) |
| --- | --- |
| `csv` | `path`, `task_id_col` (`task_id`), `input_col` (`input`), `expected_col` (`expected_output`) |
| `jsonl` | `path`, `task_id_key` (`task_id`), `input_key` (`input`), `expected_key` (`expected_output`) |
| `pydantic_evals` | `path` to a native Pydantic Evals `yaml`/`json` dataset, loaded with `Dataset.from_file()` |

All columns or keys are preserved as case metadata. A row without an id gets
`case_<row number>`. Duplicate case ids are rejected at load time.

## Development

```bash
uv run python -m unittest discover -s tests -v
uv run --with ruff ruff check src tests
```

The tests need neither Docker nor an API key.

## Examples

| Spec | Target | Needs |
| --- | --- | --- |
| `eval.echo.yaml` | `src/examples/echo_agent.py`, canned answers | nothing |
| `eval.python.yaml` | `src/examples/general_agent.py`, in-process | `OPENAI_API_KEY` |
| `eval.compose.yaml` | `src/examples/service/`, built and started by `docker-compose.eval.yaml` | Docker, `OPENAI_API_KEY` |

The example agents use the [Pydantic AI model](https://ai.pydantic.dev/models/) named in
`SIMPLE_EVAL_EXAMPLE_MODEL` (default `openai:gpt-5-mini`). The `rubric_judge` grader in
`eval.python.yaml` calls `openai:gpt-5-mini` regardless.

## License

Licensed under the [Apache License 2.0](LICENSE). Copyright 2026 awa-projekt.
