# Bind Claude issue queue items to the run that queued them

Source issue: shubhodeep1/coding-workflows#4621 (https://github.com/shubhodeep1/coding-workflows/issues/4621)
Base branch: main
Security pass: skip (ai:security: automation-produced issue)

## Summary

The Claude issue pickup trusts an `ai:claude-issue-queue` issue because `github-actions[bot]` opened it, but anyone who can edit that issue can change its title and payload afterwards. This plan binds every queue item to an immutable artifact uploaded by the default-branch workflow run that queued it, and has the pickup verify that binding before it starts a session.

## Context

- Security audit finding `editable-queue-payload-trusted-by-creator` (issue #4621, `STRIDE: Tampering`, high, `scripts/claude_issue_route.py:469`). The exploit: edit an open bot-created queue issue, replace its title and fixed-key payload, and the pickup accepts the new target because the creator is still `github-actions[bot]`. Refs #3576 (the audit's tracker lineage).
- Producers of queue items, both in coding-workflows and both writing with the job's `GITHUB_TOKEN`:
  - `scripts/claude_issue_intake.sh`, run by `.github/workflows/claude-issue-intake.yml` (`repository_dispatch` `claude-issue`, `workflow_dispatch`). One `claude_issue.v1` item per target issue; an open item for the same target is reused.
  - `scripts/claude_pr_sweep.py`, run by the `claude-pr-catch-all` job of `.github/workflows/review_autofix_sweep.yml` (`schedule` `17 * * * *`, `workflow_dispatch`). One `claude_pr_fix.v1` item per due PR; a PR with an open item is not queued again.
- Consumer: the Claude issue pickup (`.claude/commands/claude-issue-pickup.md`) runs `claude_issue_route.py queue-pending --fetch-repo shubhodeep1/coding-workflows` and starts one `/implement-issue-claude` or `/fix-claude-pr` session per pending entry. It runs in a claude.ai cloud session, where `api.github.com` goes through the agent proxy: REST only, no GraphQL (CLAUDE.md §23.A), so issue edit history (`userContentEdits`, `lastEditedAt`) is not readable there. Artifact downloads (`GET /actions/artifacts/{id}/zip`) do work through the proxy (checked in this session).
- What a writer cannot forge: artifacts belong to one workflow run and can only be uploaded from inside that run. A run's `path`, `event`, `head_branch`, and `head_sha` come from GitHub. `repository_dispatch` and `schedule` runs always use the default branch's workflow file.

## Goals

- G1. The pickup starts a session for a queue item only when a binding artifact uploaded by a trusted producer run lists that queue issue number with exactly the item's title and payload text.
- G2. A trusted producer run is a completed run of `.github/workflows/claude-issue-intake.yml` (issue items: events `repository_dispatch`, `workflow_dispatch`) or `.github/workflows/review_autofix_sweep.yml` (PR-fix items: events `schedule`, `workflow_dispatch`) in this repository, on the default branch. A `workflow_dispatch` run also needs its `head_sha` on the default branch.
- G3. An edited, unbound, or mismatched item is never started. It is listed under `ignored` with a reason and left open, so the watchdog still flags it.
- G4. The intake and the sweep write the binding for every item they open (and the intake for every item it reuses), and upload it as the run artifact `claude-issue-queue-binding`, including when a later step fails.
- G5. The pickup's GitHub reads stay batched and bounded (CLAUDE.md §15), documented in the function docstring.

## Non-goals

- Changing what `/implement-issue-claude` or `/fix-claude-pr` check once started. They keep their own gates.
- Protecting against an actor who can push to the default branch or change repository settings. They can already change the producers themselves.
- Changing the watchdog, the queue labels, the payload formats (`claude_issue.v1`, `claude_pr_fix.v1`), or the sweep's dedupe rule (AD-5).
- A new secret or any change to the pickup's cloud environment.

## Constraints

- §1 security first: fail closed. A binding that cannot be verified never starts a session (AD-3).
- §4 new env vars get defaults: `CLAUDE_ISSUE_QUEUE_BINDING_FILE` (intake) and `CLAUDE_PR_SWEEP_QUEUE_BINDING_FILE` (sweep) default to a path under the run's temp directory.
- §5 minimal change: extend `queue_pending`, `build_queue_issue`, the intake, and the sweep. No new script file.
- §6 no renames: `queue_pending`'s existing parameters, output keys (`pending`, `ignored`, `remaining`), CLI flags, labels, and log keys stay. New identifiers (`QUEUE_BINDING_*`, `append_queue_binding`, `fetch_queue_bindings`, `--bindings-json`, `--default-branch`) were checked against the repo and are unused.
- §9 tabs in Python and shell, 2-space YAML.
- §15 API hygiene: batched listings with a per-run fallback (see Approach).
- §18 no manual step: the binding is written and uploaded by the existing workflows and verified by the existing pickup call.
- §20 changelog fragment (security fix).
- §27 workflow size: `review_autofix_sweep.yml` is about 18.5 KB and `claude-issue-intake.yml` about 4.7 KB. Both stay far below the limit.

## Approach

**Binding artifact.** Each producer run keeps one JSON file, `claude_issue_queue_binding.json`:

```json
{"schema_version": "claude_issue_queue_binding.v1", "repository": "<owner>/<repo>", "run_id": 123,
 "items": [{"queue_issue": 88, "title": "[claude-issue-queue] o/r#9", "payload": "claude_issue.v1\nrepo: …\n"}]}
```

`append_queue_binding(path, repository, run_id, queue_issue, title, payload)` in `scripts/claude_issue_route.py` creates or extends it; a CLI subcommand `add-queue-binding` exposes it to the intake shell script. The workflow uploads the directory as artifact `claude-issue-queue-binding` with `if: always()` and `if-no-files-found: ignore`, so items opened before a later failure are still bound.

**Pointer.** The queue body already carries `Intake run: <run URL>` / `Sweep run: <run URL>`. That line becomes the pointer to the binding run. An editor can change the pointer, but only to another trusted run, and that run's artifact does not list this queue issue with this title and payload, so the item is refused.

**Verification at pickup** (`queue_pending(..., bindings=…)`). After the existing checks (author, marker, payload, registry, title), an item needs:
1. exactly one run-URL line for this repository matching its item type;
2. a binding record for that run: `ok` (trusted producer, completed, artifact parsed), otherwise the item is `ignored` with reason `binding_pending` (run still running), `binding_untrusted: <why>`, `binding_unavailable: <why>` (read failed), or deferred when the run was not fetched this wake;
3. a record producer that matches the item type;
4. an artifact entry for this queue issue number whose `title` and `payload` equal the item's title and payload block exactly.
Otherwise the item is `ignored` with reason `unbound: <why>` / `binding_mismatch`. With `bindings=None` the pure function skips this step. Only the sweep's dedupe read uses that, and it must count every open trusted item (AD-5). The CLI never passes `None`.

**Fetch** (`fetch_queue_bindings(repo, run_ids, default_branch)`), called by `queue-pending --fetch-repo` only for the runs referenced by the first `limit` targets:
- 1 call: `GET repos/{repo}` for the default branch (skipped when `--default-branch` is given);
- 1 call: `GET repos/{repo}/actions/artifacts?name=claude-issue-queue-binding&per_page=100` (run → artifact map);
- 2 calls: `GET repos/{repo}/actions/workflows/claude-issue-intake.yml/runs?per_page=100` and the same for `review_autofix_sweep.yml` (run metadata map);
- per referenced run: a fallback `GET …/actions/runs/{id}` when it was not in the listing, a fallback `GET …/actions/runs/{id}/artifacts` when its artifact was not listed, 1 compare call (`GET …/compare/{head_sha}...{default}`) only for `workflow_dispatch` runs, and 1 artifact zip download.
A failed listing falls back to the per-run calls. A failed per-run read marks only that run `binding_unavailable`. A failed queue read keeps exit 3, as today.

**Alternatives considered** (AD-1): an HMAC with a shared secret needs the key in both Actions and the pickup's cloud environment, adds an operator step, and any writer can still read an Actions secret through a branch workflow. Re-authorizing at pickup from target-issue labels cannot read `AI_ISSUE_IMPLEMENTER` through the proxy, and it does not bind the item.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the fix is one invariant across the producers and the consumer. A pickup that verifies before the producers write bindings would refuse every new item, and producers that write bindings without a verifying pickup would not fix the finding.

1. **Phase 1 — bind and verify queue items.** Files: `scripts/claude_issue_route.py`, `scripts/claude_issue_intake.sh`, `scripts/claude_pr_sweep.py`, `.github/workflows/claude-issue-intake.yml`, `.github/workflows/review_autofix_sweep.yml`, `.claude/commands/claude-issue-pickup.md`, `README.md`, `agents.md`, `tests/test_claude_issue_route.py`, `tests/test_claude_pr_sweep.py`, `changelog.d/4621-bind-claude-queue-payloads.md`. Done when: the tests below pass, `tests/test_claude_issue_route.py` and `tests/test_claude_pr_sweep.py` pass in full, and both workflows parse and carry the upload step. Rollback: revert the PR. Items queued while the change was live stay pickable after a revert, because the old pickup ignores the binding.

## Implementation Steps

1. `scripts/claude_issue_route.py`: add `QUEUE_BINDING_ARTIFACT`, `QUEUE_BINDING_SCHEMA_VERSION`, `QUEUE_BINDING_FILENAME`, and the producer table (workflow path, allowed events, run-URL line prefix per item type). Add `append_queue_binding`, `load_queue_binding` (validate one artifact's JSON), `evaluate_producer_run` (pure run checks), `fetch_queue_bindings` (the batched fetch above), and the binding check in `queue_pending` (new keyword `bindings`, default `None`). Wire `queue-pending --fetch-repo` to fetch bindings. Add `--bindings-json` (offline or test input; without it and without `--fetch-repo`, no run is fetched, so every item is deferred) and `--default-branch`. Add the `add-queue-binding` subcommand. Update the module docstring.
2. `scripts/claude_issue_intake.sh`: after the queue issue is created, call `add-queue-binding` with the issue number, title, and payload text. On reuse, rewrite the item (`PATCH` with the fresh body carrying this run's URL) and bind it (AD-2). A failed rewrite or binding write goes through the existing `fail queue_failed` path. Add the env var `CLAUDE_ISSUE_QUEUE_BINDING_FILE` (default `${RUNTIME_DIR}/claude-issue-queue-binding/claude_issue_queue_binding.json`).
3. `.github/workflows/claude-issue-intake.yml`: pass `CLAUDE_ISSUE_QUEUE_BINDING_FILE` under `runner.temp` to the queue step, and add an `actions/upload-artifact@v6` step (`if: always()`, name `claude-issue-queue-binding`, `if-no-files-found: ignore`, `retention-days: 30`).
4. `scripts/claude_pr_sweep.py`: after a successful queue POST, append the binding (`CLAUDE_PR_SWEEP_QUEUE_BINDING_FILE`, default `<RUNNER_TEMP or tmp>/claude-issue-queue-binding/claude_issue_queue_binding.json`). A binding write failure is logged as a warning, and the item is still counted as queued (the pickup refuses it and the watchdog flags it). Update the docstring's batching contract.
5. `.github/workflows/review_autofix_sweep.yml` (`claude-pr-catch-all` job): pass the binding file env and add the same upload step.
6. `.claude/commands/claude-issue-pickup.md` step 2: describe the binding check, the new `ignored` reasons, and the call budget. `README.md` ("Claude issue implementer", catch-all) and `agents.md` (queue paragraph): document the binding, the new env vars, and the fail-closed behaviour.
7. Tests (below) and `changelog.d/4621-bind-claude-queue-payloads.md` (`security`).

## Files & Modules

- `scripts/claude_issue_route.py`
- `scripts/claude_issue_intake.sh`
- `scripts/claude_pr_sweep.py`
- `.github/workflows/claude-issue-intake.yml`
- `.github/workflows/review_autofix_sweep.yml`
- `.claude/commands/claude-issue-pickup.md`
- `README.md`, `agents.md`
- `tests/test_claude_issue_route.py`, `tests/test_claude_pr_sweep.py`
- `changelog.d/4621-bind-claude-queue-payloads.md` [new]

## Tests

Unit (`tests/test_claude_issue_route.py`):
- a bound item is pending; an item whose body payload or title was edited after binding is ignored (`binding_mismatch`); an item retargeted to another run whose artifact lacks it is ignored; an item with no run line or two run lines is ignored;
- producer checks: wrong workflow path, wrong event, non-default `head_branch`, other repository, `workflow_dispatch` off the default branch, a run still in progress (`binding_pending`), an item type that does not match the producer;
- an unfetched run is deferred (counted in `remaining`, not `ignored`); `bindings=None` keeps the legacy grouping for the sweep dedupe;
- `append_queue_binding` round-trips and appends; `load_queue_binding` rejects a wrong schema, repository, or run id;
- CLI `queue-pending --fetch-repo` with a stubbed `gh`: the exact call list (queue, repo, artifacts listing, two run listings, zip download), per-run fallbacks, and exit 3 on a failed queue read;
- intake: new item → binding file written with the POSTed title and payload; reused item → PATCH with this run's URL and binding written; binding write failure → `queue_failed`;
- workflow wiring: both workflows upload `claude-issue-queue-binding` with `if: always()` from the binding file's directory.

Unit (`tests/test_claude_pr_sweep.py`): a queued PR fix appends a binding whose title and payload equal the item's; the dedupe still counts an unbound open item.

Verification: `python3 -m pytest -q -p no:cacheprovider tests/test_claude_issue_route.py tests/test_claude_pr_sweep.py tests/test_check_in_status_hand_back.py tests/test_implement_issue_claude_command.py`, YAML parse of both workflows, and `bash -n scripts/claude_issue_intake.sh`.

## Risks & Mitigations

- Items queued before this change have no binding, so the pickup refuses them. ACCEPTED: fail closed (AD-3). The queue drains hourly, so few items are affected. The watchdog flags them after 3 hours. `/reclarify` on the target issue re-queues it, because the intake rewrites and binds the reused item. A stale sweep item blocks re-queueing that PR until it is closed.
- An artifact upload failure leaves items unbound. The upload step fails the run visibly, the watchdog flags the item, and an issue item heals on `/reclarify`.
- Race: the pickup wakes while the producer run is still in progress → `binding_pending`, and the next wake picks it up.
- Artifact retention (30 days) is shorter than an item's life only when the pickup has been down for a month. Such items fail closed and are flagged by the watchdog.
- API budget grows from 1 call to about 4 + 1–3 per referenced run (at most 10 targets per wake). ACCEPTED: bounded, and batched per §15.

## Rollout

No flag. Merge ships the producers and the pickup check together. The pickup refreshes its checkout from `main` on every wake, so the next wake after merge verifies bindings. Consumer repos are unaffected: the intake, sweep, and pickup run only in coding-workflows. Rollback is reverting the PR.

## Auto-decisions

- AD-1 [plan, 2026-09-27] How should a queue item be bound so the pickup can verify it? — Picked: A — an artifact uploaded by the producing default-branch workflow run, verified through REST at pickup. Alternatives: B — an HMAC signature keyed by a secret shared by Actions and the pickup's cloud environment; C — re-authorize at pickup from the target issue's labels. Why: A needs no new secret or operator step and cannot be forged from outside the producing run; B adds an operator step and a key writers can exfiltrate; C cannot read the routing variable through the proxy and binds nothing. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] What happens when the intake reuses an open queue item for the same target? — Picked: A — rewrite the item's body with the fresh payload and this run's URL, then bind it in this run's artifact. Alternatives: B — stop reusing and always open a new item; C — keep reuse as is (bound only by the first run). Why: A keeps one item per target and lets `/reclarify` heal an unbound or edited item; C would leave such an item stuck. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] How should items with no valid binding (legacy, failed upload, edited) be handled? — Picked: A — fail closed: list under `ignored`, never start, leave open for the watchdog. Alternatives: B — accept items opened before the deploy; C — close them automatically. Why: §1 security first; B reopens the hole for open legacy items, and C could silently drop legitimate work. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] How should `workflow_dispatch` producer runs be trusted? — Picked: A — only when the run's `head_sha` is on the default branch (one compare call per such run). Alternatives: B — refuse `workflow_dispatch` runs; C — trust `head_branch` alone. Why: `head_branch` can name a tag that shadows the default branch; B would break manual intake re-fires and manual sweeps. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] Should the sweep's "already queued" dedupe check bindings? — Picked: A — no; keep counting every trusted, well-formed open item. Alternatives: B — count only bound items. Why: dedupe must err toward not queueing twice, and B would add artifact downloads to every sweep run. Applied in: phase 1 PR. Status: pending review

## References

- Issue #4621 (this finding), #3576 (security audit follow-ups), #4525 (why the pickup is a session), #4443 (Claude issue implementer).
- `scripts/claude_issue_route.py`, `.claude/commands/claude-issue-pickup.md`, `README.md` "Claude issue implementer".
