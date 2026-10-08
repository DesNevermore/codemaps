# MSA-full

[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) under the official ProgramBench
configuration, with one change: the agent must keep working until the budget is exhausted —
6 hours or 1,000 steps, whichever comes first — instead of submitting as soon as it thinks the
task is done. If it stops early, it is resumed within the remaining budget.

| File | Role |
|---|---|
| `system.txt` | Official system prompt (anti-cheat rules). Unmodified. |
| `instance_template.txt` | The instance prompt actually used: official text + the budget paragraph. |
| `budget_reminder.txt` | That budget paragraph on its own. |
| `continue.txt` | Returned to the agent when it submits before the budget is spent. |

The budget paragraph is the only change from the official config. Everything else — the system
prompt, the instance body, and the observation/format-error templates — is official and unmodified.
