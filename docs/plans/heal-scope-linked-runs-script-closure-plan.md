# Workflow-failure heals reach dispatched child runs and the scripts the failing step runs

## Summary

Workflow-failure heals can currently edit only the failing workflow, so a root cause inside a run the failing step dispatched, or inside a script that step runs, leaves the heal stuck waiting for a person (#8287). This plan widens the frozen heal scope deterministically, from authenticated run metadata plus repository content at the scope commit:

- the workflow files of verified linked child runs;
- the scripts the failing step reaches, up to three levels deep.

It also adds a scheduled re-check that replaces a heal whose plan was held only for paths inside that reach, and it stops clarification from offering scope changes nobody carries out.

## Context

### The incident

- **What failed.** Release v1.40.3 failed in the release gate (`Test & Mark Stable Release`, run 40746491161). The job `orphan-workflows-test` step "Dispatch & watch — internal-memory-maintenance" dispatched `.github/workflows/internal-memory-maintenance.yml` as run 40752760153. That run calls the reusable `.github/workflows/memory_maintenance.yml`, hit its 20-minute job timeout and was cancelled.
- **Root cause.** `scripts/ai_memory_lib.py` (`_clone_for_memory_branch`) made a full repository clone per memory write, and the fail-open extraction step had no step timeout. PR #8288 fixed both by hand.
- **Heal #8287 diagnosed it correctly but stalled.**
  - Its scope marker was `paths=.github/workflows/test-and-mark-stable.yml,tests/**,changelog.d/*.md`.
  - The plan gate (`scripts/files_touched_scope_guard.py`, `heal_scope_plan_out_of_scope`, around line 1083) held the plan.
  - `/approved` cannot override that hold.
  - Clarify-respond auto-answered "re-issue with the correct scope", but no code re-issues a heal for that reason.

### How the scope is built today

- **The freeze.** `scripts/workflow_failure_heal_intake.sh` freezes `SCOPE_INPUTS_FROZEN` (around lines 2492–2524) before any untrusted evidence reaches the diagnosis prompt (#6463). The inputs are the crash file, verified runs, `self_workflows` (from `verify_run`'s `referenced_paths`), `consumer_workflows`, `changed_files` and failing tests.
- **The renderer.** `render_heal_scope_marker` (`scripts/workflow_failure_heal.py` around line 3575) orders the candidates as: crash file, workflow paths, up to 8 test subjects, changed files, then the remaining subjects. It caps the list at 20 explicit paths, then appends `tests/**` and `changelog.d/*.md`.
- **Linked runs are evidence only (#8248).** `_heal_collect_linked_runs` (around lines 2528–2623) runs after the freeze. It follows same-repo `actions/runs/<id>` links found in the failed jobs' logs, reading each run, its jobs and one job log. By design, linked data never feeds the scope marker.
- **The API record already names the child's workflows.** For run 40752760153, `GET /actions/runs/<id>` returns `path: .github/workflows/internal-memory-maintenance.yml` and `referenced_workflows: [shubhodeep1/coding-workflows/.github/workflows/memory_maintenance.yml@promote-candidate]`. `verify_run` (around line 7121) already turns exactly this shape into `referenced_paths`.
- **The marker cannot change once filed.** `verify_heal_scope` and `verify_heal_scope_pipeline_edits` (around lines 3669–3740) accept a heal only when the marker is identical in every body revision. Widening a filed heal therefore needs a replacement issue.
  - Precedent: heal-scope recovery (#7191, #7333, `scripts/workflow_failure_heal_recovery_scan.sh`, hourly `recovery-scan` job in `.github/workflows/workflow-failure-heal-intake.yml`). It already replaces heal issues with a pipeline-authored issue whose scope is rendered from verified runs.
- **The failing step is already on disk.** The intake keeps `${LOG_DIR}/run-<id>-jobs.json` for each verified run (around line 1609), so the failing step of the reported run needs no new API call.

### Prototype

A depth-3 closure on the current tree, seeded from the two failing steps above, produced 15 script paths. Together with the 3 workflow paths that is 18 of the 20 slots.

| Depth | From the parent step | From the linked run's step |
|---|---|---|
| 1 | `scripts/dispatch_and_watch_workflow_run.sh` | `scripts/memory_helpers.sh`, `scripts/memory_maintenance_extract_learnings.py` |
| 2 | `scripts/comprehensive_test_and_release_gh_api.sh` | `scripts/ai_memory.py`, `scripts/ai_memory_lib.py`, `scripts/render_prompt.sh`, `scripts/openrouter_prompt_cache.py`, `scripts/repo_root.py` |
| 3 | `scripts/gh_api_retry.py`, `scripts/gh_helpers.sh` | `scripts/render_prompt.py`, `scripts/assemble_prompt.sh`, `scripts/semantic_cache.py`, `scripts/memory_injection_patterns.py` |

Related plan: `docs/plans/heal-issues-self-resolve-plan.md`. Its non-goals ("letting the model diagnosis widen any scope", "re-filing out-of-scope heals as ordinary issues automatically") stay in force: this plan widens scope only from metadata, and re-files only as pipeline-authored heal replacements.

## Goals

- **G1 (linked-run workflow scope).** A self-repo heal whose failed job's log links a same-repo run gets that run's workflow path and its same-repo `referenced_workflows` in the scope marker, and the run in `runs=`. The run must pass every provenance check in Constraints → Linked-run provenance. For #8287's inputs the marker must contain `.github/workflows/internal-memory-maintenance.yml` and `.github/workflows/memory_maintenance.yml`.
- **G2 (script closure).** The scope marker contains the top-level `scripts/` files reached from the failing step's `run:` body within 3 levels, read at the scope commit. Seeds are the failing step of each verified run and, when G1 data exists, of each qualifying linked run. For #8287's inputs the marker must contain `scripts/ai_memory_lib.py` and `scripts/memory_helpers.sh`.
- **G3 (budget).** Closure paths take up to `WORKFLOW_HEAL_SCRIPT_CLOSURE_RESERVE` (default 10) slots right after the workflow paths, nearest first. Leftover closure paths fill slots still free after everything else. The 20-path cap and the `tests/**` / `changelog.d/*.md` tail are unchanged.
- **G4 (re-check).** The hourly recovery scan finds open, pipeline-authored heal issues whose plan is held with `heal_scope_plan_out_of_scope`. When every denied path lies inside a freshly rendered scope (G1 and G2 applied, denied paths given priority), it files a pipeline-authored replacement heal with that scope and closes the original as superseded. Otherwise it posts one `needs_human` comment and one WARNING, once per denied-path set and scope render version (see `heal_scope_render_version()` under Approach).
- **G5 (clarify).** Clarify and clarify-respond never offer, and never choose, an option that changes a heal's edit scope or re-issues it. Clarify-respond receives the heal's recorded scope as a `HEAL SCOPE` fact.
- **G6 (switches).** Each behaviour has its own default-on switch, and switching it off restores today's output byte-for-byte. The one exception is the G5 prompt text; see Rollout.

## Non-goals

- Heals filed for consumer repos (Q6: A). Their scope is checked against the consumer tree, and the closure names coding-workflows files. The consumer path (`scope_workflows_key=consumer_workflows`) is untouched.
- Letting the diagnosis, the plan or any model output add a path. Every new path comes from run metadata, repository content at the scope commit, or (G4) the intersection of plan-named paths with that deterministic set.
- Raising the 20-path cap or changing the `tests/**` / `changelog.d/*.md` tail or the `agents.d/<N>-*.md` allowance.
- Following runs of other repositories, or links to runs that are not in the failed job's log.
- Resolving scripts outside top-level `scripts/` (`workflow-templates/`, `.claude/`, nested directories), or Python `importlib` / dynamic imports.
- Changing the plan gate's decision logic (`files_touched_scope_guard.py`) or the implement preflight.

## Constraints

- **Scope integrity (#6463).** Every new scope input is frozen before the diagnosis prompt is assembled. The closure is computed at render time from frozen seeds plus `git show <scope_ref>:<path>`, the same way test subjects are today.
- **Linked-run provenance (Q2: A).** A linked run qualifies for scope only when all of these hold. Otherwise it stays evidence-only (#8248 behaviour).
  1. Its id was found in a failed job's log of a verified run. This is the existing `linked-runs` discovery.
  2. `repository.full_name` equals the source repository, case-insensitive. This is the existing check.
  3. `conclusion` is one of `failure`, `cancelled` or `timed_out`.
  4. `created_at` lies within the `[started_at, completed_at]` window of a failed job of the verified parent run whose own log mentioned it. The window is that job's, not the union of the parent run's failed jobs, so discovery records the job id per link (P1 step 3).
  5. `triggering_actor.login` equals the parent run's `triggering_actor.login`, case-insensitive. A missing value on either side fails the check.
  6. The heal is self-repo (`repo == SELF_REPO`).
- **§6 naming.** No identifier is renamed or removed.
  - `render_heal_scope_marker` gains keyword-only parameters with empty defaults.
  - The `heal-scope` CLI gains new operations; existing choices are unchanged.
  - New env vars, log tokens and marker names are listed in Interfaces under Approach, and each is unique in the repo. The implementer greps for each before adding it.
- **§15 API hygiene.**
  - P1 moves the existing linked-run run and jobs reads ahead of the freeze and reuses those files in the evidence stage. That is still at most 3 calls per linked run and none without a link.
  - P2 adds no calls.
  - P3 reuses the recovery scan's existing GraphQL issue listing, which already returns each issue's last 30 comments, and adds no per-issue read beyond what the recovery path already does.
- **§18 automation.** No new script or supervisor. All work extends existing scripts on existing triggers. Nothing is added to `docs/scripts-pending-removal.md`.
- **§19.** Replacement bodies use `Refs #N` / `Re-issued from #N`, never a closing keyword.
- **§20 / §30.** Each phase ships its own `changelog.d/<N>-<slug>.md` and an `agents.d/<N>-<slug>.md` fragment. `agents.md` is never edited directly.
- **§27.** Workflow edits are limited to env switch plumbing (a few `env:` lines), which every phase needs so its switches are readable: the workflow env blocks list repository variables one by one (for example `HEAL_SCOPE_RECOVERY_ENABLED` in the `intake`, `recovery-candidates` and `recovery-scan` jobs of `.github/workflows/workflow-failure-heal-intake.yml`), with no blanket `vars.*` inheritance. No workflow file may reach the 480,000-byte guard; check with `wc -c` after each edit.

## Approach

### Interfaces (all new)

| Name | Kind | Default | Phase |
|---|---|---|---|
| `WORKFLOW_HEAL_LINKED_RUN_SCOPE_ENABLED` | env switch | on (unset, empty or `true`; `false` turns it off; any other value means off plus a `::warning::`) | P1 |
| `WORKFLOW_HEAL_SCRIPT_CLOSURE_ENABLED` | env switch | on (same parsing) | P2 |
| `WORKFLOW_HEAL_SCRIPT_CLOSURE_RESERVE` | env int | 10, clamped 0–20 | P2 |
| `HEAL_SCOPE_RESCOPE_ENABLED` | env switch | on (same parsing) | P3 |
| `HEAL_CLARIFY_SCOPE_FACTS_ENABLED` | env switch | on (same parsing) | P4 |
| `${RUNTIME_DIR}/linked_scope.json` | frozen file, schema `heal_linked_scope.v1` | written only when P1 is on and some run qualifies | P1 |
| `heal-scope linked-scope` | `workflow_failure_heal.py` operation | n/a | P1 |
| `heal-scope closure` | `workflow_failure_heal.py` operation | n/a | P2 |
| `heal-scope rescope-candidates` | `workflow_failure_heal.py` operation | n/a | P3 |
| `<!-- ai:heal-rescope:v1 outcome=<replaced\|needs_human\|failed> reason=<token> denied=<sha256-12> render=<n> run=<id> -->` | issue comment marker | n/a | P3 |
| `WORKFLOW_HEAL linked_run_scope run=<id> outcome=added\|rejected reason=<token> paths=<n>` | log line | n/a | P1 |
| `WORKFLOW_HEAL scope_closure seeds=<n> matched=<n> paths=<n> reserved=<n> truncated=<n>` and `WORKFLOW_HEAL scope_closure_seed_skipped run=<id> reason=<token>` | log lines | n/a | P2 |
| `WORKFLOW_HEAL heal_rescope issue=<n> outcome=<o> reason=<r>` | log line | n/a | P3 |

The P1 rejection reasons are `conclusion`, `outside_window`, `actor_mismatch`, `actor_missing`, `consumer_heal`, `switch_off`, `run_cap` and `malformed`.

The P2 `scope_closure_seed_skipped` reasons are `step_unmatched`, `step_ambiguous`, `step_no_run`, `yaml_error`, `pr_scope`, `branch_untrusted`, `parent_skipped` and `run_info_unavailable`.

`heal_scope_render_version()` (a function in `scripts/workflow_failure_heal.py`, new in P3) names the scope derivation the running code uses: `1 + 1×L + 2×C`, where `L` is 1 when the `heal-scope linked-scope` operation exists and `WORKFLOW_HEAL_LINKED_RUN_SCOPE_ENABLED` is on, and `C` is 1 when the `heal-scope closure` operation exists and `WORKFLOW_HEAL_SCRIPT_CLOSURE_ENABLED` is on. The value is computed at run time, so no phase edits a shared constant: neither, P1 only, P2 only and both give 1, 2, 3 and 4 whatever order the phases merge in, and switching a phase back on also changes it. P3 records it in the `ai:heal-rescope:v1` marker as `render=<n>`.

### `linked_scope.json` format

Written by P1 and read by P2. Both phases implement this exact format.

```json
{
  "schema": "heal_linked_scope.v1",
  "runs": [
    {
      "ref": "owner/repo:40752760153",
      "run_id": 40752760153,
      "parent_run_id": 40746491161,
      "event": "workflow_dispatch",
      "head_branch": "promote-candidate",
      "head_repository": "owner/repo",
      "workflow_paths": [".github/workflows/internal-memory-maintenance.yml", ".github/workflows/memory_maintenance.yml"],
      "step": {"job_id": 123043536675, "job_name": "maintenance / memory_maintenance", "step_name": "Extract repository learnings (fail-open)", "selection": "cancelled"}
    }
  ]
}
```

`step` is `null` when no step can be selected. `selection` is one of `failed`, `cancelled` or `longest`. `parent_run_id`, `event`, `head_branch` and `head_repository` (the run's `head_repository.full_name`) are copied from the linked run's API record, which P1 already reads; P2 applies its PR-scope check to them (P2 step 4). A missing field is written as `null`.

### Failing-step selection

This rule is shared by the parent run (P2) and linked runs (P1 records it). Within the run's jobs whose conclusion is `failure`, `cancelled` or `timed_out`, the first job in API order wins. Within that job, take:

1. the first step with conclusion `failure` (`failed`); else
2. the first step with conclusion `cancelled` (`cancelled`); else
3. the step with the longest `completed_at - started_at` (`longest`).

### Step-to-YAML matching (P2; Q8: A, fail closed)

1. Candidate workflow files are the run's `workflow_paths`: for the parent, its verified `referenced_paths`; for a linked run, `workflow_paths`. Each is read with `git show <scope_ref>:<path>` and `yaml.safe_load`.
2. Strip a trailing matrix suffix ` (…)` from the API job name. Take the segment after the last ` / ` (the callee job of a reusable call).
3. Collect `(file, job_key)` pairs where the job key, or the job's `name:`, equals that segment.
4. Inside those jobs, collect the steps whose `name:` equals the API step name exactly.
5. Exactly one match: use its `run:` string. Zero matches, more than one, a match without `run:`, or any YAML/git error: skip the seed and log `scope_closure_seed_skipped` with reason `step_unmatched`, `step_ambiguous`, `step_no_run` or `yaml_error`.

### Reference extraction (Q3: A)

- **Shell or YAML text.** Every match of `(?<![A-Za-z0-9_.-])([A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:sh|py))(?![A-Za-z0-9_])` names a candidate `scripts/<group1>`, subject to the prefix rule below.
- **Prefix rule.** Look at the path text directly before the basename: the longest run ending at the match, without whitespace or quotes, that ends in `/`. Keep the match when that prefix is empty, or when its last segment is `scripts` or a variable expansion (`${NAME}` or `$NAME`). Also keep it when the prefix is `)/` and that `)` closes a `$(dirname …)` command substitution on the same line, the sibling-include idiom `$(dirname "${BASH_SOURCE[0]}")/gh_helpers.sh` (for example `scripts/implement_handle_guard_block.sh:12`). Drop it for any other last segment. So `scripts/x.sh`, `.codex-workflow-src/scripts/x.sh` and `${MEMORY_SCRIPTS_DIR}/ai_memory.py` are kept, and `scripts/lib/x.sh` is dropped. Without this rule, a nested path would resolve to a top-level file that has the same basename.
- **Python files** add `from X import`, `import X`, `from scripts.X import`, `from scripts import X`, and comma-separated `import X, Y`. Each yields the candidate `scripts/X.py`.
- **Keep** a candidate only when `git cat-file -e <scope_ref>:<candidate>` succeeds and the candidate passes `_safe_heal_path`.
- The prototype table above was computed before the prefix rule. P2 re-runs it under the final rule and updates the integration test expectation if a path moves.

### Closure walk

- A breadth-first walk with depth 1 for the step body's references, up to depth 3 (a constant, `HEAL_SCOPE_CLOSURE_MAX_DEPTH = 3`).
- Each file is read once. A file counts at the smallest depth it is reached.
- Output order: by depth, then seed order (parent seeds in verified-run order, then linked seeds in `linked_scope.json` order), then first appearance.
- Files are read with `git show` at `scope_ref`; a read error skips that file.
- File reads per render are capped at 200.

### Render order (Q4: A)

```
crash_file, workflow_paths (+ P1 linked workflow paths), closure[:reserve],
test subjects[:8], changed_files, test subjects[8:], closure[reserve:]
```

Then the existing 20-path cap, existence check and tail apply. With an empty closure and no linked paths, the output is byte-identical to today.

### Re-check (Q5: A)

- **Where it runs.** P3 extends the hourly recovery scan rather than the plan gate. The scan already holds the intake's lock, reconstructs reports from pipeline-authored bodies, re-runs the unchanged intake (re-verifying run provenance and the fingerprint) and files replacements.
- **How the reconstructed report is trusted.** The existing `recovery_scan` origin accepts only `autofix_failure` reports (intake around lines 400–416), and `reconstruct_recovery_payload` is bound to the autofix body shape. P3 therefore adds a separate `rescope_scan` origin with the same binding, and no bypass for unauthenticated reports. The origin is accepted only on a `schedule` event, with `HEAL_SCOPE_RESCOPE_ENABLED` on, `SOURCE_KIND=workflow_run`, and a 64-hex expected fingerprint plus an expected issue number that come only from the scan's environment. Anything else is rejected with `_report_auth_reject "rescope_scan_invalid"`. The payload is built by a new `reconstruct_rescope_payload`, which calls the existing `build_workflow_run_payload` with the run read through the API (not the body's copy). The unchanged provenance gate then re-verifies that run. The fingerprint and duplicate decision must equal the expected values, exactly as for `recovery_scan`.
- **Why the recovery eligibility check is not reused.** `heal_scope_recovery_check` (`scripts/workflow_failure_heal.py` around line 3771) accepts only an issue whose scope status is `missing`, and every rescope candidate carries a verified marker, so it would always answer `scope_verified`. P3 keeps that check unchanged and adds a separate `heal_scope_rescope_check` (P3 step 3) that sets the same replacement state the recovery path uses.
- **How denied paths are used.** They are taken only from a pipeline-authored `ai:automation-path-plan-hold:v1` comment with `reason=heal_scope_plan_out_of_scope` and `source=plan` (the author is checked against the token's login, and the comment must be newer than the issue's newest plan comment, see P3 step 1). Only `plan.yml` emits that reason: the poller's `_plan_auto_approval_gate` never passes `--heal-scope-file`, so its `source=poller` holds carry other reasons. They are passed to the intake as a priority list. The intake places only those already inside its own freshly computed candidate set (linked workflow paths ∪ closure ∪ existing inputs) right after the crash file. It never adds a path from that list that its own derivation did not produce.

### Clarify (G5)

- A facts block names the recorded scope.
- A prompt rule in both clarify prompts says, in effect: for `ai:workflow-heal` issues the edit scope is fixed by the pipeline. Never offer or choose re-issuing or widening it. Plan within it, or state that the fix lies outside it so the plan gate holds it.

### Alternatives considered

- **Parsing `uses:` lines for child workflows.** Rejected: `referenced_workflows` in the run API is authenticated and already handled by `verify_run`.
- **Granting paths at the plan gate by editing the marker.** Rejected: implement refuses any marker change across revisions, by design.
- **Literal `scripts/...` matching only.** Rejected (Q3): it misses `${MEMORY_SCRIPTS_DIR}/ai_memory.py`-style calls.
- **Raising the cap.** Rejected (Q4).

## Decisions

### D1 — Linked-run provenance bar
- **Chosen:** same repo, named in the failed job's log, created inside the failing job's window, same triggering actor, failed/cancelled/timed_out (Q2: A).
- **Alternatives considered:** today's evidence-only checks; additionally restrict to `stable`/`main`/`promote-candidate` runs.
- **Why:** log text chooses which run is followed, so the run must be tied to the failing job by authenticated metadata. The window and actor checks do that without excluding pull-request CI heals.

### D2 — Script reference resolution
- **Chosen:** basename matching to existing top-level `scripts/<name>.sh|.py` plus Python imports (Q3: A).
- **Alternatives considered:** literal `scripts/...` paths only.
- **Why:** real call sites use variable prefixes (`${MEMORY_SCRIPTS_DIR}/ai_memory.py`); requiring the file to exist at the scope commit bounds over-matching.

### D3 — Cap budget
- **Chosen:** up to 10 closure slots right after the workflow paths; leftovers fill free slots at the end (Q4: A).
- **Alternatives considered:** leftover slots only; raising the 20-path cap.
- **Why:** without a reserve, a large changed-files list crowds out the root-cause file. The cap is a verified-marker invariant.

### D4 — Plan-hold re-check mechanism
- **Chosen:** the hourly recovery scan replaces the heal when every denied path is in the re-rendered scope; otherwise one `needs_human` comment (Q5: A).
- **Alternatives considered:** improving the hold message only; no re-check.
- **Why:** the marker cannot change in place (`verify_heal_scope_pipeline_edits`), and the recovery scan already replaces heals from verified runs under the intake lock.

### D5 — Consumer heals
- **Chosen:** self-repo heals only (Q6: A).
- **Alternatives considered:** also add the linked run's consumer workflow path.
- **Why:** consumer heal scope is checked against the consumer tree, and the closure names coding-workflows files.

### D6 — Phase split
- **Chosen:** four independently mergeable phases, each switch-gated and degrading when another phase's artifact is absent (Q7: A).
- **Alternatives considered:** two phases; one phase.
- **Why:** every merge reaches production directly, and each phase is useful on its own.

### D7 — Unmatchable failing step
- **Chosen:** skip that seed and log it (Q8: A, assumed).
- **Alternatives considered:** use every `run:` block of the job.
- **Why:** today's behaviour is the floor; whole-job closures would overrun the cap.

### D8 — Switches, audit and API budget
- **Chosen:** one default-on switch per behaviour (Q9: A); qualifying linked runs recorded in `runs=` (Q11: A); collector reads moved before the freeze and reused (Q12: A); the clarify rule (Q10: A).
- **Alternatives considered:** one shared switch; leaving `runs=` unchanged; separate pre-freeze reads.
- **Why:** independent rollback per phase, an auditable marker, and no extra API calls (§15).

## Phases & Merge Strategy

Four phases (Q7: A). Each is one PR, independently mergeable in any order, complete and production-safe on merge, and behind its own default-on switch. Where a phase consumes another phase's artifact, it detects absence and degrades to today's behaviour.

1. **P1: Linked-run workflow scope.**
   - **Scope:** move the linked-run run and jobs reads before the freeze; qualify runs under the provenance rules; freeze `linked_scope.json`; add the linked workflow paths to the self-repo render input and the linked refs to `runs=`.
   - **Files:** `scripts/workflow_failure_heal_intake.sh`, `scripts/workflow_failure_heal.py`, `.github/workflows/workflow-failure-heal-intake.yml` (env plumbing only), tests, fragments.
   - **Done when** a #8287-shaped fixture yields a marker containing both memory-maintenance workflow files and `runs=` containing the linked run; switch off gives byte-identical markers; the evidence prompt section is unchanged.
   - **Rollback:** `WORKFLOW_HEAL_LINKED_RUN_SCOPE_ENABLED=false` (repo var or workflow env), or revert the PR.
2. **P2: Failing-step script closure.**
   - **Scope:** `heal-scope closure` operation; step selection and matching; reference extraction; render-order changes; seeds from parent runs always, and from `linked_scope.json` when present and valid (schema `heal_linked_scope.v1`; anything else is ignored with a log line).
   - **Files:** `scripts/workflow_failure_heal.py`, `scripts/workflow_failure_heal_intake.sh` (pass seeds and switch to render), `.github/workflows/workflow-failure-heal-intake.yml` (env plumbing only), tests, fragments.
   - **Done when** the parent-only fixture yields `scripts/dispatch_and_watch_workflow_run.sh`; the fixture with a `linked_scope.json` yields `scripts/ai_memory_lib.py` and `scripts/memory_helpers.sh`; switch off or reserve 0 with an empty closure gives byte-identical markers.
   - **Rollback:** `WORKFLOW_HEAL_SCRIPT_CLOSURE_ENABLED=false`, or revert.
3. **P3: Plan-hold re-check.**
   - **Scope:** add rescope candidates to the recovery scan; reconstruct `workflow_run` heal reports for rescope; give the intake a `rescope_scan` origin with priority paths, a replacement path and a `needs_human` path.
   - **Files:** `scripts/workflow_failure_heal_recovery_scan.sh`, `scripts/workflow_failure_heal.py` (reconstruct and replacement-body helpers), `scripts/workflow_failure_heal_intake.sh`, `.github/workflows/workflow-failure-heal-intake.yml` (env plumbing only), tests, fragments.
   - **Done when** a held heal whose denied paths are inside the recomputed scope is replaced exactly once, and a held heal with any denied path outside it gets exactly one `needs_human` comment and WARNING per denied set and render version, and is not retried for the same denied set while the render version is unchanged.
   - **Without P1/P2 merged:** the recomputed scope equals the old one, so every candidate takes the `needs_human` path. That is safe: today's outcome plus a clearer message. Because the `needs_human` marker records `render=<n>`, each heal is evaluated once more after P1 or P2 changes `heal_scope_render_version()`, so the later automatic replacement is not lost.
   - **Rollback:** `HEAL_SCOPE_RESCOPE_ENABLED=false`, or revert.
4. **P4: Clarify scope facts and rule.**
   - **Scope:** a `HEAL SCOPE` block in `scripts/clarify_github_facts.py`, and a rule paragraph in the clarify and clarify-respond prompts.
   - **Files:** `scripts/clarify_github_facts.py`, `prompts/mode-clarify.txt`, `prompts/_templates/mode-clarify.txt`, `prompts/mode-clarify-respond.txt`, `prompts/_templates/mode-clarify-respond.txt` (identical rule text in each pair), `.github/workflows/orchestrate_clarify_respond.yml` (env plumbing only, if the switch is not already inherited), tests, fragments.
   - **Done when** a heal body with a scope marker yields the facts block and other issues yield none, and the prompt-contract tests pass.
   - **Rollback:** `HEAL_CLARIFY_SCOPE_FACTS_ENABLED=false` removes the facts block. The prompt rule is text-only and rolls back by revert (see Rollout).

## Implementation Steps

### P1: Linked-run workflow scope

1. **Split the linked-run collector** (`scripts/workflow_failure_heal_intake.sh`, `_heal_collect_linked_runs`, around lines 2528–2623).
   - Add `_heal_fetch_linked_runs`, called before the `SCOPE_INPUTS_FROZEN` `jq -n` (around line 2513). It does link discovery (`linked-runs`), the run read, the identity checks and the jobs read, writing `${RUNTIME_DIR}/linked_runs/run-<id>.json` and `-jobs.json`. Keep the existing `warn linked_run_skipped` reasons.
   - Reduce `_heal_collect_linked_runs` to the timeline and log stage, reading those files. Its API budget, log lines and output file are unchanged.
   - The fetch runs whenever `MAX_LINKED_RUNS > 0`, as today, independent of the new switch.
2. **Make the parent run's actor available.** Locate where the intake reads each reported run for `verify_run` (`verify_run_provenance`, around line 4203, and its intake caller) and read the copy the intake already writes, `${LOG_DIR}/run-${run_id}-info.json` (around line 1583, copied from `${PROVENANCE_DIR}/run-${run_id}.json` when present). Pass that file as `--parent-run-json`. Do not add another copy. No new call.
3. **Add `heal-scope linked-scope`** to `scripts/workflow_failure_heal.py` (operation choices around line 7991, handler next to `render` around line 7200).
   - Inputs: `--linked-dir`, `--parent-run-json` (repeatable), `--parent-jobs-json` (repeatable), `--parent-log-map` (a JSON map `{linked_run_id: [{"run_id": parent_run_id, "job_id": failed_job_id}, …]}` emitted by `linked-runs`; extend that operation to scan each failed job's log separately and to add the parent run id and job id per link, additively), `--repo`, `--self-repo`.
   - It applies the provenance rules and reuses `verify_run(run_json, repo=repo, kind="workflow_run")` for `workflow_paths` (`referenced_paths`). After the case-insensitive identity check, `repo` is the linked run's own `repository.full_name`, passed verbatim, so the case-sensitive comparison inside `verify_run` matches by construction.
   - It copies `parent_run_id` (the verified parent whose failed job's log named the run), `event`, `head_branch` and `head_repository.full_name` from the linked run's record into its `linked_scope.json` entry, so P2 can apply its PR-scope check without another read.
   - It records the failing step with the shared selection rule and prints `heal_linked_scope.v1` JSON, with one `linked_run_scope` log line per run on stderr. Always exit 0; on any internal error print `{"schema":"heal_linked_scope.v1","runs":[]}`.
4. **Freeze.** When the switch is on and `repo == SELF_REPO`, apply the run cap of step 5 first, then write `linked_scope.json` (chmod 0444) before the freeze with only the linked runs that fit, and add `linked_workflows` and `linked_runs` arrays to `SCOPE_INPUTS_FROZEN`. Absent or empty means `[]`. Every run in `linked_scope.json` is therefore also in the marker's `runs=`, so a run dropped by the cap cannot seed the P2 closure.
5. **Render input** (around line 3062). For `scope_workflows_key=self_workflows`, set `workflow_paths` to `self_workflows + linked_workflows` (deduped, order preserved) and `runs` to `runs + linked_runs`, capped at 3 refs in total, because `_heal_scope_marker_shape_ok` rejects more than 3 runs and the heal would not be filed. Verified parent runs come first. A linked run that does not fit is left out entirely, with its workflow paths, and logged `linked_run_scope run=<id> outcome=rejected reason=run_cap`. The consumer branch is unchanged.
6. **Switch parsing** follows `SCOPE_RECOVERY_SWITCH` (around line 234). Document it in the intake header comment block. Add `WORKFLOW_HEAL_LINKED_RUN_SCOPE_ENABLED: ${{ vars.WORKFLOW_HEAL_LINKED_RUN_SCOPE_ENABLED || 'true' }}` to the env blocks of the `intake`, `recovery-candidates` and `recovery-scan` jobs in `.github/workflows/workflow-failure-heal-intake.yml` (env lines only, next to `HEAL_SCOPE_RECOVERY_ENABLED`; check §27 with `wc -c`). The recovery jobs need it because they re-run the intake and, from P3 on, compute `heal_scope_render_version()`.
7. **Fragments:** `changelog.d/<N>-heal-linked-run-scope.md` (`changed`), and `agents.d/<N>-heal-linked-run-scope.md` targeting `Workflow architecture` (amend the #8248 paragraph's "never feeds the scope" with the qualified exception) and `Stable log prefixes (contractual)` if the `WORKFLOW_HEAL` outcome tokens are registered there.

### P2: Failing-step script closure

1. **Add the helpers** in `scripts/workflow_failure_heal.py`: `heal_scope_select_failing_step(jobs_json)` (shared rule; P1 may already contain an equivalent, in which case reuse it), `heal_scope_match_step_run(checkout, ref, workflow_paths, job_name, step_name)`, `heal_scope_extract_script_refs(text, is_python)`, and `heal_scope_script_closure(seeds, checkout, ref, max_depth=3, max_reads=200)`. Each function name is grepped for uniqueness first. The module imports only the standard library, so import PyYAML inside a `try` with a `yaml = None` fallback (as `scripts/orchestrate_lib.py` does); when it is unavailable, every seed is skipped with `reason=yaml_error`. A test pins this under `python3 -I -B`.
2. **Add `heal-scope closure`**: `--seeds-json`, `--checkout`, `--ref`. It prints `{"paths":[…],"seeds":n,"matched":n}` and always exits 0, printing an empty result on error.
3. **Extend `render_heal_scope_marker`** with keyword-only `closure_paths: Iterable[str] = ()` and `closure_reserve: int = 0`, using the render order in Approach. The `heal-scope render` input JSON gains optional `closure_paths`.
4. **Intake wiring** (around lines 3061–3064).
   - When the switch is on and the heal is self-repo, build the seeds: parent runs from `${LOG_DIR}/run-<id>-jobs.json` with their verified `referenced_paths`, plus `linked_scope.json` runs whose `step` is non-null when that file has a valid schema (P1 writes only runs that also appear in `runs=`).
   - Run `heal-scope closure` against `scope_checkout` / `scope_ref`, add `closure_paths` to `scope_inputs.json`, pass the reserve, and log `scope_closure`.
   - The seeds come only from the frozen and verified files listed above.
   - Skip closure seeding for a verified run whose own run record shows it was started by a pull request: `event` is `pull_request` or `pull_request_target`, or `head_repository.full_name` differs from the run's `repository.full_name`. Log `scope_closure_seed_skipped run=<id> reason=pr_scope`.
   - Also skip it unless the run's `head_branch` is the default branch (`PROVENANCE_DEFAULT_BRANCH`, which the intake already resolves from `WORKFLOW_HEAL_DEFAULT_BRANCH` or the event's `repository.default_branch`, around line 1189), `stable` or `promote-candidate`; log `reason=branch_untrusted`. A same-repo `push`, `schedule` or `workflow_dispatch` run on any other branch is therefore not seeded, because its content at `checkout_ref` (the run's head SHA for every `workflow_run` heal, intake around line 3036) has not been through review. Release-gate and promotion runs, such as #8287's (on `promote-candidate`), are seeded. An empty default branch means only `stable` and `promote-candidate` are accepted.
   - Read `event`, `head_branch`, `head_repository` and `repository` from the frozen `${LOG_DIR}/run-<id>-info.json` (no API call, no git ancestry check, no extra fetch). A missing or unreadable run record, or a missing field, skips the seed with `reason=run_info_unavailable`.
   - Apply the same two checks to each `linked_scope.json` seed, using its own `event`, `head_branch` and `head_repository` fields (a `null` field means `run_info_unavailable`). For a linked seed, `head_repository` is compared case-insensitively with the source repository (`SELF_REPO`): provenance rule 2 already requires the linked run's `repository.full_name` to equal it, so no extra field or read is needed. A linked seed whose `parent_run_id` names a parent run that was skipped by either check is skipped too, with `reason=parent_skipped`, so a PR-scoped parent cannot reach the closure through a child it dispatched.
5. **Workflow env:** add `WORKFLOW_HEAL_SCRIPT_CLOSURE_ENABLED: ${{ vars.WORKFLOW_HEAL_SCRIPT_CLOSURE_ENABLED || 'true' }}` and `WORKFLOW_HEAL_SCRIPT_CLOSURE_RESERVE: ${{ vars.WORKFLOW_HEAL_SCRIPT_CLOSURE_RESERVE || '10' }}` to the same three jobs' env blocks (env lines only; check §27 with `wc -c`).
6. **Fragments:** `changelog.d/<N>-heal-script-closure.md` and `agents.d/<N>-heal-script-closure.md` (`Workflow architecture`).

### P3: Plan-hold re-check

1. **Candidates** (`scripts/workflow_failure_heal_recovery_scan.sh`). The existing jq filter (around line 187) drops every issue whose body contains `ai:workflow-heal-scope`, which is exactly what rescope candidates carry. When `HEAL_SCOPE_RESCOPE_ENABLED` is on, build a second candidate list from the same nodes file, and leave the recovery filter unchanged. jq cannot compute the `denied=` sha256 or the render version, so this list comes from a new `workflow_failure_heal.py heal-scope rescope-candidates --nodes <file> --login <login> --max-attempts <n>` operation run with `python3 -I -B "${HEAL_PY}"`, the same scheduled default-branch checkout copy the scan's other calls use (`scripts/workflow_failure_heal_recovery_scan.sh` around line 78); no extra staging step is added. It decodes the hold comment's paths, computes `denied=<sha256-12>` and `heal_scope_render_version()` in Python, applies the rules below, and prints one JSON object per candidate (`number`, `denied`, `render`, base64 denied paths). It makes no API call and prints nothing on any internal error, so the rescope list is empty and the recovery list is unaffected. Keep open heal issues that meet all of these:
   - authored by the token login, labelled `ai:workflow-heal`, carrying a scope marker;
   - with a pipeline-authored `ai:automation-path-plan-hold:v1` comment carrying `reason=heal_scope_plan_out_of_scope` and `source=plan`. Pick the newest such comment; holds with any other reason (for example a later poller hold, which never carries this reason because `_plan_auto_approval_gate` runs the gate without `--heal-scope-file`) are ignored when picking it;
   - where that hold comment is newer than the issue's newest pipeline-authored plan comment (first line or marker `<!-- ai:plan-proposal:v1 -->`), compared by `(createdAt, databaseId)` as `.github/workflows/plan.yml` does (around lines 2881–2885). The listing query's comment nodes are extended from `{body author{login}}` to `{body author{login} createdAt databaseId}`. The 30 listed comments are the newest ones, so a plan comment that is not among them is older than every listed comment, including the hold; a hold that is not among them makes the issue no candidate. A hold that a later plan superseded, for example after `/reclarify`, does not count;
   - with no open PR referencing the issue. The data comes from the same GraphQL listing query, extended with `timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], last: 20)` and the source pull request's `state`. That adds no call; a missing or unreadable field excludes the issue;
   - with no pipeline-authored `ai:heal-rescope:v1 outcome=replaced` or `outcome=needs_human` comment for the same `denied=<sha256-12>` (sha256 of the sorted, decoded denied paths, first 12 hex) and the same `render=<n>` as the running code's `heal_scope_render_version()`. `outcome=failed` markers do not exclude the issue. A `needs_human` result recorded before P1 or P2 merged carries an older `render=` value, so the heal is evaluated once more after each scope phase lands instead of being skipped for good. A marker without `render=` counts as `render=0`;
   - with fewer than `HEAL_SCOPE_RECOVERY_SCAN_MAX_ATTEMPTS` pipeline-authored `ai:heal-rescope:v1 outcome=failed` comments for the same `denied=` and `render=`. The existing recovery cap filter (around lines 188–191) counts only `ai:heal-scope-recovery-scan:v1` markers, so `rescope-candidates` counts its own `failed` markers.

   Process at most `HEAL_SCOPE_RECOVERY_SCAN_MAX_PER_TICK` candidates (shared with recovery).

   **Count.** With `--list-only`, the `count=` output (around lines 194–196) becomes the recovery candidate count plus the rescope candidate count, so the `recovery-scan` job (`needs.recovery-candidates.outputs.count != '0'` in `.github/workflows/workflow-failure-heal-intake.yml`) also starts when only held heals exist. With the switch on, the `heal_scope_recovery outcome=scan reason=listed count=` log line gains a trailing `rescope=<n>` field. With the switch off the field is not printed, so the log line and `count=` output are byte-identical to today.
2. **Reconstruct** (`scripts/workflow_failure_heal.py`, next to `reconstruct_recovery_payload`, around line 3920). Add `reconstruct_rescope_payload`, with a `rescope` operation, for `workflow_run` heal bodies. `reconstruct_recovery_payload` stays unchanged and autofix-only. The new function reads the pipeline-written source run URL, target branch and head SHA, then builds the payload with `build_workflow_run_payload` from the run read through the API (see Approach → Re-check). It refuses an edited body, unless `verify_heal_scope_pipeline_edits` accepts it, and refuses missing markers, as today.
3. **Intake origin `rescope_scan`** (`scripts/workflow_failure_heal_intake.sh`).
   - Gate the origin as described in Approach → Re-check (`schedule` only, switch on, `SOURCE_KIND=workflow_run`, expected fingerprint and issue from the scan's environment; otherwise `rescope_scan_invalid`).
   - **Eligibility.** Add `heal_scope_rescope_check` (`workflow_failure_heal.py`, next to `heal_scope_recovery_check`, which stays unchanged, with a `heal-scope-rescope check` operation). It is eligible only when the expected issue is open, authored by the token login, its scope status is `verified` (directly, or through `verify_heal_scope_pipeline_edits` when edited), and no comment by that login carries an `ai:heal-scope-recovery:v1` marker; it fails closed on an unknown identity. Add `_heal_scope_rescope_evaluate` to the intake, run only for the `rescope_scan` origin instead of `_heal_scope_recovery_evaluate`. On eligibility it re-runs the budget without the original exactly as the recovery path does (around lines 2129–2160) and sets `HEAL_SCOPE_RECOVERY_ACTIVE`, `HEAL_SCOPE_RECOVERY_ISSUE` and `HEAL_SCOPE_RECOVERY_REPO`, so the existing `REPORT_AUTH=reconstructed` exit check (around line 2174) and the replacement helpers accept it. Anything else logs `heal_rescope outcome=skip reason=<token>` and exits 0 without filing.
   - Accept `HEAL_RESCOPE_PRIORITY_PATHS_FILE` (decoded denied paths) only for this origin. Require the recomputed fingerprint to equal the issue's `fp`, as recovery does.
   - Render with `priority_paths` = denied ∩ (crash file ∪ workflow paths ∪ linked workflow paths ∪ uncapped closure ∪ test subjects ∪ changed files). Add a keyword-only `priority_paths: Iterable[str] = ()` to `render_heal_scope_marker`, placed right after `crash_file` and filtered to members of the other inputs inside the function as a second guard.
   - The comparison runs after the scope render and before `gh issue create`. If every denied path is in the rendered marker: file the replacement through the recovery replacement helpers. The body is the one the intake composes from the reconstructed, re-verified report, with the newly rendered marker, plus a `Re-issued from #<N> (heal rescope)` line; as on the recovery path, the original body is never an input. Post `ai:heal-rescope:v1 outcome=replaced` and the existing `ai:heal-scope-recovery:v1 replaced_by=` marker on the original, and close it as `not_planned` with a comment.
   - Otherwise post `ai:heal-rescope:v1 outcome=needs_human reason=outside_scope` (with `render=<heal_scope_render_version()>`) listing the outside paths and send one Telegram WARNING through the existing helper.
   - A failure in either path posts `outcome=failed reason=<token>` (same `denied=` and `render=`), counted by the rescope attempt cap in step 1.
4. **Workflow env:** add `HEAL_SCOPE_RESCOPE_ENABLED` plumbing to the `recovery-candidates` and `recovery-scan` jobs in `.github/workflows/workflow-failure-heal-intake.yml` (env lines only; check §27 with `wc -c`).
5. **Fragments:** `changelog.d/<N>-heal-plan-hold-rescope.md` and `agents.d/<N>-heal-plan-hold-rescope.md`. The agents fragment documents the `ai:heal-rescope:v1` marker (its fields, the trusted author and the `render=` re-arm), the dependency on `HEAL_SCOPE_RECOVERY_ENABLED`, and the `WORKFLOW_HEAL heal_rescope` log line under `Workflow architecture`, unconditionally. `heal_rescope` is a token under the existing `WORKFLOW_HEAL` prefix, so no new prefix bullet is needed unless the implementer adds a new prefix.

### P4: Clarify scope facts and rule

1. **Facts block** (`scripts/clarify_github_facts.py`). When `HEAL_CLARIFY_SCOPE_FACTS_ENABLED` is on and the file passed with a new optional `--heal-scope-file` argument (the issue body only, never a comment file such as `clarification_comment.txt`, which carries untrusted text) contains exactly one `HEAL_SCOPE_RE` match (the file imports only the standard library today, so import the regex from `workflow_failure_heal.py` after inserting `Path(__file__).resolve().parent` into `sys.path`, inside a `try`. On any import error print no block, because the facts step fails open. A test pins the import under `python3 -I -B`), append:

   ```
   HEAL SCOPE (fixed by the pipeline; editable paths): <paths, comma-separated>
   ```

   No API call is added. With no marker, nothing is printed.
2. **Prompt rule.** Add one paragraph to the decision rules of `prompts/_templates/mode-clarify-respond.txt` and `prompts/mode-clarify-respond.txt`, and to the question rules of `prompts/_templates/mode-clarify.txt` and `prompts/mode-clarify.txt`. Use identical text in each pair:

   > For a workflow heal (`ai:workflow-heal`), the editable paths are fixed by the pipeline (see HEAL SCOPE when present). Never ask a question, offer an option or choose an answer that widens that scope or re-issues the heal. Decide within the scope; if the fix needs a path outside it, say so plainly and let the plan gate hold the plan.
3. **Workflow:** pass the switch to the facts step in `.github/workflows/orchestrate_clarify_respond.yml` if the step's env does not already inherit repo vars that way (env line only), and add `--heal-scope-file "${RUNTIME_DIR}/issue_body.txt"` to its `clarify_github_facts.py` call. Without that argument no block is printed.
4. **Fragments:** `changelog.d/<N>-heal-clarify-scope-facts.md` and `agents.d/<N>-heal-clarify-scope-facts.md`.

## Files & Modules

- `scripts/workflow_failure_heal_intake.sh` (P1, P2, P3)
- `scripts/workflow_failure_heal.py` (P1, P2, P3)
- `scripts/workflow_failure_heal_recovery_scan.sh` (P3)
- `.github/workflows/workflow-failure-heal-intake.yml` (P1, P2, P3, env only)
- `scripts/clarify_github_facts.py` (P4)
- `prompts/mode-clarify.txt`, `prompts/_templates/mode-clarify.txt`, `prompts/mode-clarify-respond.txt`, `prompts/_templates/mode-clarify-respond.txt` (P4)
- `.github/workflows/orchestrate_clarify_respond.yml` (P4, env only, if needed)
- Tests:
  - `tests/test_workflow_failure_heal.py` (P1, P2, P3)
  - `tests/test_workflow_failure_heal_scope_tests.py` (P2)
  - `tests/test_workflow_failure_heal_recovery_scan.py` (P3)
  - `tests/test_clarify_github_facts.py` (P4)
  - `tests/test_clarify_respond_standalone.py` (P4, prompt-rule presence)
- `changelog.d/<N>-*.md` [new] ×4 and `agents.d/<N>-*.md` [new] ×4

Existing test files are extended rather than new ones added, because `tests/test_ci_wires_every_test_file.py` requires every test file to be wired in `ci.yml`. A phase that does add a test file wires it in the same PR.

## Tests

### P1 (unit, in `tests/test_workflow_failure_heal.py`)

- `linked-scope` accepts a #8287-shaped fixture: parent run 40746491161 with the failed job window 18:39–19:40Z and `triggering_actor` `shubhodeep1`; linked run 40752760153 with `created_at` 19:16:08Z, conclusion `cancelled`, the same actor, and `referenced_workflows` naming `memory_maintenance.yml`. Expected workflow paths: `internal-memory-maintenance.yml` and `memory_maintenance.yml`. Expected step: `Extract repository learnings (fail-open)` with selection `cancelled`.
- Run cap: one parent run plus three qualifying linked runs yields `runs=` with 3 refs, and the last linked run is logged `reason=run_cap` with its workflow paths absent and is not written to `linked_scope.json`. `check-marker` accepts the marker.
- One rejection test per provenance reason: conclusion `success`, `created_at` outside the window, actor mismatch, actor missing, consumer heal, and a foreign-repo `referenced_workflows` entry being dropped.
- Intake-level: with the switch off, `SCOPE_INPUTS_FROZEN` and the marker are byte-identical to a golden fixture; with it on, the marker gains both paths and `runs=` gains the linked ref.
- The evidence prompt section is byte-identical with the switch on or off.
- API call count per linked run stays 3 (stub `gh` and count invocations).

### P2 (unit)

- Step selection: failed, cancelled and longest cases.
- Matching: exact, reusable `caller / callee`, a matrix suffix, an unnamed step (skipped), duplicate names (skipped), and missing `run:` (skipped).
- Extraction: `${DIR}/ai_memory.py`, `bash scripts/x.sh`, `source scripts/x.sh`, `. scripts/x.sh`, Python import forms, a non-existent name (dropped), and a nested path (dropped).
- PR scope: a run record with `event=pull_request` (or a foreign `head_repository`) yields no closure and logs `reason=pr_scope`; a same-repo `push` or `workflow_dispatch` run on a feature branch logs `reason=branch_untrusted`; a `workflow_dispatch` run on `promote-candidate` (the #8287 shape) is seeded; a missing run record logs `reason=run_info_unavailable`; a linked seed on an untrusted branch is skipped, and a linked seed whose parent was skipped logs `reason=parent_skipped`.
- Extraction keeps the `$(dirname "${BASH_SOURCE[0]}")/x.sh` sibling idiom; a seed with no `run:` logs `reason=step_no_run`.
- Closure on a temp git repo mirroring the prototype: depth limit, smallest-depth wins, deterministic order, the 200-read cap.
- Render order: the reserve placement, leftover filling, the 20 cap, and byte-identical output with an empty closure.
- Integration on the real tree at a pinned commit: the seeds from #8287 yield `scripts/ai_memory_lib.py` within the first 20 paths.

### P3 (recovery-scan harness, extending `tests/test_workflow_failure_heal_recovery_scan.py`)

- Candidate filtering: a foreign-author hold comment is ignored; a later poller hold with another reason does not hide the plan hold; a hold older than the newest plan comment is ignored; an open PR excludes the issue; a `replaced` or `needs_human` marker for the same `denied=` hash and the current `render=` excludes the issue, the same hash with an older `render=` is evaluated again, and an `outcome=failed` marker excludes it only once `HEAL_SCOPE_RECOVERY_SCAN_MAX_ATTEMPTS` of them exist.
- Count: with no markerless recovery candidate and one held heal, `--list-only` writes `count=1`; with the switch off it writes `count=0` and the listed log line has no `rescope=` field.
- `rescope-candidates` computes `denied=` from the decoded hold paths and excludes an issue with a matching `replaced` or `needs_human` marker, and prints nothing on malformed input.
- `heal_scope_render_version()` returns 1, 2, 3 and 4 for the four combinations of operation presence and switch state.
- Replaced path: exactly one replacement, the original closed `not_planned`, both markers posted. `heal_scope_rescope_check` accepts a verified-marker issue that `heal_scope_recovery_check` rejects, and rejects a foreign author, an unknown identity and an issue already carrying a trusted recovery marker.
- `needs_human` path: one comment and one WARNING, no retry on the next tick.
- A fingerprint mismatch posts `failed`.
- With P1/P2 absent (closure operation missing), every candidate takes `needs_human`.
- `priority_paths` cannot introduce a path outside the derived candidate set (unit test on `render_heal_scope_marker`).

### P4

- The facts block appears only with exactly one marker in the `--heal-scope-file` file; a marker in another `--text-file` input yields no block.
- The switch off prints nothing.
- The prompt pairs contain the identical rule text.
- Existing prompt-contract tests pass (`tests/test_render_prompt_core_modes.py`, `tests/test_phase_prompt_untrusted_context_contract.py`).

### End-to-end (per phase, after merge)

- **P1+P2:** the next heal intake for a release-gate child-run failure logs `linked_run_scope … outcome=added` and `scope_closure … paths>0`, and its marker names the child workflow and its scripts.
- **P3:** the hourly scan logs `heal_rescope` for any held heal.

## Risks & Mitigations

- **A wider unattended edit surface on `stable`.** A closure can name widely shared modules (`ai_memory_lib.py` is used by every pipeline). Mitigation: the 20-path cap, the existing plan gate, review and release gate all still apply. ACCEPTED: this is the purpose of the change.
- **Log text chooses which run is followed** (untrusted). Mitigation: the provenance rules (time window, same triggering actor, same repo, failed conclusion) tie the run to the failing job. Evidence-only behaviour is unchanged for runs that do not qualify.
- **Basename matching over-matches** (a `foo.py` mentioned in an `echo`). Mitigation: candidates must exist as top-level `scripts/` files at the scope commit. Over-inclusion only grants edit permission and does not cause edits; the reserve and cap bound it.
- **Cap pressure.** #8287 already uses 18 of 20 slots, so with changed files present, deep closure entries may be truncated. Mitigation: nearest-first ordering, the `truncated=` log, and P3 re-renders with denied paths prioritised.
- **The replacement re-runs clarify and plan** (model cost, delay). ACCEPTED: rare; the alternative is a human session.
- **The P4 prompt rule is unswitched text.** ACCEPTED: rollback by revert, the same precedent as the unswitched edits in `docs/plans/heal-issues-self-resolve-plan.md` G7.
- **The P1 collector split changes execution order.** Mitigation: a byte-identical evidence-section test and an unchanged per-run API count test.
- **Step names that collide across reusable workflows.** Mitigation: an ambiguous match skips the seed (fail closed, Q8: A).

## Rollout

- All switches default on, so each phase is active on merge. With a switch off, that phase does nothing at all: P3 builds no rescope candidate list, dispatches no `rescope_scan` intake and posts no `ai:heal-rescope:v1` comment or WARNING, and the intake rejects the origin. `HEAL_SCOPE_RESCOPE_ENABLED=false` turns off rescope alone, both the candidate scan and the `rescope_scan` origin, and leaves the recovery scan running. Rescope runs inside the `recovery-candidates` and `recovery-scan` jobs and `scripts/workflow_failure_heal_recovery_scan.sh`, which `HEAL_SCOPE_RECOVERY_ENABLED` gates, so rescope also needs that switch on: `HEAL_SCOPE_RECOVERY_ENABLED=false` turns off both. The agents fragment states this dependency. To disable one, set the repo variable or workflow env to `false`. The workflow env blocks list repository variables one by one, so each phase adds its own switch lines to the `intake`, `recovery-candidates` and `recovery-scan` jobs (P1 step 6, P2 step 5, P3 step 4); P4 adds its line to `.github/workflows/orchestrate_clarify_respond.yml`.
- Heals run only in this repository (`shubhodeep1/coding-workflows`). Consumer repos (§14) receive no behaviour change: consumer heals are excluded (Q6: A), and the clarify prompts change only text that applies to `ai:workflow-heal` issues, which are filed here.
- Changes reach the release gate's workflows on the next promotion, like any other change.
- After P1 and P2 merge, #8287-style failures should produce in-scope plans. Watch the first heal's `scope_closure` and `linked_run_scope` log lines and the plan gate decision.

## References

- Incident: release run 40746491161, child run 40752760153, heal #8287, fix PR #8288.
- Linked runs: #8248 (`agents.d/8248-heal-intake-linked-runs.md`).
- Scope freeze: #6463. Recovery: #7191, #7323, #7333. Pipeline body edits: #7444.
- Related plan: `docs/plans/heal-issues-self-resolve-plan.md`.
- Code: `scripts/workflow_failure_heal.py` (`render_heal_scope_marker`, `verify_run`, `verify_heal_scope*`, `reconstruct_recovery_payload`), `scripts/workflow_failure_heal_intake.sh` (freeze, linked runs, scope render), `scripts/files_touched_scope_guard.py` (plan gate), `scripts/workflow_failure_heal_recovery_scan.sh`, `scripts/clarify_github_facts.py`.
