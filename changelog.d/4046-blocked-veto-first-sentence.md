<!-- changelog: fixed -->
- **An "already satisfied" `BLOCKED:` verdict now closes the issue as a no-op even when the model's trailing diagnostics mention failures.** The real-obstacle veto in `.github/workflows/implement.yml` reads only the verdict's first sentence.

Seven `implement` runs in `shubhodeep1/tele-funtoken-msg-scoring` on 2026-09-07 answered a verdict such as `BLOCKED: Approved plan requires no repository changes; HEAD already contains the fix. Targeted tests pass (148). Full suite has 6 unrelated failures.` and every one went red with an ERROR alert, because the veto regex was applied to the whole reason and `failures` (or `lacks`, `unavailable`) in the aside about the pre-existing test suite overrode the already-satisfied classification. The veto now stops at a conservatively detected first sentence, held in `codex_blocked_first_sentence`; `;` and `,` do not end the sentence, and ambiguous abbreviation boundaries or extraction failures fall back to the whole reason. Thus `requires no edits; pytest is unavailable` and `no changes are required, e.g. DigitalOcean token is unavailable` remain vetoed while trailing diagnostic sentences are ignored. The positive vocabulary also gains `already contains` and `already holds`.

| The numbers that matter | Value |
| --- | --- |
| Runs that went red on an already-satisfied verdict | 7 (34162543282, 34162832548, 34163478446, 34163977712, 34163969134, 34164219124, 34166170045) |
| Consumer issues affected | #4180, #4184, #4189, #4191, #4192, #4193, #4213 |
| Sentence boundaries the veto stops at | `.`, `!`, `?` followed by whitespace and an uppercase letter or digit, excluding ambiguous abbreviation boundaries |

What this means for operators: a plan whose fix is already on the branch closes with the ✅ "Already implemented" comment and `ai:closed`, as README 10j describes, instead of an implementation-failed diagnostics comment and a Telegram ERROR alert. A genuine blocker stated as the opening sentence still fails the run.

### For contributors

`tests/test_implement_post_codex_recovery.py::test_blocked_already_satisfied_regexes_classify_real_verdicts` pins the exact `sed` expression and abbreviation fallback the workflow uses and carries the seven verbatim reasons as fixtures, plus red fixtures for an obstacle after `;`, `,`, or `e.g.`, an obstacle as the opening sentence, and the genuine `DigitalOcean credential unavailable` blocker from run 34168336869. Run 34166164615's `permits no repository edits … already cover the incident` is still not recognised by the positive half and stays red.
