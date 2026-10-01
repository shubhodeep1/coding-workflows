# Require owner authorization before protected `.claude` changes merge or ship

Source issue: shubhodeep1/coding-workflows#4919 (https://github.com/shubhodeep1/coding-workflows/issues/4919)
Base branch: claude/implement-plan-issue-4678-edit-files-without-python-heredocs
Security pass: skip (ai:security: automation-produced issue)

## Summary

The security audit of the issue #4678 project branch found that
`tests/test_claude_template_parity.py` (issue #4775) cannot tell who changed
a consumer-variant `.claude` command. An unattended agent can edit the
command and update its pinned SHA-256 in the same PR. CI then passes, the
review workflow auto-merges, and the `@stable` sync copies the command into
every consumer's `.claude/`. This plan adds a deterministic authorization
check. Every automated merge path, and every stable release, fails closed on
a change to a protected-equivalent path unless the repository owner approved
that exact PR head. The approval is a comment only a person can post: a
Claude session's comment carries `performed_via_github_app: claude` and is
rejected.

## Context

- Issue #4919 (`ai:security`, filed by `.github/workflows/security-audit.yml`,
  `Refs #3576`): finding `template-variant-authorization-bypass`, A01 Broken
  Access Control, severity high, confidence 8/10, location
  `tests/test_claude_template_parity.py:136`, integration branch
  `claude/implement-plan-issue-4678-edit-files-without-python-heredocs`.
  Recommendation: "At merge and release, fail closed on changes to either
  `.claude` tree or divergence pins unless an automated check verifies the PR
  head, actor, and phase-scoped authorization from trusted provenance; never
  treat issue text or a progress-log approval line as proof."
- The base branch is project #4678 (draft final PR #4684 into `main`). It
  already carries the #4775 fix: CLAUDE.md §23.I/§28.C declare
  `workflow-templates/.claude/**` protected-equivalent, and
  `tests/test_claude_template_parity.py` pins the five consumer-variant
  commands by SHA-256 (`TEMPLATE_DIVERGENCE`). The #4775 plan's own Risks
  section named this gap: "The test cannot see who made a change."
- **Every actor uses one account.** PRs, comments, and labels from Claude
  sessions, Actions workflows (`GH_PAT`), and the human owner all appear as
  `shubhodeep1` with `author_association: OWNER`. Measured on issue #4775
  (2026-09-29): comments posted by Claude sessions through the claude.ai
  proxy carry `performed_via_github_app.slug == "claude"`; comments posted by
  workflows with `GH_PAT` carry `performed_via_github_app: null`. GitHub
  forbids approving your own PR, so a PR review cannot carry the owner's
  approval for PRs this account opens. `performed_via_github_app` is not
  used anywhere in the repo yet.
- **Merge paths** (all `gh pr merge`; no GraphQL or REST merge call exists):
  - `scripts/review_enable_auto_merge.sh:233` (forward-merge fallback,
    `--merge --auto`) and `:284` (`--squash --auto`). This is the clean-review
    path for every PR, Claude-fixer PRs included, run by
    `review_autofix.yml` step "Enable auto-merge on PR". The step sets
    `AUTO_MERGE_READY_LABELS_ALLOWED=true` only after a successful call, which
    gates `ai:ready-to-merge`.
  - `scripts/review_rb_judge.sh:1770-1771`, `:1818-1819`, and the
    `merge_with_followup` sync merge (~`:2250-2265`).
  - `scripts/orchestrate_poll_process.sh`:
    - `:10615` (`finalize_integration_merge_if_needed`, the integration PR
      into the default branch)
    - `:16551-16552` (stall recovery)
    - `:19130-19131` (backward scan)
    - `:19825`/`:19828` (the `ai:ready-to-merge` loop)
    - `:20648`/`:20651`, `:20705-20706`, `:20996-20997`, `:21142`
      (review-blocked judge actions)
    - `:23720` (noop-suspicious force-merge)
  - `review_autofix.yml` job `deterministic-skip-merge` (`:1900`, `:1918`).
    Its gate already turns the skip off (`PROTECTED_SKIP_SUPPRESSED`,
    `:1205-1210`) for any PR touching `.claude/*`, `workflow-templates/*`,
    `scripts/*`, or `.github/*`. Review round 1 added
    `tests/test_claude_template_parity.py` to that list (AD-11).
