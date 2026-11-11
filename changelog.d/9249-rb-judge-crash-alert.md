<!-- changelog: fixed -->
- **A review-blocked judge crash no longer pages as "autofix exhausted", and pages once per PR head.** When `scripts/review_rb_judge.sh` fails (`rb_judge_status=failed`, or `judge_skip_reason=llm_failed*`/`json_parse_failed*`) before autofix reached `MAX_AUTOFIX_ITERATIONS`, the "Telegram review-blocked judge decision" step now sends `PR review blocked — review-blocked judge failed (exit <code>; <n> autofix commits, max <m>)` with the run URL instead of the false "autofix exhausted (5 iterations)" text. Refs #9249.

On 2026-11-10/11 a Python 3.14 crash in `render_prompt.py` (fixed by PR #9240) made the judge exit 1 on every forced run, and PRs #8960, #8967, #8992, #9010, #9011, #9015 and #9057 each got the same CRITICAL "autofix exhausted" alert 1–3 times although no autofix had run. The review-blocked comment now carries `<!-- rb-judge-crash:v1 head=<sha> reason=<reason> -->` on such a crash. When a comment by the token's own identity already has the same marker, the step posts no duplicate comment, the alert is logged as `RB_JUDGE_CRASH_ALERT ... repeat=true level=SUPPRESSED` and nothing is sent. A failed comment read fails open (the comment and the alert are sent). The crash alert is always CRITICAL, even when the judge wrote a retry-only skip reason such as `merge_conflict` before it crashed. On a security-pass-only run the blocked-comment step does not run, so there is no marker to check: the alert is sent and logged `repeat=unchecked`. Genuine exhaustion keeps its exact messages; labels, the `ai:needs-human` hand-over and the stall ladder are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Alerts per PR head and crash reason | 1 (was one per stall-ladder redispatch) |
| Extra REST reads | up to 3, only on judge-crash runs |
| New step outputs | `rb_judge_exit_code`, `rb_judge_crash_repeat` |

What this means for consumer repos: after the next `@stable` sync, a crashing review-blocked judge pages once per PR head with an accurate message; no configuration change is needed.
