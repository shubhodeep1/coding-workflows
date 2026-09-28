# Verify automation provenance before an issue-mode project skips its security pass

Source issue: shubhodeep1/coding-workflows#4623 (https://github.com/shubhodeep1/coding-workflows/issues/4623)
Base branch: main
Security pass: skip (ai:security: automation-produced issue)

## Summary

`/implement-issue-claude` step 6 writes `Security pass: skip` whenever the issue carries `ai:security`, `ai:check-triage`, or `ai:workflow-heal`. Anyone who can apply a label (a triage-role user or a collaborator) can therefore make an ordinary issue skip the security audit of the project built from it. This plan replaces the label-only rule with a deterministic check, `.claude/scripts/security_pass_skip.py`, that allows the skip only when the issue was created by the designated automation and is linked to that automation's own record; every other case, including any read failure, runs the security pass.

## Context

- Finding `mutable-label-skips-security-pass` (A05:2021, medium, confidence 9/10) at `.claude/commands/implement-issue-claude.md:35`, filed by `security-audit.yml` as #4623 (tracker #3576).
- The skip exists so an automation-produced issue (a security follow-up, a check-failure triage issue, a workflow-heal issue) does not run its own audit and open follow-ups of follow-ups (`implement-plan-claude.md` Issue Mode, "Security pass").
- The three producers all create their issues with `gh issue create --label <label>` and a body marker:
  - `scripts/security_audit.sh` (≈ line 1793): label `ai:security`, first line `<!-- ai:security-finding:<id> -->`, second line `Refs #<tracker>`; the tracker (`AI Security Audit Tracker`) carries `ai:security-audit` and `<!-- ai:security-audit-tracker:v1 -->` and is created by the same token (≈ line 541).
  - `scripts/check_failure_triage.sh` (≈ line 391): label `ai:check-triage`, first line `<!-- check-failure-triage:fp=<fp> -->`.
  - `scripts/workflow_failure_heal_intake.sh` (≈ line 828) with the body from `scripts/workflow_failure_heal.py` (≈ line 1625): label `ai:workflow-heal`, `<!-- workflow-failure-heal:fp=<fp> -->`.
- Those workflows authenticate with `secrets.GH_PAT` (falling back to `github.token` for the audit and triage). In every registered repo (`.github/ai/consumer_repos.json`, all user-owned by the PAT owner) a PAT-created issue has `author_association: OWNER`; a `github.token` issue is authored by `github-actions[bot]` (`type: Bot`).
- `clarify.yml` trusts OWNER, MEMBER, and COLLABORATOR authors to open issues, so issue authorship alone is not a boundary between automation and collaborators.
- `scripts/claude_issue_route.py` also computes a label-only `skip_security_pass` that rides the `claude_issue.v1` payload and queue item. No consumer reads it to make a decision: the implementation session decides from the issue itself (step 6).
- `.claude/commands/*` and `.claude/scripts/*` are mirrored byte-for-byte into `workflow-templates/.claude/` and reach consumers on the `@stable` sync (§14).

## Goals

- G1: `Security pass: skip` is written only when `.claude/scripts/security_pass_skip.py` returns `"skip": true` for the issue.
- G2: The script returns `skip: true` only when all of these hold for one skip label: (a) the author is `github-actions[bot]` (type `Bot`) or a `User` with `author_association` `OWNER`; (b) the label's first `labeled` issue event was performed by the author within 120 seconds of the issue's creation, and no other account ever applied it; (c) the body carries that label's automation marker; (d) for `ai:security`, the body's `Refs #T` names an issue that carries `ai:security-audit`, the tracker marker, and the same author.
- G3: Any other outcome, and any read failure (exit 2), makes the session write `Security pass: run`.
- G4: The script issues at most three REST calls (issue, first issue-events page, tracker) and no GraphQL (§15).
- G5: Consumer repos receive the script, the updated commands, and the allowlist entries through the `workflow-templates/.claude/` mirror.

## Non-goals

- Changing the security audit, triage, or heal producers.
- Changing the router's `skip_security_pass` payload field or its label-only computation (AD-5): it stays as an advisory, logged value (§6).
- Resuming projects: a project that already has a plan keeps the `Security pass:` line it was written with.
- Org-owned repos where the PAT user is a MEMBER: they run the security pass (safe default; see Risks).

## Constraints

