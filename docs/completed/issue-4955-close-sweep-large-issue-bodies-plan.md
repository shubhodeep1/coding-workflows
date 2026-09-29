# Stream the close sweep's issue lists to jq instead of passing them as arguments

Source issue: shubhodeep1/coding-workflows#4955 (https://github.com/shubhodeep1/coding-workflows/issues/4955)
Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
Security pass: skip (ai:security: automation-produced issue)

## Summary

`close_merged_issues_sweep` builds its work queue by passing both `gh issue list` payloads, which since issue #4813 carry every issue's full body, to `jq` as `--argjson` command-line arguments. A few large bodies push one argument past the kernel's per-argument limit, `jq` fails to start, and the `|| echo "[]"` fallback turns the queue into an empty list, so the sweep silently closes nothing. This plan streams the payloads to `jq` through stdin and replaces the silent empty queue with a visible warning.

## Context

- Security audit finding `large-issue-bodies-disable-close-sweep` (issue #4955, `STRIDE: Denial of Service`, medium, confidence 10/10), filed by `.github/workflows/security-audit.yml` against the issue #4813 project branch. Location: `scripts/orchestrate_poll_process.sh:3918` on that branch (the `issues_json` dedup `jq` call, lines 3927-3940 at the time of writing).
- Issue #4813 (phase PR #4842, merged into the project branch) added `body` to both `gh issue list --json` field lists so the target-branch rule can read each issue's `Integration branch:` line. Before that, the payload held only numbers and labels and stayed far below the limit.
- Linux caps a single `argv` or environment string at `MAX_ARG_STRLEN` = 32 pages = 131,072 bytes. GitHub allows 65,536 characters per issue body, so two or three large bodies with the same label exceed it. Reproduced locally: three `ai:merged` issues with 61,440-byte bodies give a 184,506-byte `merged_json`; `jq -c -n --argjson merged "$M" …` fails with `/usr/bin/jq: Argument list too long`, and the `|| echo "[]"` fallback yields an empty queue. `printf '%s\n' "$M" '[]' | jq -c -n 'input as $merged | input as $ready | …'` returns all three.
- Anyone who can open or edit an issue that later carries `ai:merged` or `ai:ready-to-merge` can therefore switch the sweep off for every issue, with no log line saying so.
- Everything else in the sweep already moves the payload without `exec` arguments: `echo "${issues_json}" | jq …` (`echo` is a builtin), `printf '%s' "${_sweep_issue_body}" | grep …`, and `issue_body_integration_branch`, which pipes the body to `python3` on stdin (`scripts/gh_helpers.sh:1695-1712`). `issue_pr_status.yml`'s #4813 code also pipes bodies (`printf '%s' "${payload}" | jq …`). The dedup call is the only place where issue bodies travel in `argv`.

## Goals

- The dedup queue in `close_merged_issues_sweep` is built by a single `jq -n` that reads `merged_json` and `ready_json` from stdin (`[inputs] as $lists`, exactly two documents). No issue list or body is passed as a command-line argument. The queue it produces is the same as today's for every input that works today.
- When the queue cannot be built (a list is not valid JSON, or `jq` fails), the sweep logs `::warning::CLOSE_MERGED_SWEEP queue_build_failed merged_bytes=<n> ready_bytes=<n> — skipping this cycle.` and returns 0, instead of silently processing an empty queue.
- A regression test runs the poller with three open `ai:merged` issues whose bodies are 61,440 bytes each, each with a merged closing-keyword PR into the default branch, and proves that all three are closed and no `queue_build_failed` line is logged. It fails on the current code.
- A second test gives the sweep an unparseable `ai:ready-to-merge` list and proves that the warning is logged and nothing is closed.
- A `changelog.d/` fragment (§20, section `security`), and a README note on the `ENABLE_CLOSE_MERGED_ISSUES` row (§7).

## Non-goals

- The `gh issue list` fetch fallbacks (`gh_retry … || echo "[]"`) stay as they are (AD-3). A failed fetch cannot be triggered from issue content, and it was fail-open before #4813.
- Other `--argjson` call sites in the poller, such as the wave-status `jq` at line ~702 (`candidate_details_json`) and the tracking-comment `--arg body` at line ~2645. They predate #4813, are not part of this finding, and hold no list of issue bodies (AD-4).
- The per-issue `echo "${issues_json}" | jq …` reads in the sweep loop. They stream through a pipe, so the argument limit does not apply. Their cost grows with the payload size, but that is a performance question outside this finding (§5).
- `issue_pr_status.yml`: it already pipes bodies to `jq` and `python3`.

## Constraints

- §1: security first. The fix removes the attacker-controlled path to an empty queue and makes any remaining failure visible.
- §5 minimal change set: only the dedup call and its fallback change. The queue shape, the dedup rule (a `merged_label` origin wins when an issue carries both labels), and every log line stay the same.
- §6: no identifier is renamed. The `CLOSE_MERGED_SWEEP` prefix is kept. The new log key `queue_build_failed` does not appear anywhere in `scripts/`, `.github/`, or `tests/` today (checked). The new local `_sweep_queue_err` does not collide with any name in the function or the file.
- §9: the poller uses 2-space indentation in this function, so the edit keeps it (the file does not use tabs here).
- §14: `scripts/orchestrate_poll_process.sh` reaches consumer repos with the next `@stable` release. It needs no new input, env var, or helper.
- §15: no new GitHub API call. The fix changes only how the existing payloads reach `jq`.
- §19: every PR of this project uses `Refs #4955`. The base is not the default branch, so the final-merge stage closes #4955 explicitly with `ai:merged` (Issue Mode). The security audit tracker #3576 is referenced only as `Refs`.
- The poller runs under `set -euo pipefail` (`scripts/orchestrate_poll_process.sh:12`), and `close_merged_issues_sweep` is called bare at line ~23237. A non-zero return would abort the rest of the poll cycle, so the failure path returns 0 after logging (AD-2).

## Approach

Replace

```bash
issues_json="$(jq -c -n \
  --argjson merged "${merged_json:-[]}" \
  --argjson ready "${ready_json:-[]}" '
    def normalize($origin): …;
    …
  ' 2>/dev/null || echo "[]")"
```

with a stdin-fed call that keeps the same `normalize` definition and dedup expression, and reads the two lists as `[inputs]`:

```bash
if ! issues_json="$(printf '%s\n' "${merged_json:-[]}" "${ready_json:-[]}" | jq -c -n '
    def normalize($origin): …;
    [inputs] as $lists
    | if ($lists | length) != 2 then error("expected 2 issue lists, got \($lists | length)") else . end
    | ($lists[0] | normalize("merged_label")) as $m
    | ($lists[1] | normalize("ready_label")) as $r
    | …
  ' 2>"${_sweep_queue_err}")"; then
  echo "::warning::CLOSE_MERGED_SWEEP queue_build_failed merged_bytes=<wc -c> ready_bytes=<wc -c> — skipping this cycle. jq: <first 300 bytes of stderr>"
  rm -f "${_sweep_queue_err}"
  return 0
fi
```

`printf` is a shell builtin, so the payload never goes through `execve` and the argument limit does not apply. `jq` must read exactly two documents, the merged list and then the ready list. If a list is not valid JSON, or a payload holds an extra document (for example a partial `gh` output followed by the `|| echo "[]"` fallback), `jq` exits non-zero and the new branch reports it, instead of silently shifting the ready list into the merged slot. The later `count`, `merged_count`, and `ready_count` reads already use `echo … | jq` and stay unchanged.

Alternatives: temp files with `--slurpfile` (AD-1 B) need two files and cleanup for the same result; and dropping `body` from the list to fetch each body on its own (AD-1 C) would add one API call per issue, which §15 forbids.

## Phases & Merge Strategy

**Single phase.** Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the finding is one call site, and its fix, its failure reporting, and their tests are one change.

1. **Phase 1 — stream the close sweep's queue build through stdin and surface its failures.**
   - Files: `scripts/orchestrate_poll_process.sh`, `tests/test_orchestrate_poll_process.py`, `README.md`, `changelog.d/4955-close-sweep-large-issue-bodies.md` [new]. No `.claude/**` path.
   - Done when: the new large-body test and the new unparseable-list test pass, every existing `close_merged_issues_sweep` test and `tests/test_linked_pr_implementation_guard.py` still pass, `bash -n scripts/orchestrate_poll_process.sh` is clean, and the README row and changelog fragment describe the behaviour.
   - Rollback: revert the phase PR. `ENABLE_CLOSE_MERGED_ISSUES=false` stays the emergency stop for the sweep.

## Implementation Steps

Phase 1:

1. `scripts/orchestrate_poll_process.sh` `close_merged_issues_sweep` (dedup block, lines ~3924-3940):
   - declare `local _sweep_queue_err` and `_sweep_queue_err="$(mktemp)"` before the call;
   - replace the `--argjson` call with the stdin-fed `jq -n` from [Approach](#approach), keeping the filter body unchanged apart from the leading `input` bindings;
   - on failure, log the `queue_build_failed` warning with both payload sizes and up to 300 bytes of `jq`'s stderr, remove the temp file, and `return 0`; on success, remove the temp file;
   - extend the function's header comment (the API hygiene paragraph) with one sentence: the lists reach `jq` on stdin because issue bodies can exceed the per-argument limit (issue #4955), and a queue that cannot be built is logged as `queue_build_failed` and skips the cycle.
2. `tests/test_orchestrate_poll_process.py`:
   - harness: the label-filtered `gh issue list` mock reads an optional `mock_gh_issue_list_raw_by_label` store key (set through the existing `mock_store_extra` argument, so `_run_poller` gets no new parameter). For the sweep's `--json number,labels,body` listing of a label present in it, it prints that raw text instead of the JSON list. Other listings of the same label (stall recovery lists `--json number`) and the default are unchanged;
   - new `test_close_merged_issues_sweep_closes_issues_with_large_bodies`: issues 10, 11, 12 with `ai:merged` and 61,440-byte bodies, each linked to its own merged PR into `main` whose body says `Closes #<n>`. Assert all three are in `closed_issues`, the three `CLOSE_MERGED_SWEEP issue=<n> pr=<p> origin=merged_label status=closed` lines are logged, and `queue_build_failed` is not;
   - new `test_close_merged_issues_sweep_surfaces_unparseable_issue_list`: issue 10 `ai:merged` with a merged closing PR into `main`, and `mock_gh_issue_list_raw_by_label={"ai:ready-to-merge": "not json"}`. Assert `::warning::CLOSE_MERGED_SWEEP queue_build_failed` is logged and #10 is not closed.
3. `README.md` `ENABLE_CLOSE_MERGED_ISSUES` row (line 200): add one sentence after the API hygiene clause: both lists reach `jq` on stdin, because issue bodies can exceed the per-argument limit (issue #4955); a queue that cannot be built is logged as `::warning::CLOSE_MERGED_SWEEP queue_build_failed …` and the sweep skips that cycle instead of treating it as empty.
4. `changelog.d/4955-close-sweep-large-issue-bodies.md` [new], `<!-- changelog: security -->`, following §20.D/E.

## Files & Modules

- `scripts/orchestrate_poll_process.sh`
- `tests/test_orchestrate_poll_process.py`
- `README.md`
- `changelog.d/4955-close-sweep-large-issue-bodies.md` [new]

## Tests

- Unit/integration (pytest harness, run directly as `python3 tests/test_orchestrate_poll_process.py <names>`): the two new tests, every existing `test_close_merged_issues_sweep_*` test, `tests/test_linked_pr_implementation_guard.py`, and `tests/test_issue_pr_status_target_branch_gate.py`.
- The large-body test must fail on the unpatched script: run it before the fix (expected to fail, with no `status=closed` lines) and after (passes).
- Static: `bash -n scripts/orchestrate_poll_process.sh`; `tests/test_workflow_file_size_limit.py` (no workflow file changes, run as a guard).
- CI picks the new tests up automatically: `ci.yml` "Derive orchestrate poll test subsets" collects every `test_*` function in the module.
- End to end: the chain's conformance and runtime validation stages. The security pass is skipped because this issue is itself an automation-produced `ai:security` follow-up (see the header).

## Risks & Mitigations

- A `jq` filter that behaves differently once it moves behind `input` bindings. Mitigation: the filter body is unchanged, and the existing sweep tests (both origins, dedup when both labels are present, mention-only and target-branch rejection) run against the new call.
- A `gh issue list` output with trailing text after the JSON array would now fail the build instead of being silently read as an empty queue. ACCEPTED: that is the reporting the finding asks for, and the warning names both payload sizes.
- Skipping one cycle on a build failure delays closures until the next cycle. ACCEPTED: the old behaviour skipped them silently, and the poller runs again on its own schedule.

## Rollout

Lands on the #4813 project branch with this project's final PR, reaches `main` with #4813's final PR (#4826), then reaches consumers on the next `@stable` release (§14). No flag and no migration. `ENABLE_CLOSE_MERGED_ISSUES=false` stays the emergency stop.

## Auto-decisions

- AD-1 [plan, 2026-09-29] How should the dedup queue receive the two issue lists? — Picked: A — stream both lists to one `jq -n` on stdin (`printf '%s\n' … | jq -n '[inputs] as $lists | …'`). Alternatives: B — write each list to a temp file and pass `--slurpfile`; C — drop `body` from `gh issue list` and fetch each issue's body separately. Why: A is the smallest change with no temp files and no argument limit; C adds one API call per issue (§15) and undoes #4813's no-extra-call design. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What should the sweep do when the queue cannot be built? — Picked: A — log `::warning::CLOSE_MERGED_SWEEP queue_build_failed merged_bytes=<n> ready_bytes=<n> — skipping this cycle.` with the first 300 bytes of `jq`'s stderr, and return 0. Alternatives: B — return non-zero; C — process whichever list still parses. Why: the poller runs under `set -e` and calls the sweep bare, so B would abort the whole poll cycle; C mixes label-origin policies when the `ai:merged` list is the broken one. A surfaces the failure, as the finding recommends, without widening the blast radius. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Should the `gh issue list` fetch fallbacks (`|| echo "[]"`) also report failures? — Picked: A — no, leave them unchanged. Alternatives: B — add a warning on fetch failure too. Why: the finding concerns parsing issue content; a fetch failure cannot be triggered from issue content and was fail-open before #4813 (§5). Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-29] Should other large `--argjson` / `--arg` call sites in the poller change too? — Picked: A — no; they are listed under Non-goals. Alternatives: B — convert every large `jq` argument in the file to stdin. Why: they predate #4813, hold no list of issue bodies, and are outside this finding (§5); B would touch many unrelated paths in one security follow-up. Applied in: no code change. Status: pending review

## References

- Issue #4955 (this finding); security audit tracker #3576.
- Issue #4813, phase PR #4842, final PR #4826 (the project branch this plan builds on).
- `scripts/gh_helpers.sh` `issue_body_integration_branch`; `.github/workflows/issue_pr_status.yml` (already stdin-fed).
- Linux `execve(2)`: `MAX_ARG_STRLEN` (32 × page size) per argument or environment string.
