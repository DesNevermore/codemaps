# Claude Code (multi-agent)

Claude Code prompted to follow the N-agent team paradigm, in a network-isolated container: a lead
agent coordinates 4 peers, each working in its own checkout and sharing code through git. The lead
is the one that submits.

| File | Role |
|---|---|
| `system_prompt.txt` | System prompt (anti-cheat rules), identical to the other baselines'. |
| `kickoff_multi_agent.txt` | First user message: the official task instruction + the team framing. |

Resume segments are fed a neutral continuation only (`Continue working on the task.`), so no
method hints leak in.
