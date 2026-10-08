# SpecFirst

A reimplementation of the two-stage SpecFirst method; the original
implementation is not public. Stage 1 probes the binary and writes `SPEC.md`. Stage 2 implements the program from that spec alone — the stage-1 context is discarded.

| File | Role |
|---|---|
| `spec_agent.yaml` | Stage 1: probe and specify, no implementation. |
| `synth_agent.yaml` | Stage 2: synthesize, reading the spec from the `spec_md` template var. |

Both derive from the official mini-swe-agent ProgramBench config and differ from it only in the
task instructions. The system prompt and budget limits are identical across stages, so the
comparison is not confounded by prompt drift.

Two things are worth enforcing when running this:

- **A deliverable gate** — if stage 1 produces no `SPEC.md`, re-elicit rather than proceeding.
- **Proof that the spec arrived** — check that stage 2's first user message really contains the
  spec. Otherwise the run silently degrades into plain direct synthesis and must not be counted
  as SpecFirst.