- §1: security first; the failure mode is "run the audit", never "skip".
- §5: minimal change; the decision moves into one script, the command text changes only where it states the rule.
- §6: no identifier is renamed or removed; `SECURITY_PASS_SKIP_LABELS`, `skip_security_pass`, and the `Security pass:` header keep their names and formats. New names (`security_pass_skip.py`, its functions) are checked for clashes.
- §9: tabs in Python and Markdown code, 2-space YAML.
- §14 / §20: mirror under `workflow-templates/.claude/`; one `changelog.d/` fragment (`security`).
- §15: ≤ 3 REST calls, documented in the script docstring.
- §23.D: REST only (`gh api` GET), `-R`-free absolute paths, no GraphQL.
- §28: every judgement call below is an auto-decision.

## Approach

A standalone Python script in `.claude/scripts/` (where `check_in_status.py` and `claude_fix_claim.py` live, so consumers have it) with a pure `decide_security_pass_skip(issue, events, fetch_tracker)` function and a CLI `--repo <owner>/<repo> --issue <N>` that prints one JSON line `{"skip": bool, "label": str|null, "reason": str}` (exit 0), or exits 2 on a read failure. `/implement-issue-claude` step 6 runs it and writes the header from its result; `implement-plan-claude.md` Issue Mode says how the header is set. Alternatives considered: deciding in `clarify.yml` and carrying the verdict through the dispatch payload, queue, and start prompt (touches five components, and manual runs would need a second path); prose-only rules (the model would still decide from mutable data).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes exactly one phase.

1. **Phase 1 — verified security-pass skip.** Add the script and its tests, switch both commands to it, allowlist it, mirror everything to `workflow-templates/.claude/`, update the docs and add the changelog fragment.
   - Files: see Files & Modules.
   - Done: `tests/test_security_pass_skip.py` and `tests/test_implement_issue_claude_command.py` pass; the command no longer states a label-only rule; mirror parity holds; `ruff`/repo checks pass; running the CLI against #4623 prints `skip: true` and against an issue without a skip label prints `skip: false`.
   - Rollback: revert the phase PR; the label-only rule returns.

## Implementation Steps

