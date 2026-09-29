# Implement-Plan Log — Stream the close sweep's issue lists to jq instead of passing them as arguments

- Plan: docs/plans/issue-4955-close-sweep-large-issue-bodies-plan.md
- Source issue: shubhodeep1/coding-workflows#4955
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4955-close-sweep-large-issue-bodies   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened from claude/implement-plan-issue-4813-close-sweep-target-branch-merges; phase 1 starting.

## Phases
1. [ ] Phase 1 — stream the close sweep's queue build through stdin and surface its failures (orchestrate_poll_process.sh + tests + README + changelog)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How should the dedup queue receive the two issue lists? — Picked: A — stream both lists to one `jq -n` on stdin (`printf '%s\n' … | jq -n 'input as $merged | input as $ready | …'`). Alternatives: B — write each list to a temp file and pass `--slurpfile`; C — drop `body` from `gh issue list` and fetch each issue's body separately. Why: smallest change with no temp files and no argument limit; C adds one API call per issue (§15). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What should the sweep do when the queue cannot be built? — Picked: A — log `::warning::CLOSE_MERGED_SWEEP queue_build_failed merged_bytes=<n> ready_bytes=<n> — skipping this cycle.` with the first 300 bytes of `jq`'s stderr, and return 0. Alternatives: B — return non-zero; C — process whichever list still parses. Why: the poller runs under `set -e` and calls the sweep bare, so B aborts the poll cycle; C mixes label-origin policies. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Should the `gh issue list` fetch fallbacks (`|| echo "[]"`) also report failures? — Picked: A — no, leave them unchanged. Alternatives: B — add a warning on fetch failure too. Why: not triggerable from issue content, fail-open before #4813 (§5). Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-29] Should other large `--argjson` / `--arg` call sites in the poller change too? — Picked: A — no; listed under the plan's Non-goals. Alternatives: B — convert every large `jq` argument in the file to stdin. Why: they predate #4813, hold no list of issue bodies, and are outside this finding (§5). Applied in: no code change. Status: pending review

## Lessons

## Notes
- Security pass skipped per the plan header: `security_pass_skip.py` verified #4955 as an automation-produced `ai:security` follow-up (`ai:security: created and labelled by the issue automation`).
- Issue base's own final PR #4826 (into `main`) was open (draft) at project start; the base has not merged.
- Progress comment on #4955: id 5883203945.
