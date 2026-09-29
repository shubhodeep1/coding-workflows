# Separate release track for Claude changes (`claude-stable`)

## Summary

Give Claude changes their own release track: a `claude-stable` branch and
immutable `claude-vX.Y.Z` tags, cut automatically from `main` behind a
static and unit gate, so Claude fixes stop waiting on (and being blocked by)
the workflow E2E gate, and a Claude-only change no longer spends a workflow
release cycle.

## Context

Today there is one release track. `promote-main-to-stable.yml` (daily `cycle`
job, `scripts/promote_main_cycle.sh`) runs `test-and-mark-stable.yml` as a
`gate_only` smoke gate on `main`, a proving apply-analysis run, then
fast-forwards `stable`, tags `vX.Y.Z`, and sends
`coding-workflows-stable-released` to every repo in
`.github/ai/consumer_repos.json` (13 today). Consumers then run
`update_workflows.yml`, which renders wrapper pins to the `stable` commit and
copies three more things from that same commit: `workflow-templates/.claude/**`
(the `claude_sync` step, `update_workflows.yml:190-249`), root `CLAUDE.md`
through the `workflow-templates/CLAUDE.md` symlink (`claude_md_sync`,
`:251-292`), and changelog assets. `/seed-repo` copies `.claude/` and
`CLAUDE.md` from the `@stable` release too.

So a change that only touches Claude files waits for, and can be blocked by,
the live E2E gate, which does not exercise any of it; and a workflow
regression holds back Claude fixes. `promote_main_cycle.sh:147-156`
(`is_code_path`) even counts `CLAUDE.md` and `.claude/` as code, so a
Claude-only merge costs a full smoke gate and a proving run.

Claude logic also runs inside Actions. Consumers call the reusable workflows at
a pinned `stable` commit, and those workflows stage Claude scripts from their
own support commit: `clarify.yml` stages `claude_issue_route.py` and
`claude_issue_handoff.sh` (`clarify.yml:162-235`, resolving `SCRIPT_REF=stable`
by name); `review_autofix.yml` carries the Claude-fixer gate inline
(`review_autofix.yml:720-813`) and stages
`review_autofix_step_claude_fixer_handoff.sh` through
`scripts/stage_workflow_support.sh`; `issue_pr_status.yml` stages
`ingest_implement_plan_lessons.py` (`issue_pr_status.yml:144`, used at
`:543-568`).

Decisions taken in the clarification round (session of 2026-09-27):

| Q | Decision |
|---|---|
| Q17 | Separate plan, implemented **before** `docs/plans/move-checking-roles-to-claude-plan.md`, so that plan's Claude side ships on this track |
| Q18 | The track covers `.claude/**`, `workflow-templates/.claude/**`, `CLAUDE.md` **and** the Actions-side Claude plumbing |
| Q19 | New `claude-stable` branch + `claude-vX.Y.Z` tags; consumer sync and `/seed-repo` take Claude assets from the Claude track; wrappers keep `stable` |
| Q20 | Gate = static + unit checks only (no live E2E) |
| Q21 | Release automatically on each merge to `main` that touches Claude paths |
| Q22 | Each Claude release records a minimum workflow release; consumers hold a Claude update until their workflow release meets it; the gate checks the required release exists on `stable` |
| Q23 | When everything `main` has beyond `stable` is under Claude paths, the workflow promote skips its gate and cuts no workflow release |
| Q24 | `changelog.d/` fragments marked `<!-- channel: claude -->` fold into `CHANGELOG.md` under a `claude-vX.Y.Z` heading at Claude releases; workflow releases skip them |
| Q27 | Claude logic in shared workflows moves into scripts that the workflow fetches at run time from the newest compatible `claude-v*` release, verified to be on `claude-stable` |
| Q28 | Claude workflows that run only in coding-workflows keep running from `main` |
| Q31 | Gate parity check: byte-identity for `hooks/`, `.claude/scripts/`, `settings.json`, `CLAUDE.md`; commands exempt (tailored per side); template command references must resolve |
| Q32 | Ordered phases accepted: P1 first, then P2–P4 in any order |

## Automation surface (CLAUDE.md §18.E)

