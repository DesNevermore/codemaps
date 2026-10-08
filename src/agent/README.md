# CodeMaps orchestrator (`src/agent/`)

A runnable multi-agent system that reverse-engineers a black-box ProgramBench
reference binary into a behaviour-matching re-implementation, driven entirely by the
**oracle** (differential testing), and certifies it before spending the one-shot
hidden-test exam.

## Components
| file | role |
|---|---|
| `llm.py` | model-agnostic client in canonical Anthropic block form, over the `anthropic`/`openai` SDKs (see `providers.py`). Env-configured. `StubLLM` = zero-cost dry-run. |
| `container.py` | offline (`--network none`) cleanroom session around `<instance>:task_cleanroom`; exec/cp/run helpers; oracle + candidate runners. |
| `tools.py` | the agent toolset + deterministic executors: `run_oracle`, **`diff`** (byte-compare oracle vs candidate, auto-records corpus), `write_file`/`read_file`, `shell`, `run_regression`, `take_exam` (score only), `submit`. **No tool reads the hidden tests.** |
| `generator.py` | coverage engine: `mine_docs` (flags/enums from --help/README), `mine_strings` (enum/error names the oracle *emits*), `candidate_coverage` (coverage.py on the candidate we own). |
| `orchestrator.py` | the closed loop: recon → seed → differential-converge → adversarial hunts → readiness → exam → score-feedback, with budget caps + monitor guard. |
| `run_task.py` | CLI; records trajectory + score under `src/agent/runs/<id>/` and `trajectories/`. |

## Run

Dry-run (no API, ~0 tokens — validates wiring end-to-end):
```
uv run python src/agent/run_task.py <instance_id> --dry-run --max-turns 8
```

Single real task (submission@1):
```
PB_LLM_PROVIDER=claude PB_LLM_EFFORT=high \
uv run python src/agent/run_task.py <instance_id> \
    --max-turns 200 --max-exams 24 --max-submissions 1 --wall-clock 10800 --docker-cpus 4
```

Parallel experiment sweep (easy → medium → hard), submission@1, RAM-governed:
```
uv run python src/agent/experiment.py \
    --provider claude --effort high \
    --difficulty easy,medium,hard --limit 0 \
    --max-parallel 4 --ram-cap 0.65 --per-task-ram-gb 5 --docker-cpus 4 \
    --max-turns 200 --max-exams 24 --max-submissions 5
```
Results land in `experiments/<run_id>/` — `manifest.json`, `summary.csv`, `final.json`,
and one `NNN_<instance>/` dir per task (`score.json` with the per-submission pass_rate
curve, `transcript.json`, `corpus.json`, `solution/`).

## Providers (the compatibility layer, `providers.py`)
Both backends are translated to/from one canonical Anthropic block dialect, so the
orchestrator is provider-agnostic:

| backend | SDK | effort knob |
|---|---|---|
| `anthropic:<model>` | `anthropic` | extended thinking (`budget_tokens`) |
| `openai:<model>` | `openai` | `reasoning_effort` |

Named aliases (`claude`, `gpt55`, `deepseek`, `qwen`, …) live in `providers.MODELS`.
Set `PB_LLM_API_KEY` (or `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) and `PB_LLM_PROVIDER`;
`PB_LLM_BASE_URL` points either SDK at a compatible proxy.
`PB_LLM_EFFORT` ∈ `none|minimal|low|medium|high|xhigh|max`.

## submission@1 semantics
`submit` = the single **deliberate, counted** scored attempt (1 per task). The agent is
instructed to think hard and only submit when confident. A readiness gate (delivery smoke +
fuzz burst + adversarial judge panel) vets the candidate first; if it finds a divergence it
blocks the submit and hands the issue back to fix **without consuming the submission**, so the
agent keeps refining until it passes the gate, then the submit is finalized. `score.json`
records the submission's pass_rate; `pass_at_1` = whether that submission hit 100%.

For Pass@k experiments, pass `--max-submissions k` (>1); CodeMaps then allows k counted
submissions and `best_pass_rate` is the max over them.

## Budget knobs (cost control)
- `--max-turns` — hard cap on LLM round-trips (dominant token driver).
- `--max-exams` — hidden-exam checkpoints (slow wall-clock, not tokens).
- `--wall-clock` — seconds before the loop stops.
- `PB_LLM_EFFORT` — `medium` is ~3–4× cheaper than `xhigh` on output/thinking tokens;
  use `xhigh` only when a task's reasoning is genuinely deep.
- The transcript window is auto-truncated to keep context (and cost) bounded.

## Safety / anti-cheat (enforced)
- RE container runs `--network none`; reference binary is execute-only (behaviour only).
- Hidden tests are sealed: `take_exam`/`submit` return the **aggregate score only**.
- `monitor.py` guard before container start + every exam; exam is serialized
  (workers=1, cpus≤6). The orchestrator never touches containers it didn't create
  (its own are named `pb-re-<rand>`).

## Honesty (what to expect)
Oracle-only pass-rate ≈ the fraction of behaviour the
generator reaches. Small/enumerable tasks → 100%; medium/large tasks typically
plateau 90–99% (which scores **0** under the strict `pass_rate==1.0` "resolved" bar).
The score JSON reports both the resolved verdict and the exact pass-rate, plus
exams_used / corpus_size / token usage.
