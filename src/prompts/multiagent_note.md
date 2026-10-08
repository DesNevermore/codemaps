

TEAM SUPPORT (this run has helpers):
- GOAL: your FIRST counted `submit` should be behaviourally identical to the oracle. There is NO practice
  exam — you cannot peek at your score. The ONLY way to get there is to fully understand and
  exploit the documentation AND drive exhaustive differential coverage against the oracle, so
  lean HARD on `diff`, the coverage sweeps, and your own adversarial probing. The regression gate
  blocks `submit` while any recorded case fails, so fix the corpus to clean before submitting.
- Coverage sweeps run CONTINUOUSLY in the background (every ~25 turns), so the corpus keeps
  growing — this is normal. Fix what the gate shows you (run_regression → edit source with bash →
  rebuild), then keep probing for new dimensions.
- The harness periodically runs a FULL COVERAGE SWEEP: parallel divergence-HUNTERS
  (such as arg-parsing & error text, empty/huge/binary/invalid-UTF-8 input, numeric/formatting) AND completeness-CRITICS (which distrust the docs and probe behaviour
  --help/README never mention: undocumented flag aliases, unlisted options, env/locale
  effects, exit-code & protocol conventions). Every divergence they find is recorded as a
  regression case. You never read that record directly — `run_regression` is your window onto it:
  it reports how many cases pass/fail, lists the first few FAILING inputs with the exact
  oracle-vs-candidate divergence, and groups them by region and by root cause. That is how you
  discover WHAT to fix without ever seeing the tests.
- You can also trigger them yourself: `request_hunt` (ordinary dimensions). After any sweep/hunt, ALWAYS
  `run_regression` to list the new failing cases, FIX each (edit the source with `bash`), rebuild.
- A failing case POINTS AT a bug; it does not specify the fix. It tells you WHERE you are wrong,
  not what is right. So when one fails, go back to `run_oracle`/`diff` around that input, work out
  the behaviour the oracle actually implements, and build that feature properly — the case then
  passes as a side effect. Special-casing an output so a recorded case goes green, without
  understanding what the oracle does there, is how a candidate clears every recorded case and still
  fails the inputs nobody recorded.
- Adversarial readiness JUDGES gate `submit`: when you submit, skeptics first try to break
  your candidate. If they find ANY divergence, your submission is BLOCKED (it is NOT consumed)
  and the divergences are added to your corpus to fix. So you never waste your submission on a
  breakable candidate — submission only goes through once the panel can't break you.
- TIP for re-implementing library behaviour: offline you cannot import the third-party library
  the target tool relied on, so you must RE-IMPLEMENT its behaviour yourself — and the oracle is
  your spec. Whatever looks like a hidden table or constant inside that library is usually
  OBSERVABLE from the oracle directly: measure it, don't guess it, and don't trust any local
  library's assumptions. Example (display width): feed a single character through the oracle in a
  width-sensitive mode and count the columns it allocates — that IS the oracle's width for that
  codepoint; build your width table from those measurements.
- TIP for hidden flags: --help does NOT list every flag the binary accepts. For each
  documented flag, brainstorm plausible synonyms/abbreviations/near-spellings and probe
  whether the oracle silently accepts them (exit 0) — let the oracle tell you which alias
  is real, then make your candidate accept it too (wire the alias into your arg parser). This
  is the one defect class diff-testing misses, since you can't diff a flag you never tried