| Item | This plan |
|---|---|
| New or extended scripts | New: `scripts/claude_track.py` (P1), `scripts/resolve_claude_support.sh` (P3), `scripts/review_autofix_step_claude_fixer_gate.sh` (P3, a verbatim move out of `review_autofix.yml`). Extended: `scripts/assemble_changelog.py` (P1), `scripts/stage_workflow_support.sh` (P3), `scripts/promote_main_cycle.sh` (P4). No script needs a manual run (§18.A). |
| Scheduler / PR-push entry points | New `.github/workflows/claude-release.yml`: `push` to `main` filtered to the Claude-track paths, a daily fallback `schedule`, `workflow_dispatch`, and a dispatch from the `release` jobs of `.github/workflows/test-and-mark-stable.yml` and `.github/workflows/mark-stable.yml`. Consumers: `.github/workflows/update_workflows.yml`, triggered by `workflow-templates/ai-update-workflows.yml` (daily `0 4 * * *` cron, `coding-workflows-stable-released`, and the new `coding-workflows-claude-released` dispatch). Shared workflows: the existing support-staging steps of `review_autofix.yml`, `clarify.yml` and `issue_pr_status.yml`, which run on their existing triggers. Daily promote: the existing `cycle` job of `.github/workflows/promote-main-to-stable.yml`. |
| Long-running supervisor (§18.C) | None new. `claude-release.yml` is an event-driven workflow with a daily backstop: each run starts, releases or skips, and exits. Restart and crash recovery come from the next push, dispatch or daily tick; concurrency group `claude-release` (no cancel) serialises runs; the kill switch is `CLAUDE_RELEASE_ENABLED`. |
| DB gate (§18.D) | Not applicable: no database operation. |
| §18.F registry | One entry in `docs/scripts-pending-removal.md` (P1) for `.github/workflows/claude-release.yml` + `scripts/claude_track.py`, type `supervisor` (matching the existing `scripts/auto_release_stable.sh` entry), removal trigger `permanent — review annually`. Removal preflight checks: `gh workflow view claude-release.yml -R shubhodeep1/coding-workflows` still shows the push, schedule and dispatch triggers; `git ls-remote origin refs/heads/claude-stable refs/heads/main` shows no Claude-track change on `main` since the latest `claude-v*` tag, or a replacement release path is live; `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_claude_track.py tests/test_claude_release_workflow_contract.py` exits 0. Owner: @shubhodeep1. |

## Goals

- G1. Every merge to `main` that changes a Claude-track path and passes the
  Claude gate produces a `claude-vX.Y.Z` tag on `claude-stable` and a GitHub
  Release, without running any live E2E job.
- G2. Consumers receive `.claude/**` and `CLAUDE.md` from the newest Claude
  release whose declared minimum workflow release is at or below the
  workflow release they are being synced to; `/seed-repo` does the same.
- G3. Shared reusable workflows run their Claude scripts from the newest
  compatible, `claude-stable`-verified Claude release, and fall back to their
  own verified support commit when none exists.
- G4. The daily workflow promote cycle skips (`PROMOTE_CYCLE_SKIPPED
  reason=no_code_changes`) when every changed path since the `stable` tag is
  a Claude-track path.
- G5. Claude-channel changelog fragments land under a `claude-vX.Y.Z` heading
  and are never folded by a workflow release.
- G6. A Claude release is never cut from a commit whose declared minimum
  workflow release is not already tagged on `stable`.

## Non-goals

- Changing how workflow wrappers are pinned or released.
- A live end-to-end Claude smoke run (Q20).
- The routing label contract (`ai:claude` without `ai:codex` ⇒ Codex phases
  skip): the 5–7 line gates in `plan.yml:575-581`, `implement.yml:186-190`
  and the poller's `STALL_SKIP … reason=claude_routed`
  (`orchestrate_poll_process.sh:15799`) are the Codex pipeline's own skip
  rules. They stay inline on the workflow track.
- The `if:` conditions in `review_autofix.yml` that route jobs on the gate's
  `claude_fixer_mode` output. They stay in the YAML as the hook point;
  only the gate *classification* moves (P3).
- Hardening `clarify.yml`'s support-ref resolution for its **non-Claude**
  scripts (`SCRIPT_REF=stable` by name). Only the Claude scripts move to the
  strict resolver here.
- `auto-release-stable.yml`: it compares the `stable` branch to the `stable`
  tag, and that branch only moves through the promote (which P4 already
  gates) or an operator's manual promote, which stays an explicit override.
- Pinning the coding-workflows-only Claude workflows
  (`claude-issue-intake.yml`, `claude-issue-queue-watchdog.yml`, the
  `claude-pr-catch-all` job) to `claude-stable` (Q28).
- Deleting orphaned `.claude/` files in consumers (the sync never deletes
  today; unchanged).

## Constraints

- **§6 naming immutability.** No existing identifier is renamed or removed.
  New identifiers (checked unique on 2026-09-27 with a repo-wide grep, zero
  hits): branch `claude-stable`; tag prefix `claude-v`; workflow
  `.github/workflows/claude-release.yml`; script `scripts/claude_track.py`;
  resolver `scripts/resolve_claude_support.sh`; files
  `.github/ai/claude_track_paths.txt`, `.github/ai/claude_track_requires.json`;
  repository_dispatch event `coding-workflows-claude-released`; repo var
  `CLAUDE_RELEASE_ENABLED`; log prefixes `CLAUDE_TRACK_RELEASE`,
  `CLAUDE_TRACK_SYNC`, `CLAUDE_TRACK_SUPPORT`; changelog marker
  `<!-- channel: claude -->`; `assemble_changelog.py` flags `--channel` and
  `--heading`. `review_autofix.yml` step names, ids, outputs and log lines
  (`AUTOFIX_GATE_CLAUDE_FIXER*`, `AUTOFIX_GATE_SKIP
  reason=claude_fixer_awaiting_session`) keep their exact text.
