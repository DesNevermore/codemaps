# CodeMaps

**Manufacturing a Progress Signal: Self-Evaluating Coding Agents for Long-Horizon Program
Synthesis from Scratch**

CodeMaps is a framework that lets coding agents **manufacture and update an explicit progress
signal** during long-horizon program synthesis from scratch. In the
[ProgramBench](https://github.com/programbench/programbench) setting, an agent must re-implement a
program given only usage documentation and an execute-only reference binary — with no correctness
oracle and no repository tests to measure itself against.

CodeMaps builds that missing signal out of executable tests whose expected observations are
measured from the reference program. The tests give the agent reproducible repair targets, the
corpus gives it regression checks, and an obligation ledger tells it which documented behaviors
it has not probed yet.

## Prerequisites

### 1. The ProgramBench benchmark

CodeMaps imports `programbench` for task metadata and the official grader. It is a separate
upstream project and is **not** vendored here:

```bash
# (a) as a package, into the same environment
uv pip install programbench

# (b) or as a checkout, added to PYTHONPATH
git clone https://github.com/programbench/programbench third_party/ProgramBench
export PYTHONPATH=$PWD/third_party/ProgramBench/src:$PYTHONPATH

# (c) or as a submodule
git submodule add https://github.com/programbench/programbench third_party/ProgramBench
git submodule update --init --recursive
```

### 2. Docker and an API key

Runs happen in network-isolated containers, so a working `docker` is required. See
[Model access](#model-access) for credentials.

## Quick start

```bash
uv sync
export PB_LLM_API_KEY=...
uv run python src/agent/run_task.py <instance_id>
```

Add `--dry-run` to exercise the loop with a scripted stub client and no API calls.

```bash
uv run python src/agent/experiment.py --difficulty easy,medium,hard --max-parallel 10
```

## Repository layout

```
src/
  agent/              # CodeMaps source code
  prompts/            # every model-facing prompt, as editable .md files
  exam.py             # one-shot hidden-test exam (wraps the official grader)
baselines/
  msa-full/                # mini-swe-agent with full budget
  claude-code-multiagent/  # Claude Code, N-agent team paradigm
  specfirst/               # SpecFirst (arXiv:2607.27167), reimplemented
third_party/          # (gitignored) optional ProgramBench checkout
```

## Baselines

All three run under the same budget as CodeMaps: 6 hours, 1,000 turns, one graded submission.

**MSA-full** — [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) under the official
ProgramBench configuration, additionally instructed to keep working until the time or step limit
is reached, and resumed within the remaining budget if it stops early.
See [baselines/msa-full/README.md](baselines/msa-full/README.md).

**Claude Code** — Claude Code prompted to follow the N-agent team paradigm, in a network-isolated
container. See [baselines/claude-code-multiagent/README.md](baselines/claude-code-multiagent/README.md).

**SpecFirst** — a specification agent probes the binary and writes a structured spec, then a
synthesis agent implements from that spec alone. The original implementation is not public, so
this is a reimplementation following the paper.
See [baselines/specfirst/README.md](baselines/specfirst/README.md).

## Model access

`src/agent/providers.py` talks to models through the official **`anthropic`** and **`openai`**
SDKs. Both accept a `base_url`, so any compatible proxy works without code changes:

```bash
# Vendor endpoint (default)
export PB_LLM_PROVIDER=claude

# Through a LiteLLM / vLLM / OpenRouter / Azure proxy
export PB_LLM_BASE_URL=http://localhost:4000
export PB_LLM_PROVIDER=openai:my-proxied-model
```

Aliases live in `providers.MODELS`; add a row there to name a new model, or use the explicit
`anthropic:<model>` / `openai:<model>` form for anything unlisted.

The SDKs handle retries and backoff for 429/5xx. Two things sit on top: a cross-process token
bucket (`ratelimit.py`) keeps a whole host of concurrent runs under a per-key request/min ceiling,
and a context-overflow repair drops the oldest whole turns and retries instead of losing the run.

## Evaluation

Scoring uses the upstream grader without modification — `src/exam.py` packs a submission, pulls
the task image, and delegates to `programbench eval`, then reports the official pass rate (active
branches only, ignored tests dropped):

```bash
uv run python src/exam.py <instance_id> <solution_dir>
```

Hidden test suites are never exposed to the agent, so they cannot enter the progress signal.