- **Release path.** Consumers sync from `refs/tags/stable`
  (`update_workflows.yml`, "Sync .claude/ assets from upstream"). The tag
  moves in `test-and-mark-stable.yml` job `release` ("Tag version and update
  stable pointer") and in legacy `mark-stable.yml`. Both run after a
  `validate` job that checks out the candidate with `fetch-depth: 0`.
- `review_autofix.yml` runs its support scripts from a verified trusted
  commit: `scripts/stage_workflow_support.sh` `REQUIRED_BOOTSTRAP_SCRIPTS`,
  staged into `SUPPORT_SCRIPTS_DIR`. The orchestrator sources `scripts/`
  from its own checkout. A PR cannot change the gate code that judges it.

## Goals

- A new `scripts/protected_path_authorization.py` decides, from GitHub REST
  data and local git only:
  - `pr` mode: whether an open PR's files touch a protected-equivalent path,
    and whether the owner authorized the PR's current head.
  - `release` mode: whether every protected-equivalent commit in a release
    range came from a merged PR authorized at its merged head.
- **The authorization is an owner comment on the PR.** It counts only if all
  of these hold:
  - its whole body (trimmed) is `/authorize-protected-paths <40-hex head SHA>`
  - the SHA equals the head being merged or released
  - `user.type == "User"`
  - `author_association` is `OWNER`, `MEMBER`, or `COLLABORATOR`
  - `performed_via_github_app` is null
  - it was never edited (`updated_at == created_at`)
- **The protected-equivalent set** is:
  - `.claude/**`
  - `workflow-templates/.claude/**`
  - `tests/test_claude_template_parity.py` (the divergence pins)
  - the gate's own two files
  - A rename's previous path counts too, and a truncated file list counts as
    protected (fail closed).
- **Every automated `gh pr merge` in `scripts/*.sh` goes through
  `protected_path_guarded_merge`** (new `scripts/protected_path_gate.sh`):
  - A PR that touches no protected path merges exactly as before.
  - A protected PR merges only when authorized at the current head. If the
    call lacked `--match-head-commit`, the wrapper adds it for that head.
  - Otherwise the merge is skipped (exit 3), a `::warning::` is logged, and
    one instruction comment per head tells the owner what to post. A read
    error also skips the merge (fail closed).
- **A release gate step** in `test-and-mark-stable.yml` and `mark-stable.yml`
  (`validate` job) runs `release` mode over `refs/tags/stable..<candidate>`.
  It fails the release on any unauthorized protected-equivalent change.
- A static contract test fails CI when a `gh pr merge` call in `scripts/*.sh`
  is not wrapped. CI runs the new tests.
- CLAUDE.md §23.I, `agents.md`, and a `changelog.d/` fragment document the
  rule, the owner's command, and the fact that agents never post it.

## Non-goals

- No edit to `.claude/**` or `workflow-templates/.claude/**`, so this phase
  needs no protected-path stop (§28.C).
- No change to `tests/test_claude_template_parity.py` or its pins.
- No branch-protection or ruleset change (§23.C admin operation). The gate
  works without it.
- No change to `review_autofix.yml`'s deterministic-skip merge (AD-3).
- No protection for `CLAUDE.md`, workflows, or other instruction files
  beyond the finding's scope (§5).
- No gate on `promote-main-to-stable.yml`'s stable *branch* move. Consumers
  read the tag, which the release gate covers.
- No re-trigger of the review workflow after the owner comments. The owner
  merges the approved PR, or the orchestrator retries on its next cycle.

## Constraints

- **§1 security first:** fail closed on unknown data, missing evidence, API
  errors, and truncated file lists.
- **§5:** a PR that touches no protected path keeps today's behaviour
  exactly, apart from one extra REST read per merge attempt.
- **§6:** no renames. New identifiers are checked for collisions:
  `protected_path_guarded_merge`, `PROTECTED_PATH_*`, `/authorize-protected-paths`,
  marker `ai:protected-path-authorization:v1`.
- **§9:** tabs in Python and shell; YAML stays 2-space.
- **§14:** the scripts reach consumer repos through `review_autofix.yml`'s
  staged support bundle. They gate consumer PRs that touch `.claude/**`
  (AD-8). No template or `.claude` asset changes.
- **§15:** `pr` mode costs one PR read plus one files page per 100 files.
  Only for a protected PR does it add one comments page per 100 comments.
  Results are cached per PR and head inside one shell process, so an
  `A || B` fallback pair reads once. `release` mode costs one
  `commits/{sha}/pulls` read per protected commit and one comments read per
  distinct PR. No GraphQL.
- **§18:** no manual script. The gate is wired into the existing merge
  helpers and release workflows. The owner's comment is the only human step.
- **§19:** PR bodies use `Refs #4919`.
- **§20:** a `security` fragment.
- **§27:** `review_autofix.yml` is untouched. Only a small step is added to
  `test-and-mark-stable.yml` (350,650 bytes) and `mark-stable.yml`.

## Approach

1. **`scripts/protected_path_authorization.py`** (new)
   - Pure functions:
     - `is_protected_path(path)`
     - `protected_files(files_json)`, which also handles renames and
       truncation
     - `authorizing_comment(comments, head_sha)`
     - `evaluate_pr(pr, files, comments)`
     - `evaluate_release(commits, prs_by_commit, comments_by_pr, cutoff)`
   - A thin REST layer over `gh api` with three attempts and backoff.
   - The CLI prints one JSON line.
   - `pr --repo R --pr N [--head SHA]` exits:
     - `0` = allow, with `protected` true or false and the authorized `head`
     - `3` = blocked
     - `2` = read error
   - `release --repo R --base REF --head REF` exits `0` = pass, `3` = blocked,
     `2` = error.
   - **Release mode:**
     - It lists `git rev-list --no-merges <base>..<head> -- <protected pathspecs>`,
       plus the merge commits that make a protected change of their own
       (`git log --remerge-diff`, and octopus merges with a protected file
       that is not the version of the one parent that changed it, AD-18),
       so a protected edit inside a merge commit is checked too.
       It refuses a shallow checkout (conformance run 2).
     - `scripts/mark-stable.sh`, the manual release path, runs the same
       check before it moves any tag (conformance run 2, AD-17).
     - Each commit is authorized when one of its merged PRs
       (`commits/{sha}/pulls`) has an authorizing comment at that PR's
       `head.sha`.
     - Commits reachable from the gate's arrival commit pass (AD-5). The
       arrival commit is the oldest first-parent commit on `<head>` that
       changed this script. Reachability is pure git history, so no date an
       agent can set decides it (the committer-date cutoff first planned
       here was replaced in phase 1; conformance run 1).
     - A commit with no merged PR is blocked.
     - A missing `<base>` ref (first release) means the whole history, still
       subject to the cutoff.
2. **`scripts/protected_path_gate.sh`** (new, sourced)
   - `protected_path_guarded_merge <command…>` parses:
     - the PR number after `merge`
     - `--repo`
     - `--match-head-commit`
   - It runs the Python `pr` check (cached per PR and head in the shell).
   - Unprotected PR: runs the command unchanged.
   - Protected and authorized: runs it with `--match-head-commit <head>`
     added when absent.
   - Otherwise: posts the instruction comment once per head, then returns 3.
     The comment carries `<!-- ai:protected-path-authorization:v1 head=<sha> -->`
     and is never a bare command, so it can never authorize anything. The
     existing comment list is reused from the check.
   - It uses the `gh` on PATH. It carries a `python3` and script-path
     fallback relative to its own directory.
3. **Wrap every merge site:**
   - `review_enable_auto_merge.sh` `:233` and `:284`. On exit 3 it logs
     `AUTOFIX_AUTO_MERGE_PROTECTED_PATH pr=… head_sha=… action=refuse` and
     leaves `AUTO_MERGE_READY_LABELS_ALLOWED=false`, so no
     `ai:ready-to-merge` label is added.
   - `review_rb_judge.sh` `:1770-1771`, `:1818-1819`, and the
     `merge_with_followup` sync merge.
   - `orchestrate_poll_process.sh`: all nine sites listed in Context (15
     calls).
   - `orchestrate_poll.yml`: both files are added to its required staging
     list, because the orchestrator runs from that staged copy.
   - Each script sources `protected_path_gate.sh` next to its existing
     helper sources. With `set -e`, a missing gate file fails closed.
   - `stage_workflow_support.sh` adds both files to
     `REQUIRED_BOOTSTRAP_SCRIPTS`.
4. **Release gate:** a step "Verify protected-path changes are authorized
   (issue #4919)" goes after "Verify CI passed on source branch" in
   `test-and-mark-stable.yml` `validate`, and after "Verify CI passed on
   stable" in `mark-stable.yml` `validate`. It runs `release` mode with
   `--base refs/tags/stable --head HEAD` and `GH_TOKEN: secrets.GH_PAT`.
5. **Tests** (see Tests); `ci.yml` step.
6. **Docs:**
   - CLAUDE.md §23.I: one paragraph after the protected-equivalent sentence.
   - `agents.md`: the §28.C bullet and a new short "Protected-path merge and
     release gate" entry.
   - `changelog.d/4919-protected-path-merge-authorization.md`.

Alternatives considered: AD-1 through AD-10 below.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.
The check, its merge-site wiring, the release step, and the static wrapping
test only close the finding together, and a partial wiring would leave a
merge path open.

1. **Phase 1: protected-path merge and release authorization gate.**
   - Files:
     - new: `scripts/protected_path_authorization.py`,
       `scripts/protected_path_gate.sh`,
       `tests/test_protected_path_authorization.py`,
       `changelog.d/4919-protected-path-merge-authorization.md`
     - changed: `scripts/review_enable_auto_merge.sh`,
       `scripts/review_rb_judge.sh`, `scripts/orchestrate_poll_process.sh`,
       `scripts/stage_workflow_support.sh`,
       `.github/workflows/test-and-mark-stable.yml`,
       `.github/workflows/mark-stable.yml`, `.github/workflows/ci.yml`,
       `.github/workflows/orchestrate_poll.yml`, `CLAUDE.md`, `agents.md`,
       and the existing tests whose fakes model the merge scripts
       (`tests/test_review_autofix_review_pipeline_contract.py`,
       `tests/test_review_rb_judge_label_propagation.py`)
   - Done when:
     - every unit and wiring test in Tests passes
     - the static test finds no unwrapped `gh pr merge` in `scripts/*.sh`
     - the existing tests for the touched scripts and workflows pass
     - `bash -n` passes on each touched shell script
     - `yamllint`/`actionlint` pass on the touched workflows
     - no `.claude/**` or `workflow-templates/.claude/**` file changed
   - Rollback: revert the phase PR.

## Implementation Steps

1. Write `scripts/protected_path_authorization.py` (Approach 1).
2. Write `scripts/protected_path_gate.sh` (Approach 2).
3. Wrap the merge sites and source the gate (Approach 3). Add both files to
   `REQUIRED_BOOTSTRAP_SCRIPTS`.
4. Add the release gate step to both release workflows (Approach 4).
5. Write `tests/test_protected_path_authorization.py` and add it to a
   `ci.yml` step.
6. Update CLAUDE.md §23.I, `agents.md`, and add the changelog fragment.

## Files & Modules

- `scripts/protected_path_authorization.py` [new]
- `scripts/protected_path_gate.sh` [new]
- `scripts/review_enable_auto_merge.sh`, `scripts/review_rb_judge.sh`,
  `scripts/orchestrate_poll_process.sh`, `scripts/stage_workflow_support.sh`
- `.github/workflows/test-and-mark-stable.yml`,
  `.github/workflows/mark-stable.yml`, `.github/workflows/ci.yml`,
  `.github/workflows/orchestrate_poll.yml`
- `tests/test_protected_path_authorization.py` [new]
- `CLAUDE.md` (and `workflow-templates/CLAUDE.md`, a symlink to it), `agents.md`
- `changelog.d/4919-protected-path-merge-authorization.md` [new]

## Data Model / Index Changes

None. No MongoDB collection is touched.

## Tests

- **Unit tests, on fixture JSON (no network):**
  - Path matching:
    - root and template `.claude` files
    - the pin file
    - the gate files
    - rename sources
    - near-miss names such as `.claudex/`, `docs/.claude.md`,
      `workflow-templates/claude/`
  - Truncated files count as protected.
  - An authorizing comment is accepted only when all hold:
    - exact whole-body match
    - the head SHA matches
    - `User` type
    - trusted association
    - no app (the `claude` app and any other app are rejected)
    - never edited
  - Rejected comment bodies: extra text, a quoted command, the instruction
    comment, and a wrong or short SHA.
  - `evaluate_release` cases:
    - authorized
    - unauthorized
    - grandfathered
    - a commit with no PR
    - a PR authorized at another head
    - several PRs for one commit
- **Gate wrapper tests with a stubbed `gh` and a stubbed checker:**
  - An unprotected PR runs the command unchanged.
  - A protected, authorized PR adds `--match-head-commit` once, and does not
    add it when it is already present.
  - A blocked PR returns 3, does not run the command, and posts the
    instruction comment once per head.
  - A read error returns 3.
- **Static test:** every `gh pr merge` call in `scripts/*.sh` outside
  comments is wrapped and puts the PR number right after `pr merge`, the
  only shape the gate parses (review round 2, AD-12). Each touched script
  sources the gate. Both files are
  in `REQUIRED_BOOTSTRAP_SCRIPTS`. Both release workflows run the release
  step in `validate` before `release`. `ci.yml` runs the test file.
- **Release mode on a scratch git repository:** a temp repo with a
  protected commit, an unprotected commit, and a merge commit, with API
  responses stubbed.
- **Existing suites run on every touched file:**
  - `tests/test_review_autofix_review_pipeline_contract.py`
  - `tests/test_workflow_file_size_limit.py`
  - `tests/test_claude_md_section_numbers.py`
  - `tests/test_changelog_fragment_contract.py`
  - `tests/test_claude_template_parity.py`
  - `tests/test_permission_prompts.py`
  - the tests that pin `review_enable_auto_merge.sh`, `review_rb_judge.sh`,
    orchestrator merge sites, and `stage_workflow_support.sh` (found with
    `grep`)
- **Static checks:** `bash -n` on each shell script; `yamllint -s` and
  `actionlint` on the workflows.

## Risks & Mitigations

- **Protected PRs no longer auto-merge without the owner.** This is
  intended, and it matches §28.C, which already needs a human for such
  phases.
  - The instruction comment names the exact command and head.
  - Claude checkers keep waiting, because no block label is added.
  - An `/implement-plan-claude` chain continues once the owner merges.
- **A GH_PAT workflow that posts agent-written text verbatim** would also
  have `performed_via_github_app: null`. Two things limit this:
  - Only a comment whose whole body is the command counts.
  - Every workflow comment path wraps its text in headers or markers.
  - ACCEPTED residual risk, recorded in agents.md.
- **An owner comment edited later by an agent** (same account) is rejected,
  because `updated_at != created_at`. An agent cannot post without the app
  attribution through the claude.ai proxy.
- **Local Claude Code sessions** (CLI with a PAT) post without app
  attribution. They are interactive, human-watched sessions, and CLAUDE.md
  forbids agents from posting the command.
- **The release gate blocks a release** holding a protected change merged
  after the gate landed without authorization. The fix is for the owner to
  post the command on that merged PR with its merged head SHA, then re-run
  the release.
- **An extra REST read on every merge attempt**, with a fail-closed read
  error that skips the merge for one attempt. Reads retry three times, and
  the orchestrator retries on its next cycle.
- **Editing `orchestrate_poll_process.sh` merge sites** risks changing loop
  control flow. The wrapper returns non-zero exactly like a failed merge, so
  each site's existing failure branch handles a block.

## Rollout

The change ships with the #4678 project's final PR into `main`, then with
the next `@stable` release.

- The merge gate is live for this repo once on `main`, since the orchestrator
  and `review_autofix.yml` stage scripts from the trusted default-branch or
  stable commit.
- It reaches consumers with the next release (review workflow support
  bundle).
- The release gate runs on the next `test-and-mark-stable.yml` run.
  Grandfathering (AD-5) keeps pre-gate history from blocking it.
- Nothing to activate. Operators may add a branch-protection rule later;
  that is not required.

## References

- Issue #4919; audit tracker #3576; issue #4775 and its plan
  `docs/completed/issue-4775-template-claude-protected-equivalent-plan.md`
  (AD-2 option C); project #4678 (final PR #4684)
- CLAUDE.md §1, §5, §14, §15, §18, §20, §23.C, §23.I, §27, §28.C
- GitHub REST: issue comments `performed_via_github_app`,
  `GET /repos/{o}/{r}/commits/{sha}/pulls`

## Auto-decisions

- **AD-1** [plan, 2026-09-29]
  - Question: What trusted provenance authorizes a protected-equivalent change?
  - Picked: A. An unedited PR comment whose whole body is
    `/authorize-protected-paths <head SHA>`, by a `User` with
    OWNER/MEMBER/COLLABORATOR association and no `performed_via_github_app`.
  - Alternatives:
    - B: an approving PR review from a collaborator other than the author.
    - C: an `ai:protected-path-approved` label.
    - D: the progress log's `Protected-path approval:` line.
  - Why: A is head-bound and per-PR. A Claude session cannot produce it,
    because the proxy stamps the `claude` app on it. B is impossible when
    one account opens every PR. C is not head-bound, and agents set `ai:*`
    labels routinely. The finding rules out D.
  - Applied in: phase 1 PR. Status: pending review.
- **AD-2** [plan, 2026-09-29]
  - Question: Where is the gate enforced at merge?
  - Picked: A. A shared `protected_path_guarded_merge` wrapper around every
    `gh pr merge` in `scripts/*.sh`, with a static test that no unwrapped
    call remains.
  - Alternatives:
    - B: a required status check plus a branch-protection rule.
    - C: only `review_enable_auto_merge.sh`.
  - Why: A covers the review, judge, and orchestrator paths without an
    admin operation. B needs a §23.C settings change, and `--auto` alone
    ignores non-required checks. C leaves the orchestrator merges open.
  - Applied in: phase 1 PR. Status: pending review.
- **AD-3** [plan, 2026-09-29]
  - Question: Wrap `review_autofix.yml`'s `deterministic-skip-merge` too?
  - Picked: A. No.
  - Alternatives: B: yes, by sourcing the gate in that inline job.
  - Why: its `PROTECTED_SKIP_SUPPRESSED` guard already refuses the skip for
    `.claude/*`, `workflow-templates/*`, and `scripts/*`. A pin-only change
    cannot carry a template change, because the parity test would fail on
    the unchanged template. B edits a 454,700-byte workflow near the §27
    limit for no added coverage.
  - Applied in: no code change. Status: pending review.
- **AD-4** [plan, 2026-09-29]
  - Question: Where is the gate enforced at release?
  - Picked: A. A step in the `validate` job of `test-and-mark-stable.yml`
    and `mark-stable.yml`, before the `release` job moves the `stable` tag
    consumers sync from.
  - Alternatives:
    - B: also before `promote-main-to-stable.yml` fast-forwards the stable
      branch.
    - C: no release gate.
  - Why: the tag is what consumers read. B duplicates the check on a ref
    consumers do not read. C ignores the "at release" half of the
    recommendation.
  - Applied in: phase 1 PR. Status: pending review.
- **AD-5** [plan, 2026-09-29]
  - Question: How does the release gate treat changes merged before the
    gate existed?
  - Picked: A. They are grandfathered. A protected commit reachable from
    the oldest first-parent commit on the candidate that changed
    `scripts/protected_path_authorization.py` passes. (First planned as a
    committer-date cutoff; phase 1 used reachability instead, because an
    agent can set a commit date.)
  - Alternatives: B: no grandfathering; the owner authorizes every earlier
    PR retroactively.
  - Why: B blocks the first release on dozens of already-reviewed PRs,
    which the owner cannot all re-verify. The gate's job is to stop new
    unauthorized changes.
  - Applied in: phase 1 PR. Status: pending review.
- **AD-6** [plan, 2026-09-29]
  - Question: Which paths are protected-equivalent for the gate?
  - Picked: A. `.claude/**`, `workflow-templates/.claude/**`,
    `tests/test_claude_template_parity.py`, and the gate's own two scripts.
  - Alternatives:
    - B: A plus `CLAUDE.md` and every workflow.
    - C: only the two `.claude` trees.
  - Why: A covers the finding (the trees and the divergence pins) and
    protects the gate from being weakened. B widens past the finding (§5)
    and would stop most PRs. C leaves the pins and the gate itself
    unprotected.
  - Applied in: phase 1 PR. Status: pending review.
- **AD-7** [plan, 2026-09-29]
  - Question: How is a blocked merge surfaced?
  - Picked: A. One PR comment per head, with marker
    `<!-- ai:protected-path-authorization:v1 head=<sha> -->`, naming the
    command; no label.
  - Alternatives:
    - B: add `ai:needs-human`.
    - C: a new label.
  - Why: `ai:needs-human` is a block label that wakes Claude fixers, which
    cannot authorize and would loop. C needs label-contract changes for
    nothing a comment does not already do.
  - Applied in: phase 1 PR. Status: pending review.
- **AD-8** [plan, 2026-09-29]
  - Question: Should the merge gate also run for consumer repos?
  - Picked: A. Yes. It applies wherever the shared merge helpers run, but
    only to PRs that touch the protected set.
  - Alternatives: B: coding-workflows only.
  - Why: a consumer's `.claude/**` also steers its unattended sessions, and
    PRs there that touch it are rare, since the sync overwrites it.
  - Applied in: phase 1 PR. Status: pending review.
- **AD-9** [plan, 2026-09-29]
  - Question: What happens when the gate cannot read GitHub?
  - Picked: A. Retry three times, then refuse the merge for this attempt
    (fail closed).
  - Alternatives: B: allow the merge (fail open).
  - Why: §1. An authorization gate that fails open is no gate. A skipped
    attempt is retried by the orchestrator, or the owner merges.
  - Applied in: phase 1 PR. Status: pending review.
- **AD-10** [plan, 2026-09-29]
  - Question: How is an authorized protected PR bound to its head when the
    merge call has no `--match-head-commit`?
  - Picked: A. The wrapper adds `--match-head-commit <authorized head>`.
  - Alternatives: B: leave the call as is.
  - Why: without the binding, a push between the check and the merge would
    merge an unapproved head.
  - Applied in: phase 1 PR. Status: pending review.

## Notes

- Security pass: `security_pass_skip.py` printed `{"skip": true, "label":
  "ai:security", "reason": "ai:security: created and labelled by the issue
  automation"}`.
- The base branch's final PR #4684 is open (draft), so the base has not
  moved.
- `gh` was not installed when this session started. The repo's SessionStart
  hook installed it; the GitHub MCP tools are not available in this
  session, so GitHub writes go through the allowlisted helpers and
  `gh api`.