- **§4 env defaults.** `CLAUDE_RELEASE_ENABLED` defaults to `true`;
  `assemble_changelog.py --channel` defaults to `all`, so every existing
  caller (including consumer `update_workflows.yml`) keeps its behaviour.
- **§14.** `.github/ai/consumer_repos.json` is the dispatch list for the new
  event; no consumer is added or removed.
- **§15 API budget.** Documented per new call site in Implementation Steps.
  The promote cycle reuses its existing compare payload (no new call).
- **§18.** No manual scripts. `claude-release.yml` runs on push, dispatch
  and a daily fallback schedule; it gets a `docs/scripts-pending-removal.md`
  entry (type `supervisor`, `permanent — review annually`), matching the
  existing `auto_release_stable.sh` entry.
- **§19.** PR bodies use `Refs #N`.
- **§20.** A `changelog.d/` fragment ships with P1 (and with each later phase
  that changes consumer-visible behaviour). §20 itself is updated for the new
  marker (P1).
- **§21 / §23.** Release pushes and tag writes run in Actions with `GH_PAT`,
  never from a session.
- **§27.** `review_autofix.yml` is 451,394 bytes (28.6 KB under the 480,000
  guard). P3 only removes inline lines from it; `test-and-mark-stable.yml`
  (350,650 bytes) and `mark-stable.yml` gain one small step each.
- **Security.** Code fetched at run time from the Claude track executes with
  the workflow's secrets. It is accepted only from an immutable
  `claude-v*` tag whose commit is an ancestor of `claude-stable`, is
  re-verified after checkout, and is staged out of tree, mirroring the
  `review_autofix.yml:290-352` / `security-audit.yml:49-131` method, not
  `clarify.yml`'s by-name `stable` ref.

## Approach

**Track definition.** `.github/ai/claude_track_paths.txt` lists the Claude
track's path globs, one per line (Q18):

```
CLAUDE.md
workflow-templates/CLAUDE.md
.claude/**
workflow-templates/.claude/**
scripts/claude_*
scripts/review_autofix_step_claude_fixer_*.sh
scripts/ingest_implement_plan_lessons.py
.github/workflows/claude-issue-intake.yml
.github/workflows/claude-issue-queue-watchdog.yml
.github/workflows/claude-release.yml
.github/ai/claude_track_paths.txt
.github/ai/claude_track_requires.json
tests/test_claude_*
tests/test_check_in_status*.py
tests/test_stale_routines.py
tests/test_pr_merge_status_guard.py
tests/test_pr_watch_guard.py
tests/test_pr_check_in_reminder.py
tests/test_gh_api_write_guard.py
tests/test_implement_plan_claude_command.py
tests/test_implement_issue_claude_command.py
tests/test_review_autofix_claude_fixer_mode.py
tests/test_claude_md_section_numbers.py
```

`scripts/claude_track.py` is the single reader of that file, so no second
glob dialect exists. It does **not** use `fnmatch` (whose `*` also matches
`/` and which has no recursive `**`). It uses its own small matcher,
covered by unit tests: patterns and paths are split on `/`; `**` as a whole
segment matches zero or more segments; within a segment `*` matches any run
of characters except `/` and `?` matches one such character; everything
else is literal. So `.claude/**` matches `.claude/hooks/x.py`, while
`scripts/claude_*` matches `scripts/claude_pr_sweep.py` but not
`scripts/claude_x/y.py`. `**` is valid only as a whole segment, in any
position (`a/**/b` matches `a/b` and `a/x/y/b`; `**/test_x.py` matches it at
any depth). A pattern with `**` inside a segment (`a**b`, `x/**.py`) is
invalid: `check` fails on it and `classify` exits non-zero rather than guess.
`changelog.d/**` and `CHANGELOG.md` are neutral: they never make a change
"workflow" or "Claude".

**Compatibility.** `.github/ai/claude_track_requires.json` holds
`{"min_workflow_version": "vX.Y.Z"}`, edited by any PR whose Claude change
needs a workflow release. Initial value: the `stable` release current when
P1 merges. A Claude release is cut only when that tag exists and its commit
is an ancestor of the `stable` branch (G6). Consumers and workflows read the
file at each candidate `claude-v*` tag to pick the newest compatible release.

**Version order.** Every comparison in this plan ("newest", "latest", `min ≤
workflow version`, the patch bump) is numeric, never lexical:
`claude_track.py` parses workflow tags with `^v([0-9]+)\.([0-9]+)\.([0-9]+)$`
and Claude tags with `^claude-v([0-9]+)\.([0-9]+)\.([0-9]+)$` and compares the
three captures as an integer tuple, so `v1.10.0` is newer than `v1.9.0`. A tag
that does not match its pattern (including the peeled `^{}` lines of `git
ls-remote`) is skipped and logged under the caller's prefix
(`CLAUDE_TRACK_RELEASE`, `CLAUDE_TRACK_SYNC` or `CLAUDE_TRACK_SUPPORT`) with
`skip reason=unparsed_tag`, and the order `git ls-remote` returns is never
relied on.

