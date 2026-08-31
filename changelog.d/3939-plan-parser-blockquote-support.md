<!-- changelog: fixed -->
- **The plan workflow's orchestrator auto-answer parser now accepts the blockquoted clarification-question format the prompt itself mandates.** False "Auto-answer parser failed … No Q-ID blocks detected" alerts no longer fire on prompt-conformant output.

`prompts/mode-plan.txt` instructs the planner to emit clarification questions inside a markdown blockquote (`> **Q1: <question>**` through `> Reply:`), but the auto-answer parser and the structured-clarification-block detector in `.github/workflows/plan.yml` anchored their regexes on `^\s*` and never matched the `> ` prefix. An emission that followed the template verbatim raised a false parser-failure alert and forced a human `/answer`, as on shubhodeep1/fun-token-multi-chain#434 (run 33355986371). The five regexes now take an optional `(?:>\s*)*` blockquote prefix, the two `Q_COUNT` greps in `.github/workflows/clarify.yml` carry the same tolerance, and `tests/test_plan_auto_answer_recommended_parser.py` pins single- and multi-question blockquoted fixtures, including the `> Reply:` line.

| The numbers that matter | Value |
| --- | --- |
| Regexes widened in `plan.yml` | 5 |
| `Q_COUNT` greps widened in `clarify.yml` | 2 |
| New parser tests | 4 (48 total passing) |
| Triggering incident | shubhodeep1/fun-token-multi-chain#434, run 33355986371 |

What this means for operators: orchestrator-managed plan runs whose clarification questions follow the canonical blockquoted template are auto-answered as designed instead of stalling on a false "No Q-ID blocks detected" alert awaiting a human `/answer`.

### For contributors

The stall-recovery parser `extract_recommended_answers` in `scripts/orchestrate_poll_process.sh` still anchors on `^\s*` and would miss blockquoted comments; it is deliberately untouched here and tracked as a follow-up.
