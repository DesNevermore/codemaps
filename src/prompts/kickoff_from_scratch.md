Begin: choose a language and implement the candidate from scratch.

Your tools: `bash` reads, writes and builds your candidate in /workspace; `run_oracle` runs the reference binary; `diff` and `diff_batch` compare the two on inputs you choose; `run_regression` re-runs every case recorded so far; `submit` delivers.

Done means: `compile.sh` produces `./executable`, and it matches the oracle on every documented flag, option, subcommand and workflow — not just the happy path. Anything left unimplemented stays a failure.

The oracle is your only source of truth. PROBE IT: run it on inputs you invent, read what it actually does, and implement that. Recorded failing cases are POINTERS, not the specification — a case tells you where you are wrong, not what is right. When one fails, go back to the oracle around that input, understand the behaviour it implies, then implement the feature properly. Patching output to satisfy a case without understanding what the oracle does there is how a candidate passes the recorded cases and still fails everywhere else.