**Release.** `claude-release.yml` triggers on `push` to `main` filtered to the
Claude globs (the YAML `paths:` list is pinned to the globs file by a contract
test), on `workflow_dispatch` (optional `version_tag`), on a daily fallback
schedule, and on a dispatch from the workflow release jobs (so a release held
for G6 is retried as soon as the workflow release it waits on exists). The
gate runs the Claude unit tests and `claude_track.py check`. The release job
releases `main`'s tip at gate time (`T`): it skips if `claude-stable` already
contains `T`; fast-forwards `claude-stable` to `T` (ff-only; the branch only
ever takes `main` commits); tags `claude-vX.Y.Z` at `T` (patch bump from the
latest `claude-v*`; first release `claude-v1.0.0`; `version_tag` overrides);
creates the GitHub Release; dispatches `coding-workflows-claude-released`
(`version`, `sha`) to every registered consumer. Changelog assembly runs last
as a separate commit to `main` (`--channel claude --heading claude-vX.Y.Z`),
so the tagged commit is exactly the tested one; if that push fails the
fragments simply wait for the next release.

**Consumers (P2).** `update_workflows.yml` already resolves `UPSTREAM_SHA`
from `refs/tags/stable`. It now resolves that commit's `vX.Y.Z` tag (the
workflow release being applied), then runs `claude_track.py resolve` to pick
the newest `claude-v*` tag whose `min_workflow_version` ≤ that version and
whose commit is on `claude-stable`, and copies `.claude/` and `CLAUDE.md`
from that tag. No compatible release, or any resolution error, falls back to
today's copy from `UPSTREAM_SHA` and logs `CLAUDE_TRACK_SYNC
source=stable_fallback reason=<…>`. The consumer wrapper
`workflow-templates/ai-update-workflows.yml` also listens for
`coding-workflows-claude-released`.

**Shared workflows (P3).** `scripts/resolve_claude_support.sh` (workflow
track, since it is the hook) runs after the workflow's existing support
resolution. From the caller identity it already validated:
same-repo `refs/heads/main` → use the run's own checkout (Q28 parity: this
repo runs Claude code from `main`); `refs/tags/stable` or a consumer's
40-hex pin → map the support commit to its `vX.Y.Z` tag, resolve the newest
compatible `claude-v*` release, check out that exact commit into
`.claude-support-src`, re-verify `HEAD`, and stage the requested Claude
scripts out of tree. It outputs `claude_support_sha` and
`claude_support_source` (`claude_release` | `support_fallback`) and logs
`CLAUDE_TRACK_SUPPORT`. `review_autofix.yml`'s gate classification
(`:720-813`) moves verbatim into
`scripts/review_autofix_step_claude_fixer_gate.sh`, sourced by the same step
so the variables and outputs are unchanged.

**Workflow track skip (P4).** `promote_main_cycle.sh` asks
`claude_track.py classify` about each path from the compare payload it
already fetches; Claude-track paths count as non-code.

Alternatives rejected: a fast lane on the single `stable` branch (Q19 B,
still couples the tracks); a separate repository (Q19 C, splits history and
the review pipeline); fetching Claude scripts from a moving ref by name
(fails the security constraint).

## Decisions

### D1 — Tag the tested commit; fold the changelog afterwards

- **Chosen:** tag `claude-vX.Y.Z` at the gated `main` commit and push the
  assembled changelog to `main` as a separate follow-up commit.
- **Alternatives considered:** commit the changelog onto `claude-stable` before
  tagging (as the workflow release does on `stable`).
- **Why:** `claude-stable` stays a pure fast-forward of `main`, so no second
  forward-merge workflow is needed and the tag is exactly what the gate tested.

### D2 — Declared minimum workflow release, not "current stable"

- **Chosen:** authors set `min_workflow_version` in
  `.github/ai/claude_track_requires.json`.
- **Alternatives considered:** record the `stable` version current at release time.
- **Why:** only the author knows when a Claude change depends on an unreleased
  workflow change; recording "current" would demand the newest workflow
  release even when not needed and miss real dependencies.

### D3 — Fall back to today's source, never block

- **Chosen:** consumers and workflows fall back to the `stable`/support commit
  when no compatible Claude release exists or resolution fails.
- **Alternatives considered:** fail closed.
- **Why:** the fallback is the already-trusted path, so rollout needs no flag day
  and a Claude-track outage cannot stop the workflow pipeline.

### D4 — Routing label gates stay on the workflow track

- **Chosen:** keep the `claude_routed` skip gates inline.
- **Alternatives considered:** move them into `claude_issue_route.py`.
- **Why:** they are the Codex pipeline's own skip rule on a label contract, a few
  lines each; moving them would make Codex phases depend on the Claude track.

## Phases & Merge Strategy

The user accepted ordered phases (Q32). P1 ships the track and the resolver
code that the later phases call; P2, P3 and P4 each need P1 merged and do not
depend on each other. Each later phase falls back to today's behaviour when no
compatible Claude release exists, so no phase breaks production when merged.
Under `/implement-plan-claude` the phases merge in order into one project
branch and reach `main` together in the final PR.

1. **P1 — Claude track, gate and release workflow.** Adds the path list,
   `claude_track.py` (`classify`, `check`, `next-version`, `resolve`), the
   requires file, `claude-release.yml`, the changelog `channel` support and
   the workflow-release trigger. Consumers are unaffected: nothing reads
   `claude-stable` yet.
   - Done when: CI green; a dispatch of `claude-release.yml` on `main` cuts
     `claude-v1.0.0` on `claude-stable` (post-merge activation, see Rollout);
     workflow releases pass `--channel workflow`.
   - Rollback: revert the phase; delete `claude-stable` and `claude-v*`
     (ask-first, §23.C). No consumer ever read them.
2. **P2 — Consumers and `/seed-repo` take Claude assets from the track.**
   - Done when: `tests/test_update_workflows_claude_track.py` passes; a
     consumer sync after the next `@stable` release logs
     `CLAUDE_TRACK_SYNC source=claude_release tag=claude-v…`.
   - Rollback: revert; the sync returns to copying from `UPSTREAM_SHA`.
3. **P3 — Shared workflows load Claude scripts from the track.**
   - Done when: resolver tests pass; `review_autofix.yml` shrinks and its
     existing Claude-fixer tests pass unchanged; a consumer review run logs
     `CLAUDE_TRACK_SUPPORT source=claude_release`.
   - Rollback: revert; workflows stage Claude scripts from their own support
     commit again.
4. **P4 — Workflow promote ignores Claude-only changes.**
   - Done when: `tests/test_promote_main_cycle.py` shows a Claude-only
     compare payload skips with `reason=no_code_changes`.
   - Rollback: revert the `is_code_path` change.

## Implementation Steps

### P1 — Claude track, gate and release workflow

1. `.github/ai/claude_track_paths.txt` [new] — the glob list above.
2. `.github/ai/claude_track_requires.json` [new] —
   `{"min_workflow_version": "<current stable vX.Y.Z at merge time>"}`.
3. `scripts/claude_track.py` [new] — stdlib only, tabs, stable prefixes:
   - `classify --paths-file <file>` / `classify --path <p>…`: prints
     `claude`, `neutral` or `workflow` per path and an overall verdict
     (`claude_only` when every non-neutral path is Claude; `none` when all
     are neutral).
   - `check --repo-root .`: (a) `.claude/settings.json` and the template copy
     parse as JSON and every hook `command` under `.claude/hooks/` exists;
     (b) Q31 parity: byte-identity of every file under `.claude/hooks/`,
     `.claude/scripts/`, `.claude/settings.json` with its
     `workflow-templates/.claude/` twin, and `workflow-templates/CLAUDE.md`
     resolving to root `CLAUDE.md`; (c) every `.claude/hooks/…` or
     `.claude/scripts/…` path named in a template command exists in the
     template tree; (d) `claude_track_requires.json` is valid and its tag
     matches `^v[0-9]+\.[0-9]+\.[0-9]+$`; (e) every `changelog.d/*.md` has at
     most one valid `channel` marker.
   - `requires-released --repo <slug>`: exit 0 when the required tag exists
     and its commit is an ancestor of `stable` (one `git ls-remote --tags`,
     one compare call); exit 3 (`held`) otherwise.
   - `next-version`: the numerically newest `claude-v*` tag (Version order)
     + 1 patch, or `claude-v1.0.0` when none parses.
   - `resolve --workflow-version vX.Y.Z --repo <slug>`: one
     `git ls-remote --tags origin 'claude-v*'`, sorted newest first by the
     integer tuple (Version order); for each
     candidate one shallow `git fetch` and `git show <tag>:.github/ai/claude_track_requires.json`;
     first candidate with `min ≤ workflow version` (integer tuples) whose
     commit is an ancestor of `claude-stable` (one compare call) wins. Prints
     `{"tag","sha","min_workflow_version"}` or exits 3 when none. Budget:
     ≤ 1 API call per candidate tried, capped at 10 candidates.
4. `scripts/assemble_changelog.py` — `parse_fragment` also accepts an
   optional `<!-- channel: claude -->` line directly before or after the
   category marker (returns `(category, channel, body)`); new
   `assemble --channel {all,workflow,claude}` (default `all`) filters
   fragments; new `--heading <text>` (only with `--channel claude`) inserts
   `## [<text>] - <date>` with category subsections directly below the
   `## [Unreleased]` block instead of into it. Only the selected fragments
   are deleted.
5. `.github/workflows/mark-stable.yml` and
   `.github/workflows/test-and-mark-stable.yml` (`changelog_assemble` step,
   `:5576`) — pass `--channel workflow`; after a successful `release` job,
   one step runs `gh workflow run claude-release.yml --ref main` (fail open,
   `::warning::`).
6. `.github/workflows/claude-release.yml` [new] — triggers above;
   `concurrency: claude-release` (no cancel); kill switch
   `vars.CLAUDE_RELEASE_ENABLED` (default `true`).
   - Job `gate`: checkout `main` tip `T`; ruff + `python3 -m py_compile` on
     Claude-track Python; `bash -n` + shellcheck on Claude-track shell;
     pytest on the Claude test files listed in the paths file plus
     `tests/test_claude_track.py`, `tests/test_assemble_changelog.py`,
     `tests/test_changelog_fragment_contract.py`; `claude_track.py check`;
     `claude_track.py requires-released` (exit 3 → job output
     `held=true`, logged `CLAUDE_TRACK_RELEASE outcome=held
     min_workflow_version=…`, not a failure).
   - Job `release` (needs `gate`, skipped when held): skip if
     `claude-stable` contains `T` (`CLAUDE_TRACK_RELEASE outcome=up_to_date`);
     create `claude-stable` at `T` if absent, else `git push origin
     T:refs/heads/claude-stable` ff-only (refuse and alert if not a
     fast-forward); create the annotated tag with the same
     `publish_tag_with_remote_verification` approach as
     `test-and-mark-stable.yml:5650-5723`; `gh release create` with notes
     from the `claude` fragments; dispatch
     `coding-workflows-claude-released` with `client_payload[version]`,
     `client_payload[sha]` to each repo in `consumer_repos.json`
     (1 call per consumer, same as the workflow release).
   - Job `changelog` (needs `release`): on a fresh `main` checkout, run
     `assemble --channel claude --heading claude-vX.Y.Z`, commit
     `chore: assemble Claude changelog for claude-vX.Y.Z`, push to `main`
     with the 4-attempt backoff; on a moved `main`, re-run on the new tip
     (the commit only touches `CHANGELOG.md` and `changelog.d/`).
   - Failure of any job → Telegram ERROR via `scripts/tg_helpers.sh`
     (`TG_BOT_SECRET`), `CLAUDE_TRACK_RELEASE outcome=failed stage=<job>`.
7. `tests/test_claude_track.py` [new], `tests/test_claude_release_workflow_contract.py`
   [new] (triggers, `paths:` equal to the globs file, concurrency, kill
   switch default, ff-only push, no `--force`), updates to
   `tests/test_assemble_changelog.py` (channel parsing, filter, heading
   insertion, default `all` unchanged). Add them to `ci.yml` and to the
   `validate-scripts` job lists of `test-and-mark-stable.yml` and
   `mark-stable.yml`.
8. Extend `tests/test_pr_merge_status_guard.py::test_template_copies_are_identical`
   — or add `tests/test_claude_template_parity.py` [new] calling
   `claude_track.py check` — so the Q31 parity runs in normal CI too.
9. Docs: README "Claude release track" section (flow, vars, log prefixes,
   failure modes, first-release activation); `agents.md` workflow
   architecture item 16 and stable log prefixes; CLAUDE.md §20.B/§20.C
   (channel marker; which job folds which channel) — text only, no section
   renumbering (§6); `docs/scripts-pending-removal.md` entry for
   `claude-release.yml` + `scripts/claude_track.py`; `docs/INVENTORY.md`
   and the agents.md repo tree; `changelog.d/<pr>-claude-release-track.md`.

### P2 — Consumers and `/seed-repo`

10. `.github/workflows/update_workflows.yml` — after `fetch`: new step
    `claude_track_resolve` maps `UPSTREAM_SHA` to its `vX.Y.Z` tag (from the
    `git ls-remote --tags` output the step fetches once; the numerically
    newest when several point at it) and runs
    `claude_track.py resolve` from the upstream clone; outputs
    `claude_source_sha` and `claude_source_dir`.
    - Checkout: the step fetches the tag into the existing upstream clone
      (`git -C "${TMPDIR}/upstream" fetch --force --no-tags --depth 1 origin
      "refs/tags/<claude tag>"`), then builds a sparse, detached worktree of
      that exact commit at `${TMPDIR}/claude-track` in three explicit
      commands, because a plain `git worktree add` either checks out the full
      tree or (as git 2.43 does) copies the stable clone's `workflow-templates
      scripts` patterns, depending on the runner's git:
      `git -C "${TMPDIR}/upstream" worktree add --no-checkout --detach
      "${TMPDIR}/claude-track" <claude_source_sha>`;
      `git -C "${TMPDIR}/claude-track" sparse-checkout set
      workflow-templates/.claude` (cone mode, like the stable clone: repo-root
      files such as `CLAUDE.md` and the files directly in
      `workflow-templates/`, such as the `CLAUDE.md` symlink, come with it; a
      bare root-file pattern is rejected in cone mode);
      `git -C "${TMPDIR}/claude-track" checkout --detach <claude_source_sha>`
      (with `--no-checkout` the tree is empty until this runs). The sparse
      patterns are per-worktree (git enables `extensions.worktreeConfig` in
      the temp clone), so the stable checkout's patterns are unchanged. It then
      verifies `git -C "${TMPDIR}/claude-track" rev-parse HEAD` equals
      `claude_source_sha` and that `workflow-templates/CLAUDE.md` resolves.
      Any failure leaves both outputs empty.
    - It lives beside the stable checkout in the same `mktemp -d` directory
      (`${TMPDIR}/upstream` is untouched and still used for wrappers and
      scripts), so the runner's temp cleanup removes both; nothing is written
      into the consumer's workspace.
    - The existing `claude_sync` and `claude_md_sync` steps take their source
      root from `claude_source_dir` when it is set, and from
      `${TMPDIR}/upstream` exactly as today when it is empty. Step ids and
      outputs are unchanged; the commit body names the Claude tag.
11. `workflow-templates/ai-update-workflows.yml` — add
    `coding-workflows-claude-released` to `repository_dispatch.types`.
12. `.claude/commands/seed-repo.md` and its template twin — copy `.claude/`
    and `CLAUDE.md` from the resolved Claude release (fallback `@stable`),
    and say which in the seed PR body.
13. `tests/test_update_workflows_claude_track.py` [new] (resolution, hold,
    fallback, step ids unchanged); `tests/test_seed_repo_command.py` [new]
    (both copies name the Claude-release source and the `@stable` fallback).
14. README / agents.md sync section; changelog fragment.

### P3 — Shared workflows load Claude scripts from the track

15. `scripts/resolve_claude_support.sh` [new] — inputs:
    `WORKFLOW_SUPPORT_SHA`, `WORKFLOW_SUPPORT_REF` (already verified by the
    caller), requested script names. Behaviour as in Approach; fail closed on
    a tag whose commit is not on `claude-stable` or a `HEAD` mismatch; fall
    back (`support_fallback`) when no compatible release exists or the lookup
    errors. API budget: `claude_track.py resolve` (≤ 10 compare calls, in
    practice 1) once per job.
16. `scripts/stage_workflow_support.sh` — accept `--claude-src <dir>` so
    names in a new `CLAUDE_TRACK_SCRIPTS` list (`claude_issue_route.py`,
    `claude_issue_handoff.sh`, `ingest_implement_plan_lessons.py`,
    `review_autofix_step_claude_fixer_handoff.sh`,
    `review_autofix_step_claude_fixer_gate.sh`) are staged from it when
    given; unchanged otherwise.
17. `scripts/review_autofix_step_claude_fixer_gate.sh` [new] — the
    `review_autofix.yml:720-813` block moved verbatim (defines no new
    variables; relies on `gate_fetch_marker_comments` and the gate's
    variables, which stay in the YAML). The YAML sources it through the §27
    resolving wrapper; add it to `REQUIRED_BOOTSTRAP_SCRIPTS` and the
    §27 test registry.
18. `review_autofix.yml` (gate job and codex-agent job), `clarify.yml`
    ("Stage workflow support files"), `issue_pr_status.yml` (`:144`) — call
    `resolve_claude_support.sh` after their existing support resolution and
    stage the Claude scripts from its output.
19. Tests: `tests/test_resolve_claude_support.py` [new] (trust checks,
    fallback, same-repo main path); `tests/test_review_autofix_claude_fixer_mode.py`
    unchanged and passing; `tests/test_workflow_file_size_limit.py` passing.
20. README / agents.md ("Workflow file size limit" list, stable log prefixes);
    changelog fragment.

### P4 — Workflow promote ignores Claude-only changes

21. `scripts/promote_main_cycle.sh` — `code_changes_between` writes the
    compare payload's filenames to a temp file and calls
    `claude_track.py classify --paths-file`; Claude-track paths count as
    non-code. `is_code_path` keeps its current behaviour for any path the
    classifier does not mark Claude; a classifier failure fails **toward
    code** (a spurious cycle beats a missed one, matching the truncation
    rule). Header comment updated.
22. `tests/test_promote_main_cycle.py` — replace
    `test_claude_md_and_dot_claude_count_as_code` with
    `test_claude_track_paths_are_not_code` plus a mixed-diff case; the old
    test's intent is recorded in the new test's docstring.
23. README (promote cycle) and `docs/scripts-pending-removal.md` promote
    entry wording; changelog fragment.

## Files & Modules

- `.github/ai/claude_track_paths.txt` [new]
- `.github/ai/claude_track_requires.json` [new]
- `scripts/claude_track.py` [new]
- `scripts/resolve_claude_support.sh` [new]
- `scripts/review_autofix_step_claude_fixer_gate.sh` [new]
- `.github/workflows/claude-release.yml` [new]
- `scripts/assemble_changelog.py`
- `scripts/stage_workflow_support.sh`
- `scripts/promote_main_cycle.sh`
- `.github/workflows/mark-stable.yml`
- `.github/workflows/test-and-mark-stable.yml`
- `.github/workflows/update_workflows.yml`
- `.github/workflows/review_autofix.yml`
- `.github/workflows/clarify.yml`
- `.github/workflows/issue_pr_status.yml`
- `.github/workflows/ci.yml`
- `workflow-templates/ai-update-workflows.yml`
- `.claude/commands/seed-repo.md`, `workflow-templates/.claude/commands/seed-repo.md`
- `tests/test_claude_track.py` [new], `tests/test_claude_release_workflow_contract.py` [new],
  `tests/test_update_workflows_claude_track.py` [new], `tests/test_resolve_claude_support.py` [new],
  `tests/test_claude_template_parity.py` [new], `tests/test_seed_repo_command.py` [new],
  `tests/test_assemble_changelog.py`,
  `tests/test_promote_main_cycle.py`
- `README.md`, `agents.md`, `CLAUDE.md` (§20 text), `docs/INVENTORY.md`,
  `docs/scripts-pending-removal.md`, `changelog.d/*` [new]

## Tests

- **Unit:** `test_claude_track.py` (glob semantics incl. `**` in leading,
  middle and trailing position and rejection of `**` inside a segment, neutral
  paths, parity pass/fail fixtures, requires-file validation, version bump,
  numeric version order across `v1.9.0`/`v1.10.0` and `claude-v1.9.0`/`claude-v1.10.0`,
  unparsed tags skipped, resolve ordering/hold/ancestry with a fake `gh`/`git`); `test_assemble_changelog.py`
  (channel marker both orders, filter, heading insertion, default unchanged);
  `test_resolve_claude_support.py`; `test_promote_main_cycle.py`.
- **Contract:** `test_claude_release_workflow_contract.py`;
  `test_update_workflows_claude_track.py`; existing
  `test_review_autofix_claude_fixer_mode.py` and
  `test_workflow_file_size_limit.py` unchanged and green.
- **End to end (activation, post-merge):** dispatch `claude-release.yml`
  → `claude-v1.0.0` on `claude-stable`, GitHub Release, 13 dispatches
  logged; next consumer sync logs `CLAUDE_TRACK_SYNC source=claude_release`;
  a consumer `ai-review.yml` run logs `CLAUDE_TRACK_SUPPORT
  source=claude_release`; a Claude-only merge to `main` produces a
  `claude-v1.0.1` and the next promote tick logs `PROMOTE_CYCLE_SKIPPED
  reason=no_code_changes`.

## Risks & Mitigations

- Runtime-fetched Claude code runs with workflow secrets → immutable tag +
  `claude-stable` ancestry + post-checkout `HEAD` check + out-of-tree
  staging; fallback is the caller's own verified support commit, never a
  moving ref.
- A Claude release needs an unreleased workflow change → the requires file
  and G6 hold the release; the workflow release job re-dispatches
  `claude-release.yml`; the daily schedule is the backstop.
- Version skew in a consumer mid-sync → the sync resolves the Claude release
  against the exact workflow release it is applying in the same run.
- `CHANGELOG.md` edits from both tracks on different branches → both only
  insert lines; `.gitattributes` `merge=union` is the backstop for the
  `stable`→`main` forward merge.
- Moving the Claude-fixer gate into a script changes where
  security-relevant verdict checks live → moved verbatim, same log lines,
  existing tests must pass unchanged, and the script is staged only from a
  verified commit.
- The `paths:` filter drifts from the globs file → contract test.
- ACCEPTED — pending first release: consumers keep today's behaviour until
  `claude-v1.0.0` exists and their next `@stable` sync carries P2.

## Rollout

1. Merge the project (P1–P4 via one final PR).
2. Post-merge activation (automation, not a manual script): the push to
   `main` touches Claude paths, so `claude-release.yml` runs and cuts
   `claude-v1.0.0`; if held, the next workflow release re-dispatches it.
3. The next `@stable` workflow release carries P2/P3 to consumers; from then
   on Claude changes reach consumers on each Claude release.
4. Kill switch: `CLAUDE_RELEASE_ENABLED=false` stops new Claude releases;
   consumers keep the last compatible one. Reverting P2/P3 returns consumers
   to `stable`-sourced Claude assets.

## References

- `docs/plans/move-checking-roles-to-claude-plan.md` (follow-up plan, ships
  its Claude side on this track)
- `.github/workflows/promote-main-to-stable.yml`, `scripts/promote_main_cycle.sh`,
  `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/mark-stable.yml`,
  `.github/workflows/update_workflows.yml`, `scripts/stage_workflow_support.sh`,
  `scripts/assemble_changelog.py`
- CLAUDE.md §6, §14, §15, §18, §20, §21, §23, §27
