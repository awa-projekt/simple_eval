# simple-eval

Deutsch | [English](README.en.md)

Ein reproduzierbares Eval-Harness für LLM-Systeme, aufgebaut auf
[Pydantic Evals](https://ai.pydantic.dev/evals/).

Eine Eval wird einmal in YAML beschrieben – wie das zu evaluierende System gestartet
wird, woher seine Eingaben kommen und wie seine Ausgaben bewertet werden – und läuft in
drei unabhängigen Phasen:

| Phase | Befehl | Liest | Schreibt | Berührt das System? |
| --- | --- | --- | --- | --- |
| **1. Run** | `simple-eval run spec.yaml` | Spec + Datensatz | `outputs.jsonl`, `manifest.json` | ja |
| **2. Evaluate** | `simple-eval evaluate <run>` | Run-Artefakt | `evaluation.json`, `results.jsonl` | nein |
| **3. Analyze** | `simple-eval analyze <run>` | Run-Artefakt | stdout | nein |

Die Trennung lohnt sich, weil die teure, nicht deterministische Phase genau die ist, die
man *am seltensten* ausführen möchte. Sind die Ausgaben einmal aufgezeichnet, kostet es
nichts, eine Rubrik zu ändern und neu zu bewerten, und die Ergebnisse können dadurch
nicht verfälscht werden.

Bewertung, Scoring, Reports und Telemetrie bleiben bei Pydantic Evals. Dieses Projekt
übernimmt die Schicht, die Pydantic Evals bewusst auslässt: das System hochfahren,
festhalten, was passiert ist, und belegen, dass sich der Aufbau wiederherstellen lässt.

## Installation

Voraussetzungen:

- Python 3.13 oder neuer und [uv](https://docs.astral.sh/uv/)
- git, um den Commit eines Runs festzuhalten; Runs außerhalb eines Repositorys
  funktionieren, gelten aber als nicht reproduzierbar
- Docker mit Compose v2, nur für `compose`-Targets
- ein API-Key des Providers, nur für Targets und Grader, die ein LLM aufrufen

In einem Klon dieses Repositorys:

```bash
uv sync
uv run simple-eval --help
```

`run` und `evaluate` laden eine `.env`-Datei aus dem aktuellen Verzeichnis oder einem
übergeordneten, dort können also Provider-Keys liegen. `.env` wird von git ignoriert.

## Schnellstart

`eval.echo.yaml` evaluiert einen Agenten mit fest hinterlegten Antworten
(`src/examples/echo_agent.py`) auf `data.csv`. Dafür braucht es weder API-Key noch
Docker:

```bash
uv run simple-eval run eval.echo.yaml --run-id demo
uv run simple-eval evaluate runs/demo
uv run simple-eval analyze runs/demo --by category
```

Der Agent kennt drei der fünfzehn Antworten, die meisten Fälle schlagen also fehl, und
die Tabelle nach Kategorie zeigt, welche. In `runs/demo/` liegt alles, was aufgezeichnet
wurde.

## Die Spec

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

Felder auf oberster Ebene:

| Feld | Standard | |
| --- | --- | --- |
| `version` | `1` | Version des Spec-Formats |
| `name` | Pflicht | erscheint in Reports |
| `targets` | Pflicht | die zu evaluierenden Systeme, siehe [Targets](#targets) |
| `dataset` | Pflicht | siehe [Datensätze](#datensätze) |
| `environment` | keins | eine frische Umgebung pro Ausführung, siehe [Umgebungen](#umgebungen) |
| `graders` | `[{uses: exact_match}]` | siehe [Bewertung](#bewertung) |
| `run` | `{trials: 1, max_concurrency: 4}` | |
| `artifacts_dir` | `runs` | wo Run-Verzeichnisse angelegt werden |
| `env_passthrough` | `[]` | Umgebungsvariablen, deren Vorhandensein festgehalten wird, siehe [Reproduzierbarkeit](#reproduzierbarkeit) |
| `pythonpath` | `[]` | Verzeichnisse, die auf `sys.path` gelegt werden |
| `diff_paths` | `[.]` | Code, dessen nicht committete Änderungen das Manifest festhält |

Relative Pfade beziehen sich auf das Verzeichnis der Spec. Unbekannte Felder werden
abgelehnt.

Das Schema für Autovervollständigung und Validierung im Editor erzeugen:

```bash
uv run simple-eval schema
```

## Abhängigkeiten sind Sache von Docker Compose

Es gibt keine eigene Konfiguration für Services oder Health-Probes. Compose kümmert sich
bereits um Startreihenfolge, Health-Checks, Umgebungsvariablen, Netzwerk und Abbau, also
ruft das Harness `docker compose up --wait` auf und überlässt Compose den Rest:

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

Den Port **ohne** festen Host-Port veröffentlichen. Jedes Target jedes Runs bekommt ein
eigenes Compose-Projekt (`simple-eval-<run_id>-<target>`), und das Harness ermittelt den
zugewiesenen Host-Port über `docker compose port`. Erst das macht parallele Runs und
geteilte CI-Runner sicher.

Der `health`-Block in der Spec ist eine zweite Prüfung, auf Seite des Harness. Es gibt
ihn, weil ein Compose-`healthcheck` *im* Container läuft und Distroless- oder
Scratch-Images keinen definieren können – und weil „Compose meldet healthy“ nicht
dasselbe ist wie „mein Endpoint antwortet“.

Die Services werden immer abgebaut, auch wenn ein Run fehlschlägt, und die Compose-Logs
landen in `<run>/logs/<target>/compose.log`.

## Targets

`targets` listet die zu evaluierenden Systeme, jedes mit eindeutigem `name`. Alle
Targets laufen auf denselben Fällen, ihre Ergebnisse sind also vergleichbar. Es gibt
zwei Arten:

```yaml
targets:
  - { name: service, kind: compose, file: docker-compose.eval.yaml, endpoint: {...} }
  - { name: agent, kind: python, entrypoint: my_pkg.agent:run, args: { model: small } }
```

`python` läuft im selben Prozess. Damit kostet das Iterieren an einem Python-Agenten
keinen Image-Rebuild. Es eignet sich für die schnelle Entwicklungsschleife und für
schnelle CI; `compose` ist für den maßgeblichen, reproduzierbaren Run und für Systeme,
die nicht in Python geschrieben sind. Der Entrypoint ist entweder

- eine **Funktion**, pro Fall aufgerufen als `fn(case, env, **args)`, synchron oder
  asynchron, oder
- eine **Klasse**, einmal vor dem ersten Fall als `cls(**args)` erzeugt, sodass eine
  fehlerhafte Konfiguration scheitert, bevor irgendetwas läuft. Die Instanz wird pro Fall
  als `instance(case, env)` aufgerufen. Ein optionales `provenance()` liefert, was das
  Manifest über sie festhalten soll (eine aufgelöste Konfiguration, Prompt-Texte); ein
  optionales `close()`, synchron oder asynchron, läuft nach dem letzten Fall.

`case` ist ein `CaseInput`: `case_id`, `trial`, `prompt`, die `metadata` des Falls und
die `run_id`. `env` ist die Umgebung des Falls oder `None`, wenn es keine gibt.
`pythonpath` in der Spec listet Verzeichnisse relativ zur Spec, die auf `sys.path`
gelegt werden, damit Entrypoints und Grader des Projekts importierbar sind.

### Ausgaben

Ein Target gibt einen beliebigen JSON-Wert zurück, den Grader als `ctx.output` sehen,
oder ein `TargetResult(output, attributes)`. `attributes` werden in `outputs.jsonl`
neben der Ausgabe gespeichert (Traces, Token-Verbrauch, Zwischenstände), aber nie
bewertet. Die Ausgabe eines Compose-Targets ist der JSON-Wert an `output_pointer`. Die
eingebauten String-Grader vergleichen eine strukturierte Ausgabe als ihren JSON-Text.

## Umgebungen

Ein System mit Zustand, etwa einer Datenbank, in die es schreibt, kann nicht zwei Fälle
gegen dieselbe Kopie laufen lassen. Eine `environment` gibt jeder Ausführung ihre
eigene:

```yaml
environment:
  entrypoint: my_pkg.env:Sandbox   # async context manager, per (target, case, trial)
  args: { template_db: data/template.db }
  setup_timeout_s: 120
  keep_workdir: failed             # failed | always | never
```

Wie bei einem Target ist der Entrypoint eine Funktion, aufgerufen als
`fn(case, workdir, **args)`, oder eine Klasse, einmal als `cls(**args)` erzeugt und als
`instance(case, workdir)` aufgerufen, mit denselben optionalen `provenance()` und
`close()`. Der Aufruf liefert einen asynchronen Context Manager. simple-eval betritt ihn,
bevor das Target läuft, übergibt seinen Wert als `env` an das Target und verlässt ihn
danach wieder, auch wenn das Target fehlschlägt:

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

`workdir` ist `<run>/envs/<target>/<case>-<trial>/`, leer und gehört allein dieser
Umgebung. Nach einem erfolgreichen Fall wird es entfernt, nach einem fehlgeschlagenen
behalten. `run.max_concurrency` gibt an, wie viele Umgebungen gleichzeitig existieren,
sodass zustandsbehaftete Fälle parallel laufen, ohne etwas zu teilen. Ein
fehlgeschlagenes Setup wird als Fehler des Falls festgehalten, und das Target wird nicht
aufgerufen.

## Reproduzierbarkeit

Jeder Run hält vor dem Start seine Herkunft in `manifest.json` fest: Git-Commit,
Dirty-Flag sowie Diff und nicht versionierte Dateien unterhalb der `diff_paths` der Spec
(Standard: das Verzeichnis der Spec), Hash von `uv.lock`, Hash der Spec, Hash des
Datensatzinhalts, Hash der aufgelösten Compose-Konfiguration und **Image-Digests statt
Tags**. Als portabel gilt ein Run nur, wenn seine Eingaben genau genug festgelegt sind,
um ihn anderswo nachzustellen:

```
support-agent → runs/20260901T161205-a3f9c1
  15 cases x 3 trials, 0 execution errors
  not reproducible elsewhere: uncommitted changes in working tree
```

Secrets werden nie gespeichert. Festgehalten werden nur die *Namen* der
`env_passthrough`-Variablen und ein Hash ihrer Werte, damit sich Abweichungen zwischen
Runs erkennen lassen. Das Manifest enthält allerdings die URL des Git-Remotes, absolute
Pfade, die Plattformangabe sowie den nicht committeten Diff und nicht versionierte
Dateien unterhalb von `diff_paths`. Ein Run-Verzeichnis sollte deshalb vor dem Teilen
durchgesehen werden.

Zwei Dinge werden damit nicht behauptet. Identische *Ergebnisse* lassen sich gegen ein
Live-LLM nicht reproduzieren – Provider tauschen Modell-Snapshots aus, und auch
Temperatur 0 driftet. Und ein lokal gebautes Image hat keinen Registry-Digest, Runs damit
gelten daher nur lokal als portabel.

## Bewertung

Grader werden deklarativ angegeben und über eine Registry aufgelöst, für die üblichen
Fälle ist also kein Code nötig. `when` beschränkt einen Grader auf Fälle, deren Metadaten
passen.

| `uses` | |
| --- | --- |
| `exact_match` | standardmäßig ohne Groß-/Kleinschreibung und ohne umgebende Leerzeichen |
| `contains_expected` | die erwartete Ausgabe kommt in der tatsächlichen vor |
| `regex` | Mustervergleich mit der Ausgabe |
| `rubric_judge` | LLM als Richter mit dem vollständigen Kontext des Falls; liefert einen Score und ein Bestanden |
| `equals`, `equals_expected`, `contains`, `is_instance`, `llm_judge` | native Evaluatoren von Pydantic Evals |
| `my_pkg.graders:MyGrader` | jede Unterklasse von `Evaluator`, über ihren Pfad |

Eigene Grader bekommen den vollständigen `EvaluatorContext`. Jede Spalte des Datensatzes
bleibt in `ctx.metadata` erhalten, sodass Rubriken, Kategorien und Referenzen pro Fall
ohne zweiten Lookup verfügbar sind. Ein Grader, der mehrere Teile einer Ausgabe
beurteilt, benennt seine Ergebnisse `<metric>.<item>`, z. B. `value.price` und
`value.currency`: Die Analyse fasst eine Metrik über ihre Items zusammen, und
`results.jsonl` teilt den Namen in `metric` und `item` auf.

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

Namen und Argumente der Grader werden in Phase 1 geprüft, bevor der Run startet, sodass
ein Tippfehler nie eine teure Ausführung kostet.

In der Evaluate-Phase ist `ctx.inputs` ein `CaseRef` (`target`, `case_id`, `trial`,
`prompt`) statt eines einfachen Strings; darüber wird eine aufgezeichnete Ausführung für
das Replay identifiziert. In Reports erscheint es als der Prompt, und
`ctx.attributes["runner_duration_s"]` enthält die in Phase 1 tatsächlich gemessene
Latenz.

## Analyse

```bash
uv run simple-eval analyze runs/<run_id> --by category --baseline runs/<other_run_id>
```

Berichtet pro Target: Bestehensquoten pro Metrik (`--items` listet jede einzelne
Assertion), Quoten pro Gruppe, mittlere Latenz, Fälle, die über die Trials hinweg
instabil sind, und eine vorzeichenbehaftete Differenz zu einem Baseline-Run für die
Targets, die beide Runs gemeinsam haben.

`run` akzeptiert `--target NAME` und `--case ID` (beide wiederholbar), `--limit N`,
`--concurrency N` und `--trials N` und gibt für jede abgeschlossene Ausführung eine Zeile
aus.

## Neu bewerten ohne neu auszuführen

```bash
uv run simple-eval run eval.compose.yaml            # expensive, once
uv run simple-eval evaluate runs/<run_id>           # graders from the spec
uv run simple-eval evaluate runs/<run_id> --spec stricter.yaml   # free, no re-execution
uv run simple-eval analyze runs/<run_id>
```

## Run-Artefakt

```
runs/<run_id>/
  manifest.json      # spec as run + full provenance, written before the first case
  outputs.jsonl      # one recorded execution per line (phase 1)
  evaluation.json    # native Pydantic Evals EvaluationReport (phase 2)
  results.jsonl      # the same results, one row per assertion, score or label (phase 2)
  envs/<target>/     # environment workdirs kept from failed cases
  logs/<target>/     # compose output
```

Zeilen in `results.jsonl` enthalten `target`, `case_id`, `trial`, `kind` (`assertion`,
`score`, `label`, `grader_error`, `execution_error`), `grader`, `name`, `metric`,
`item`, `value` und `reason`, sodass jede Aggregation ein Group-by ist.

`outputs.jsonl` ist der Vertrag zwischen den Phasen. Alles Nachgelagerte wird daraus
abgeleitet, und nichts Nachgelagertes kann es verändern.

## Datensätze

| `kind` | Felder (Standardwerte) |
| --- | --- |
| `csv` | `path`, `task_id_col` (`task_id`), `input_col` (`input`), `expected_col` (`expected_output`) |
| `jsonl` | `path`, `task_id_key` (`task_id`), `input_key` (`input`), `expected_key` (`expected_output`) |
| `pydantic_evals` | `path` zu einem nativen Pydantic-Evals-Datensatz in `yaml`/`json`, geladen mit `Dataset.from_file()` |

Alle Spalten bzw. Schlüssel bleiben als Metadaten des Falls erhalten. Eine Zeile ohne ID
bekommt `case_<Zeilennummer>`. Doppelte Fall-IDs werden schon beim Laden abgelehnt.

## Entwicklung

```bash
uv run python -m unittest discover -s tests -v
uv run --with ruff ruff check src tests
```

Die Tests brauchen weder Docker noch einen API-Key.

## Beispiele

| Spec | Target | Benötigt |
| --- | --- | --- |
| `eval.echo.yaml` | `src/examples/echo_agent.py`, fest hinterlegte Antworten | nichts |
| `eval.python.yaml` | `src/examples/general_agent.py`, im selben Prozess | `OPENAI_API_KEY` |
| `eval.compose.yaml` | `src/examples/service/`, gebaut und gestartet über `docker-compose.eval.yaml` | Docker, `OPENAI_API_KEY` |

Die Beispiel-Agenten verwenden das [Pydantic-AI-Modell](https://ai.pydantic.dev/models/)
aus `SIMPLE_EVAL_EXAMPLE_MODEL` (Standard `openai:gpt-5-mini`). Der Grader
`rubric_judge` in `eval.python.yaml` ruft in jedem Fall `openai:gpt-5-mini` auf.

## Lizenz

Lizenziert unter der [Apache License 2.0](LICENSE). Copyright 2026 awa-projekt.