1. Create `.claude/scripts/security_pass_skip.py`: constants for the skip labels (same order as `SECURITY_PASS_SKIP_LABELS`), per-label marker regexes, the tracker label and marker, the 120-second window; the pure decision function; `gh api` fetch helpers (issue, `issues/<N>/events?per_page=100`, tracker issue); CLI; docstring with the §15 call budget and exit codes.
2. Copy it to `workflow-templates/.claude/scripts/security_pass_skip.py`.
3. Edit `.claude/commands/implement-issue-claude.md` step 6: run the script; `skip: true` → `Security pass: skip (<label>: automation-produced issue)` (the header format stays as the Issue Mode header block documents it, per the §6 constraint above; AD-7); anything else → `Security pass: run`, recording the reason in the plan's Notes when a skip label was present. Mirror to the template.
4. Edit `.claude/commands/implement-plan-claude.md` Issue Mode "Security pass" bullet to say the header is set only by the verified check. Mirror to the template.
5. Add `Bash(python3 .claude/scripts/security_pass_skip.py *)` and the `PYTHONDONTWRITEBYTECODE=1` variant to `.claude/settings.json` and `workflow-templates/.claude/settings.json`.
6. Comment `SECURITY_PASS_SKIP_LABELS` / `route_issue` in `scripts/claude_issue_route.py` as advisory (the payload field is logged only; the session's verified check decides).
7. Add `tests/test_security_pass_skip.py`; extend `tests/test_implement_issue_claude_command.py`; add the new test file to the `Claude issue implementer tests` step in `.github/workflows/ci.yml`.
8. Update `README.md` (Claude issue implementer section, ≈ line 1251) and `agents.md` (≈ line 248); add `changelog.d/4623-verify-security-pass-skip.md`; update `docs/INVENTORY.md` if its parity gate covers `.claude/scripts/`.

## Files & Modules

- `.claude/scripts/security_pass_skip.py` [new]
- `workflow-templates/.claude/scripts/security_pass_skip.py` [new]
- `.claude/commands/implement-issue-claude.md`, `workflow-templates/.claude/commands/implement-issue-claude.md`
- `.claude/commands/implement-plan-claude.md`, `workflow-templates/.claude/commands/implement-plan-claude.md`
- `.claude/settings.json`, `workflow-templates/.claude/settings.json`
- `scripts/claude_issue_route.py` (comments only)
- `tests/test_security_pass_skip.py` [new], `tests/test_implement_issue_claude_command.py`
- `.github/workflows/ci.yml`
- `README.md`, `agents.md`, `changelog.d/4623-verify-security-pass-skip.md` [new], `docs/INVENTORY.md` (if required)

## Tests

- Unit (`tests/test_security_pass_skip.py`): each label's happy path; collaborator / MEMBER author; `User` named `github-actions[bot]`; label added later by another actor; label added by the author after the window; missing marker; marker for the wrong label; `ai:security` without `Refs`, tracker without the label, without the marker, or by another author; tracker read failure; no skip label; two skip labels where only the second verifies; CLI exit 0 JSON and exit 2 on a failed read (with `gh` stubbed); mirror byte-equality.
- Contract (`tests/test_implement_issue_claude_command.py`): the command references the script and no longer states the label-only rule; the plan command's Issue Mode states the verified rule.
- Manual (evidence for the PR): the CLI against #4623 (skip) and an ordinary issue (run).

## Risks & Mitigations

- Org-owned consumer where the PAT user is a MEMBER: the skip never verifies and the security pass runs, which can open follow-ups of follow-ups. ACCEPTED — safe direction; every registered repo is user-owned today.
- A write collaborator can create issues as `github-actions[bot]` from a branch workflow, or read `GH_PAT` from one. ACCEPTED — that is write access to repository secrets, outside this finding; the fix closes the label-only (triage-role) path.
- A later re-application of the label by another account could sit beyond the first 100 issue events. Mitigation: the issue-events endpoint lists label and state events only (no comments), and a full 100-event page is treated as unverifiable, so the pass runs.
- Clock skew between `created_at` and the `labeled` event. Mitigation: 120-second window (observed gap on #4623: 1 second).

## Rollout

Ships on merge to `main`; consumers get it on the next `@stable` sync. No flag: the failure mode is running the audit. Rollback is reverting the PR.

## Auto-decisions

- AD-1 [plan, 2026-09-27] Where does the verification live? — Picked: A — a deterministic `.claude/scripts/security_pass_skip.py` the command runs, mirrored to consumers. Alternatives: B — decide in `clarify.yml` and carry the verdict through payload, queue, and start prompt; C — prose rules in the command. Why: one component, testable, available in consumer checkouts, and the model no longer decides from mutable data. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] Which identity counts as the designated automation? — Picked: A — author `github-actions[bot]` (Bot) or a `User` with `author_association: OWNER`, and the skip label's first `labeled` event performed by that author within 120 s of creation. Alternatives: B — a configured login list (needs a new env var no session reliably has); C — any OWNER/MEMBER/COLLABORATOR (does not fix the finding). Why: both signals are set by the creating token and cannot be produced by a triage-role user or a collaborator acting as themselves. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] What proves the link to a valid automation record? — Picked: A — the label's own body marker, plus for `ai:security` a `Refs #T` tracker carrying `ai:security-audit`, the tracker marker, and the same author. Alternatives: B — marker only; C — also require a tracker comment naming the finding id (paginates every tracker comment). Why: satisfies the recommendation within 3 REST calls (§15). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] What happens on a read error or a failed check? — Picked: A — write `Security pass: run`. Alternatives: B — stop the project as BLOCKED. Why: §1, the safe option costs only an audit run. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] What happens to the router's label-only `skip_security_pass` field? — Picked: A — keep field and computation (§6), document it as advisory. Alternatives: B — tighten the router with the same checks. Why: no component decides from it; §5 minimal change. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-27] Does this project skip its own security pass? — Picked: A — skip: #4623 passes the new rule by hand (author `shubhodeep1`, OWNER; `ai:security` labelled by the author 1 s after creation; `<!-- ai:security-finding:mutable-label-skips-security-pass -->` marker; `Refs #3576`, which carries `ai:security-audit`, the tracker marker, and the same author). Alternatives: B — run it. Why: the issue is an automation-produced follow-up under both the old and the new rule; conformance, validation, and the reviewer panel still run. Applied in: no code change. Status: pending review

## References

- Issue #4623, tracker #3576
- `.claude/commands/implement-issue-claude.md`, `.claude/commands/implement-plan-claude.md` (Issue Mode)
- `scripts/security_audit.sh`, `scripts/check_failure_triage.sh`, `scripts/workflow_failure_heal_intake.sh`, `scripts/workflow_failure_heal.py`
