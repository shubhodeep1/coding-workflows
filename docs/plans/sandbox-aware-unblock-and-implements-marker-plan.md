# Sandbox-aware unblock verdicts, `Implements #N` PR adoption, and a default-branch reference for the planner

## Summary

Stop a repeat of the fun-token-multi-chain#639 failure chain. Three changes: the unblock judge stops giving the implementer instructions its sandbox cannot carry out. A person's PR can be linked to a blocked orchestrator item with an explicit `Implements #N` line. The planner's sandbox gets the default branch as an `isolated-base` reference, as the implementer's already does.

## Automation entry points (CLAUDE.md §18.E)

| Question | Answer |
| --- | --- |
| New script? | No. Every phase changes existing code only. |
| Entry points | Phase 1: `scripts/unblock_judge.sh` + `scripts/unblock_ledger.py`, run by `.github/workflows/unblock_judge.yml` (dispatched through `unblock_judge_dispatch.yml` by the poller's unblock scan in `.github/workflows/orchestrate_poll.yml`). Phase 2: `scripts/orchestrate_poll_process.sh`, run by `.github/workflows/orchestrate_poll.yml` (schedule). Phase 3: `scripts/run_plan_codex.sh`, run by the "Run Codex planning" step of `.github/workflows/plan.yml` (issue events). |
| New supervisor (§18.C)? | No. |
| DB work (§18.D)? | None. |
| Removal registry (§18.F)? | Not applicable: no new script or supervisor. |

## Context

The fun-token-multi-chain#639 chain, step by step:

1. **Implementer could not see the baseline.** #639's approved plan said "restore the current-main coinflip code". The implement sandbox (`scripts/codex_isolated_exec.sh`, `--network none`, no host `.git`, no remote) gave the agent one synthetic commit, so it returned `BLOCKED` twice (runs 38875907463, 38920624589).
   - **Already fixed on `main` by #7548** (not yet on `stable`). `seed-git` adds the default branch as branch `isolated-base`; `.github/workflows/implement.yml` sets `CODEX_ISOLATED_GIT_BASE_REF`; `prompts/mode-implement.txt` tells the agent to use `git show isolated-base:<path>`.
   - Only implement sets the base ref. The planner also runs through `codex_isolated_exec.sh` (`scripts/run_plan_codex.sh`, `--mode read-only`, and the Claude path via `claude_run`). It checks out the **integration branch** (`plan.yml` "Checkout repository", `ref: steps.refctx.outputs.ref || default_branch`), so on a project item it cannot compare against production.
   - The unblock judge does **not** need this. It runs through `scripts/clarify_isolated_run.sh` on a snapshot of `target/`, which `unblock_judge.yml` checks out at the default branch ("Checkout default branch for the judge to read").
2. **Judge spent a retry on an impossible instruction.** The round-2 `retry_budget` verdict told the implementer to `git fetch origin main`, `git show <merge-base>:<path>` and "try npm ci". `prompts/mode-judge-unblock.txt` says nothing about the implementer's environment. `scripts/unblock_ledger.py` `validate()` checks the verdict's shape and limits, not what its instructions ask for. The retry failed the same way and the item hit `item_cap`.
3. **A person's fix was not recognised.** PR #667 (head `claude/amazing-galileo-e32yjo`, base `orchestrator/project-579`, body `Refs #639`) did the work.
   - `_pr_json_is_issue_implementation_pr` (`scripts/orchestrate_poll_process.sh`) accepts only head `ai/issue-<n>` or a closing keyword (`_pr_json_closes_issue`), so the poller logged `LINKED_PR_CROSS_REF_REJECTED ... reason=not_implementation_pr`.
   - #639 stayed `ai:blocked` until #667's body was hand-edited to `Closes #639`.
   - The strictness is intentional: issue #3817 / PR #3825 adopted a PR that only said `Refs #3817` and closed an unimplemented issue. The interactive session wrote `Refs` partly because CLAUDE.md §19 steers sessions toward `Refs` for anything orchestrator-related, though §19 only forbids closing keywords against **tracking** issues.
4. The `ai:needs-human` label-repair loop that followed is fixed separately in PR #7651 and is out of scope here.

## Goals

- G1: An unblock verdict whose `instructions` require network or remote access (fetch/pull/clone/push, `origin/…` refs, package installs, `curl`/`wget`, the `gh` CLI) never reaches an item. The judge is re-asked once in the same run with the refusal reason. A second refusal takes the existing `invalid_verdict` path.
- G2: The unblock judge prompt states what the implementer's sandbox can and cannot do, including `isolated-base`.
- G3: A merged PR whose body has a line `Implements #N` and whose base is the default branch or an `orchestrator/project-<n>` branch counts as item #N's implementation PR. A plain `Refs #N` or bare mention still does not.
- G4: CLAUDE.md §19 and the `investigate-issue` / `validate-consumer-issue` commands tell interactive sessions to link a PR that implements an orchestrator **item** (not a tracking issue) with `Implements #N`, never bare `Refs`.
- G5: The planner's sandbox git has branch `isolated-base` (the default branch, filtered as for implement), and `prompts/mode-plan.txt` explains it.
- G6: Each new behaviour has a kill switch ending in `_ENABLED`, default `true`.

## Non-goals

- The label-repair fix (PR #7651) and promoting #7548 to `stable`; both ship through the normal release.
- Giving the unblock judge an `isolated-base`; it already reads the default branch (see Context 1).
- A baseline at the commit the project branch forked from (rejected in Q2).
- An issue-side `/adopt-pr` command (rejected in Q4).
- Network access of any kind for the implementer.

## Constraints

- §6: no rename or removal. `_pr_json_is_issue_implementation_pr`, `_pr_json_closes_issue`, `validate()`, `LINKED_PR_CROSS_REF_REJECTED` and every existing env var keep their names and behaviour. New names were checked for collisions in this repo (all have zero existing hits):
  - `_pr_json_implements_issue`
  - `ORCH_IMPLEMENTS_MARKER_ENABLED`
  - `UNBLOCK_INSTRUCTION_SANDBOX_CHECK_ENABLED`
  - `PLAN_ISOLATED_BASE_ENABLED`
  - `PLAN_ISOLATED_BASE_BRANCH`
  - `LINKED_PR_IMPLEMENTS_MARKER`
- §15: no new GitHub API calls. Phase 2 reuses the PR JSON the callers already fetch and the memoized `_review_run_default_branch`. Phase 3 adds one `git fetch` of a single branch, depth 1.
- §19: `Implements` is not a GitHub closing keyword, so it never auto-closes anything, and `scripts/lint_pr_body_auto_close.py` needs no change. The §19 ban on closing keywords against `ai:orchestrator-tracking` issues stays as is.
- §20 / §30: each phase adds one `changelog.d/<issue>-<slug>.md` and, where documentation changes, one `agents.d/<issue>-<slug>.md`; never edit `CHANGELOG.md` or `agents.md` directly.
- §27: `plan.yml` is 173,405 bytes and Phase 3 adds only an `env:` line; logic goes in `scripts/run_plan_codex.sh`.
- Prompts exist twice: `prompts/mode-*.txt` (used) and `prompts/_templates/mode-*.txt` (the `PROMPT_PRELUDE_REFACTOR_ENABLED` path, README "Phase O"). Every prompt change edits both.
- §9: tabs in shell and in Python files that already use tabs (`unblock_ledger.py` does); 2-space YAML.
- §14: everything propagates to consumer repos through the `@stable` sync of reusable workflows, `scripts/`, `prompts/`, `CLAUDE.md` and `.claude/commands/`. No wrapper change is needed.

## Approach

**Phase 1: sandbox-aware unblock verdicts.** Two layers.

The prompt tells the judge what the implementer can do: no network, no git remote, no `origin/*` refs, no package installs, no `gh` and no GitHub token. It can use dependencies installed before the run, the checked-out files, and branch `isolated-base` (when present) for the default branch's files. Instructions must stay within that.

A deterministic check in `unblock_ledger.py` refuses the verdict when an instruction field matches a fixed list of network or remote patterns. `unblock_judge.sh` then re-asks the model once with the refusal reason. A deterministic check alone would loop: a refused verdict waits ~6 hours and the same prompt usually yields the same instruction. So the prompt prevents most cases and the check catches the rest.

**Phase 2: `Implements #N`.** Adds a third, explicit, opt-in way for `_pr_json_is_issue_implementation_pr` to accept a PR. It keeps #3817's protection: a mention or `Refs` still never counts, and a merge into an unrelated branch is refused by a base check inside the new helper. The close sweep keeps its stricter `_pr_json_base_is_target_merge` check on top. Interactive guidance changes in the same phase so sessions actually write the line.

Alternatives considered:
- An `/adopt-pr` issue command (Q4 B): a second trust path to maintain.
- Teaching sessions to use `Closes #N` only (Q4 D): this still fights §19's "use Refs" wording, and a closing keyword auto-closes the item on default-branch merges.

**Phase 3: planner `isolated-base`.** Reuses #7548's mechanism unchanged. Before the sandbox runs, `run_plan_codex.sh` fetches the default branch into `refs/codex-isolated/base` and exports `CODEX_ISOLATED_GIT_BASE_REF`. `codex_isolated_exec.sh` passes it to `seed-git` in both engines and both modes.

## Phases & Merge Strategy

The three phases touch disjoint behaviour. Each can merge alone, in any order, and each is safe in production on merge. Phases 1 and 3 both edit prompt files but different ones (`mode-judge-unblock.txt` vs `mode-plan.txt`), so there is no textual overlap either.

1. **Phase 1 — Unblock verdicts respect the implementer sandbox.**
   - Files: `prompts/mode-judge-unblock.txt`, `prompts/_templates/mode-judge-unblock.txt`, `scripts/unblock_ledger.py`, `scripts/unblock_judge.sh`, `tests/test_unblock_judge.py`, plus fragments.
   - Done when: a mocked verdict with `git fetch origin main` in `instructions` is refused, re-asked once, and the second mocked verdict is acted on. With `UNBLOCK_INSTRUCTION_SANDBOX_CHECK_ENABLED=false`, the first verdict is accepted as today.
   - Rollback: set `UNBLOCK_INSTRUCTION_SANDBOX_CHECK_ENABLED=false` (repo variable) or revert the PR. No state is written.
2. **Phase 2 — `Implements #N` links a PR to an orchestrator item.**
   - Files: `scripts/orchestrate_poll_process.sh`, `tests/test_linked_pr_implementation_guard.py`, `CLAUDE.md` (§19), `.claude/commands/investigate-issue.md`, `.claude/commands/validate-consumer-issue.md`, plus fragments.
   - Done when: a PR JSON fixture with `Implements #639` and base `orchestrator/project-579` is accepted; the same with `Refs #639`, or with base `feature/x`, is rejected; with `ORCH_IMPLEMENTS_MARKER_ENABLED=false` the marker is ignored.
   - Rollback: set `ORCH_IMPLEMENTS_MARKER_ENABLED=false` or revert. Items already closed through the marker stay closed; this matches the existing closing-keyword path.
3. **Phase 3 — The planner sees the default branch as `isolated-base`.**
   - Files: `scripts/run_plan_codex.sh`, `.github/workflows/plan.yml` (one `env:` entry on the "Run Codex planning" step), `prompts/mode-plan.txt`, `prompts/_templates/mode-plan.txt`, `tests/test_plan_codex_step_extraction.py` (or a new `tests/test_run_plan_isolated_base.py`), plus fragments.
   - Done when: a test run of the new `run_plan_codex.sh` block with a fake `git` exports `CODEX_ISOLATED_GIT_BASE_REF=refs/codex-isolated/base` after a successful fetch and leaves it empty after a failed one or when disabled.
   - Rollback: set `PLAN_ISOLATED_BASE_ENABLED=false` or revert. A failed fetch only leaves the branch out, as in implement.

## Implementation Steps

### Phase 1

1. `prompts/mode-judge-unblock.txt` and `prompts/_templates/mode-judge-unblock.txt`: after the verdict menu, add a paragraph headed "Implementer environment". It says:
   - The implementer and every fix-up issue run unattended in a container with no network, no git remote, no `origin/*` refs, no GitHub token and no `gh` CLI.
   - Dependencies are installed before the agent starts; it cannot install packages.
   - It can read the checked-out branch and, when `git branch --list isolated-base` shows it, the default branch's files with `git show isolated-base:<path>`.
   - Instructions for `retry_budget`, `descope`, `reissue`, `accept_with_followup` and `operator_step` must use only these. When the baseline is needed, name `isolated-base`, never `git fetch` or a merge base.
   - The pipeline refuses instructions that need network or remote access.
   - If the work cannot be done without network access, choose `operator_step` with an `_UNSET_OPERATOR_STEP` placeholder instead of `retry_budget`.
2. `scripts/unblock_ledger.py`: add `SANDBOX_INCOMPATIBLE_PATTERNS`, a tuple of `(id, compiled regex)`, case-insensitive, word-bounded:
   - `git_remote_op`: `\bgit\s+(fetch|pull|clone|push|remote|ls-remote)\b`
   - `git_history_deepen`: `--unshallow|--deepen`
   - `origin_ref`: `\borigin/[A-Za-z0-9._/-]+`
   - `package_install`: `\b(npm\s+(ci|install|i)|yarn(\s+install)?|pnpm\s+(install|i)|pip3?\s+install|poetry\s+install|bundle\s+install|cargo\s+fetch|go\s+mod\s+download)\b`
   - `http_fetch`: `\b(curl|wget)\s`
   - `gh_cli`: `\bgh\s+(api|pr|issue|run|repo|release|workflow)\b`

   Add `_check_sandbox_instructions(text: str) -> None`, which raises `UsageError(f"sandbox_incompatible:{id}")` on the first match. In `validate()`, when the new keyword argument `sandbox_check: bool = False` is true, call it on `normalised["instructions"]` for `retry_budget`, `descope`, `reissue`, `accept_with_followup` and `operator_step`. Never call it on `operator_instructions` (written for a person) or `answer`. Add the CLI flag `--sandbox-check` to the `validate` subcommand to set it. Default off keeps every existing caller and test unchanged.
3. `scripts/unblock_judge.sh`:
   - Read `UNBLOCK_INSTRUCTION_SANDBOX_CHECK_ENABLED` (default `true`; anything other than `false` counts as `true`) and append `--sandbox-check` to `validate_args` when enabled.
   - When validation fails and `.error` in `verdict.json` starts with `sandbox_incompatible:`, and no re-ask has happened in this run:
     - log `UNBLOCK_JUDGE item= kind= stop= fingerprint= op=instruction_sandbox_check outcome=reasked reason=<id>`;
     - set `UNBLOCK_JUDGE_REASK_NOTE` to one line: "Your previous verdict was refused: its instructions need network or remote access (<id>). The implementer has none. Rewrite them using only the Implementer environment above.";
     - call `unblock_ask_model` again and re-run validation once.
   - In `unblock_ask_model`, after the prompt template and before the context JSON, append `=== PREVIOUS VERDICT REFUSED ===` and the note when `UNBLOCK_JUDGE_REASK_NOTE` is non-empty.
   - On a second failure, log `op=instruction_sandbox_check outcome=refused_after_reask reason=<id>` and fall through to the existing `invalid_verdict` handling unchanged.
   - `MOCK_UNBLOCK_JUDGE_JSON` keeps working; add `MOCK_UNBLOCK_JUDGE_JSON_REASK` (test-only, default empty), used for the second answer when set.
4. Fragments:
   - `changelog.d/<issue>-unblock-sandbox-instructions.md` (`fixed`);
   - `agents.d/<issue>-unblock-sandbox-instructions.md`, one block for `## Integration-sync verifier + bootstrap contract` (the section holding the "Unblock judge (Phase 7)" bullet) describing the check, the re-ask and the kill switch, and one block for `## Stable log prefixes (contractual)` adding a bullet that documents the new `UNBLOCK_JUDGE` fields `op=instruction_sandbox_check outcome=reasked|refused_after_reask reason=` (no new prefix name, so no `LOG_PREFIX.name=` line).

### Phase 2

5. `scripts/orchestrate_poll_process.sh`: next to `_pr_json_closes_issue`, add `_pr_json_implements_issue <issue_num> <pr_json>`. It returns 0 (yes), 1 (no) or 2 (bad input), and its docstring states the input, the output and that it makes 0 API calls.
   - **Body match**, case-insensitive: `(^|[^[:alnum:]_-])implements[[:space:]]+(#N|<GITHUB_REPOSITORY>#N|https?://github\.com/<GITHUB_REPOSITORY>/issues/N)([^[:alnum:]_]|$)`, with `N` the exact number. Other repositories' references never match.
   - **Base guard:** `.base.ref` must equal the repository default branch (`_review_run_default_branch`, memoized, no extra call after the first per process) or match `^orchestrator/project-[0-9]+$`.
   - On a body match with a refused base, log `LINKED_PR_IMPLEMENTS_MARKER issue= pr= base= outcome=rejected reason=base_not_target|default_branch_unknown` and return 1. On acceptance, log `LINKED_PR_IMPLEMENTS_MARKER issue= pr= base= outcome=accepted`.
6. Same file: in `_pr_json_is_issue_implementation_pr`, after the `_pr_json_closes_issue` check, add `if [ "${ORCH_IMPLEMENTS_MARKER_ENABLED:-true}" != "false" ] && _pr_json_implements_issue "${issue_num}" "${pr_json}"; then return 0; fi`. Update the function's comment block to list the third rule and why it is explicit, citing #3817 and fun-token-multi-chain#639. Every caller (wave reconcile, close sweep, stall recovery, `orchestrate_stall_retired_conflict.sh`) gets the rule through this one function.
7. `CLAUDE.md` §19: after the "blocker bug" bullet, add a bullet. A PR that implements an orchestrator **item** issue (labelled `ai:orchestrator-managed`, not `ai:orchestrator-tracking`), for example one a person finishes after the pipeline blocked, carries a line `Implements #N` so the poller links it. Bare `Refs #N` is not enough. `Implements` is not a closing keyword and is safe beside the tracking-issue rule. Edit only that bullet: §6 forbids renumbering, and `tests/test_claude_md_section_numbers.py` must stay green.
8. `.claude/commands/investigate-issue.md` and `.claude/commands/validate-consumer-issue.md`: where each tells the session how to reference the issue in a PR body, add that an orchestrator item gets `Implements #N` (tracking issues keep `Refs #N`).
9. Fragments:
   - `changelog.d/<issue>-implements-marker.md` (`added`);
   - `agents.d/<issue>-implements-marker.md`: a block for `## Label-repair contradiction policy (current branch)` (where the poller's `reconcile_managed_issue_labels` / linked-PR adoption is documented) describing the marker, the base guard and the kill switch;
   - a "Stable log prefixes (contractual)" block registering `LINKED_PR_IMPLEMENTS_MARKER` and its `LOG_PREFIX.name=` line.

### Phase 3

10. `.github/workflows/plan.yml`, step "Run Codex planning" (the one that runs `bash scripts/run_plan_codex.sh`): add `PLAN_ISOLATED_BASE_BRANCH: ${{ github.event.repository.default_branch }}` to its `env:`.
11. `scripts/run_plan_codex.sh`, before the first sandbox call (the `claude_run PLAN` / `codex_isolated_exec.sh run --mode read-only` block):
    - When `PLAN_ISOLATED_BASE_ENABLED` is not `false` and `PLAN_ISOLATED_BASE_BRANCH` passes the same validation as implement (`^[A-Za-z0-9._/-]+$` and `git check-ref-format --branch`), run `git fetch --no-tags --depth=1 origin "+refs/heads/${PLAN_ISOLATED_BASE_BRANCH}:refs/codex-isolated/base"`.
    - On success, export `CODEX_ISOLATED_GIT_BASE_REF=refs/codex-isolated/base` and log `CODEX_ISOLATION base_branch=<b> outcome=fetched role=plan`.
    - On failure, log `::warning::CODEX_ISOLATION base_branch=<b> outcome=fetch_failed role=plan` and leave it unset.
    - When invalid or disabled, log `::notice::CODEX_ISOLATION base_branch=unset_or_invalid outcome=skipped role=plan`.
    - This mirrors `implement.yml`'s block with the same log prefix (`CODEX_ISOLATION`, already registered) plus a `role=plan` field.
12. `prompts/mode-plan.txt` and `prompts/_templates/mode-plan.txt`: add the "Default-branch reference" paragraph from `prompts/mode-implement.txt` (lines 24–38 on `main`), reworded for planning. When the plan restores or preserves production behaviour, read it from `isolated-base` and cite paths there. Never plan steps that fetch from a remote.
13. Fragments:
    - `changelog.d/<issue>-plan-isolated-base.md` (`changed`);
    - `agents.d/<issue>-plan-isolated-base.md`, appending to "Isolated Codex agents": the planner now sets `CODEX_ISOLATED_GIT_BASE_REF`, the kill switch `PLAN_ISOLATED_BASE_ENABLED`, and the log lines.

## Files & Modules

- Phase 1:
  - `.github/workflows/unblock_judge.yml` (kill-switch `env:` line)
  - `prompts/mode-judge-unblock.txt`
  - `prompts/_templates/mode-judge-unblock.txt`
  - `scripts/unblock_ledger.py`
  - `scripts/unblock_judge.sh`
  - `tests/test_unblock_judge.py`
  - `changelog.d/<issue>-unblock-sandbox-instructions.md` `[new]`
  - `agents.d/<issue>-unblock-sandbox-instructions.md` `[new]`
- Phase 2:
  - `.github/workflows/orchestrate_poll.yml` (kill-switch `env:` lines)
  - `scripts/orchestrate_poll_process.sh`
  - `tests/test_linked_pr_implementation_guard.py`
  - `CLAUDE.md`
  - `.claude/commands/investigate-issue.md`
  - `.claude/commands/validate-consumer-issue.md`
  - `changelog.d/<issue>-implements-marker.md` `[new]`
  - `agents.d/<issue>-implements-marker.md` `[new]`
- Phase 3:
  - `.github/workflows/plan.yml`
  - `scripts/run_plan_codex.sh`
  - `prompts/mode-plan.txt`
  - `prompts/_templates/mode-plan.txt`
  - `tests/test_run_plan_isolated_base.py` `[new]` (or an extension of `tests/test_plan_codex_step_extraction.py`)
  - `changelog.d/<issue>-plan-isolated-base.md` `[new]`
  - `agents.d/<issue>-plan-isolated-base.md` `[new]`

## Tests

Phase 1 (unit, in `tests/test_unblock_judge.py`):
- `validate(..., sandbox_check=True)` refuses each pattern id. The #639 round-2 text is a fixture and must be refused as `git_remote_op`.
- It accepts the same instructions rewritten to use `git show isolated-base:<path>`.
- It never checks `operator_instructions` or `answer`.
- `sandbox_check=False` matches today's output byte for byte.

Phase 1 (integration-style, using the existing `MOCK_UNBLOCK_JUDGE_JSON` harness):
- First mock refused, then `MOCK_UNBLOCK_JUDGE_JSON_REASK` accepted: one `outcome=reasked` line and the verdict acted on.
- Both mocks refused: `outcome=refused_after_reask` and the existing `invalid_verdict` wait marker.
- Kill switch `false`: no check and no re-ask.

Phase 2 (in `tests/test_linked_pr_implementation_guard.py`, which already runs the shell functions against PR JSON):
- Accepted: `Implements #639` with base `orchestrator/project-579`; `implements shubhodeep1/<repo>#639`; the issue URL form; base equal to the default branch.
- Rejected: `Refs #639`; `Implements #6390`; `Implements other/repo#639`; base `feature/x` (`reason=base_not_target`); default branch unreadable and base `feature/x` (`reason=default_branch_unknown`).
- `ai/issue-639` and `Closes #639` behave as today.
- `ORCH_IMPLEMENTS_MARKER_ENABLED=false` ignores the marker.
- `tests/test_claude_md_section_numbers.py` and `tests/test_lint_pr_body_auto_close.py` stay green (§19 lint unaffected).

Phase 3:
- A shell test sources the new `run_plan_codex.sh` block with a stub `git`: fetch success exports the ref, fetch failure warns and leaves it unset, `PLAN_ISOLATED_BASE_ENABLED=false` skips, and an invalid branch name (`a..b`) skips.
- `tests/test_codex_isolated_workspace.py` already covers `seed-git` with a base ref; no change.
- `tests/test_workflow_file_size_limit.py` stays green.

All phases:
- `python3 scripts/assemble_agents.py check --repo-root .`
- `python3 scripts/assemble_changelog.py --repo-root . --dry-run assemble`
- The repo's CI (`ci.yml`)

## Risks & Mitigations

- **Pattern false positives.** An instruction that says "do not run `git fetch`" is refused. Mitigation: the re-ask note names the pattern id, and the prompt tells the judge never to mention these commands, so the rewrite drops the mention. ACCEPTED: refusing a harmless mention costs one model call; letting a real network instruction through costs a whole round.
- **Pattern false negatives** (a network step phrased in prose, e.g. "download the old file"): the prompt layer covers these. ACCEPTED: a deterministic check cannot parse every phrasing.
- **A second refusal still lands in `invalid_verdict` and waits ~6h.** Mitigation: unchanged existing behaviour, logged as `refused_after_reask` so it is visible.
- **`Implements #N` misuse** (a PR that does only part of the item writes it): the poller adopts and closes the item. Mitigation: it is an explicit opt-in a person types; the base guard limits it to the default branch and project branches; the close sweep keeps `_pr_json_base_is_target_merge`; and the wave judge still reviews the integration branch before project completion. ACCEPTED: the same trust level as `Closes #N` today.
- **The planner's fetch adds time and a failure point.** Mitigation: depth 1, single branch, failure only omits `isolated-base` (as in implement). ACCEPTED.
- **Consumers on `stable` without #7548:** the Phase 1 prompt says "when `git branch --list isolated-base` shows it", so it is accurate before and after the promotion.

## Rollout

- Each phase ships on merge to `main`, then to consumers on the next `stable` promotion and wrapper sync (§14, `.github/ai/consumer_repos.json`). No consumer wrapper changes.
- Kill switches, all default `true` and set to `false` as a repository variable to turn a phase off without a revert:
  - `UNBLOCK_INSTRUCTION_SANDBOX_CHECK_ENABLED` (Phase 1)
  - `ORCH_IMPLEMENTS_MARKER_ENABLED` (Phase 2)
  - `PLAN_ISOLATED_BASE_ENABLED` (Phase 3)

  Each is forwarded from a repository variable as `<NAME>: ${{ vars.<NAME> || 'true' }}`, in the same phase's PR:
  - `UNBLOCK_INSTRUCTION_SANDBOX_CHECK_ENABLED`: the `env:` of the `unblock_judge.yml` step that runs `scripts/unblock_judge.sh` (the one setting `TARGET_DIR="${GITHUB_WORKSPACE}/target"`);
  - `ORCH_IMPLEMENTS_MARKER_ENABLED`: both poll-step `env:` blocks in `orchestrate_poll.yml`, next to the existing `UNBLOCK_JUDGE_ENABLED: ${{ vars.UNBLOCK_JUDGE_ENABLED || 'true' }}` lines;
  - `PLAN_ISOLATED_BASE_ENABLED`: the `env:` of `plan.yml`'s "Run Codex planning" step, beside `PLAN_ISOLATED_BASE_BRANCH`.
- Signals after rollout:
  - `UNBLOCK_JUDGE ... op=instruction_sandbox_check` lines in unblock judge runs;
  - `LINKED_PR_IMPLEMENTS_MARKER outcome=accepted` in poller logs when a person's PR is linked;
  - `CODEX_ISOLATION ... role=plan outcome=fetched` in plan runs.
- Rollback per phase: kill switch, then revert the phase's PR if needed. No state migration.

## References

- fun-token-multi-chain#639 (blocked item), #662 (needs-human digest), #667 (the PR a person made), project #579
- Implement runs 38875907463 and 38920624589 (BLOCKED for lack of the baseline)
- coding-workflows #7548: `isolated-base` for the implement sandbox
- coding-workflows #7651: label repair keeps `ai:needs-human` (out of scope)
- Issue #3817 / PR #3825: why mention-only PRs are rejected
- `scripts/orchestrate_poll_process.sh`: `_pr_json_is_issue_implementation_pr`, `_pr_json_closes_issue`, `_pr_json_base_is_target_merge`
- `scripts/unblock_ledger.py` `validate()`, `scripts/unblock_judge.sh` `unblock_ask_model`
- `scripts/codex_isolated_exec.sh`, `scripts/codex_isolated_workspace.py` `seed-git`
- `prompts/mode-implement.txt` "Default-branch reference"
