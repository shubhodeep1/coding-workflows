# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/) per `docs/release-policy.md`.

## [Unreleased]

### Added
- **Consumer security audits + change-gated scanning.** The weekly security audit and workflow retro now run by default, and the audit reaches every consumer repo. `.github/workflows/security-audit.yml` gained a `workflow_call` trigger and a new synced wrapper `workflow-templates/ai-security-audit.yml` (weekly `0 8 * * 0` + manual dispatch, registered in `workflow-templates/profiles/full.txt`), so each of the repos in `.github/ai/consumer_repos.json` audits its own default branch after the next `@stable` bump — a consumer run stages this repo's `scripts/` + `prompts/` from a `@stable` support checkout and needs `OPENROUTER_API_KEY` (plus optionally `GH_PAT`) in the consumer repo. Two cost gates keep the weekly spend proportional to actual change: `scripts/security_audit.sh` records the audited HEAD on the tracker issue body (marker `<!-- ai:security-audit-last-sha:… -->`), skips entirely when HEAD is unchanged (`SECURITY_AUDIT_SKIP_IF_UNCHANGED`, default `true`, log-only skip), and otherwise audits only the commits since the last audited SHA (`SECURITY_AUDIT_INCREMENTAL`, default `true`; the post-filter drops findings citing unchanged files as `suppressed_out_of_scope`; first runs, history rewrites, and diffs over 200 files fall back to the full scope). The weekly retro similarly skips zero-activity windows — no workflow runs and no merged PRs, surfaced as the additive `has_activity` field in the `workflow_retro.v1` JSON — when `WORKFLOW_RETRO_SKIP_IF_NO_ACTIVITY` (default `true`) is set, leaving a `WORKFLOW_RETRO_SKIP_V1:` log line instead of an LLM pass, tracker comment, or Telegram alert. Refs #3496.
- **Per-consumer weekly retros via centralized fan-out.** The source repo's weekly-retro job gains a `Consumer retro fan-out` step (`WORKFLOW_RETRO_CONSUMER_FANOUT_ENABLED`, default `true`) running the new `scripts/workflow_retro_fanout.sh`: because collect-logs already gathers every consumer repo's workflow runs into the workflow-log report artifact, the script loops the `.github/ai/consumer_repos.json` roster (source repo excluded), builds each repo's retro from that artifact with `scripts/workflow_retro.py --repo <slug>`, honors the consumer's own `WORKFLOW_RETRO_ENABLED` repo var (one fail-open GET per consumer per week), skips no-activity repos, and upserts the week-marked narrative onto that consumer's `AI Workflow Weekly Retro` tracker issue via `GH_PAT`. Per-repo outcomes are logged as `WORKFLOW_RETRO_FANOUT_V1: repo=… status=…`; individual failures fail open and the step errors only when every attempted consumer fails. Consumers need no new secrets or workflows for retros. The consumer-facing clarify gate in `.github/workflows/clarify.yml` now also skips issues labeled `ai:security-audit` / `ai:retro` (mirroring `.github/workflows/internal-clarify.yml`) so the tracker issues this fan-out and the consumer security audit create never trigger a consumer clarify run. Refs #3496.
- Capacity-fallback editor model for the codex retry loops. When the primary editor model saturates its provider capacity, the plan, implement, and review/autofix retry loops now switch to a fallback model on their final attempt instead of burning that attempt on the same overloaded model.

  The trigger was AI Plan run `28640359211` (consumer issue #3515): OpenRouter's pooled OpenAI org sat at its per-model TPM ceiling for `gpt-5.4` (180M/180M) for a sustained ~10 minutes, so all three plan attempts hit the same saturated bucket and the run failed. A new repo var `WORKFLOW_EDITOR_FALLBACK_MODEL` (default `openai/gpt-5.5`) names a model in a different provider capacity bucket. On the final attempt of each codex editor loop, the loop switches `--model` to that slug via the `MODEL_EDITOR_FALLBACK` env; earlier attempts still use `WORKFLOW_EDITOR_MODEL` (`gpt-5.4`) unchanged. `openai/gpt-5.5` is now declared in `scripts/codex_model_catalog.json` (fields mirror `gpt-5.4`) so `apply_patch`/verbosity resolution stays intact when the switch fires — the `--model` CLI flag overrides the config's `model` while still resolving against the same `model_catalog_json`.

  | The numbers that matter | Value |
  | --- | --- |
  | New repo var | `WORKFLOW_EDITOR_FALLBACK_MODEL` (default `openai/gpt-5.5`) |
  | Workflows wired | `.github/workflows/plan.yml`, `.github/workflows/implement.yml` (main pass), `.github/workflows/review_autofix.yml` (`scripts/review_apply_fixes.sh`) |
  | Fires on | final attempt only (plan 3/3, implement 5/5, review editor 3/3) |
  | Catalog entry added | `openai/gpt-5.5` in `scripts/codex_model_catalog.json` |
  | Regression test | `tests/test_editor_capacity_fallback_contract.py` (5 cases) |

  What this means for operators: sustained per-model capacity crunches on `gpt-5.4` no longer guarantee a failed run — the last retry escapes to a different bucket automatically, and the switch is off unless `WORKFLOW_EDITOR_FALLBACK_MODEL` differs from `WORKFLOW_EDITOR_MODEL`. Point it at any catalog-declared slug to change the fallback, or set it equal to the primary (or clear it) to disable the behavior.

  For contributors: the implement repair pass (`implement_repair`, a codex-thread continuation with a `replace-prefix` transform) and the parallel reviewer models are intentionally out of scope — the repair pass is not the token-heavy call that hits TPM saturation, and swapping its model mid-continuation is riskier. The reviewer roster keeps its own `scripts/reviewer_failback_chains.json` mechanism. `openai/gpt-5.5`'s catalog fields are mirrored from `gpt-5.4` (same OpenAI generation) and should be verified against the live model before relying on non-editor behavior. `WORKFLOW_EDITOR_MODEL`, `MODEL_EDITOR`, and the `model` config key are unchanged (`CLAUDE.md` §6).
- **`/apply-url`** — new read-only interactive slash command, shipped byte-identical in both `.claude/commands/apply-url.md` (this repo's interactive sessions) and `workflow-templates/.claude/commands/apply-url.md` (the consumer template synced downstream), mirroring the existing `/analyze-log` / `/apply-analysis` split. Takes **one or more URLs** plus an optional free-form focus in `$ARGUMENTS`, fetches the seed page(s) (`WebFetch` for public text, `pdftotext` for PDFs, `agent-browser` for JS/auth-walled pages per §17), then **follows in-content links/pagination a bounded depth** — same registrable domain, depth ≤2, ~15-page cap, dedupe, stop on diminishing returns — until it has a full grasp of the resource. It loads repo context first (`README.md`, `agents.md`, `CLAUDE.md`, relevant `/db/contracts/*`) so every finding is **anchored to this repo**, then maps each extracted idea into one of four buckets — **IMPROVEMENT** (strengthens existing code, cites the `file:line` it touches), **NEW-FEATURE** (net-new capability that fits, cites where it lands), **ALREADY-PRESENT** (cite where), **NOT-APPLICABLE** (one-line why) — grounding each recommendation in **both** the source (page/section) and the repo (`file:line`). Recommendations are ranked by §1 priority order (security/correctness before perf/speed) and carry §6 (alias-not-rename), §10 (`/db/contracts/*` update), and §18 (scheduler-wired, not manual) flags where relevant. The command is **read-only**: no edits, no files written, no commits, no PR — its deliverable is the chat report, with a pointer to `/write-plan` → `/implement-plan-claude` (or `/apply-analysis`) as the ship path. A source idea with no repo `file:line` anchor goes under Not-applicable, never as a floating suggestion (no repo citation → no recommendation). Honors §0/§2 (stop and ask on zero/inaccessible URLs or content too thin to map), §7, §9, §17. Propagates to every repo in `.github/ai/consumer_repos.json` via the existing `update_workflows.yml:136-195` `.claude/` sync step (daily 04:00 UTC cron + `repository_dispatch` on `@stable` release + manual dispatch); propagation lands on the next sync run after this merges to `main` and `stable` is bumped. Additive command doc only — no source files, schemas, or workflows changed.
- Six new interactive slash commands, each shipped in both `.claude/commands/<name>.md` (this repo's interactive sessions, read at `main`) and `workflow-templates/.claude/commands/<name>.md` (the consumer template synced downstream), mirroring the existing `/write-plan`, `/investigate-issue`, `/analyze-log` split:
  - **`/implement-plan-claude`** — the in-session implementation variant. Resolves the plan doc from `$ARGUMENTS` (typically `docs/plans/<slug>-plan.md`), implements it **directly in the Claude Code session**, then re-verifies completeness against the *actual* repo (runs the plan's tests/linters, greps for the code/wiring with `file:line` evidence) and **only on a clean sweep** `git mv`s the doc into `docs/completed/`; otherwise leaves it in `docs/plans/` and reports what remains. Branches, commits grouped by scope (§12.E), pushes, and opens a ready-for-review PR against the dynamically-resolved default branch. Honors §5/§6/§7/§9/§10/§14/§15/§18/§19. Repo-local and consumer-template copies are byte-identical. (Replaces the earlier single `/implement-plan` command, which was split into this in-session variant plus `/implement-plan-ai`; both were unreleased.)
  - **`/implement-plan-ai`** — the orchestrator hand-off variant. Resolves the same plan doc, then dispatches the AI orchestrator's `workflow_dispatch` trigger (`ai-orchestrate.yml` in consumer repos, `internal-orchestrate.yml` in this library — auto-detected) with the plan fed in as a **reference-only** `project_description` (first line = the tracking-issue title; an instruction preamble telling the orchestrator to preserve the plan's `Phases & Merge Strategy` split; the plan's path + branch + PR URL so the orchestrator's codex agent can read it from the branch/PR even before it merges). The orchestrator then decomposes the work into a dependency DAG of issues, opens an `ai:orchestrator-tracking` issue, and ships each phase as its own PR. This command opens **no** PR and moves **no** doc itself — that deliberately avoids the `lint-plan-archival.yml` gate and the premature-"done" archival trap from `docs/postmortems/2026-05-18-project-2734-stall.md`; the orchestrator owns implementation, the per-phase PRs, and archiving the plan on verified completion. Reports the dispatched run + tracking issue. Honors §0/§2/§6/§18/§19. Repo-local and consumer-template copies are byte-identical.
  - **`/audit-plans`** — read-only. Reads every plan under `docs/plans/` (+ `docs/completed/` for dedup/regression), classifies each **completely / partially / not implemented** against current repo state with `file:line`/test citations (verified, not the plan's self-reported status), and recommends the single next highest-value plan to implement (weighing §1 priority order, dependencies, blast radius, effort). No edits, no PR. Byte-identical across both copies.
  - **`/apply-analysis`** — the orchestrator hand-off variant for the recommendation docs under `analysis/` (e.g. `workflow-optimization-*.md`; excludes the state files `last_collection_timestamp.txt` / `validation-selftest-status.json` and prior `recommendation-processing-report*.md`). Mirrors `/implement-plan-ai` but over the `analysis/` docs instead of a single plan: it dispatches the AI orchestrator's `workflow_dispatch` trigger (`ai-orchestrate.yml` in consumer repos, `internal-orchestrate.yml` in this library — auto-detected) with the selected docs fed in as a **reference-only** `project_description` (first line = the tracking-issue title; an instruction preamble telling the orchestrator to read each doc in full, **validate every recommendation against the *current* repo**, classify VALID&SAFE / VALID-BUT-RISKY / STALE / INVALID, **implement only the valid-and-safe subset** and defer the risky/contract-touching ones with rationale, honoring §1/§6/§10/§18; the docs' paths + branch + PR URL so the orchestrator's codex agent can read them from the branch/PR even before they merge). The orchestrator then decomposes the safe subset into a dependency DAG of issues, opens an `ai:orchestrator-tracking` issue, ships each phase as its own PR, and — on verified completion — owns the source-doc cleanup + `analysis/recommendation-processing-report.md`. This command **no longer applies recommendations in-session**: it opens **no** PR, deletes **no** doc, and writes **no** report itself; it reports the dispatched run + tracking issue. `$ARGUMENTS` optionally filters which docs are handed off (a specific doc / date / glob; empty = all). Honors §0/§2/§6/§18/§19. Byte-identical across both copies. (Replaces the earlier in-session apply variant, which validated-and-applied the safe subset and opened its own PR; that was unreleased.)
  - **`/verify-activation`** — read-only. Given an issue/PR/plan/feature reference in `$ARGUMENTS`, determines whether the project is fully implemented **and activated** — i.e. whether it will run automatically on its trigger or needs a manual step (a `*_ENABLED` flag defaulting off, an unset secret, a cron not yet on the default branch, a `workflow_dispatch`-only trigger, an unstarted supervisor, or — for consumers — an un-wired wrapper / un-bumped upstream pin). Emits LIVE / DORMANT (+ the exact activation steps) / INCOMPLETE. The consumer-template copy adds the `[CONSUMER]`/`[UPSTREAM]`/`[BOTH]` side classification and pins upstream reads to the consumer's resolved `UPSTREAM_SHA`; the repo-local copy reads this repo at `main`.
  - **`/validate-consumer-issue`** — validate-then-act. Validates a reported issue **and a proposed fix**: is the issue real/reproducible/correctly diagnosed (VALID-UPSTREAM vs CONSUMER-MISCONFIG vs NOT-REPRODUCIBLE), and is the fix CORRECT / CORRECT-BUT-INCOMPLETE / INCORRECT / UNNECESSARY (root-cause, completeness, §1/§5/§6/§10 safety, cross-consumer blast radius). Then **acts on the verdict like `/investigate-issue`**: when the issue is a genuine defect and the correct fix (the proposal, or a corrected/completed version derived from the evidence — never a proposal judged INCORRECT applied as-is) is fully evidence-based, safe, lands in a repo this session can push to, and needs no clarification, it implements the fix — apply, verify, commit, push, open a PR — via a Decision Rule that classifies findings EVIDENCE-BASED vs HYPOTHESIS. Otherwise it stays a read-only verdict: §6 (unaliased public rename/removal), §10 (collection/index change without its `/db/contracts/*` update), a cross-consumer break, multiple fixes with material tradeoffs, a HYPOTHESIS finding, or a blocking inaccessible resource all route to STOP-and-ask; CONSUMER-MISCONFIG routes back to the consumer with the exact setup change; NOT-REPRODUCIBLE asks for a repro. The repo-local copy judges inbound consumer reports against this upstream library at `main` and lands a valid fix on this repo's `stable` branch (branch off `stable`, PR base `stable`, never `main` — unless the request explicitly names a different target branch); the consumer-template copy ("validate own repo") classifies `[CONSUMER-INTERNAL]` vs `[UPSTREAM]`, pins upstream reads to the consumer's `UPSTREAM_SHA`, implements only the side this session can push to (CONSUMER-INTERNAL on the consumer's default branch; UPSTREAM read-write only when the repo *is* `shubhodeep1/coding-workflows`, landing on its `stable` branch), and otherwise routes an upstream-bound fix to a `shubhodeep1/coding-workflows` session as a proposed diff.
  - All five propagate to every repo in `.github/ai/consumer_repos.json` via the existing `update_workflows.yml:136-195` `.claude/` sync step (mirrors `workflow-templates/.claude/` → consumer `.claude/`; daily 04:00 UTC cron + `repository_dispatch` on `@stable` release + manual dispatch). Propagation lands on the next sync run after this merges to `main` and `stable` is bumped. These are additive command docs only — no source files, schemas, or workflows are changed.
- New `/write-plan` slash command at `workflow-templates/.claude/commands/write-plan.md` (mirrored at `.claude/commands/write-plan.md`). Takes a free-form task description in `$ARGUMENTS`, runs a CLAUDE.md §2-style Q1/Q2 clarification loop until every blocking ambiguity is resolved (including a slug-confirmation question used for both the filename and branch name), writes a detailed implementation plan to `docs/plans/<slug>-plan.md`, then opens a PR on a new `claude/write-plan-<slug>` branch targeting the repo's default branch (resolved dynamically via `gh repo view --json defaultBranchRef`, not hardcoded `main`). The command is plan-only — no source-file edits during the invocation; implementation comes later via `/investigate-issue` or direct work. Mandatory pre-task reads (`README.md`, `agents.md`, `CLAUDE.md`, relevant `/db/contracts/*.yml`) and explicit § citations (§6 naming immutability, §10 MongoDB rules, §14 consumer-repo propagation, §15 GitHub API hygiene) are baked into the prescribed plan structure. Branch collisions append `-2`, `-3`, … to the slug; force-pushes are forbidden. The command propagates to every repo in `.github/ai/consumer_repos.json` via the existing `update_workflows.yml:136-195` `.claude/` sync step (daily 04:00 UTC cron + `repository_dispatch` on `@stable` release + manual dispatch); propagation lands on the next sync run after this merges to `main` and `stable` is bumped.
- Stable-branch release flow. `test-and-mark-stable.yml` and `mark-stable.yml` are now both restricted to dispatches from the `stable` branch — a new `source` job in each rejects any other ref with a clear error pointing at `promote-main-to-stable.yml`. This prevents accidentally tagging `main` as stable and dragging in untested in-flight work. `resolve-version` (in `test-and-mark-stable.yml`) patch-bumps from the latest `vX.Y.Z` tag reachable from `stable`'s HEAD (`git tag --merged HEAD …`), so a stable release on top of `v1.4.2` becomes `v1.4.3` even when `main` is on a higher version. The `validate` and `release` jobs check out `stable` and the GitHub Release is created with `--target stable`. The moving `stable` git tag — what consumer repos pin to via `@stable` — and the `coding-workflows-stable-released` repository_dispatch are unaffected; consumers automatically pick up stable patch releases with no changes.
- New `forward-merge-stable-to-main.yml` — on every push to `stable`, attempts a clean direct merge into `main` so bug fixes can't be lost when `main` is later promoted to stable. Falls back to opening a PR if the merge has conflicts or branch protection rejects the direct push. `ci.yml` now also runs on push/PR to `stable` so the release-gate "Verify CI passed on source branch" check has data to inspect.
- `test-and-mark-stable.yml` now has a `sync-to-main` job that dispatches `forward-merge-stable-to-main.yml` after a successful stable release. Skipped on dry runs. This is belt-and-braces — the forward-merge already runs on every push to `stable` (typically via the bug-fix PR merge), but explicitly tying a sync to the release event ensures `main` can never silently drift behind a tagged stable release.
- New `promote-main-to-stable.yml` — single-shot workflow for promoting `main` to the new stable baseline (typical for minor/major version bumps). Validates that `stable` is fast-forwardable to `main`, fast-forwards `stable` to `main`'s HEAD, and dispatches `test-and-mark-stable.yml` on the freshly-updated `stable` branch. Forwards all `test-and-mark-stable.yml` inputs (`version_tag`, `test_repo`, `skip_e2e`, `dry_run`, `phase_timeout`, `review_timeout`) for parity. Refuses to run if `stable` has commits not on `main` (operator should investigate the divergence first; do not bypass via force-push). For BUG-FIX patch releases on the existing stable line, dispatch `test-and-mark-stable.yml` directly from `stable` instead.

- Pre-flight MCP handshake probe (`scripts/mcp_handshake_probe.py`) plus `probe_mcp_handshake` helper in `scripts/setup_serena.sh`. For each enabled optional MCP server (Context7, Git), the setup script now performs a JSON-RPC `initialize` exchange before writing its `[mcp_servers.<name>]` block to `~/.codex/config.toml`. Servers that fail the probe (timeout, EOF mid-handshake, malformed/error response, id mismatch) are omitted, preventing Codex from emitting a `tools[N]` entry whose `function` field is `undefined` — the failure shape that some OpenRouter back-ends (notably Azure) reject with HTTP 400 and that previously caused `implement.yml`, `validate.yml`, and `review_autofix.yml` retries to fail. Defence-in-depth alongside the `@upstash/context7-mcp@2.1.8` pin from #1705. Gated by the new `MCP_HANDSHAKE_PROBE_ENABLED` env var (default `true`); timeout configurable via `MCP_HANDSHAKE_PROBE_TIMEOUT` (default `15`, in seconds). Reproduction harness lives at `tests/fixtures/mcp_handshake/mock_mcp_close_on_init.py` (closes connection during `initialize`); end-to-end coverage in `tests/test_mcp_handshake_probe.py` exercises probe success/timeout/EOF/spawn-failure/invalid-JSON/error-response/id-mismatch paths plus the bash-level `setup_serena.sh` gate (block written iff probe passes; opt-out via `MCP_HANDSHAKE_PROBE_ENABLED=false`).

- **`shubhodeep1/drhyg_ecommerce_automation` is now a registered consumer repo.** It receives the `@stable` `repository_dispatch` on every release.

The repo was seeded with the standard profile from `@stable` in its seed PR (`drhyg_ecommerce_automation#1`): 12 standard-profile wrapper workflows plus `ai-update-workflows.yml`, the `.claude/` command and hook assets, and the root `CLAUDE.md`. This registration adds it to `.github/ai/consumer_repos.json`, so tagging a new `@stable` release dispatches an immediate sync to it instead of leaving it to the daily 04:00 UTC cron.

| The numbers that matter | Value |
| --- | --- |
| Consumer repos registered | 13 (was 12) |
| Wrapper workflows seeded | 13 (standard profile + self-updater) |
| Seed source ref | `stable` (`f7b0aa59`) |

What this means for operators: the release `GH_PAT` must hold `repo` scope on `shubhodeep1/drhyg_ecommerce_automation`, and that repo needs `GH_PAT` and `OPENROUTER_API_KEY` secrets plus `WORKFLOW_PROFILE=standard` set before its wrappers can run.

- **The AI review pipeline now resolves the PR review threads it has addressed.** `review_autofix.yml` gains a `Resolve addressed PR review threads` step that marks a reviewer's thread resolved once the editor's `PR comment audit:` section has given that comment a validated disposition; an `applied` disposition additionally requires a productive commit.

The pipeline has always read PR review comments — `scripts/review_collect_pr_metadata.sh` fetches them into `PR_ALL_COMMENTS_CONTEXT_FILE`, the reviewer and editor prompts both inline it, and the editor must audit each one — but nothing ever closed the thread. A comment the editor fixed on iteration 2 looked exactly like one it had never read, so a PR whose findings were all addressed still read as a PR whose review feedback had been ignored. On `shubhodeep1/fun-token-multi-chain#404`, 11 of 12 Copilot findings were fixed in code across four autofix rounds and all 12 threads still showed unresolved. Resolution is keyed on the audited comment's id rather than its file and line, so two comments at one location cannot close each other. An `ignored` disposition resolves the thread too, but only after the editor's stated reason is posted as a reply, so a reviewer can see the rejection and reopen.

| The numbers that matter | Value |
| --- | --- |
| New repo vars | `REVIEW_RESOLVE_THREADS_ENABLED` (`true`), `REVIEW_RESOLVE_THREADS_MAX` (`50`) |
| GitHub API calls added per run | 1 paginated thread query + 1 mutation per resolved thread + 1 reply per `ignored` thread |
| Threads resolved per run before the cap warns | 50 |
| New tests | 31 in `tests/test_review_resolve_review_threads.py` |

What this means for operators: a PR that has been through the autofix loop now shows its review threads in the state the pipeline actually left them, so an open thread is a real signal again rather than the default. Set `REVIEW_RESOLVE_THREADS_ENABLED=false` in a consumer repo to keep the previous behaviour and leave every thread untouched.

- **`main` promotes itself to `stable` once a day, and only after it has been proven end to end.** A `BLOCKED` implementation no longer costs credits, and a patch merged into the `stable` branch releases itself.

`promote-main-to-stable.yml` now runs on a daily schedule (`0 0 * * *`). Each tick checks for a code change on `main` since the `stable` tag (analysis docs, `docs/`, `CHANGELOG.md` and most `*.md` do not count; `CLAUDE.md` and `.claude/` do), runs `test-and-mark-stable.yml` in the new `gate_only` mode on `main` as a smoke gate, then hands one `analysis/workflow-optimization-*.md` doc to the orchestrator as a proving run. Gate-only runs carry the cycle run ID so the scheduler waits for its exact smoke run rather than an unrelated concurrent dispatch. When that run merges, the orchestrator poller dispatches a second doc as a verifying run on top of it. When the verifying run is ready to merge, the poller confirms nothing untested landed since the smoke-tested commit, fast-forwards `stable` to exactly the proving merge (`target_sha`), runs the full release gate, and holds the verifying merge until the release finishes so `main` HEAD is the version under test. A tick that finds a cycle in flight, an earlier cycle for the same tip, or fewer than two analysis docs does nothing; a failed tip is not retried until `main` moves again. `orchestrate.yml` gained optional `tracking_labels` and `tracking_comment` inputs so a dispatcher can bind the project it starts. Separately, `auto-release-stable.yml` checks every 6 hours whether the `stable` branch is ahead of the `stable` tag and dispatches the release gate when it is. And when the implementer answers a deliberate `BLOCKED:` verdict, `implement.yml` parks the issue in `ai:blocked` instead of `ai:awaiting-approval`, so stall recovery stops re-running a plan that cannot succeed.

| The numbers that matter | Value |
| --- | --- |
| Promote cycle cadence | daily, `0 0 * * *` |
| Orchestrator runs per promotion | 2 (proving, verifying), 2 analysis docs |
| Release gates per promotion | 2 (`gate_only` smoke gate on `main`, full gate on `stable`) |
| `stable`-branch release check | every 6 hours |
| Merge hold cap while the release runs | 21600 s (`COMPREHENSIVE_PROMOTION_HOLD_MAX_SECS`) |
| Kill switches | `PROMOTE_CYCLE_ENABLED`, `APPLY_ANALYSIS_ON_MAIN_ENABLED`, `AUTO_RELEASE_STABLE_ENABLED` |
| Implement runs saved per blocked issue | 3 of 4 (tele-funtoken-msg-scoring#4395 re-ran the same `BLOCKED` plan four times) |

What this means for operators: nobody dispatches a release any more. A code change on `main` reaches consumers after the next daily tick proves it, with the whole chain visible as tracking-issue comments and stable log prefixes; a `stable`-branch hotfix is tagged within six hours; and an issue that needs credentials or a product decision shows up as `ai:blocked` with the reason and resume instructions instead of a chain of identical failed runs. Manual `promote-main-to-stable.yml` dispatch still works as the override.

### For contributors

The proving and verifying runs are ordinary orchestrator projects labelled `ai:comprehensive-test-pending` with a marker comment (`apply-analysis-*` lines) that the poller reads. A doc carrying that marker on any tracking issue, or listed in `analysis/recommendation-processing-report.md`, is never dispatched again. Log prefixes: `PROMOTE_CYCLE_SKIPPED reason=…`, `PROMOTE_CYCLE_FAILED reason=…`, `PROMOTE_CYCLE_DISPATCHED`, `APPLY_ANALYSIS_*`, `COMPREHENSIVE_VERIFICATION_*`, `COMPREHENSIVE_PROMOTION_*`, `AUTO_RELEASE_*`, `IMPLEMENT_BLOCKED_TERMINALIZED`.

- **Human-needed escalations and failed releases now heal themselves.** When the pipeline labels an issue or pull request `ai:needs-human` (or a terminal latch such as `ai:check-triage-escalated`, `ai:destructive-blocked`, `ai:scope-blocked`, `ai:harness-broken`, `ai:resolver-escalated`, `ai:security-pass-failed`), or when a release / promotion workflow run fails in coding-workflows, a fix issue is opened for the same clarify → plan → implement → review pipeline instead of only an admin Telegram message.

Consumers get a new `ai-workflow-failure-heal.yml` wrapper (profile `full`, on by default) that reports the escalation to coding-workflows with the failed runs linked and the release pin recorded. The `Workflow Failure Heal Intake` workflow in coding-workflows fetches the failed job logs, diagnoses against the source at that release SHA, and routes by classification: shared-workflow defects become an `ai:workflow-heal` issue in coding-workflows targeting `stable`, so the fix ships as a hotfix through `auto-release-stable.yml` and reaches every consumer on the next sync; defects in the consumer's own code become an issue in that consumer; configuration problems and transient failures only alert and leave a comment on the escalated issue. Failures are fingerprinted so the same bug in many consumers is one issue with occurrence comments, recurrences are capped by a lineage generation, and open-issue and per-day budgets bound the volume.

| The numbers that matter | Value |
| --- | --- |
| Escalation labels that trigger a report | 7 |
| Release workflows reported on failure | 5 (`Test & Mark Stable Release`, `Mark Stable Release`, `Promote main to stable`, `Auto release stable`, `Forward-merge stable to main`) |
| Lineage cap per fingerprint (`WORKFLOW_HEAL_MAX_LINEAGE_DEPTH`) | 3 |
| Open-issue / per-day budgets (`WORKFLOW_HEAL_MAX_OPEN_ISSUES`, `WORKFLOW_HEAL_MAX_ISSUES_PER_DAY`) | 10 / 20 |
| Kill switch | `WORKFLOW_HEAL_ENABLED=false` |
| New labels | `ai:workflow-heal`, `ai:workflow-heal-escalated` |

What this means for operators: the Telegram feed still shows every escalation, but each one now also carries a link to the heal issue (or a diagnosis comment explaining why no code fix applies), and the fix arrives through the normal reviewed pipeline without anyone opening an issue by hand. Nothing new has to be configured: the consumer `GH_PAT` already reaches coding-workflows, and the wrapper arrives on the next `@stable` sync.

### For contributors

Shared logic lives in `scripts/workflow_failure_heal.py` (payload validation, error-signature fingerprinting, dedup / lineage / budget decisions, issue composition) and is exercised by `tests/test_workflow_failure_heal.py`, which also runs `scripts/workflow_failure_heal_report.sh` and `scripts/workflow_failure_heal_intake.sh` end to end against a mock `gh` and mock `codex`. Stable log prefixes are `WORKFLOW_HEAL_REPORT` (reporter) and `WORKFLOW_HEAL` (intake). Smoke-test fixtures and promote / auto-release runs that failed only because the smoke gate failed are skipped so the gate run's own report is the single record.

- **Repeated review/autofix failures now file workflow-heal issues.** When the AI review workflow fails on the same pull request for two runs in a row, the failing run reports itself to the workflow-failure-heal intake in `shubhodeep1/coding-workflows`, which opens a hotfix issue for the LLM pipeline instead of leaving the PR stuck behind a Telegram alert.

Until now a `review_autofix.yml` failure such as `editor_empty_noop` produced only the admin Telegram message and a PR comment, and the poller retried the run indefinitely if the cause was systemic. The reusable `review_autofix.yml` now has a `Report autofix failure to workflow failure heal` step in its failure path that counts the consecutive failure comments on the PR, waits until the streak reaches `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK` (default 2, repo variable), and sends a `workflow-failure-heal` `repository_dispatch` with a new `source_kind: autofix_failure`. The intake fingerprints the report by workflow, failure reason (`autofix:<reason>`), and evidence signature, so one systemic cause opens one issue per lineage, and the issue targets `stable` like every other heal issue. Resolver escalations, closed PRs, branch-review mode, and smoke-test fixtures are never reported, and a consumer whose stable ref predates the reporter script simply skips the step.

| The numbers that matter | Value |
| --- | --- |
| Failure streak before a report | `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK` = 2 runs |
| Extra GitHub API calls per report | 1 (`POST /dispatches`; PR payload and comments come from the run) |
| Evidence attached per report | up to 4000 chars (summary line, editor flags, log tails) |
| Reporter script | `scripts/workflow_failure_heal_autofix_report.sh` |

What this means for operators: a PR whose AI review keeps failing for the same reason now shows up as a `ai:workflow-heal` issue in coding-workflows after the second failed run, with the run's evidence attached, and the existing kill switch (`WORKFLOW_HEAL_ENABLED=false`) turns the reporter off together with the label-based reporters.

### For contributors

The reporter and the heal Python helper are staged through the `OPTIONAL_BOOTSTRAP_SCRIPTS` loop in `scripts/stage_workflow_support.sh`, so no consumer wrapper changes. The intake script reads `failure_reason`, `failure_streak`, and `failure_evidence` from the payload and feeds them to the diagnosis prompt as untrusted context; `tests/test_workflow_failure_heal.py` covers the streak counter, payload validation, issue composition, the intake path, the reporter's skip reasons, and the workflow wiring.

- **Projects parked by an older workflow engine now resume on their own.** A tracking issue in `ai:security-pass-failed` is reset once, exactly like `/re-security-pass`, when a newer engine polls it, and the `ai:needs-human` latch that the staged-support restore failure set is released once the engine that caused it is gone.

Until now both states were dead ends that only a human comment or label edit could leave, even when the thing that parked the project was the engine itself. binance-blessings#249 finished all four phases and eight fix-ups on 2026-09-07, then exhausted its security-pass budget twice (2026-09-08 and 2026-09-18) on stable pins `431d537` and `3c2d8ec`, engines with a 3-cycle budget, full re-audits with no findings memory, and no exhaustion judge; three of the five findings that ended those runs sat on code older than the project. The fixed engine reached that repository hours after the second exhaustion, and every poll tick logged `Project already failed, skipping.` until `/re-security-pass` was typed by hand. In this repository, #4113 (project #3965 fix cycle 7) halted in `ai:needs-human` on a staged-support re-base conflict in run 35072286584, PR #4119 removed the cause, and the issue stayed parked because the handler had also removed the phase label, so even a human `/approved` was refused with `reason=wrong_phase`. The scheduled poller (`.github/workflows/orchestrate_poll.yml`, `scripts/orchestrate_poll_process.sh`) now records the engine commit at every terminal security-pass path, resets a parked project once per engine commit that differs from it, and runs a small sweep that releases the staged-support latch, restores `ai:awaiting-approval`, and re-approves the issue even when no tracking project remains open.

| The numbers that matter | Value |
| --- | --- |
| Security-pass fix issues merged on binance-blessings#249 before it parked | 6 (#277, #279, #281, #284, #286, #288), 21 distinct finding IDs, none repeated |
| Time binance-blessings#249 waited for a human after the first exhaustion | 9 days (2026-09-08 to 2026-09-17) |
| Auto resets per project per engine commit | 1 (`security_pass_auto_reset_engine_shas`, last 20 engines kept) |
| Latch releases per issue per engine commit | 1 (`<!-- ai:needs-human-auto-release ... engine=<sha> -->` marker) |
| New GitHub API calls per source-repository tick | 1 paginated REST issues read, plus 1 comments read, 2 label-events reads, and 1 paginated labels read per candidate issue, then 1 label edit and 1 comment per released issue |
| Kill switches (both default `true`) | `SECURITY_PASS_AUTO_RESET_ON_ENGINE_CHANGE`, `STAGED_SUPPORT_LATCH_AUTO_RELEASE_ENABLED` |

The review-blocked judge's `close_and_reissue` replacement no longer strands a security-pass fix either. On the same project the judge closed fix PR #293 after its retry budget and reissued #292 as #294 with only its own review-blocked footer, so `resolve_security_pass_fix_successor` could not adopt #294, the poller parked #249 as `fix_issue_closed_without_merged_pr` three minutes later, #294 was planned against `main` instead of `orchestrator/project-249`, and its two-file `files_touched` allowlist made `implement.yml`'s scope guard reject the nine contract, README, changelog and test paths the fix needed, latching `ai:scope-blocked`. `scripts/review_rb_judge.sh` now copies the parent's `**Orchestrator metadata**` block into the reissue ahead of the review-blocked footer and unions the spot-fix allowlist with the files the closed PR changed. The block is accepted only when its tracking issue and integration branch match the PR's GitHub-reported `orchestrator/project-<n>` base; inconsistent body metadata falls back to base-derived lineage without an unverified local ID, and canonical lineage lines are stripped from judge-generated prose so duplicate model output cannot redirect successor adoption.

What this means for operators: a project that was parked because the engine could not converge is re-audited by the engine that can, on its first poll after the `@stable` sync, and continues through the delta re-audits and the exhaustion judge without anyone typing `/re-security-pass`. Projects parked before this release count as parked by an unknown engine and are picked up the same way, so expect one billed audit per parked project per consumer after the next sync. An engine that fails a project itself never re-runs it; `/re-security-pass` and `/security-pass-waive` still work as before, and a `/re-security-pass` comment on the tick always wins over the automatic reset. A staged-support latch that recurs on the same engine stays parked for a human, and every other `ai:needs-human` reason is untouched.

Only genuine three-way staged-support rebase conflicts carry the auto-release marker. Missing ledgers or baselines, unsafe paths, merge-tool failures, ambiguous same-second provenance, and issues that still carry `ai:implementing` remain human-gated; compatibility with pre-marker comments is limited to the known #4113 incident from run `35072286584`.

### For contributors

`resolve_orchestrator_engine_sha` runs once at startup and sets `ORCHESTRATOR_ENGINE_SHA` from `ORCHESTRATE_ENGINE_SHA` (test override) or the HEAD of the `.codex-workflow-src` support checkout; an unresolvable engine logs `ORCHESTRATOR_ENGINE_SHA sha=unknown source=unresolved` and both new paths skip rather than guess from the consumer's own HEAD. State gains `security_pass_failed_engine_sha` and `security_pass_auto_reset_engine_shas`, normalized by `ensure_security_pass_state_fields`. The reset block sits between the `/re-security-pass` handler and the `/revalidate` handler in the tracking-issue loop and logs `SECURITY_PASS_AUTO_RESET` or `SECURITY_PASS_AUTO_RESET_SKIPPED ... reason=engine_unresolved|same_engine|already_reset_on_engine`. In `shubhodeep1/coding-workflows` only, `release_staged_support_needs_human_latches` runs after standalone stall recovery or through a sweep-only quiet-tick step. It matches the reason-specific `<!-- ai:needs-human-latch reason=staged_support_rebase_conflict -->` marker, plus only the exact pre-marker #4113 incident from run `35072286584`, and requires trusted repository authorship, strict current-label provenance, no residual `ai:implementing`, and `scripts/implement_staged_support_workspace.sh` in the engine. The sweep revalidates the live labels and latest latch event immediately before the transition, leaving a concurrently changed human gate untouched. A failed `/approved` response is reconciled against paginated comment history before compensation, so an accepted write with a lost response remains released; a confirmed post failure restores the latch. If posting `/approved` and restoring the latch both fail, the shared order-independent latch guard blocks managed and standalone auto-approval while a CRITICAL alert identifies the half-applied release. A GraphQL cache entry whose comments field is missing, unavailable, or a full 100-comment window is replaced with paginated REST history; fetch and parser failures leave auto-approval blocked for that tick. Regression coverage lives in `tests/test_orchestrate_poll_process.py`, `tests/test_implement_post_codex_recovery.py`, `tests/test_orchestrate_poll_workflow_contract.py`, and `tests/test_review_rb_judge_label_propagation.py`.

- **Pushed pull requests now get a cheap Haiku check-in, and `/implement-plan-claude` runs every stage in its own fresh session through to `/verify-activation` and `/deploy-activate`.** A new CLAUDE.md §26 has every interactive session start a small Haiku checker for each PR it pushes, and the same checker drives `/implement-plan-claude` from phase to phase without ever waking the expensive session.

Until now a session pushed a branch, opened its pull request, and went quiet, and `/implement-plan-claude` waited on each phase with a Sonnet Routine that, it turns out, could not act: sessions started by a Routine with `create_new_session_on_fire` get no MCP tools and no repository, so its checker could never start the next step. Both now use a Haiku session started with `create_session`, which does get the repository, `gh`, and the claude-code-remote tools. It runs `.claude/scripts/check_in_status.py` every 3 hours (re-armed with `send_later`), and the script, not the model, decides whether the PR merged, closed, got blocked, or is stuck. For a §26 check-in the checker writes the terminal report itself from next steps the pushing session gave it, renames itself `PR #<n> merged — …`, and sends one push notification. For `/implement-plan-claude` it starts the next stage session, titled `implement-plan <slug> — phase 2/4` (or `security-pass`, `validation`, `verify-activation`, `deploy-activate`), which archives the previous stage unless that one is waiting on you. After the completion PR merges the command runs `/verify-activation`, loops on its fix PRs, and starts `/deploy-activate` in its own session when the verdict is DORMANT. A 24-hour safety net restarts a stalled chain.

| The numbers that matter | Value |
| --- | --- |
| Check-in interval | 180 minutes |
| Checker model | `claude-haiku-4-5-20251001` |
| GitHub REST calls per check | 1 in terminal-only mode; non-terminal checks add 1 per 100 check runs and up to 3 for an old failure |
| "Stuck" threshold | conflict or failed check, head older than 6 hours, no workflow run active |
| Safety net | one wake of the last stage session after 1440 minutes, only if the chain stalled |
| Verify-activation cycles before asking | 3 |
| New `CLAUDE.md` section | §26 |
| Pull request | #4304 |

What this means for operators: run `/implement-plan-claude` in Auto mode (the command asks for it) and leave it; the session list shows one open session per plan, named after its current stage, and you are notified when a stage starts, when the project is LIVE, or when `/deploy-activate` is waiting for you at Step 1. After any other push, the Haiku checker tells you when the PR lands and what is left. Neither touches CI or review comments on its own. `.claude/settings.json` now pre-approves the tools these flows call, so sessions stop asking at start-up.

### For contributors

- `.claude/scripts/check_in_status.py` (new, mirrored to `workflow-templates/.claude/scripts/`) takes `--pr N [--terminal-only]`, `--run ID`, or `--issues a,b`, reads over REST only, prints one JSON line, and exits 2 on a failed read; `tests/test_check_in_status.py` runs in its own `ci.yml` step.
- `permissions.allow` adds file edits, `claude/*` pushes, `gh` REST and run reads, the four security-audit / validate dispatches, the GitHub MCP and claude-code-remote tools, `CronCreate` / `CronDelete` / `PushNotification`, and the helper. `permissions.ask` keeps `gh api` writes behind a prompt. Allow rules cannot match the generated MCP server name (`mcp__<uuid>__…`) that `create_session` children see, which is why stage sessions need Auto mode.
- `.claude/hooks/pr_check_in_reminder.py` is a `PostToolUse` hook under the anchored matcher `^(?:Bash|mcp__.*__create_pull_request|mcp__.*__push_files|mcp__.*__create_or_update_file)$`; its reminder text now points at the Haiku checker session.

### For contributors

Only entries the editor explicitly listed are eligible, and an entry whose audited path disagrees with the real comment's path is skipped — that pair of rules is what keeps a mis-keyed audit line from burying a live finding, which is a case observed in production rather than a hypothetical. Context-file parsing is first-wins per field because comment bodies are dumped into the same `entry[N].<field>` stream after the structured fields, so last-wins parsing would let attacker-controlled comment prose containing `entry[0].id: 999` redirect a resolve at an unrelated thread. Thread lookup uses GraphQL because REST has no resolve-review-thread endpoint; the §21.D/§23.D preference for REST addresses the Claude Code Web agent proxy in interactive sessions and does not apply to this Actions-side caller. Every failure path warns and exits 0, and the step carries `continue-on-error: true`.

- **OpenCode now has an inert, pinned Phase 1 foundation and a dispatchable all-model rollout gate.** A role-scoped configuration writer, shared command/output/bootstrap helpers, and the exact `opencode-ai@1.18.23` install action support a live smoke across all reviewer and editor slots without changing any production runtime path.

After this change reaches `main`, operators must dispatch `opencode-live-smoke.yml` without a model filter and record the all-green run URL on tracking issue `#3845` before the read-side or write-side cutover begins. Existing production workflows continue to use the independently pinned `CODEX_VERSION`; no current review/autofix invocation is routed through OpenCode by this phase.

- **The orchestrator poller now sends a CRITICAL Telegram alert when a project's final integration PR is squash-merged into the default branch.**

Operators previously had no reliable Telegram signal at the moment a validated project actually landed in `main`: the only send at that point was the DEBUG-level "completed after validation pass" summary in `mark_validation_complete`, which any `ALERT_MSG_LEVEL` above `DEBUG` suppresses. The merge-success arm of `finalize_integration_merge_if_needed` in `scripts/orchestrate_poll_process.sh` now fires `tg_send_msg` at `CRITICAL` level, so every `ALERT_MSG_LEVEL` threshold from `DEBUG` through `CRITICAL` delivers it; only the explicit `ALERT_MSG_LEVEL=SILENT` opt-out (which silences all helper-based Telegram sends) suppresses it. The message carries the merged PR's title, the PR url, the tracking-issue url, and a readable gate description (`validation passed`, `operator ai:ready-to-merge`, or `validation disabled`). The title comes from the PR JSON snapshot the tick already fetched, so no additional GitHub API call is issued. The existing DEBUG summary is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Alert level | `CRITICAL` |
| New GitHub API calls per merge | 0 |
| Test pinning the alert | `test_final_merge_success_sends_critical_telegram_alert` |

What this means for operators: every final integration merge (for example a `feat:` squash PR that passed runtime validation) now produces one Telegram message with direct links to the merged PR and its tracking issue, without needing `ALERT_MSG_LEVEL=DEBUG`.

### For contributors

The alert lives between the "Final merge complete" tracking comment and the arm's `return 0`; a source-anchor test in `tests/test_orchestrate_poll_process.py` pins its placement, the gate mapping, both urls, and the `CRITICAL` level.

- **Security-pass exhaustion no longer stops a project for a human.** When a project spends its consolidated security-fix budget with findings still open, an autonomous exhaustion judge now decides per finding whether the project completes with the finding accepted as a tracked known risk, gets one more fix cycle, or needs a human; `/security-pass-waive` records the same acceptance by hand.

Until now `MAX_SECURITY_PASS_CYCLES` exhaustion labelled the tracking issue `ai:security-pass-failed`, posted "Manual intervention is required", and the project stayed there until someone commented `/re-security-pass`. On tele-funtoken-msg-scoring#4281 and #3955 every consolidated fix issue merged, no finding ever repeated, and the findings that exhausted the budget sat on lines the fix cycles themselves had written; three projects in that repository were parked in the terminal state at once. The scheduled poller (`.github/workflows/orchestrate_poll.yml`, `scripts/orchestrate_poll_process.sh`) now consults `prompts/mode-judge-security-pass-exhaustion.txt` at that point. The judge reads the audited checkout and returns one decision per remaining finding: `accept_with_followup` stores a waiver in state and files a non-blocking `ai:security` follow-up issue through the normal issue pipeline, `keep_fixing` creates one more consolidated fix issue for exactly those findings, and `fail` keeps the terminal path with the verdict posted on the tracking issue. The judge is consulted again whenever the budget is exhausted again (`MAX_SECURITY_PASS_JUDGE_ROUNDS=0` is unbounded). A waived finding is handed to the audit engine as accepted (`SECURITY_AUDIT_WAIVED_FINDINGS`) and any re-report of it, by id or by location, is dropped by the engine and again by the poller. Any judge failure, including the `SECURITY_PASS_EXHAUSTION_JUDGE_ENABLED=false` kill switch, falls back to the previous terminal behaviour, never to a silent pass.

| The numbers that matter | Value |
| --- | --- |
| Projects parked in `ai:security-pass-failed` in tele-funtoken-msg-scoring when this landed | 3 (#3928, #3955, #4281) |
| Fix cycles merged on #3955 after its operator reset, before the next exhaustion | 3 of 3, 9 distinct finding IDs |
| Judge decisions per remaining finding | `accept_with_followup`, `keep_fixing`, `fail` |
| Default judge round cap (`MAX_SECURITY_PASS_JUDGE_ROUNDS`) | `0` (unbounded) |
| Waiver location match window (`SECURITY_AUDIT_WAIVER_LINE_WINDOW`) | 40 lines |
| New GitHub API calls | 1 `gh issue create` per accepted finding, plus 1 cached reconciliation search per tracking issue and poller process |

What this means for operators: a project that keeps producing new medium-severity findings in its own fix code now converges on its own. Accepted findings arrive as `ai:security` issues that the normal clarify → plan → implement pipeline picks up later, so nothing is dropped, and the tracking issue carries the judge's decision table for every round. Projects that were already in `ai:security-pass-failed` before this release stay there until `/re-security-pass` or `/security-pass-waive <finding_id> ...` is commented; the new path applies only to exhaustions that happen after it ships.

### For contributors

`security_pass_exhaustion_judge` runs from the exhaustion branch of `run_security_pass_inline` and returns 0 only after fully applying a verdict; every other outcome (`SECURITY_PASS_JUDGE_SKIPPED`, `SECURITY_PASS_JUDGE_FAILED`) leaves `security_pass_terminal_failure` in charge. State gains `security_pass_waived_findings`, `security_pass_followup_issues`, and `security_pass_judge_rounds`, all normalized by `ensure_security_pass_state_fields`; `/re-security-pass` and the kill-switch release reset the round counter but keep waivers. `scripts/security_audit.sh` accepts `SECURITY_AUDIT_WAIVED_FINDINGS` (findings-json only, fails closed on malformed input) and reports the suppression as `counts.suppressed_waived`. The `/security-pass-waive` scan runs before the `security-pass-fixing` handler so it works in both the failed and the fixing state; only human OWNER/MEMBER/COLLABORATOR comments are honoured. Regression coverage lives in `tests/test_orchestrate_poll_process.py`, `tests/test_security_audit_workflow_contract.py`, and `tests/test_orchestrate_lib.py`.

- **Per-PR changelog fragments replace direct `CHANGELOG.md` edits, so concurrent PRs can no longer conflict on the changelog.** Agents now write one `changelog.d/<issue-or-pr>-<slug>.md` file per PR; automation folds those fragments into `CHANGELOG.md` and deletes them.

Newest-entry-first insertion meant every concurrently-open PR edited the same hunk at line 1 of `CHANGELOG.md`, so git had to conflict — and each conflict cost a full LLM run through `scripts/review_conflict_resolve.sh` at `THINKING_LEVEL_CONFLICT_RESOLVER=high` with a 50-minute per-attempt budget. Two PRs now never touch the same path, so there is nothing to conflict. The new `scripts/assemble_changelog.py` folds fragments in at release time here (the `release` job of `mark-stable.yml` and `test-and-mark-stable.yml`) and, in consumer repos, inside the existing `update_workflows.yml` sync job that already commits to the default branch — consumer repos have no release workflow and no push-triggered wrapper, so that sync is the only automation available to them. A fifth sync category delivers the mechanism to every repo in `.github/ai/consumer_repos.json`: it is additive only, creating `changelog.d/.gitkeep` and adding a delimited managed block carrying `CHANGELOG.md merge=union` to `.gitattributes` without disturbing consumer-local attribute rules. The assembler auto-detects each repo's layout — Keep a Changelog upstream, bare `## YYYY-MM-DD` date headings in consumers such as `tele-funtoken-msg-scoring` — so no repo converts its changelog history, and insertion is purely additive: no existing line is removed or reflowed.

| The numbers that matter | Value |
| --- | --- |
| Consumer repos reached | 12 (`.github/ai/consumer_repos.json`) |
| Sync categories in `update_workflows.yml` | 4 → 5 |
| Conflicting paths between two concurrent changelog PRs | 0 (demonstrated in `tests/test_assemble_changelog.py`) |
| Layouts supported | Keep a Changelog + `## YYYY-MM-DD` date headings |
| New repo var | `AUTOFIX_SKIP_SUPPRESS_ON_CONFLICT` (default `true`) |
| Extra GitHub API calls added | 0 (CLAUDE.md §15 — existing `/pulls/{n}` fetch extended) |

Three related defects close with it. `CLAUDE.md` §20 is rewritten to be self-contained: it now states explicitly when an entry is required and carries the entry structure and voice rules inline, instead of pointing at `docs/changelog-style.md` — a file that exists only upstream and is not in the sync scope, so the pointer dangled in all 12 consumer repos. `prompts/mode-implement.txt` (and its `_templates` source) no longer routes `CHANGELOG` through the `<!-- anchor:NAME -->` guardrail, which `CHANGELOG.md` has never carried anchors for; it teaches the fragment workflow instead. And `review_autofix.yml`'s deterministic doc-only / small-diff skip path no longer swallows merge conflicts: a PR reporting `mergeable=false` (or `mergeable_state=dirty`) keeps the codex-agent path so its conflict is resolved in the same run rather than waiting for the orchestrator stall-recovery cron.

What this means for consumer repos: nothing to install and nothing to convert — the fragment directory, the `.gitattributes` backstop, and the assembly step all arrive on the next `@stable` sync, and the `CLAUDE.md` §20 rule that tells agents to use them arrives through the same sync it always did. Existing changelog history is untouched.

### For contributors

`scripts/assemble_changelog.py` has two subcommands: `assemble` (fold fragments, delete them) and `ensure-assets` (create `changelog.d/.gitkeep` and the `.gitattributes` managed block). Both are idempotent and run only from automation (§18.A/§18.B); the script is registered in `docs/scripts-pending-removal.md` per §18.F. Consumer repos never carry a copy — `update_workflows.yml` runs upstream's script from the `@stable` support checkout, so the §20 rule and the machinery implementing it cannot drift. Release-time assembly fails open: if the assembled commit cannot be pushed, the working tree resets to the remote tip so the version tag still points at pushed history and the fragments simply wait for the next release. The `merge=union` backstop applies to real `git merge` invocations, including the CI merge replay in `scripts/review_conflict_prepare.sh`, not to GitHub's server-side mergeability estimate; it is a textual union with no understanding of entry semantics, so fragments remain the mechanism and it is only the net underneath.

- **Interactive Claude Code sessions can now manage Cloudflare Workers for funtoken.io, ft.games, and 5m.fun via two new session env vars, `FUNTOKEN_IO_CF` and `FT_GAMES_CF`.** A new CLAUDE.md §24 maps each credential to its sites, splits Cloudflare work into self-serve reads, self-serve Worker deploys the task calls for, and ask-first destructive writes, and reaches every consumer repo through the existing root `CLAUDE.md` sync.

Sessions previously had no standing policy for Cloudflare, so any Worker change bounced back to the operator. Each var holds one Cloudflare account's credentials as a single `<account_id>:<api_token>` string: `FUNTOKEN_IO_CF` covers the funtoken.io website, `FT_GAMES_CF` covers ft.games and 5m.fun, and the two are not interchangeable — §24.A pins the site-to-credential mapping, the first-colon split, and the transport (`wrangler` via `CLOUDFLARE_ACCOUNT_ID`/`CLOUDFLARE_API_TOKEN`, or the REST API). §24.B makes read-only calls (Worker scripts, bindings, routes, deployments, DNS on covered zones, `wrangler tail` logs) an explicit carve-out from the §2 ask-first rule. §24.C makes creating and editing Workers self-serve when the task asks for it — that is what the credentials exist for — with validate-before-deploy and preserve-rollback constraints. §24.D keeps deletions, DNS/zone setting changes, KV/R2/D1 data wipes, and secret rotation behind a mandatory §2 Q/A confirmation, not superseded by §12. §24.F adds a per-repo `## Cloudflare resources` registry to the root agents file, mirroring the §22.C DigitalOcean registry.

| The numbers that matter | Value |
| --- | --- |
| New `CLAUDE.md` section | §24 |
| New env vars | `FUNTOKEN_IO_CF` (funtoken.io), `FT_GAMES_CF` (ft.games, 5m.fun) — session environment, not Actions secrets |
| Credential format | `<account_id>:<api_token>`, split on the first `:` |
| Actions workflows that read them | 0 |
| New `agents.md` section | `## Cloudflare resources` (identifier registry) |

What this means for operators: set `FUNTOKEN_IO_CF` and/or `FT_GAMES_CF` in the Claude Code session environment of any repo whose sessions should manage Workers for those sites; nothing else to install. The §24 rules and the registry convention arrive in consumer repos on the next `@stable` sync via the root `CLAUDE.md` mirror in `update_workflows.yml`. Sessions never print the credentials, and a missing or rejected token degrades to "report once and continue" rather than a retry loop.

### For contributors

The unattended pipelines read `unattended_system_instructions.md` and never see `CLAUDE.md`, so §24 governs interactive sessions only — no codex-driven phase gains Cloudflare access from this change. §24.G forbids committing any workflow, script, or hook that reads either var from the session environment; Actions-side Cloudflare work would need its own repo secret and its own review. Registry entries are identifiers under §6.

- **Interactive Claude Code sessions can now execute approved DigitalOcean and Cloudflare API writes without a second permission prompt.** `.claude/settings.json` gains a `permissions.allow` list covering `curl` PUT / POST / PATCH calls to `api.digitalocean.com` and `api.cloudflare.com`.

Until now, a DO app-spec update or Cloudflare Worker write that the operator had already approved in the CLAUDE.md §22.B / §24.D Q/A flow was blocked a second time by the harness Bash permission layer, stalling the session until someone clicked through (or failing outright in unattended-adjacent web sessions). The allowlist removes only that harness prompt; the CLAUDE.md policy layer is unchanged, so Claude still asks in Q/A format before any mutation, and reads remain self-serve per §22.A / §24.B. Rules are command-prefix matches, so sessions compose approved mutations in the canonical form `curl -q -sS -X PUT https://api.digitalocean.com/... -H ... -d @spec.json` (`-q` first disables implicit curl config, followed by the method and URL). The existing Bash PreToolUse hook checks the complete command and restores the harness prompt when later curl options, redirects, command substitutions, or chained commands could override or extend the allowlisted operation. `DELETE` is deliberately not allowlisted: destroys keep the interactive prompt as a second gate.

| The numbers that matter | Value |
| --- | --- |
| Allow rules added | 6 (PUT / POST / PATCH × 2 hosts) |
| Hosts covered | `api.digitalocean.com`, `api.cloudflare.com` |
| Files changed | Settings and mirrored PreToolUse hook copies under `.claude/` and `workflow-templates/.claude/` |
| Methods still prompting | `DELETE` (and any non-canonical command form) |

What this means for operators: after you answer the §2 Q/A approval, Claude runs the DO / Cloudflare write itself, with no further prompt to babysit. Consumer repos pick the same behaviour up automatically on their next `.claude/` assets sync from the `stable` ref (daily 04:00 UTC cron or the `@stable` `repository_dispatch`).

### For contributors

The template copies under `workflow-templates/.claude/` are the ones the `Sync .claude/ assets from upstream` step of `update_workflows.yml` mirrors into consumers; the repo-root copies govern sessions in this repo only. Both settings and hook pairs remain identical. Unattended pipelines are unaffected — they read `unattended_system_instructions.md` and never load these session settings.

- **Interactive Claude Code sessions can now query DigitalOcean directly via `DIGITALOCEAN_ACCESS_TOKEN`, instead of asking the operator to fetch data by hand.** A new CLAUDE.md §22 splits DigitalOcean work into self-serve reads and ask-first mutations, and reaches every consumer repo through the existing root `CLAUDE.md` sync.

Sessions previously had no standing policy for DigitalOcean, so any question about a deployed app's env vars, logs, or deployment status bounced back to the operator. §22.A makes read-only calls (app specs, deployed env vars, build/deploy/runtime logs, deployment status, metrics) an explicit carve-out from the §2 ask-first rule: the session pulls the data itself, via `doctl` or the REST API with the `DIGITALOCEAN_ACCESS_TOKEN` env var. §22.B keeps provisioning and every other mutation (creating, resizing, destroying, redeploying, spec or env var changes) behind a mandatory §2 Q/A confirmation that names the resource type, size, region, and billing impact — not superseded by §12's proactive scope. §22.C adds a per-repo `## DigitalOcean resources` registry to the root agents file (`agents.md` here, `AGENTS.md` in consumer repos): sessions read app/db IDs from the table first, ask once when an ID is missing, and record it in the same PR so it is never asked for again. The pre-task context-loading rule now names both `agents.md` and `AGENTS.md` casings so the every-session read applies in every repo.

| The numbers that matter | Value |
| --- | --- |
| New `CLAUDE.md` section | §22 |
| New env var | `DIGITALOCEAN_ACCESS_TOKEN` (session environment, not an Actions secret) |
| Actions workflows that read the token | 0 |
| New `agents.md` section | `## DigitalOcean resources` (ID registry) |

What this means for operators: set `DIGITALOCEAN_ACCESS_TOKEN` in the Claude Code session environment of any repo where sessions should verify DigitalOcean state themselves; nothing else to install. The §22 rules and the registry convention arrive in consumer repos on the next `@stable` sync via the root `CLAUDE.md` mirror in `update_workflows.yml`. Sessions never print the token, and a missing or expired token degrades to "report and continue" rather than a retry loop.

### For contributors

The unattended pipelines read `unattended_system_instructions.md` and never see `CLAUDE.md`, so §22 governs interactive sessions only — no codex-driven phase gains DigitalOcean access from this change. Registry entries are identifiers under §6: correcting or removing a recorded ID goes through the §2 ask flow.

- **Interactive Claude Code sessions now have a standing policy for the `GH_TOKEN` PAT, so GitHub work that needs privileges beyond the session's built-in tooling runs from the session instead of bouncing back to the operator.** A new CLAUDE.md §23 splits GitHub work into self-serve reads, self-serve routine repository writes, and ask-first destructive or administrative writes, and reaches every consumer repo through the existing root `CLAUDE.md` sync.

`GH_TOKEN` was already referenced ad hoc by individual slash commands and by `.claude/hooks/session-start.sh`, but no section said when a session may use it, what it may do with it, or how it degrades — so the answer varied per command. §23.A makes reads self-serve: pull requests, issues, review threads, Actions run and job logs, commits, file contents at any ref, repo variables, and workflow definitions, including in a consumer repo whose wrapper is implicated. §23.B makes the writes a task already implies self-serve too — pushing the session's working branch, opening and updating its PR, comments, review replies, labels — while §23.C keeps merging, force-pushing, deletions, repo and org administration, and workflow dispatches behind a §2 Q/A confirmation that names the exact repository and object, and is explicitly not superseded by §12's proactive scope. Commands whose documented job is to dispatch a workflow (`/implement-plan-ai`, `/apply-analysis`) carry their own approval, so the session does not re-ask before the dispatch those commands describe. §23.D pins the transport: MCP tools first, `GH_TOKEN` when MCP is scope-gated or lacks the capability, and `gh api` REST ahead of GraphQL-backed commands, because Claude Code Web's agent proxy serves only a pinned set of GraphQL operations and 403s the rest — the same reasoning §21.D applies to the merged-PR guard. The twenty `gh` CLI blurbs across `.claude/commands/` and `workflow-templates/.claude/commands/` now point at §23 rather than each restating the auth check, the mandatory `-R <owner>/<repo>` flag, and the hook-probe caveat in its own words.

| The numbers that matter | Value |
| --- | --- |
| New `CLAUDE.md` section | §23 |
| New env var | `GH_TOKEN` (session environment, not an Actions secret) |
| Actions workflows changed | 0 |
| Command files repointed at §23 | 20 (10 in `.claude/commands/`, 10 in `workflow-templates/.claude/commands/`) |

What this means for operators: set `GH_TOKEN` in the Claude Code session environment of any repo where sessions should reach Actions logs, other repositories, or endpoints the MCP surface does not expose. Classic PATs need `repo` plus `workflow` (and `read:org` for org metadata); fine-grained PATs need contents, pull requests, issues, and `actions: read`. The SessionStart hook already installs `gh` and reports whether the token authenticates, and a missing or invalid token degrades to the `mcp__github__*` tools with one diagnostic line rather than a retry loop. The §23 rules arrive in consumer repos on the next `@stable` sync via the root `CLAUDE.md` mirror in `update_workflows.yml` and the `.claude/` asset sync.

### For contributors

`GH_TOKEN` is the same identifier Actions workflows already export from `secrets.GH_PAT`, so §23.G records the split explicitly: the Actions reading is governed by workflow YAML and `unattended_system_instructions.md`, the session reading by §23, and unifying the two in either direction is a §6 breaking change requiring the §2 ask flow. The unattended pipelines never see `CLAUDE.md`, so no codex-driven phase gains access from this change. §15's API-call hygiene is restated in §23.F as binding on interactive `gh` and `curl` calls, not just workflow code.

- **Commits and pushes onto a branch whose pull request already merged are now blocked before they happen, instead of silently stranding the work.** A new `PreToolUse` hook checks PR merge status on every `git commit` and `git push` in an interactive Claude Code session.

Sessions that run for days or weeks outlive the PRs they open. When the PR for the working branch merges mid-session, further commits land on history that is already in the default branch and that no open PR carries anywhere, so the work never ships and disappears when the branch is deleted. The rule against this existed only as prose in `CLAUDE.md`, which is furthest from the model's live context exactly when a session has run long enough for the merge to happen. `.claude/hooks/pr_merge_status_guard.py` now enforces it deterministically: the harness runs it on every Bash tool call regardless of what the model remembers, and a block is reported back with the `git checkout -B <branch> origin/<default>` remediation already filled in.

The guard blocks only when all three conditions hold: a merged PR exists for the current branch, no open PR exists for it, and that merged PR's head commit is an ancestor of `HEAD`. The third condition is what makes it self-clearing. Branch names are reused after the reset, so the merged PR keeps matching the branch forever, and ancestry is what separates stacking on merged history from fresh work that reuses the name. The guard goes quiet the moment the branch is reset, before the replacement PR exists, so no override flag is needed to commit the fix.

| The numbers that matter | Value |
| --- | --- |
| Consumer repos reached | 12 (`.github/ai/consumer_repos.json`) |
| Conditions required before a command is blocked | 3 |
| Guarded git subcommands | `commit`, `push` |
| GitHub API calls per guarded command | 1, cached 300s per `<slug>/<branch>` |
| New env var | `CLAUDE_PR_MERGE_GUARD` (unset = enabled; `off` disables) |
| New `CLAUDE.md` section | §21 |

What this means for operators: nothing to install. The hook, its `PreToolUse` wiring in `.claude/settings.json`, and the §21 rule that documents it all arrive in consumer repos on the next `@stable` sync, through the existing `workflow-templates/.claude/` mirror in `update_workflows.yml`. Every failure mode allows the command and prints a warning naming the branch, so a lapsed token degrades the guard to a no-op rather than blocking all committing.

### For contributors

PR state is read over REST (`gh api repos/<slug>/pulls?state=all`), not `gh pr list`. Claude Code Web's agent proxy serves only a pinned set of GraphQL operations and rejects the rest with HTTP 403, and `gh pr list` is GraphQL-backed, so using it as the primary transport would have made the guard fail open on every commit in exactly the long-running web sessions it exists to protect. `gh pr list` stays wired as a transport fallback for environments where REST is gated instead; it retries the same question rather than issuing a second query, so the §15 budget of one call per guarded command holds. Cached data can satisfy an allow, but a block is always re-verified against a live call first, so opening a new PR clears the guard immediately rather than after the TTL. The hook is invoked as `python3 .claude/hooks/pr_merge_status_guard.py` rather than relying on its executable bit, because the consumer sync copies with plain `cp`, which leaves an existing destination's mode untouched. `tests/test_pr_merge_status_guard.py` covers the detection rule, the fail-open contract, both transports, and the block-then-reset-then-allow sequence end to end against a real git repository.

- **Orchestrator projects can opt into a mandatory, SHA-bound security pass before validation or final merge.**

When `ENABLE_SECURITY_PASS=true`, the scheduled poller audits the composed integration head, creates one normal-pipeline fix issue for surviving findings, re-audits after merged fixes, and fails closed when the engine is unavailable. The dark launch defaults off; `MAX_SECURITY_PASS_CYCLES=3`, `/re-security-pass`, visible status updates, and CRITICAL alerts bound and expose recovery.

### For contributors

The poller reuses `scripts/security_audit.sh` findings-JSON mode under `codex_heartbeat.sh`. Pass validity is tied to the exact integration SHA, and all completion routes share the same gate.

- **New `/seed-repo` interactive command onboards a consumer repository in one run.** `/seed-repo <owner>/<repo> [core|standard|full]` (default `standard`) replaces the manual copy-the-templates onboarding steps.

From a Claude Code session, the command seeds a new consumer repo end to end: it copies the chosen profile's wrapper workflows from `workflow-templates/` at `@stable` into the target's `.github/workflows/`, always adds `ai-update-workflows.yml` (the sync's self-updater, which `update_workflows.yml` never creates on its own), mirrors the `workflow-templates/.claude/` command and hook assets plus the root `CLAUDE.md`, and lands it all as a single seed PR in the target repo. It then sets the target's `WORKFLOW_PROFILE` repo variable after a confirmation prompt (unset, the first sync run would widen a `core`/`standard` seed to `full`) and opens a second PR registering the repo in `.github/ai/consumer_repos.json` per CLAUDE.md §14. Already-seeded repos get a report instead of a second seed. The command ships in `.claude/commands/` and in `workflow-templates/.claude/commands/`, so existing consumers receive it on their next sync.

| The numbers that matter | Value |
| --- | --- |
| Command file | `.claude/commands/seed-repo.md` (mirrored to `workflow-templates/.claude/commands/`) |
| Default profile | `standard` (12 wrappers) |
| PRs opened per seeding | 2 (seed PR in the target, registration PR here) |
| Source ref for all copied files | `stable` |

What this means for operators: onboarding a new repo no longer requires hand-copying templates or remembering the §14 registry step — run `/seed-repo`, merge the two PRs, and add the `GH_PAT` / `OPENROUTER_API_KEY` secrets the command lists; the daily sync owns everything afterwards.

### For contributors

The command never edits templates and copies byte-for-byte from `stable`, so the consumer's first `ai-update-workflows.yml` run diffs clean. Secrets are deliberately out of scope — values never transit the chat; the command only names what to add.

### Changed
- **`SECURITY_AUDIT_ENABLED` and `WORKFLOW_RETRO_ENABLED` defaults flipped `false` → `true`.** The Phase J security audit and Phase F weekly retro shipped default-off during their acceptance windows; both now run automatically on their existing crons (`0 8 * * 0` and `0 9 * * 1`) with no repo-var changes needed. Any repo can still opt out by setting the variable to `false`. Contract tests (`tests/test_security_audit_workflow_contract.py`, `tests/test_workflow_log_analysis_failure_contract.py`) and the `README.md` / `agents.md` env-var rows were updated to pin the new defaults. Refs #3496.
- `ORCH_INTEGRATION_STALE_ALERT_HOURS` now accepts `0` to fully disable the stale-integration `WARNING` alert (`INTEGRATION_STALE_ALERT_SENT`) emitted by `scripts/orchestrate_poll_process.sh::check_integration_branch_staleness`, mirroring the existing `ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS=0` off-switch (startup validation relaxed from "positive integer" to "non-negative integer"; a `0` early-returns before touching state, so the disabled path is a true no-op). The reusable poll workflow `.github/workflows/orchestrate_poll.yml` now plumbs `ORCH_INTEGRATION_STALE_ALERT_HOURS: ${{ vars.ORCH_INTEGRATION_STALE_ALERT_HOURS || '0' }}` — i.e. **disabled by default**: because `main` only catches up to a project's integration branch at the single end-of-project squash, this alert otherwise fired for the entire lifetime of every multi-wave project (e.g. project #3042 sitting 16 commits ahead while mid-flight at wave 7/12) — healthy progress, not a stall. The genuinely-actionable "final merge jammed" signal stays covered by `ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS` and integration backpressure (`ORCH_INTEGRATION_MAX_AHEAD_COMMITS`). Set repo variable `ORCH_INTEGRATION_STALE_ALERT_HOURS` to a positive integer (e.g. `6`) to re-enable per repo. The script fallback default is unchanged at `6` for any direct caller that sets the env to a non-numeric value. New regression test `test_integration_stale_alert_disabled_when_hours_zero` in `tests/test_orchestrate_poll_process.py`; README env-var rows updated.
- Canonicalized the shipped review/autofix pipeline docs: `README.md` now documents floor rules, advisory consolidator/raw-bundle authority, ledger persistence and `accepted-residual`, checklist/scoping knobs, parser fail-open, and workflow-summary override metrics; `agents.md` now carries the durable review-pipeline contract/env-var table; the stale review-pipeline planning doc was removed.
- Flipped LLM verbosity to `low` at every layer across the repo per operator policy. Previously the `-c model_verbosity=high` CLI flag was set on every `codex exec` callsite (≈20 sites in `scripts/*.sh` and `.github/workflows/*.yml`), `scripts/write_codex_config.sh:236` wrote `model_verbosity = "high"` into `~/.codex/config.toml`, `scripts/codex_model_catalog.json:354` carried `"default_verbosity": "medium"` for the `openai/gpt-5.4` catalog entry, and `.github/workflows/review_autofix.yml:129` defaulted `EDITOR_VERBOSITY` to `medium`. All four layers now read `low`. The historical `high` value was a workaround for the openai/codex#11151 announce-without-emit failure mode (implement / review_autofix smoke runs at 2026-05-07 12:41 / 12:42, where the model emitted a reasoning trace and exited without a tool call); the workaround now relies on the `include_apply_patch_tool = true` line that `scripts/write_codex_config.sh` writes alongside `model_verbosity` as the primary belt-and-suspenders. If the announce-without-emit pattern recurs at `low`, raise verbosity at the layer that needs it (start with the implement / review_autofix editor callsites, since those are the original 11151 reproducers). `agents.md` per-phase verbosity table and the `scripts/write_codex_config.sh:224-241` rationale comment were updated to match. `tests/test_write_codex_config.py` assertions were flipped from `model_verbosity = "high"` to `model_verbosity = "low"`. Third-party reviewer models (`minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`, `deepseek/deepseek-v4-pro`, `qwen/qwen3.6-plus`, `x-ai/grok-4.1-fast`) carry `support_verbosity = false` in the catalog; codex CLI logs `model_verbosity is set but ignored as the model does not support verbosity` and continues — operationally moot for those rows. `MODEL_VERBOSITY` env-var names, `VERBOSITY_*` repo-vars, and the `model_verbosity` config key are unchanged (per CLAUDE.md §6).
- `update_workflows.yml` now defaults its in-workflow `ALERT_MSG_LEVEL` env to `SILENT` (was `DEBUG`), so the per-run `🔍 DEBUG: Workflow wrappers updated in <repo> …` Telegram notification fired by `tg_send_msg "${MSG}" "DEBUG"` at the end of the update step is suppressed by `tg_helpers.sh::_tg_should_send` (msg=DEBUG=0 < threshold=SILENT=99). Consumer repo overrides still take precedence: `vars.ALERT_MSG_LEVEL=DEBUG` (or any level ≤ DEBUG) re-enables the alert, and the `alert_msg_level` `workflow_dispatch` input continues to override per-run. README's `ALERT_MSG_LEVEL` row updated to call out the exception. No code paths other than this notification are affected — `update_workflows.yml` has no other helper-based Telegram sends, and the raw-curl fallback only fires when the runtime `tg_helpers.sh` fetch from `coding-workflows@stable` fails.
- `review_autofix.yml` "Telegram success" step (the step ending in `tg_send_tracked "${PR_NUMBER}" "${MSG}" "DEBUG"` that emits the `🔍 DEBUG: PR processed: #${PR_NUMBER}` ping) now sources its step-scoped `ALERT_MSG_LEVEL` env from the new dedicated `PR_PROCESSED_ALERT_LEVEL` variable, defaulting to `SILENT` (was `vars.ALERT_MSG_LEVEL || 'DEBUG'`). The DEBUG ping fired once per successful run, so orchestrator-driven branches that synchronize many times (N pushes → N pings) drowned admin chats in `PR processed: #N` notifications; the existing `ALERT_MSG_LEVEL` knob was too coarse because consumers may still want OTHER DEBUG alerts from the same workflow. Mirrors the prior `update_workflows.yml` `SILENT` default pattern. Consumer repos that want the ping back can set `vars.PR_PROCESSED_ALERT_LEVEL=DEBUG` (no PR required); values above `DEBUG` (e.g. `WARNING`, `ERROR`, `CRITICAL`, `SILENT`) keep the ping suppressed. The underlying `tg_send_tracked "${PR_NUMBER}" "${MSG}" "DEBUG"` line is unchanged — only the per-step threshold defaults higher. Consumers who had `vars.ALERT_MSG_LEVEL` set to `WARNING`/`ERROR`/`CRITICAL`/`SILENT` were already suppressed and remain suppressed (no regression). No other `tg_send_*` call sites in `review_autofix.yml` are affected: the workflow's other helper-based Telegram sends fire at `WARNING`/`WARN`/`ERROR`/`CRITICAL` (error paths only), and the only other DEBUG-level `tg_send_tracked` call (the "Telegram conflict resolution message" step) is gated on `env.CONFLICT_RESOLVED == 'true'` and does not run once per successful iteration, so it stays on the shared `ALERT_MSG_LEVEL` default. README's `ALERT_MSG_LEVEL` row gets a second `**Exception:**` sentence and a new dedicated `PR_PROCESSED_ALERT_LEVEL` row in the env-vars table.
- `ensure_label_exists` in `scripts/validate_process.sh` no longer routes the "label already exists, skipping" duplicate-label case through `tg_notify` (DEBUG); it now emits a local `::debug::` stderr line, matching the existing behaviour in `scripts/label_helpers.sh` and `scripts/orchestrate_poll_process.sh`. Genuine label-create failures still raise a `tg_notify` WARNING. No Telegram alert is sent for expected duplicate-label races.
- H8: made reviewer watchdog PR-state polling interval configurable via `REVIEW_PR_STATE_POLL_INTERVAL_SECS` in `scripts/review_run_reviewers.sh` (default `10`, valid `10..3600`), with `rate_limit_audit_fallback` warning and fail-open fallback to default for invalid/out-of-range inputs.
- Added H4 PR comment hydration shim in `scripts/gh_helpers.sh`: `gh_pr_with_all_comments` now uses a single GraphQL call for PR metadata + issue/review comments with deterministic ordering, mandatory fail-open REST fallback, and shared legacy JSON output contract for judge consumers.
- review/autofix now caches PR `closingIssuesReferences(first: 50)` once per job in `LINKED_ISSUES_JSON` and reuses it for linked-issue status/label updates and Telegram single-issue links, preserving existing PR title/body REST fallback and downstream behavior.
- Completed H1 migration for remaining workflow surfaces by replacing
  support-file GitHub Contents API fetch loops in `validate.yml` and
  `issue_pr_status.yml` with checkout-based staged support transport,
  preserving existing gate behavior and `${SCRIPT_REF} -> main` fallback.
- Extended staged AI memory schema lists to include the new cache schema
  entries for actions runs and workflow log analysis (best-effort staging
  until files exist on support refs).
- H7: removed repo label-existence GET probes from `ensure_label_exists`
  in `scripts/orchestrate_poll_process.sh` and `scripts/validate_process.sh`,
  now relying on direct `gh label create` with idempotent `already_exists`/422
  handling (including DEBUG skip logging) across those scripts and
  `scripts/label_helpers.sh`.
- Added H6 cross-run workflow log analysis cache persistence in
  `scripts/collect_workflow_logs.py` + `scripts/ai_memory_lib.py` using
  ai-memory branch fail-open reads/writes, ETag-aware run collection, 304
  snapshot reuse, and 500-entry LRU seen-set trimming for jobs/log archives.
- Removed all cycle-based runtime reasoning effort downgrades — every phase now
  uses the configured `THINKING_LEVEL_*` as-is (`xhigh` by default) for all cycles:
  - Removed adaptive judge downgrade (`xhigh` → `high` after cycle 3) in `orchestrate_poll_process.sh`.
  - Removed adaptive validate downgrade (`xhigh` → `high` after cycle 3) in `validate_process.sh`.
  - Removed issue-summary generation reasoning override (forced `low`) in `implement.yml`.
  - Removed `REVIEW_REASONING_SCHEDULE` / `REVIEW_AUTODOWNGRADE_DISABLED` reviewer cycle schedule machinery in `review_autofix.yml`.
- Trimmed static prompt assembly in planning, implementation, and review/autofix workflows to reduce token overhead.
- Updated review/autofix prompt assembly to inline pre-assembled static context directly into editor/reviewer prompts (removed runtime "read pre_assembled_static.txt first" round-trip instructions).
- Updated README to remove all references to adaptive reasoning, reasoning schedules, and smoke test reasoning overrides.

- **The default editor model for every codex-driven phase is now `openai/gpt-5.5`; the capacity fallback moved to `openai/gpt-5.4`.**

Every workflow that previously defaulted its model to `openai/gpt-5.4` — clarify, plan, implement (editor and diagnose), orchestrate, orchestrate-poll (judge and conflict resolver), review_autofix (editor and consolidator), validate, check-failure triage, workflow-log-analysis, validation refresh, the security audit, and the release gate's `ALT_EDITOR_MODEL` canary — now defaults to `openai/gpt-5.5`. The `WORKFLOW_EDITOR_FALLBACK_MODEL` capacity fallback in `plan.yml`, `implement.yml`, and `review_autofix.yml` moved the opposite way to `openai/gpt-5.4`, so the final retry attempt still escapes to a different OpenRouter/OpenAI per-model TPM bucket. Repo-var names are unchanged and any explicitly set repo var keeps overriding the default; the `gpt-5.4-mini` / `gpt-5.4-nano` auxiliary defaults are untouched.

| The numbers that matter | Value |
| --- | --- |
| New primary editor default | `openai/gpt-5.5` |
| New capacity-fallback default (`WORKFLOW_EDITOR_FALLBACK_MODEL`) | `openai/gpt-5.4` |
| Workflow files with swapped default literals | 13 |
| Repo-var override names changed | 0 |

What this means for operators: repos that never set `WORKFLOW_EDITOR_MODEL` (or the per-phase model vars) start running every codex phase on `gpt-5.5` at the next `@stable` sync, and a sustained `gpt-5.5` capacity crunch now falls back to `gpt-5.4` on the final retry attempt. Repos that pin models via repo vars see no change.

- **Three of the six review-autofix reviewer slots move to cheaper models with 1M+ token windows.** `x-ai/grok-4.6` becomes `x-ai/grok-4.20`, `moonshotai/kimi-k3` becomes `z-ai/glm-5.2`, and `mistralai/mistral-small-2603` becomes `google/gemini-3.1-flash-lite` in the `REVIEWER_MODELS` roster of `.github/workflows/review_autofix.yml`.

Across five recent successful review runs, Kimi K3 and Grok 4.6 accounted for $14.52 of the $19.09 reviewer spend that logged usage, and output tokens were only 17% and 3% of their cost respectively, so lowering reasoning effort would not have fixed it. The cost sits in input: each reviewer pass sends 170K to 400K uncached prompt tokens, Grok 4.6 charges $2.00 per million for those and $0.50 per million for cache reads, and Kimi K3 re-read 20 million cached tokens in eight calls. Mistral Small's slot was replaced for a different reason: its 262K window overflowed on most reviewer prompts and its shared OpenRouter capacity was rate-limited upstream, so it succeeded in roughly one run in eight. Every replacement keeps at least a 1M token window, and `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` plus the `opencode-live-smoke.yml` roster follow the same swap.

| The numbers that matter | Value |
| --- | --- |
| Grok slot, input / output per M tokens | $2.00 / $6.00 to $1.25 / $2.50 |
| Kimi slot, input / output per M tokens | $1.70 / $8.50 to $0.65 / $2.04 |
| Mistral slot, context window | 262K to 1M |
| Estimated Grok + Kimi spend on the five sampled runs | $14.52 to about $7.30 |
| New catalog entries | 4 (`glm-5.2`, `glm-5.3-flashx`, `grok-4.3`, `gemini-3.1-flash-lite`) |

What this means for operators: reviewer cost per PR should drop by roughly half with no change to reasoning effort, the two-pass structure, or the six-slot panel, and the Mistral slot stops burning retries and stall budget on every run. Override `vars.REVIEW_TIER_STANDARD_REVIEWER_SLUGS` or the workflow roster if a repo needs the previous models; the retired slugs stay in the catalog.

### For contributors

`scripts/reviewer_failback_chains.json` maps each new reviewer to a same-family target with a 1M+ window: `x-ai/grok-4.20 -> x-ai/grok-4.3`, `z-ai/glm-5.2 -> z-ai/glm-5.3-flashx`, and `google/gemini-3.1-flash-lite -> google/gemini-3-flash-preview`. The old `x-ai/grok-4.20 -> x-ai/grok-4.1-fast` entry was dropped because OpenRouter and models.dev no longer list that slug; the retired-roster entries for `moonshotai/kimi-k3` and `x-ai/grok-4.6` remain for operator overrides. `docs/codex-model-reference.md` was regenerated from the catalog via `make generate`.

- **A failed stable release or promote-cycle tick is retried on the next tick, up to three attempts per tip, instead of waiting for the branch to move.**

Until now one failed `test-and-mark-stable.yml` run froze the schedulers on that commit: `auto-release-stable.yml` skipped every 6-hour tick with `AUTO_RELEASE_SKIPPED reason=last_gate_failed`, and the daily cycle in `promote-main-to-stable.yml` skipped with `PROMOTE_CYCLE_SKIPPED reason=no_code_changes_since_failed_run`, until a new commit landed or an operator re-ran the gate by hand. That was right for a failing test on the commit and wrong for everything transient. On 2026-09-21 the v1.29.7 release run (35570966035) passed its whole gate and then lost the tag push to a GitHub-side timeout, and nothing would have retried it. Both schedulers now count the failed runs on the current tip and dispatch again while the count is below the budget; a cancelled stable-release gate run does not count, while an externally cancelled daily promote-cycle job counts toward the promote-cycle budget. Once the budget is spent the old hold applies, so a deterministic failure stops costing gate runs, and the `Workflow Failure Heal Intake` hotfix that fixes it moves the branch and unlocks the next attempt.

| The numbers that matter | Value |
| --- | --- |
| `AUTO_RELEASE_STABLE_MAX_ATTEMPTS` (repo var, `auto-release-stable.yml`) | default `3` failed gate runs per `stable` tip |
| `PROMOTE_CYCLE_MAX_ATTEMPTS` (repo var, `promote-main-to-stable.yml` cycle job) | default `3` failed cycle runs per `main` tip |
| Retry cadence | the schedulers' own ticks: every 6 hours on `stable`, daily on `main` |
| Extra API cost | none on `stable`; at most `PROMOTE_CYCLE_MAX_ATTEMPTS` compare calls per cycle tick |

What this means for operators: a release that failed on a runner loss, a GitHub-side rejection or another one-off goes out on a later tick by itself. Set either variable to `1` to restore the previous hold-on-first-failure behaviour. The skip lines now carry `attempts=N max=M`, so a tip that is genuinely stuck is visible as such.

### For contributors

`scripts/auto_release_stable.sh` counts completed runs on the `stable` branch with the tip's SHA and a `failure`, `timed_out` or `startup_failure` conclusion from the same 30-run read it already made. `scripts/promote_main_cycle.sh` walks failed scheduled cycle runs newest first, counts each one that no code change separates from the tip, and stops at the first one a code change does; the newest failed run keeps the `base_not_ancestor` and `guard_unavailable` handling it had. Both scripts reject a non-positive budget at startup.

### For contributors

`tests/test_editor_capacity_fallback_contract.py` now pins `openai/gpt-5.4` as the fallback slug, and `scripts/codex_model_catalog.json` swaps the two entries' role descriptions (no generated-reference field changed). Historical references to the earlier `gpt-5.4` cutover and issue #3515 keep their original wording.

- **Review/autofix read-side model calls now run through OpenCode.** Both reviewer passes, cache probes, and consensus summarisation use isolated read-only OpenCode configurations with the existing reasoning, retry, failback, heartbeat, and ledger contracts. The later write-side cutover, documented separately in this release, moves the remaining review/autofix model calls to OpenCode.

- **Review/autofix now runs its complete model pipeline on OpenCode.** The editor, review-blocked judge and judge-fix path, consolidator, and merge-conflict resolver use isolated reviewer/writer configurations while preserving retries, fallback models, watchdogs, ledgers, write guards, and fingerprint gates.

The reusable review workflow no longer installs Codex or creates and mutates a shared Codex configuration. Reviewer-role configurations deny shell and structured write access to the checkout, and OpenCode installation failures stop the workflow immediately. Existing `CODEX_*` compatibility identifiers and watchdog helper names remain unchanged, and a requested thread-reuse path now logs its documented fallback to a fresh full prompt. OpenCode failures never fall back to Codex; they emit the stable `opencode_agent_failure` alert, while the advisory consolidator retains its existing fail-open empty-artifact behavior.

Reverting this P3 change restores the Codex installation and write-side invocation paths without reverting the P2 OpenCode reviewer and summariser cutover. Do not propagate this change to `@stable` until the documented three-run review parity hold completes.

- **The opencode cutover's cache-read parity gate is now forward-only.** The `@stable` hold criterion no longer requires comparing post-cutover cache-read telemetry against a pre-P2 Codex baseline.

Planning for issue #3873 blocked with `BLOCKED: Pre-P2 GitHub logs/artifacts contain cache_read_input_tokens=na` (run 33210878301) because the gate in `docs/plans/opencode-review-autofix-cutover-plan.md` demanded a numeric pre-P2 cache-read baseline that never existed: the Codex-era pipeline logged `cache_read_input_tokens=na` on every production reviewer call (verified against run 33177800142) and the run ledger records only input/output/total tokens. The plan's production criterion now splits the telemetry gate: latency stays within ~20% of the pre-cutover baseline reconstructed from GitHub Actions run metadata, while cache-read is gated on the three post-cutover runs themselves — each must emit numeric cache-read telemetry, show `cache_read_input_tokens > 0` where prompt reuse is expected, and stay within ~20% of one another. The parity report records the pre-P2 cache-read baseline as `unavailable`, never as zero.

What this means for operators: the `@stable` hold for the opencode cutover is now satisfiable without inventing historical telemetry; unblocking issue #3873 requires only a `/answer` after this merges. No workflow or script behaviour changes in this PR.

- **`/audit-plans` now gates its recommendation on in-flight orchestrator work.** The command only recommends a plan that can be started without risking merge conflicts with running orchestrator projects, and explicitly recommends nothing when no plan qualifies.

The audit still classifies every plan under `docs/plans/` and ranks the remaining work by value, but the final pick now passes a merge-conflict screen first. The command lists open `ai:orchestrator-tracking` issues and their unmerged PRs (integration and wave PRs on `orchestrator/project-*` branches, plus `Refs #N`-linked PRs), builds a footprint from those PRs' changed files and the declared scope of waves that have not yet produced PRs, and screens the ranked candidates against it. The recommendation is the highest-ranked conflict-free plan; when every candidate overlaps in-flight work, the report says so and names each blocking issue or PR and the overlapping paths instead of hedging with a "least risky" pick. If GitHub state cannot be read, the pick is reported as UNSCREENED rather than assumed safe.

What this means for operators: a `/audit-plans` recommendation is now safe to hand straight to `/implement-plan-ai` or `/implement-plan-claude` without first checking whether a running orchestrator project is already touching the same files, and a "no recommendation" outcome is a deliberate wait-for-merge signal, not a failure. Both command copies (`.claude/commands/` and `workflow-templates/.claude/commands/`) ship the change, so consumer repos pick it up on the next `@stable` sync.

- **`/audit-plans` now archives verified-complete plans and sweeps the scripts-pending-removal registry on every run.** The audit report and conflict-gated recommendation stay chat-only, but the command is no longer strictly read-only.

Each run now moves every `docs/plans/*.md` plan it classifies COMPLETE (with `file:line` / test evidence) into `docs/completed/` via `git mv`, and evaluates every entry in `docs/scripts-pending-removal.md` against its §18.F contract: a script is removed, with its registry entry deleted in the same commit, only when its removal trigger is met and every preflight check passes exactly as written. Entries marked `permanent — review annually` are never auto-removed, and a failing or unrunnable check keeps the script and is reported. When either action changed something, the run commits (one commit per scope), pushes, and opens a maintenance PR whose body satisfies the plan-archival lint (`Refs #N` per moved plan, `## De-scoped phases` when the tracking issue has unchecked boxes); a run with nothing to archive or remove makes no commit and opens no PR, exactly as before.

What this means for operators: completed plans no longer linger in `docs/plans/` waiting for a manual move, and scripts whose documented removal conditions are met are cleaned up automatically instead of accumulating in the registry — each such change arriving as a reviewable maintenance PR, never a direct push. Both command copies (`.claude/commands/` and `workflow-templates/.claude/commands/`) ship the change, so consumer repos pick it up on the next `@stable` sync.

- **`/verify-activation` now audits the code before it will call a project COMPLETE.** A new `Correctness: PASS / CONCERNS / FAIL` axis runs on every invocation, and a FAIL downgrades `Implemented:` to PARTIAL.

The command used to grade implementation by presence: it checked that the files existed and the linked PRs were merged, then spent its depth on the activation question. A feature that was fully wired, correctly triggered, and subtly wrong came back LIVE. The implementation check now maps every acceptance criterion to the code that satisfies it, and a new audit step reads that code against plan conformance, security, error paths, concurrency and idempotency, naming immutability (§6), DB contracts (§10), API budget (§15), automation bias (§18), test coverage, and docs. It also runs the repo's existing tests and linters against the local checkout, in a form that changes nothing: no edits, commits, pushes, workflow dispatches, or network mutations, with formatters in check mode only.

Findings are ranked BLOCKER or CONCERN and classified EVIDENCE-BASED or HYPOTHESIS, so an unverifiable concern cannot pass silently as PASS, and a check that could not run is reported as `could-not-run` instead of being dropped. The verdict vocabulary is unchanged: `LIVE / DORMANT / INCOMPLETE` and `COMPLETE / PARTIAL / NOT` keep their meanings, and a FAIL reaches `/deploy-activate` through the PARTIAL to INCOMPLETE path its completeness gate already stops on.

| The numbers that matter | Value |
| --- | --- |
| Audit surface | Files the linked merged PRs touched, files the plan names, immediately reachable call sites |
| New report lines | `Correctness:`, `Audit findings`, `Checks run` |
| Verdict tokens added | 0 — the axis is separate from LIVE / DORMANT / INCOMPLETE |
| `/deploy-activate` changes needed | 0 |
| Extra API calls | One `pulls/<N>/files` call per linked PR |

What this means for operators: a `/verify-activation` run takes longer and tells you more. LIVE now means implemented, audited, and running, rather than wired and running. A project whose code does not do what its plan said comes back INCOMPLETE with the defect cited at `file:line`, and `/deploy-activate` refuses to emit a runbook for it.

### For contributors

Both copies of the command changed. The consumer template copy is side-aware: it audits at the ref for the side under test (`THIS_REPO@main` or the resolved `UPSTREAM_SHA`, never upstream `main` when the consumer is pinned to a release), adds a wrapper-to-upstream input contract check, tags findings `[CONSUMER]` or `[UPSTREAM]`, and records upstream checks it cannot execute as `could-not-run` rather than treating unread code as correct. The command stays read-only and never fixes what it finds; remediation routes to `/investigate-issue`, `/code-review --fix`, or `/validate-consumer-issue` for an upstream defect.

- **CI now runs deterministic shared shell-block policy guards immediately after checkout.** Guard failures stop the job before Python setup, dependency installation, lint, and tests consume runner time.

The `Shared shell-block anti-regression checks` step retains its existing Codex configuration, memory bootstrap, Telegram helper, and watchdog-helper policies. Rejections now emit the secret-safe `CI_GUARD_FAILURE` diagnostic with the guard, check, file, line, expected policy, and policy-specific scanned-file set. The lint job keeps its existing four-way orchestrator-poll sharding and 45-minute timeout.

| The numbers that matter | Value |
| --- | --- |
| Guard position | Immediately after checkout |
| Diagnostic fields | 6 |
| Lint job timeout | 45 minutes |

What this means for contributors: deterministic policy drift now fails early with enough context to identify the affected helper and file without exposing the matched source line.

- **Merge conflicts on the generated workspace manifest are now resolved deterministically, without a Codex resolver run.** When `.ai/.workspace_source_manifest.txt` is the only conflicted file, the whole LLM resolver invocation is skipped.

Every AI PR that adds files appends lines to `.ai/.workspace_source_manifest.txt`, the sorted file inventory generated by `scripts/workspace_init.sh`, so any two concurrently-open wave PRs merging into a shared base conflicted on it with near-certainty. Each such conflict cost a full Codex resolver run (~5 minutes at `high` reasoning) plus a re-dispatched review cycle — PR #3909 alone burned three resolver runs in one day, each with the manifest as the sole conflicted path. `scripts/review_conflict_prepare.sh` now resolves a two-sided content conflict on the manifest with exact set algebra, `(ours ∩ theirs) ∪ (ours − base) ∪ (theirs − base)`, keeping both sides' additions and honouring either side's deletions. When nothing else is conflicted it commits the two-parent `[ai-merge-resolve]` merge itself and `scripts/review_conflict_resolve.sh` short-circuits before any model invocation; when other conflicts remain, only those go to the Codex resolver. Push, the Telegram conflict-resolution notice, and the re-review dispatch behave exactly as before, and integration-sync branches (`orchestrator/project-*`) plus delete/modify conflict shapes still use the Codex resolver unchanged.

| The numbers that matter | Value |
| --- | --- |
| Codex resolver runs saved per manifest-only conflict | 1 (~5 min LLM time) |
| Manifest-only resolver runs on PR #3909, 2026-08-30 | 3 of 3 |
| New repo var (kill switch) | `CONFLICT_MANIFEST_UNION_ENABLED`, default `true` |

What this means for operators: the "Merge conflicts resolved automatically" Telegram notice still arrives for these resolutions, but the run behind it no longer spends an LLM resolver attempt when the manifest was the only conflict. Set repo var `CONFLICT_MANIFEST_UNION_ENABLED=false` to route manifest conflicts back through the Codex resolver.

### For contributors

The union-merge lives in `scripts/review_conflict_prepare.sh` between the resolver-allowlist capture and the empty-allowlist check; `tests/test_conflict_manifest_union_contract.py` pins the contract (kill switch default, integration-sync exclusion, exact commit message, `MERGE_CONFLICT` left intact) and functionally exercises the extracted live pipeline against a real scratch-repo conflict.

- Made deleted-integration-branch security findings repairable through the normal automated fix loop.

The orchestrator now recreates a confirmed-absent integration branch only at the verified immutable final-PR head, verifies race winners without overwriting them, and clears stale final-delivery state before opening the consolidated fix issue. Repairs therefore advance the tree re-audited by the security pass and flow through replacement validation and final delivery.

- **A review-blocked judge `spot-fix` verdict now keeps the closed PR's work by default.** `REISSUE_PRESERVE_BASELINE_ENABLED` defaults to `true` instead of `false`.

When the review-blocked judge closes a PR with `close_and_reissue` and asks for `reissue_mode: spot-fix`, `scripts/review_rb_judge.sh` now pushes the closed PR's head to an `ai/reissue-baseline/pr-<n>-<sha12>-<run>-<attempt>` branch and records `prior_pr_baseline_branch` and `files_touched` in the replacement issue, and `implement.yml` starts the next implement run from that branch. Before this change the same verdict was silently downgraded to `redo` unless a repo had set the variable itself: on fun-token-multi-chain run 33600594555 the judge asked to keep 16 commits of dependency, documentation, and regression-test work from PR #454, logged `REISSUE_BASELINE_DISCARDED requested=spot-fix reason=feature_flag_disabled`, and replacement issue #455 shipped with no baseline branch. Repos that want the old behaviour set `REISSUE_PRESERVE_BASELINE_ENABLED=false`.

| The numbers that matter | Value |
| --- | --- |
| Default before / after | `false` / `true` |
| Workflow fallbacks flipped | 3 (`review_autofix.yml` judge step, `implement.yml` job env and baseline resolver) |
| Flag introduced | 2026-08-19 (commit 6d08bfc), bake-out flip never landed |

What this means for operators: nothing to configure. A `spot-fix` reissue no longer throws away the closed PR's work, and the implement side keeps its guards (trusted issue author, exact branch format, PR number and head-SHA checks, fail-open to the base branch), so a bad baseline costs one implement run rather than a bad merge. Set the variable to `false` on a repo to force every reissue back to `redo`.

### For contributors

The script-level `:-false` fallbacks in `scripts/review_rb_judge.sh` and the resolver's Python default in `implement.yml` were flipped alongside the workflow defaults so a missing env var behaves the same everywhere. `test_review_autofix_wires_reissue_preserve_baseline_flag_default_false` is now `..._default_true`. The three sibling Phase flags (`JUDGE_INTERIM_ENABLED`, `CONSOLIDATOR_REJECT_SCHEMA_ENABLED`, `BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED`) still default to `false`.

- **Review/autofix resolves base-branch conflicts before the reviewers run, queues `ai/issue-*` PRs that edit the same files behind the oldest one (merge train), and serializes orchestrator siblings that declare no `files_touched`.**

On 2026-09-07 the pipeline sent 18 "Merge conflicts resolved automatically" pings across three repos: every conflict was found only after a 35–96 minute reviewer pass and an editor commit, and a burst of ten same-file PRs against `main` conflicted on every merge. `review_autofix.yml` now hands a content conflict found by the pre-review merge-topology gate straight to the existing resolver tail and re-dispatches the review on the merged head, so reviewers and editor never work on a head the base already conflicts with. A new `scripts/review_merge_train.sh` labels a younger `ai/issue-*` PR `ai:merge-queued` when its changed files overlap an older open `ai/issue-*` PR on the same base, and releases it (label removed, review re-dispatched) from `cancel_on_pr_close.yml` whenever a blocker closes and from `orchestrate_poll.yml` on every tick, including in repos with no active orchestrator project. Automated release retires the queued marker before removing the label, while failed label creation writes no marker, so only a human label removal authorizes the one-shot bypass; managed and standalone stall recovery also treat queued PRs as intentional waits without empty commits, judge calls, or alerts. The orchestrator partition guard treats an empty `files_touched` list as unknown scope and serializes it, closing the bypass project binance-blessings#249 used. The resolver, review-blocked judge and poller keep any root file the consumer repo tracks instead of removing it as a workflow artifact.

| The numbers that matter | Value |
| --- | --- |
| Resolver rounds for N overlapping PRs | at most N (was up to N²) |
| `PRE_REVIEW_CONFLICT_RESOLVE_ENABLED` default | `true` |
| `MERGE_TRAIN_ENABLED` / `MERGE_TRAIN_MAX_OLDER_PRS` defaults | `true` / `20` |
| `CONFLICT_RESOLVED_ALERT_LEVEL` default | unset (falls back to `ALERT_MSG_LEVEL`, then `DEBUG`) |
| New label | `ai:merge-queued` |
| New partition overlap type | `unknown_scope` |
| Workflows changed | `review_autofix.yml`, `orchestrate_poll.yml`, `cancel_on_pr_close.yml` |

What this means for consumer repos: on the next `@stable` sync, overlapping AI PRs wait their turn instead of fighting over the base, and each PR resolves its conflict at most once and before any reviewer spend. Set `MERGE_TRAIN_ENABLED=false` or `PRE_REVIEW_CONFLICT_RESOLVE_ENABLED=false` to opt out; set `CONFLICT_RESOLVED_ALERT_LEVEL=SILENT` to drop the per-resolution Telegram ping.

### For contributors

The merge train's API budget is documented in the script header (§15): this PR's paths normally come from the diff `review_collect_pr_metadata.sh` already fetched (Git-quoted headers fall back to the canonical PR-files API), older PRs cost one `pulls/N/files` page set each (cached per run, capped by `MERGE_TRAIN_MAX_OLDER_PRS`), blocker-bearing gates inspect the marker comments once, and release performs one cycle-local active-runs lookup plus marker retirement before dispatching. The 16 reviewer/editor-phase steps carry `env.AUTOFIX_PRE_REVIEW_RESOLVE != 'true'`; the tail from `Detect merge conflicts` onward is unchanged. The poller's managed and standalone conflict loops and stall-recovery paths skip `ai:merge-queued` PRs so they do not dispatch rounds or empty commits the train is holding back.

- **The AI label sync no longer sends a Telegram ping when nothing changed.** `sync_ai_labels.yml` now drops its clean-run `🔍 DEBUG: AI label sync for <repo>: created=0, updated=0, unchanged=N, errors=0` message by default; warnings and errors still arrive.

Every `@stable` release fires the `coding-workflows-stable-released` dispatch into each consumer repo's `ai-sync-labels.yml` wrapper, and the reusable `sync_ai_labels.yml` workflow answered every one of those runs with a DEBUG ping even when all labels were already in place. A new repo var, `AI_LABEL_SYNC_SUCCESS_ALERT_LEVEL`, is applied as the `Telegram status` step's threshold only when the run outcome is clean (exit 0, zero per-label errors); it defaults to `SILENT`, so `scripts/tg_helpers.sh` suppresses that one message. Runs with per-label errors (`WARNING`) or a failed sync (`ERROR`) keep honouring the global `ALERT_MSG_LEVEL` exactly as before, and the run's step summary still shows the counts. Dry-runs are gated the same way as real runs.

| The numbers that matter | Value |
| --- | --- |
| New repo var | `AI_LABEL_SYNC_SUCCESS_ALERT_LEVEL` |
| Default | `SILENT` |
| Opt back in | `vars.AI_LABEL_SYNC_SUCCESS_ALERT_LEVEL=DEBUG` |
| Pings affected | the clean-run `DEBUG` message only |

What this means for operators: after the next `@stable` sync the label-sync workflow is quiet unless it created or updated a label with errors, or failed outright. Set `vars.AI_LABEL_SYNC_SUCCESS_ALERT_LEVEL=DEBUG` on any repo where you still want a confirmation ping per run.

### For contributors

The knob follows the `PR_PROCESSED_ALERT_LEVEL` pattern in `review_autofix.yml`: it overrides `ALERT_MSG_LEVEL` for one step, not the workflow, and only on the DEBUG branch of the summarised outcome. The consumer wrapper template `workflow-templates/ai-sync-labels.yml` is unchanged because it already calls the reusable workflow via `@stable`.

- **Interactive Claude Code sessions no longer watch pull requests after pushing them.** A new CLAUDE.md §25 forbids subscribing to PR activity, offering to watch a PR, and the event-driven autofix CI / address-comments mode, and a `PreToolUse` hook enforces it.

Until now the harness prompt told a session to offer PR watching after it opened a pull request, and an accepted offer turned into a `subscribe_pr_activity` subscription that woke the session on every CI failure and review comment to autofix and reply. §25 turns that off in this repo and, through the existing root `CLAUDE.md` mirror in `update_workflows.yml`, in every consumer repo on the next `@stable` sync. The rule is strict: it holds even when the user asks for a watch in the session, and the reply names §25 and the reviewed change needed to re-enable it. Fixing CI or addressing review comments still happens when the user asks for it directly, under plain §12; the §12.G add-ons that only made sense for the event-driven mode are marked inactive and kept in place so section numbers stay stable. `.claude/hooks/pr_watch_guard.py`, wired in `.claude/settings.json` under the matcher `mcp__.*__subscribe_pr_activity`, blocks every `subscribe_pr_activity` call from any MCP server and never blocks `unsubscribe_pr_activity`; it ships to consumers through the same `.claude/` asset sync as the §21 merged-PR guard and is part of the `/seed-repo` asset set.

| The numbers that matter | Value |
| --- | --- |
| New `CLAUDE.md` section | §25 |
| Hook | `.claude/hooks/pr_watch_guard.py` |
| Settings matcher | `mcp__.*__subscribe_pr_activity` |
| Test file now run by `ci.yml` | `tests/test_pr_watch_guard.py` |
| GitHub API calls issued by the hook | 0 |

What this means for operators: after a session pushes a branch and opens its pull request, it reports the link and stops. CI failures and review comments on that PR are handled by the unattended review pipeline as before, or by a session when you ask it to in chat, never by a background subscription. Scheduled self check-ins (`send_later`, Routines) are unaffected. Nothing to configure; the hook and the §25 text arrive in consumer repos on the next sync.

### For contributors

The hook has no environment-variable escape hatch, unlike the §21 guard's `CLAUDE_PR_MERGE_GUARD`: re-enabling PR watching for a repo means editing §25 and removing the hook entry from `.claude/settings.json` in a reviewed change. The settings matcher is a regex so that a subscribe tool exposed by a future MCP server is caught without a settings edit; `tests/test_pr_watch_guard.py` asserts it selects every known subscribe tool under both anchored and unanchored regex interpretation and never selects the unsubscribe tools. The unattended pipelines never read `CLAUDE.md`, so the `review_autofix` workflow and orchestrator stall recovery are unchanged.

- **Security-pass findings must recommend automated controls, not human gates.** The security-audit prompt, the exhaustion-judge prompt, the consolidated fix-issue body, and the advisory follow-up body now carry a mitigation policy that forbids recommending a human approval step, an operator-run command, or a manual sign-off; a recommendation may involve a person only when it starts with `HUMAN GATE REQUIRED:` and names the narrowest trigger condition.

Project #3965's security pass reported `prompt-injection-authorizes-pr-merge` (issue #4017) with the recommendation "require deterministic policy and independently authenticated human approval before merging or closing PRs". The fix issue carried that text verbatim, the implementer followed it (PR #4029), and every terminal review-blocked judge decision on the integration branch then waited for a maintainer to post `/review-blocked-approve`. PR #4079 sat pending for eleven hours behind that gate while the review sweep re-ran the judge every 30 minutes. The prompts now state the automation-bias rule the planner and implementer are already bound by (unattended_system_instructions.md §20), so the auditor phrases mitigations as identity- and provenance-scoped authorization, fail-closed validation, least-privilege tokens, or sandboxing instead.

| The numbers that matter | Value |
| --- | --- |
| Prompts changed | `prompts/mode-security-audit.txt`, `prompts/mode-judge-security-pass-exhaustion.txt` (and their `_templates/` sources) |
| Issue bodies changed | consolidated `[security-pass] … fix cycle N` issue, `[security-pass] Advisory: …` follow-up |
| Escape prefix | `HUMAN GATE REQUIRED:` |
| Regression test | `tests/test_security_audit_prompt_policy.py` |

What this means for operators: security-pass fix cycles keep the pipeline unattended by default. A finding that genuinely needs a person shows up with the `HUMAN GATE REQUIRED:` prefix in the findings table, so the gate is scoped to that condition rather than to every terminal decision.

### For contributors

Findings, severities, and the findings-JSON schema are unchanged; only the shape of `recommendation` text is constrained. The follow-up plan to narrow the existing gate on `orchestrator/project-3965` is `docs/plans/review-blocked-approval-gate-trust-scope-plan.md`.

- **Security-pass re-audits now treat the previous fix cycle's own code as fresh attack surface.** Each delta re-audit hands the audit engine the hunks the merged security-fix PR wrote, keeps those files in scope for one further re-audit, and tells the auditor to look for new defect classes in them instead of only re-verifying earlier findings.

Until now a re-audit after a merged fix re-verified the previously reported findings, looked for remaining instances of the same defect class, and narrowed its scope to files changed since the last audited commit plus files those findings cited. Nothing told the auditor that the fix's new code was new, and a fix-touched file that no finding cited left scope after one cycle. On tele-funtoken-msg-scoring#4281 every fix issue merged and no finding ever repeated, yet the project spent 3 of 3 cycles and ended in `ai:security-pass-failed`; the last finding was a hole in `_season_pool_settlement_readiness`, a predicate cycle 1's fix created and cycles 2 and 3 audited without being told it was fresh. The scheduled poller (`.github/workflows/orchestrate_poll.yml`, `run_security_pass_inline` in `scripts/orchestrate_poll_process.sh`) now computes a fix-cycle diff entry from its local checkout on every delta re-audit, carries the previous cycle's entry over once from the new `security_pass_fix_touched_files` state field, and passes both to `scripts/security_audit.sh` as `SECURITY_AUDIT_FIX_CYCLE_DIFFS`. The engine appends the added and modified hunks to the prompt as newly introduced code with rules to audit readiness and state-transition predicates, money-state transitions, and idempotency fences for new defect classes, and keeps the existing prior-findings rules unchanged. Hunks are capped; past the cap the remaining files are listed by name and the run log says so. Every step fails open: a git failure, a malformed entry, or an unresolvable commit logs a warning and the audit runs exactly as before. No new GitHub API call is made.

| The numbers that matter | Value |
| --- | --- |
| Fix cycles spent on tele-funtoken-msg-scoring#4281 before it failed | 3 of 3, every fix issue merged, no repeated finding |
| Re-audits a fix cycle's code stays in scope after its merge | 2 (the next re-audit and one more) |
| Default hunk cap (`SECURITY_AUDIT_FIX_DIFF_MAX_LINES` / `SECURITY_AUDIT_FIX_DIFF_MAX_BYTES`) | 1200 lines / 96000 bytes |
| Files per fix-cycle entry handed to the engine | at most 200 |
| Fix-cycle entries kept in project state | at most 3 |
| New GitHub API calls | 0 |

What this means for operators: a project whose fix introduces a new hole is now told about that hole on the very next re-audit, while the fix code is still in scope, instead of discovering it on the last cycle of the budget. The `SECURITY_PASS_SCOPE` log line gains `fix_cycle_diff_entries=<n> fix_cycle_diff_files=<n>`, and the engine log gains a `security-audit: fix-cycle-diffs=...` line. `ENABLE_SECURITY_PASS=false` still disables the whole pass; there is no separate switch for this addition because it never blocks an audit.

### For contributors

`security_pass_fix_touched_files` holds `{cycle, since_sha, head_sha, files}` rows, normalized by `ensure_security_pass_state_fields`, written by the same `jq` that records `security_pass_head_sha`, and cleared wherever `security_pass_reported_findings` is cleared. The engine input is findings-json only and, unlike `SECURITY_AUDIT_PRIOR_FINDINGS`, fails open. The convergence plan's D2 (`docs/plans/security-pass-convergence-plan.md`) is independent but must treat carried-over fix-cycle hunks as new code if it ships. Coverage: `tests/test_security_audit_workflow_contract.py` (`fix_cycle` tests) and `tests/test_orchestrate_poll_process.py` (`test_security_pass_reaudit_hands_fix_cycle_diff_to_engine_and_carries_it_once` plus the extended first-audit, delta, and `/re-security-pass` tests).

- **`/deploy-activate` now executes DigitalOcean steps itself when `DIGITALOCEAN_ACCESS_TOKEN` is present, instead of handing every DO command to the operator to paste.** Applies to both this repo's command and the consumer-repo template.

The command's "you guide, I execute" contract gains one carve-out, wired to CLAUDE.md §22. When the session environment carries `DIGITALOCEAN_ACCESS_TOKEN`, the session runs DigitalOcean API calls directly (`doctl` when installed, REST otherwise): reads such as app specs, deployed env vars, deployment status, and logs are self-serve at any point while building the runbook, and mutations such as spec/env-var updates or forced redeploys still go through the one-step-at-a-time loop — the step is emitted with the exact resource and change named, and only runs after the operator confirms it. Resource IDs are resolved from the `## DigitalOcean resources` table in the repo's root agents file (`agents.md` here, `AGENTS.md` in consumer repos): the session uses recorded IDs without re-asking, and when an ID is missing it asks once in Q/A format, verifies the ID resolves with a read call, and records it in the table in the same push as the activation log so no future session asks again. Without the token (or on 401/403), the command falls back to its original guide-and-paste mode for the DigitalOcean steps.

| The numbers that matter | Value |
| --- | --- |
| Files changed | `.claude/commands/deploy-activate.md`, `workflow-templates/.claude/commands/deploy-activate.md` |
| New command section | `## DigitalOcean Steps` |
| Env var gating the behavior | `DIGITALOCEAN_ACCESS_TOKEN` (session environment) |
| ID registry | `## DigitalOcean resources` in the root agents file (CLAUDE.md §22.C) |

What this means for operators: during a `/deploy-activate` run, DigitalOcean steps no longer bounce to your terminal when the session has the token — the session reads DO state itself and performs the confirmed mutation for you, then shows the API output. The consumer-repo template picks the change up on the next `@stable` sync; repos without the token see no behavior change.

### For contributors

Token hygiene and the ask-first mutation posture of CLAUDE.md §22 are unchanged — the command's step-confirmation loop is what satisfies §22.B's approval requirement. The unattended pipelines never read `.claude/commands/`, so no codex-driven phase gains DigitalOcean access from this change.

- **`/investigate-issue` now lands upstream fixes itself when the library is attached to the session.** A consumer-repo session that also has `shubhodeep1/coding-workflows` checked out no longer stops at "open an upstream session" — it auto-chains into `/validate-consumer-issue` and ships the fix.

Until now, an `[UPSTREAM]` root cause found from a consumer repo always ended read-only: the command emitted a proposed diff pinned to the consumer's upstream SHA and told the operator to open a separate `shubhodeep1/coding-workflows` session to apply it. That hand-off existed because a consumer session could not push to the library. When both repos are attached to the same session, that is no longer true, so `/investigate-issue` now detects a pushable upstream working tree, finishes the investigation exactly as before, then chains into `/validate-consumer-issue`, which applies, verifies, commits, pushes, and opens the upstream PR in the attached checkout. With no upstream attached, both commands behave exactly as they did — the read-only hand-off is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Command templates changed | `workflow-templates/.claude/commands/investigate-issue.md`, `workflow-templates/.claude/commands/validate-consumer-issue.md` |
| Checks a checkout must pass to count as attached | 4 — is the library, clean tree, reachable `origin`, push permitted |
| Default landing branch for the upstream fix | `stable` (`main` only when the consumer pins `@main`) |
| New Evidence Ledger fields | `UPSTREAM_ATTACHED`, `UPSTREAM_CHECKOUT`, `UPSTREAM_BASE` |
| Behaviour when upstream is not attached | unchanged |

What this means for operators: attach `shubhodeep1/coding-workflows` alongside the consumer repo before running `/investigate-issue`, and an upstream defect comes back as a PR against `stable` instead of a diff to carry to another session. A `[BOTH]` root cause produces two PRs — the consumer one first, then the upstream one — never one commit spanning two repos. Nothing is attached automatically: if the library is absent, dirty, or not pushable, the run keeps the old read-only ending.

### For contributors

Attachment is detected, never created — the commands will not clone the library to unlock the write path, and a dirty upstream tree counts as not attached so in-flight work is never branched over. Diagnosis stays pinned to `UPSTREAM_SHA` even when a checkout is attached; the checkout is a write target only, and it sits at whatever ref it was cloned to. `UPSTREAM_BASE` resolves to `stable` for `@stable`, version-tag, and raw-SHA pins, since a tag is not a mergeable PR base — a tag-pinned fix is validated at the pinned SHA and re-verified against `stable` before push, and carries the usual port-to-`main` caveat. `/validate-consumer-issue` keeps full ownership of the decision to land: being chained grants no exemption from its `MISCONFIG` / `NOT-REPRODUCIBLE` / `INCORRECT` verdicts or the §6 and §10 gates. A rejected push restores the attached tree to its original branch and falls back to the read-only output rather than leaving a half-applied fix behind. Upstream PR bodies reference the consumer issue with `Refs owner/repo#N`; cross-repo auto-close keywords are forbidden (§19).

- Hardened planning, implementation, judging, and review prompts with shared application-security and money-handling checks.

- **The security-audit engine now supports an inert machine-readable findings mode.** Callers can request strictly validated, confidence- and scope-filtered findings JSON without tracker, label, follow-up, or notification side effects, while the existing weekly issue-producing mode remains the default.

- The orchestrator's mandatory current-head security pass now defaults on before validation or finalization. Operators retain the immediate `ENABLE_SECURITY_PASS=false` kill switch.

- **The AI pipeline moves to the latest models across every role: the editor family jumps to GPT-5.6 and four of the six reviewer slots move to their families' current releases.** `WORKFLOW_EDITOR_MODEL` now defaults to `openai/gpt-5.6-sol` in every codex-driven phase, `WORKFLOW_EDITOR_FALLBACK_MODEL` moves from `openai/gpt-5.4` to `openai/gpt-5.5`, and every `openai/gpt-5.4-mini` utility role now defaults to `openai/gpt-5.6-luna`.

The editor, consolidator, judge, clarify, plan, orchestrate, validate, security-audit, check-failure-triage, and workflow-log-analysis phases all follow the single `WORKFLOW_EDITOR_MODEL` default to `gpt-5.6-sol` (1.05M context, currently $2/$10 per Mtok promotional pricing). The capacity-fallback slot — the model each editor retry loop switches to on its final attempt — becomes `gpt-5.5`, keeping the different-TPM-bucket property while staying one generation behind the primary. The lightweight roles (release-gate log analyser, weekly retro, unselected-run summaries, reviewer consensus summariser, materiality fallback, behavioural smoke) move from `gpt-5.4-mini` to `gpt-5.6-luna` at a quarter of the input price. The reviewer roster in `review_autofix.yml` swaps `minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`, `qwen/qwen3.6-plus`, and `x-ai/grok-4.20` for `minimax/minimax-m3`, `moonshotai/kimi-k3`, `qwen/qwen3.7-plus`, and `x-ai/grok-4.6` — the Kimi swap is a repair as much as an upgrade, since Moonshot retired the k2 series upstream on 2026-05-25. Review-tier defaults, reviewer failback chains, and `scripts/codex_model_catalog.json` all follow (seven new catalog entries in total), and `deepseek/deepseek-v4-pro` plus `mistralai/mistral-small-2603` stay put as already-current.

| The numbers that matter | Value |
| --- | --- |
| New editor default | `openai/gpt-5.6-sol` (1.05M context) |
| New editor fallback | `openai/gpt-5.5` (was `gpt-5.4`) |
| Utility-role default | `openai/gpt-5.6-luna` ($0.20/$1.20 per Mtok) |
| Reviewer slots updated | 4 of 6 |
| New catalog entries | 7 (`gpt-5.6-sol`, `gpt-5.6-luna`, `minimax-m3`, `kimi-k3`, `kimi-k2.7-code`, `qwen3.7-plus`, `grok-4.6`) |
| Kimi K2.5 upstream retirement date | 2026-05-25 |

What this means for operators: repos overriding any of the model repo vars (`WORKFLOW_EDITOR_MODEL`, `WORKFLOW_EDITOR_FALLBACK_MODEL`, `LOG_ANALYZER_MODEL`, `XPOLL_SUMMARISER_MODEL`, `REVIEWER_*`, `REVIEW_TIER_*`) keep their overrides; repos on the defaults pick up the new models on the next `@stable` sync. No env var names changed, and reasoning-effort defaults (`xhigh` policy) are unchanged.

### For contributors

The prompt-budget sizing comments in `scripts/review_apply_fixes.sh` and `scripts/gh_helpers.sh` now note that the 200k-token input budget is bound by the `gpt-5.5` fallback's 272k window, not the 1.05M-context primary. Retained failback entries (`qwen3.6-plus -> qwen3-coder-plus`, `grok-4.20 -> grok-4.1-fast`) stay in `scripts/reviewer_failback_chains.json` for operator roster overrides. `docs/codex-model-reference.md` was regenerated from the catalog via `make generate`.

- **`/verify-activation` now fixes what it finds.** After grading a project LIVE / DORMANT / INCOMPLETE, the command applies every evidence-based audit finding and every code-only activation gap on a branch, verifies each fix, and opens one ready-for-review PR that lists each issue, why it was an issue, and the fix applied.

The command used to stop at the diagnosis: it reported defects at `file:line` and pointed the operator at `/investigate-issue` or `/code-review --fix` for the remedy. It now carries the remedy itself. A new fix step runs after the verdict, selects the findings that qualify under a written Fix Policy, applies the smallest change that removes each defect, re-runs the check that demonstrated it, and ships the result as a single PR on `claude/verify-activation-<slug>`. The chat report gains `Fix PR:`, `Fixes applied`, and `Not fixed` sections, and the PR body carries a table with the same four columns per fix: the finding, why it is an issue, what changed, and the check that proves it. The verdict is still graded against the default branch before any fix is applied, so a fix PR never upgrades INCOMPLETE to LIVE in the same run, and `/deploy-activate` keeps gating on the reported verdict.

| The numbers that matter | Value |
| --- | --- |
| Findings fixed automatically | EVIDENCE-BASED BLOCKER and CONCERN, plus `CODE-FIXABLE` activation gaps |
| Findings never fixed automatically | HYPOTHESIS, §6 renames, §10 contract changes, §12.D tradeoffs, `OPERATOR` gaps |
| PRs per run | 1, reused across re-runs while it stays open |
| Operator steps performed | 0 — repo-vars, secrets, pin bumps, merges, `@stable` tags stay under `To activate` |
| New report sections | `Fix PR:`, `Fixes applied`, `Not fixed` |

What this means for operators: a `/verify-activation` run on a defective project now ends with a PR to review instead of a list of follow-up commands to type. Activation gaps that need a repo setting, a secret, a merge, or a release tag are still enumerated for you and never performed. Anything the command chose not to fix is listed under `Not fixed` with the reason, and with a Q/A question when a decision would unblock it.

### For contributors

All three copies changed: the repo-local command, the consumer template under `workflow-templates/.claude/commands/`, and consumer copies received on the next `.claude/` sync. The consumer template fixes the `[CONSUMER]` side only: `[UPSTREAM]` findings and the upstream half of a `[BOTH]` finding are reported with a proposed fix at `UPSTREAM_SHA` and routed via `/validate-consumer-issue`, and a consumer session never pushes to `shubhodeep1/coding-workflows` even when that checkout is attached. The fix step runs under CLAUDE.md §12 (PR Review Mode): §6 naming immutability and §10 contracts stay hard rules, every fix must be verified before it is pushed, and the PR body follows §19 (`Refs #N`, never an auto-close keyword) and §20 (a `changelog.d/` fragment when a fix changes observable behaviour). The `/deploy-activate` intro in all three copies now describes its companion as diagnose-and-fix rather than diagnose-only.

### Removed

- **Interactive slash commands no longer pin a model.** The `model:` frontmatter on every `.claude/commands/*.md` file is gone, so a `/command` runs on the model the operator picked for the session.

Each of the 12 commands in `.claude/commands/` previously declared its own model tier (`sonnet`, `opus`, or `best`), and three of them (`/apply-url`, `/audit-plans`, `/verify-activation`) also ran in a forked subagent via `context: fork` with `background: false` so the pin would hold past the first turn. All of those keys are removed. The command files now start directly with their prompt body, and the model in effect is whatever the session is set to, via `/model` or the session's configured model, for every turn of the command. The unattended pipeline is unaffected: it selects its models through repo-vars and reads `unattended_system_instructions.md`.

| The numbers that matter | Value |
| --- | --- |
| Command files with `model:` removed | 12 |
| Command files with `context: fork` / `background: false` removed | 3 |
| Consumer-repo files changed | 0 (`.claude/commands/` is not synced) |

What this means for operators: pick the model once with `/model` and every slash command honours it, including the later turns of commands that stop for CLAUDE.md §2 questions. Nothing about any command's body, arguments, or behaviour changed.

### For contributors

The `## Interactive slash-command model pins` section in `agents.md` is replaced by `## Interactive slash-command model selection`, which records that the absence of a pin is deliberate. Keep the command body as the first line of each file: a leading `---` is parsed as frontmatter.

### Fixed
- Reviewer and editor prompt renders no longer hard-fail when a PR diff embeds a template `{% include "..." %}` line. The review/autofix pipeline builds the reviewer and editor prompt bodies by concatenating the raw PR diff, comments, and reviewer findings, then runs them through `scripts/render_prompt.sh` for placeholder substitution. `render_prompt.py` was also expanding `{% include "..." %}` directives over that already-composed body, so a single context line such as `{% include "_partials/site_footer.html" %}` — ubiquitous in template-driven consumer repos (Jinja/Django/Nunjucks/Twig/Liquid all share the syntax) — was parsed as a real prompt-fragment include, failed to resolve under the prompt search path, and took the whole run down with `PromptAssemblyError`. This surfaced on consumer `tele-funtoken-msg-scoring` AI Review run `29182737982` (consumer PR 3548), where the reviewer step exited in ~1s and every downstream editor/commit step was skipped.

  The three render sites that embed untrusted PR content now set the new opt-in `RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED=1` env var, which maps to `render_prompt.py --input-already-assembled` and skips include-assembly for that body while still rendering the static-scaffolding placeholders. The earlier `RENDER_PROMPT_SKIP_SYNTAX_VALIDATION=1` fix only silenced the `validate_supported_template_syntax` gate, which runs *after* include expansion, so it never covered this path.

  | The numbers that matter | Value |
  | --- | --- |
  | New opt-in env var | `RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED` (default unset → include assembly unchanged) |
  | Wired in `render_prompt.sh` | maps to `--input-already-assembled`, guarded against double-add |
  | Body render sites fixed | `scripts/review_run_reviewers.sh` (reviewer base + model-family overlay), `scripts/review_apply_fixes.sh` (editor) |
  | Regression tests | `tests/test_assemble_prompt.py` (unfixed body hard-fails; fixed body keeps the include line literal and still renders placeholders) |
  | Trigger | consumer `tele-funtoken-msg-scoring` run `29182737982`, consumer PR 3548 |

  What this means for operators: a consumer PR whose diff contains templating syntax no longer deterministically fails AI Review. No consumer-side change is required — the fix lives entirely in this library's shared prompt renderer and ships on the next `@stable` bump. Trusted `prompts/` template renders keep strict include assembly and still fail loudly on a genuine missing fragment, so real prompt-authoring typos are still caught (`tests/test_assemble_prompt.py` still asserts this).

- **Consumer-repo artifact cleanup no longer deletes or overwrites files the repo actually tracks.** A repo-owned root-level `agents.md` survives the review, judge, and orchestrator commit paths, while tracked `pre_assembled_static.txt` content survives implement, editor, resolver, and judge cleanup.

Five cleanup sites removed workflow-staged artifacts from a consumer repo's working tree by name, without checking whether the repo tracked that path. Each feeds a later `git add -u` / `git add -A` staging pass, so the working-tree removal was recorded in the commit as a real deletion. `agents.md` is the collision that matters: CLAUDE.md §22.C and §24.F require DigitalOcean and Cloudflare resource IDs to live in the repo's root agents file, and the pipeline stages an artifact at exactly that path. Each site now skips tracked paths and logs `Preserving repo-tracked path during artifact cleanup: <path>`; implement, review-editor, merge-resolver, review-blocked-judge, and bootstrap-overwritten poller paths also restore the consumer's `HEAD` content before staging, while untracked artifacts are still removed.

| The numbers that matter | Value |
| --- | --- |
| Scripts fixed | `review_conflict_resolve.sh`, `review_rb_judge.sh`, `orchestrate_poll_process.sh`, `review_conflict_prepare.sh`, `implement_commit_changes.sh`, `review_commit_changes.sh` |
| Guarded paths per site | 5, 16, 13, 8, 1, and 4, respectively |
| Incident | `shubhodeep1/binance-blessings` PR #255, merge commit `b974f8b` |
| Regression test | `tests/test_consumer_artifact_cleanup_preserves_tracked.py` |

What this means for consumer repos: a tracked `agents.md`, `ai_pipeline.md`, `unattended_system_instructions.md`, `pre_assembled_static.txt`, or consumer-owned `scripts/` file is no longer deleted or replaced with workflow-generated content by an AI commit, and the false `⚠️ Editor changes lost` retry loop that followed such a deletion no longer starts.

- **Stable release workflows now recover safely after partial publication failures.** Both workflows prefer the repository PAT for release API operations.

`mark-stable.yml` and `test-and-mark-stable.yml` retain an existing immutable version tag only when it points to the intended release commit. They treat an existing matching GitHub Release as complete, while conflicting tags and non-404 lookup errors still fail closed. Operators can rerun after tag publication without deleting or retargeting immutable tags.

- **Stable release Git pushes now prefer the repository PAT.** Release checkouts use `GH_PAT` for changelog and tag publication when configured, while retaining `github.token` as a fallback.

- **The wave judge no longer overflows codex-cli's prompt cap when a merged PR commits minified bundles.** Every PR diff embedded in the judge prompt is now bounded by bytes, per PR and across the prompt, and the poller logs the assembled prompt size before the first codex exec of each judge phase.

Project #3928 on tele-funtoken-msg-scoring stopped advancing because `AI Orchestrate Poller` run 35425771769 raised `Orchestrator Judge failed for #3928. Manual review needed.` on a green job. Both judge attempts died before the model ran with `turn/start failed: Input exceeds the maximum length of 1048576 characters`. The prompt's merged-PR-diff block was "truncated" only by `head -n 500`, and two wave-5 PRs (#4416 and #4425, the committed Cloudflare Worker `dist` refreshes) carried 44-66 KB single-line bundles, so 500 lines still meant about 390 KB each and the whole prompt reached about 1.28 M characters. `scripts/orchestrate_poll_process.sh` now caps each wave-judge and inline review-blocked-judge diff at `JUDGE_PR_DIFF_MAX_BYTES` after the existing line cap, spends one shared `JUDGE_PR_DIFFS_TOTAL_MAX_BYTES` budget across the wave prompt (merged PRs first, in sorted issue order, so the cache-stable merged block never depends on an open PR's size), and prefixes every cut diff with a note that tells the judge to read the files directly. A failed byte truncation elides that diff rather than embedding the original payload, and either prompt bypasses attempts that cannot reach the model when it still exceeds the character limit. Both knobs are declared in `orchestrate_poll.yml` as repo variables with defaults.

| The numbers that matter | Value |
| --- | --- |
| codex-cli `turn/start` stdin cap | 1,048,576 characters |
| Judge prompt in the failing run | ~1,279,000 characters |
| Largest single diff line | 66,064 bytes |
| PR #4416 / PR #4425 after the 500-line cap | ~391 KB / ~391 KB |
| `JUDGE_PR_DIFF_MAX_BYTES` default | 65536 bytes |
| `JUDGE_PR_DIFFS_TOTAL_MAX_BYTES` default | 524288 bytes |
| Affected poll run / tracking issue | 35425771769 / #3928 |

What this means for operators: a wave whose PRs commit large generated artifacts now reaches a judge verdict instead of a CRITICAL alert, with no operator action. The poll log prints byte and character counts before the first attempt and raises a workflow warning above 950,000 characters, so a future overshoot is diagnosable from the run log. A prompt above 1,048,576 characters fails immediately through the existing judge-failure path rather than repeating the same rejected request. Raise the two repo variables on repos whose judge needs more diff context; the API-call count is unchanged (one diff fetch per linked PR).

- **The `python-mongo-repo-checks` validation image now installs the consumer's `requirements.txt`.** Repo checks that import the consumer's own modules no longer fail with `ModuleNotFoundError`.

Until now `workflow-templates/validation-harness/python-mongo-repo-checks/Dockerfile.app.j2` installed only the harness tools (`pyyaml`, `jsonschema`, `jinja2`, `pytest`). Any consumer whose `custom_tests` ran unit tests against modules importing third-party packages failed runtime validation, and the failure was reported as `raw_status=harness_error`. The template now runs `pip install -r` on the consumer's requirements file after `COPY . /workspace`, the same way `python-mongo-flask` already does. The file defaults to `requirements.txt`, can be overridden with `slots.requirements_file` in `.ai/validate.yml`, and is skipped when absent.

| The numbers that matter | Value |
| --- | --- |
| Reported by | shubhodeep1/binance-blessings#249, validation run 35727922881 |
| Symptom | `Ran 61 tests`, `errors=9`, all `No module named 'requests'` |
| After the fix, same consumer head, `python:3.12-slim` | `Ran 677 tests`, `OK`, repo-check exit 0 |

What this means for consumer repos on `python-mongo-repo-checks`: validation now exercises your real dependency set. Consumers without a `requirements.txt` see no change. A requirements file that cannot install on `python:3.12-slim` (no compiler in the image) now fails the image build instead of failing later on imports.

- **The security-pass loop now converges on its own, and advisories parked in `ai:blocked` before a merge re-plan themselves.** The exhaustion judge may grant another fix cycle at most `MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS` times (default 2); later rounds turn `keep_fixing` into deferred advisories, and the final merge re-answers follow-ups the planner had parked.

Project #3965 finished its one-issue plan on 2026-09-03 and then spent sixteen days in the security pass: seven consolidated fix cycles across three resets, because the exhaustion judge chose `keep_fixing` in both of its rounds and `MAX_SECURITY_PASS_JUDGE_ROUNDS=0` left the sequence unbounded. Its two waived findings (#4090, #4091) were filed before the integration branch merged, the planner answered `BLOCKED: PR #3968 is still open`, and standalone stall recovery skips `ai:blocked` by design, so both waited for a human `/answer`. Two changes in `scripts/orchestrate_poll_process.sh` close both gaps. `security_pass_exhaustion_judge` now tells the judge when `keep_fixing_available` is `false` and, past the cap, rewrites any `keep_fixing` decision to `accept_with_followup` (`SECURITY_PASS_JUDGE_KEEP_FIXING_CAPPED`), so the remaining findings become deferred `ai:security` advisories and completion continues; `fail` verdicts are unchanged. `security_pass_unblock_filed_advisory_followups`, called from every site that records `final_merge_status = "merged"`, posts one `/answer [auto-answered-by-poller]` on each already-filed follow-up that is open and labelled `ai:blocked`, and records each issue in `security_pass_followups_merge_checked` so it is read at most once (`SECURITY_PASS_ADVISORY_FOLLOWUP_UNBLOCKED`).

| The numbers that matter | Value |
| --- | --- |
| New repo variable | `MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS` (default `2`; `0` restores the unbounded loop) |
| Judge rounds spent on #3965 before this shipped | 2, both `keep_fixing` (cycles 6 and 7 on a 5-cycle budget) |
| API cost of a successful re-plan check | one issue GET per follow-up, plus one paginated comments GET and at most one POST per `ai:blocked` follow-up |
| Issues this reproduces | #4090, #4091 (`ai:blocked`), #4113 (cycle 7 of #3965) |

What this means for operators: a security-pass project that reaches the exhaustion judge a third time no longer gets a further fix cycle; its remaining findings become non-blocking advisories filed after the merge, and the project completes. Follow-ups that were already parked in `ai:blocked` re-enter planning when the integration branch lands on the default branch, with a `🔓 Security-pass advisory follow-ups re-planned` comment on the tracking issue. A human is still needed only for a project-wide `fail` verdict (`ai:security-pass-failed`), which the judge reserves for findings the automated pipeline cannot land.

- **Validation stops before template rendering when renderer dependencies are unavailable.**

The reusable validate workflow now confirms that PyYAML, jsonschema, and Jinja2 can be imported by the Python interpreter used for validation. If dependency installation fails or imports are unavailable, validation reports the existing harness-error outcome without invoking the template renderer. The tracking-issue comment includes the preflight diagnostic, while the workflow still collects status and artifacts.

What this means for operators: missing renderer dependencies produce a clear failure report rather than an attempted render with a misleading Python environment probe.

### For contributors

State gains `security_pass_followups_merge_checked` (issue numbers, deduped, last 100; follow-ups the filer creates after the merge are added at creation); the judge diagnostics JSON gains `max_keep_fixing_rounds` and `keep_fixing_available`, and `prompts/mode-judge-security-pass-exhaustion.txt` carries the matching rule. Converted decisions keep the judge's justification behind the prefix `[keep_fixing capped after <c> judge round(s); converted to advisory follow-up]`, and the accept-all judge comment says how many were converted.

- **Security-pass fix cycles now advance only when the merged fix PR targets the project's integration branch.**

Security-pass polling now validates a merged fix PR's base branch before consuming a fix cycle. A PR merged into `main` or another branch no longer advances a project whose fix belongs on its integration branch, even when the fix issue already carries `ai:merged`. Both the batched GraphQL path and the conditional timeline fallback enforce the same check while preserving retry behavior when evidence lookup fails. The fallback reuses the PR payload it already fetches, so polling gains no unconditional API request.

| The numbers that matter | Value |
| --- | --- |
| Merged-evidence paths protected | 2 |
| New unconditional API calls per poll | 0 |
| Wrong or missing candidate bases accepted | 0 |

What this means for operators: security-pass cycle budgets now reflect fixes that actually reached the integration branch, rather than unrelated or misdirected merges.

### For contributors

Regression coverage exercises both cached GraphQL evidence and direct-lookup timeline evidence with an initial `ai:merged` label, while retaining the valid integration-branch path.

- **Stall-recovery re-issues keep their preserved PR baseline, and the implement editor can no longer poison the staged-support ledger through its own test run.**

Two implement-side defects kept project #4139's security-pass fix issue from ever producing a PR. First, when orchestrator stall recovery re-issued the review-blocked fix issue #4227 as #4242, it appended its `Re-issued from #4227` trailer after the `**Review-blocked reissue metadata**` footer, and the `Resolve trusted prior PR baseline branch` step in `.github/workflows/implement.yml` treated the trailer as part of the footer, logged `Baseline override ignored: review-blocked reissue metadata footer is incomplete`, and implemented against the planning ref instead of the preserved head of PR #4174. The parser now reads the footer up to the first `---` rule after its header, so any appended re-issue trailer is ignored while trailing prose without a rule still fails closed. Second, every codex editor launch now runs through `env -u STAGED_SUPPORT_LEDGER -u STAGED_SUPPORT_BASE_DIR -u STAGED_SUPPORT_EDITOR_HEAD_LEDGER -u IMPLEMENT_STAGED_SUPPORT_RUN_DIR`, so a `pytest` the editor starts cannot append fixture paths to the live run's editor-head ledger and fail the post-editor reinstall with `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=scripts/helper.sh`. PR #4243 already strips those variables in `tests/conftest.py`, but a review-blocked baseline branch or an unsynced integration branch checks out an older `conftest.py`, so the workflow now holds on its own.

| The numbers that matter | Value |
| --- | --- |
| Implement runs that lost the preserved baseline | 2 (35656715219, 35668070395) |
| Implement runs that failed the post-editor reinstall | 5 (35614385686, 35628923735, 35642366131, 35656715219, 35668070395) |
| Editor launch sites scrubbed | 2 (implementation attempt loop, post-Codex syntax repair loop) |

What this means for operators: the next implement run of a stall-recovered review-blocked re-issue checks out the closed PR's preserved head again, and a self-repo editor that validates its change with the staged-support tests completes instead of failing after the edit, whatever `conftest.py` the checked-out branch carries. No new variables, labels or workflow inputs; consumer repos never set the ledger paths and are unaffected.

### For contributors

`tests/test_review_rb_judge_reissue_baseline.py` covers the poller's two stall-recovery trailer wordings, stacked trailers, metadata-looking lines inside a trailer, and prose without a rule. `tests/test_implement_post_codex_recovery.py` pins both `env -u` launch lines and proves the `CODEX_THREAD_REUSE_*` prefix assignments still reach the helper.

- **A release-gate run now produces one Telegram message, its final pass/fail.** The per-phase pings from the smoke-test pipeline no longer leak past the gate's `SILENT` setting.

`test-and-mark-stable.yml`, and `promote-main-to-stable.yml` which dispatches it, were always meant to send a single combined notification from the `notify` job. Every pipeline workflow the smoke fixture triggers already exported `ALERT_MSG_LEVEL=SILENT` on detection, but each Telegram step declared its own step-level `ALERT_MSG_LEVEL` from repo vars, and a step's `env:` block overrides the job-level value in GitHub Actions. Operators therefore still received "Clarification required", "Plan awaiting approval", orchestrator and review pings during every release. The 18 step-level declarations across `clarify.yml`, `plan.yml`, `implement.yml`, `review_autofix.yml`, `orchestrate.yml`, `orchestrate_poll.yml` and `orchestrate_clarify_respond.yml` now read the job env first. `validate.yml` gains an optional `alert_msg_level` input (the gate passes `SILENT` to its standalone validate smoke), and `check_failure_triage.yml` runs silent for PRs labelled `e2e-smoke-test`.

| The numbers that matter | Value |
| --- | --- |
| Step-level declarations fixed | 18 across 7 workflows |
| New `validate.yml` input | `alert_msg_level` (default empty, honours `vars.ALERT_MSG_LEVEL`) |
| Contract test | `tests/test_smoke_alert_silencing_contract.py` |

What this means for operators: a release or promote run posts the `Release … SUCCEEDED` / `FAILED` message and nothing else. Production issues are unaffected, since only the smoke fixture sets the job-level `SILENT` value; `vars.ALERT_MSG_LEVEL`, `PR_PROCESSED_ALERT_LEVEL` and `CONFLICT_RESOLVED_ALERT_LEVEL` keep their existing meaning outside smoke runs.

### For contributors

Consumer repos receive the updated `ai-validate.yml` wrapper on the next `@stable` sync; the new input is optional, so wrappers that predate it keep working. Any new Telegram step in a smoke-silencing workflow must declare `ALERT_MSG_LEVEL: ${{ env.ALERT_MSG_LEVEL || … }}`; the contract test fails otherwise.

- **The release E2E gate now reserves enough time to exercise review-blocked recovery and attributes the result to its own poller run.** The review phase reserves 15 minutes beyond its longest permitted step, and unsafe timeout combinations fail before test issues are created. Phase 6 registers the branch-scoped run created after its dispatch so concurrent pollers cannot replace its status or logs; transient pinned-run and issue-label reads retry within their bounded window, then preserve the existing non-blocking timeout path and downstream verification.

- **The release gate's `e2e-smoke-test` job cap is now 300 minutes (was 120), so a healthy but slow smoke run is no longer cancelled while Phase 6 is still inside its own 30-minute window.**

Release v1.29.7 failed on `Test & Mark Stable Release` run 35479338161 with every phase healthy: Phase 4 (wait for review & autofix) spent exactly its 75-minute `REVIEW_STEP_TIMEOUT`, Phase 6 (review-blocked simulation) started at +99m, and the 120-minute job cap cancelled it at idle 1222s of its 1800s `PHASE_TIMEOUT` window. The cancel also skipped `Deep verify`, so `Verify all phases passed` blocked the release on `Internals` even though a Phase 6 timeout is scored as a non-blocking warning. Phase 6 could not finish sooner because its poller dispatch queued behind a scheduled `Internal: AI Orchestrate Poller` run that held the `ai-orchestrate-poll` concurrency group for 40 minutes while judging two live projects, and was then displaced from the single pending slot by later force-tick dispatches. The 300-minute cap in `.github/workflows/test-and-mark-stable.yml` is aligned with the workflow's 295-minute conservative serial-budget guard rather than a typical run, and its timeout comments now match that hard runner limit.

| The numbers that matter | Value |
| --- | --- |
| `e2e-smoke-test` `timeout-minutes` | 120 → 300 |
| Phase 4 per-step cap (`REVIEW_STEP_TIMEOUT`, unchanged) | 75m, clamped at 90m |
| Phase 6 inactivity window (`PHASE_TIMEOUT`, unchanged) | 30m |
| Failing run | 35479338161 (v1.29.7) |
| Parent `promote-main-to-stable.yml` cycle cap (unchanged) | 340m |

What this means for operators: a release-gate run whose review phase legitimately takes the full 75 minutes now gets its review-blocked simulation and deep verification instead of a `cancelled` gate, at the cost of the hard runner cap allowing a genuinely hung run to take up to 180 minutes longer to fail. Phase scoring, phase timeouts, and the promote cycle are unchanged.

- **The SessionStart hook no longer reports a valid `GH_TOKEN` as "invalid or expired" in Claude Code on the web.** It now probes over REST and says plainly when the session's agent proxy is substituting its own GitHub credential.

Every web session opened with `WARNING: 'gh auth status' failed ... (likely invalid or expired)` even when the PAT in the cloud environment was fine. Two things caused it. Claude Code on the web routes every `api.github.com` call through an agent proxy that strips the `Authorization` header and signs the request with its own short-lived GitHub App token, so `GH_TOKEN` never reaches GitHub at all. And `gh auth status` verifies over GraphQL, which that proxy refuses with HTTP 403, so the command misreports the token whatever its state. `.claude/hooks/session-start.sh` now checks with `gh api user`, sends one unauthenticated `GET /user`, and when that also returns 200 prints a `NOTE` naming the real situation: calls authenticate as the proxy's identity, reach only repositories attached to the session, GraphQL and some Actions paths are refused, and PAT-backed access needs a local Claude Code session. CLAUDE.md §23.A gains a "Web sessions" paragraph with the same facts and the `add_repo` / GitHub App installation route for reaching more repositories; §23.D and the README `GH_TOKEN` row stop recommending `gh auth status`.

| The numbers that matter | Value |
| --- | --- |
| API calls added at session start | 1 unauthenticated `GET /user` (the GraphQL `gh auth status` call is removed) |
| Files shipped to consumer repos on the next `@stable` sync | `.claude/hooks/session-start.sh`, `CLAUDE.md` |
| Pull request | #4172 |

What this means for operators: on the web, do not expect a session-environment PAT to widen GitHub reach; enable the repositories in the Claude GitHub App installation or attach them per session instead, and use a local CLI, desktop, or IDE session when the PAT itself is needed (other repositories without attaching them, GraphQL, repository variables). The hook now distinguishes confirmed proxy substitution, absence of an always-on proxy credential, and an inconclusive substitution probe without claiming that a failed probe proves the PAT was forwarded.

### For contributors

The substitution probe deliberately sends no credential rather than a bogus one, so the hook never produces bad-credential attempts against the user's account. `workflow-templates/.claude/hooks/session-start.sh` must stay byte-identical to the root copy; `tests/test_session_start_extract_repo_slug.py` enforces it.

- **Self-repo review runs no longer die in "Collect PR metadata" when a PR-branch helper reads `LINKED_ISSUE_METADATA_FILE`.** `review_autofix.yml` now exports the artifact path alongside `LINKED_ISSUE_CONTEXT_FILE`.

In this repository a pull request's review executes the PR-head copies of the `scripts/` helpers under `review_autofix.yml@main`, because `internal-review.yml` pins the reusable workflow to `main` while the support-ref step stages helpers from the PR's commit. PR #4174 made `scripts/review_collect_pr_metadata.sh` require `LINKED_ISSUE_METADATA_FILE` and added the export only to its own copy of the workflow, so every review run at that head failed before the reviewers started and the stall poller re-dispatched the same failure four times. `main` now exports `LINKED_ISSUE_METADATA_FILE=${RUNTIME_DIR}/linked_issue_metadata.json` in the "Initialize runtime workspace" step, `agents.md` documents the staging skew, and `unattended_system_instructions.md` §8 tells the editor that a new variable read by a staged helper must default inside the helper.

| The numbers that matter | Value |
| --- | --- |
| Workflow export added | `.github/workflows/review_autofix.yml`, "Initialize runtime workspace" |
| Failed review runs at one head | 35546298657, 35549937758, 35551938072, 35552937934 |
| Incident PR | #4174 (`ai/issue-4173`, head `662aacb`) |

What this means for operators: a review of a self-repo PR that ships this artifact no longer stalls on the env check, and the retry loop on PR #4174 ends once its branch also carries the in-helper default.

### For contributors

Nothing on `main` reads the new variable yet; the export exists so PR-head helpers that do read it find the same path the PR's workflow copy would set. The general rule is the in-helper default, not a `main`-side export per artifact.

- **The daily promote cycle and the 6-hourly auto-release no longer cancel each other's release gate, and a cancelled gate is retried instead of blocking the next release.**

On the first night both schedules fired at 00:00 UTC, the cycle's `gate_only` smoke run on `main` cancelled the stable release's E2E job through the gate's per-repository `cancel-in-progress` group, so no v1.29.7 was cut, and every later `auto-release-stable.yml` tick skipped with `AUTO_RELEASE_SKIPPED reason=last_gate_failed conclusion=cancelled`. `scripts/promote_main_cycle.sh` now waits for `test-and-mark-stable.yml` to be idle on every branch before dispatching (skipping with `reason=gate_busy` if it never frees within the idle-wait budget), `scripts/auto_release_stable.sh` counts active gate runs on any branch and any active `promote-main-to-stable.yml` run as in flight, and neither script treats a `cancelled` gate as a failed tip any more (`PROMOTE_CYCLE_SKIPPED reason=smoke_gate_cancelled` on the cycle side). `auto-release-stable.yml` runs at 30 past the hour so the two never start together.

| The numbers that matter | Value |
| --- | --- |
| auto-release cron | `30 */6 * * *` (was `0 */6 * * *`) |
| cycle wait for an idle gate | up to `PROMOTE_CYCLE_GATE_IDLE_WAIT_SECS` (default 1800s), polling every `PROMOTE_CYCLE_GATE_POLL_SECS` |
| cycle wait for its dispatched gate | `PROMOTE_CYCLE_GATE_WAIT_SECS` (default 18000s), starting after dispatch |
| E2E smoke job cap asserted by `tests/test_ci_poll_test_sharding.py` | 300 minutes (was 180, stale since #4168) |

What this means for operators: a stable patch and the daily cycle can coexist; the release goes out on the next 6-hour tick after the gate is free, and a gate cancelled by concurrency or a runner loss is simply retried rather than waiting for a human.

- **Release budget contracts now allow memory compaction to finish and stay synchronized with their workflow limits.** Memory maintenance receives a 20-minute job cap, while its 40-minute release watcher covers the 15-minute registration window, the full child runtime, and 5 minutes of slack. Contract tests enforce the complete timeout hierarchy and derive the E2E smoke limit from the workflow's named budget.

- **Stable releases now push the assembled changelog.** The `release` job's changelog step named the branch as a bare `stable`, which git rejects because `stable` is also a tag.

Every release from the `stable` branch retried the changelog push four times, logged "dst refspec stable matches more than one", and released without folding `changelog.d/` (the v1.29.7 gate, run 35570966035, carried 113 unfolded fragments). The push in `test-and-mark-stable.yml` and `mark-stable.yml` now targets `HEAD:refs/heads/<branch>`, and the stale-tip check inside the retry loop reads the tip with `git ls-remote origin refs/heads/<branch>` instead of a `git fetch` that resolved to the tag and never refreshed `origin/stable`.

What this means for operators: the next stable release folds the accumulated fragments into `CHANGELOG.md` on the `stable` branch and the release notes are extracted from the assembled file. Releases from `main` were never affected, because no tag is named `main`.

- **Stable releases now recover safely from ambiguous Git tag push failures.**

Release operators no longer lose an otherwise valid release when GitHub accepts a tag push but times out before confirming it. `.github/workflows/mark-stable.yml` and `.github/workflows/test-and-mark-stable.yml` now retry tag publication with exponential backoff and inspect the exact remote tag after each failed push. A matching remote object confirms that publication succeeded despite the failed response. A conflicting immutable version tag still fails without force, while only the existing `stable` and major pointers retain force-update behavior.

| The numbers that matter | Value |
| --- | --- |
| Maximum publication attempts per tag | 5 |
| Retry delays | 2, 4, 8, and 16 seconds |
| Remotely verified tags | Version, `stable`, and major-version tags |

What this means for release operators: transient, ambiguous GitHub responses can recover automatically, while genuine immutable-tag conflicts and unverifiable remote state continue to block the release.

- **The release step that tags a version now retries each tag push up to five times, so a transient GitHub rejection no longer fails a fully green release gate.**

`Test & Mark Stable Release` run 35570966035 (stable, v1.29.7) passed every gate job and then lost the release in "Tag version and update stable pointer": GitHub rejected the first push of `refs/tags/v1.29.7` with "Unable to determine if workflow can be created or updated due to timeout; `workflows` scope may be required.", the step ran each push once, and the job failed without cutting the tag. Because `scripts/auto_release_stable.sh` treats a failed gate on the current tip as `last_gate_failed`, auto-release then stopped re-dispatching that tip until a human re-ran the gate. Both `.github/workflows/test-and-mark-stable.yml` and `.github/workflows/mark-stable.yml` now publish the version, `stable`, and major-version tags through the bounded `publish_tag_with_remote_verification` helper. After a failed push, the helper verifies the exact remote tag and accepts the publication only when its object ID matches the local tag; genuine rejections still fail after the last attempt.

| The numbers that matter | Value |
| --- | --- |
| Attempts per tag push | 5 |
| Backoff between attempts | 2s, 4s, 8s, 16s |
| Tag pushes covered per workflow | 3 (`refs/tags/<version>`, `refs/tags/stable`, `refs/tags/<major>`) |
| Failing run | 35570966035 (v1.29.7) |

What this means for operators: a release gate that turns green no longer depends on a single push succeeding on the first try, and the auto-release tick is not left parked on a tip whose only failure was a server-side hiccup. Manual releases via `scripts/mark-stable.sh` are unchanged.

### For contributors

`tests/test_mark_stable_release_tag_refspec_contract.py` pins `publish_tag_with_remote_verification`, its five-attempt bound, remote-object verification, and the fully qualified `refs/tags/` publication calls in both workflows.

- **Stable release workflows now recover safely after partial publication failures.** Both workflows prefer the repository PAT for release API operations.

`mark-stable.yml` and `test-and-mark-stable.yml` retain an existing immutable version tag only when it points to the intended release commit. They treat an existing matching GitHub Release as complete only when it is published, non-draft, and not a prerelease; drafts, prereleases, conflicting tags, and non-404 lookup errors fail closed. Operators can rerun after tag publication without deleting or retargeting immutable tags.

- **Self-repo implement runs no longer fail after a successful edit because the editor's own test run wrote into the workflow's staged-support ledger.**

Implement runs 35614385686, 35628923735, 35642366131 and 35656715219 (issues #4227 and #4242, project #4139) each produced a complete change set and then died in the post-editor `reinstall` with `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=scripts/helper.sh`. `implement.yml` exports `STAGED_SUPPORT_EDITOR_HEAD_LEDGER` into the job environment, the codex editor inherits it, and when the editor validated its change with `pytest tests/test_implement_post_codex_recovery.py` the staged-support round-trip test ran `scripts/implement_staged_support_workspace.sh restore` with an environment copied from `os.environ`. The helper prefers that inherited variable over its ledger-relative default, so the test appended its fixture path `scripts/helper.sh` to the live run's editor-head ledger, failed its own ledger assertion, and left an entry the workflow could not reinstall. `tests/conftest.py` now strips the four staged-support runtime variables for the whole pytest session, the same way it already strips `GIT_DIR` / `GIT_WORK_TREE`, and `tests/test_pytest_git_env_isolation.py` pins it with a nested pytest run against a sentinel ledger.

| The numbers that matter | Value |
| --- | --- |
| Failed implement runs on the same defect | 4 (35614385686, 35628923735, 35642366131, 35656715219) |
| Variables stripped per session | `STAGED_SUPPORT_LEDGER`, `STAGED_SUPPORT_BASE_DIR`, `STAGED_SUPPORT_EDITOR_HEAD_LEDGER`, `IMPLEMENT_STAGED_SUPPORT_RUN_DIR` |
| Stall recovery cost before the re-issue | 2 retries, 1 stall-judge run, 127 minutes in `ai:implementing` |

What this means for operators: a self-repo implement run whose editor runs the staged-support tests completes instead of failing after the edit, so the stall poller stops retrying and re-issuing the same deterministic failure. The fix lives in the branch's `tests/conftest.py`, so an in-flight integration branch picks it up on its next `chore: sync main` merge.

### For contributors

Tests that exercise `scripts/implement_staged_support_workspace.sh` or `scripts/implement_commit_changes.sh` may still set the ledger variables explicitly; the session fixture only removes values inherited from the launching workflow. `WORKFLOW_RUNTIME_ENV_VARS` in `tests/conftest.py` is the combined list.

- **Release runs no longer page operators with per-phase clarify and plan alerts, and a PR that merely discusses the smoke fixtures is no longer mistaken for one.** The fixture detectors missed two of the gate's four title shapes, missed orchestrator-decomposed children entirely, and classified PRs from body prose.

Silencing a release run has three links: detect the fixture, export `ALERT_MSG_LEVEL=SILENT`, and have every Telegram step read that job-level value. The earlier fix repaired the third link across 18 step declarations, but left the first one narrow, so correctly-plumbed steps kept sending alerts at the repo's default `DEBUG`. `clarify.yml`, `plan.yml`, `implement.yml` and `review_autofix.yml` matched the literal `[E2E Smoke Test]`, which never matched `[E2E Clarify Negative Test]` (different middle token) or `[E2E Smoke Test alt-model]` (no `]` directly after `Test`). All four now use `^\[E2E `, covering every shape `test-and-mark-stable.yml` builds.

Orchestrator-decomposed children carry no marker at all: the `orchestrate-decompose-test` job hands a `[E2E Orchestrate Smoke <run_id>]` project description to the decomposer, which writes its own child titles, so no title pattern can match them. `plan.yml` and `implement.yml` now resolve the parent tracking issue instead, reusing the `^\[Orchestrator\] E2E ` signal `orchestrate_clarify_respond.yml` already relies on. In `implement.yml` this also restores the atomic `force-review` + `e2e-smoke-test` labels on such children's PRs, which `IS_SMOKE_TEST` gates.

`review_autofix.yml` now classifies a PR from that `e2e-smoke-test` label rather than by scanning the PR title and body for a fixture tag. The old scan matched any real PR that merely discussed the tags, which pinned reviewer and editor reasoning, silenced that PR's review alerts, and exported `IS_SMOKE_TEST` so `scripts/review_apply_fixes.sh` appended a "must call `apply_patch` on `tests/e2e_smoke_canary.txt`" directive to the editor prompt, on a PR that had nothing to do with the canary. The label is reliable because `implement.yml` applies it atomically at `gh pr create`, so it is already in the `pull_request: opened` payload the gate snapshots. The anchored linked-issue-title check stays as a second signal.

| The numbers that matter | Value |
| --- | --- |
| Detection sites corrected | 7 across 4 workflows |
| Fixture title shapes now matched | 4 of 4 (was 2 of 4) |
| Extra API calls | at most 1 per plan run and 1 per implement run, only when the title check misses and a `Tracking issue: #N` ref is present |
| Contract test | `tests/test_smoke_alert_silencing_contract.py` (15 tests, was 7) |

What this means for operators: a release or promote run posts its `Release … SUCCEEDED` / `FAILED` message and nothing else. Run 35672590166 sent a `CRITICAL` "Clarification required" for the negative-test fixture plus two `DEBUG` "Implementation plan generated" pings for the decomposed canary children; all three are now suppressed. Alerts on real issues are unchanged, and both parent lookups fail open, so an unreachable tracking issue leaves a genuine project's alerts on rather than silencing it or mislabelling its PR.

### For contributors

Anchoring at `^` also closes a false positive the previous `\[E2E Smoke Test\b` pattern had in `implement.yml`: a production issue titled e.g. "Fix [E2E Smoke Test] flake" was treated as the fixture itself and silenced. The parent lookup is resolved once in `implement.yml`'s precheck step and exported as `IS_SMOKE_PARENT`, so the main detect step reuses it instead of issuing a second call. `PR_TITLE` is retained in `review_autofix.yml`'s detect step although nothing reads it any more, because §6 forbids removing an existing identifier without the ask flow.

The contract test now pins the detection half of the chain as well as the plumbing: it derives the fixture tag set from `test-and-mark-stable.yml` itself, so a newly added fixture shape that no detector matches fails CI instead of reaching an operator's phone. Narrowing the detector constant, narrowing any workflow's use of it, reintroducing free-text PR-body matching, or dropping the parent lookup each fail a test.

- **A transient GitHub artifact-service error no longer fails the AI Orchestrate Poller.** The `Upload state snapshot artifact` step is now `continue-on-error`, so a blip in GitHub's artifact backend stops turning a healthy poll cycle red.

Operators stop getting ERROR alerts for poll runs that did all their work. On 2026-09-22 around 00:00 UTC, GitHub's artifact backend rejected `FinalizeArtifact` with a non-retryable 403 for about 40 seconds, and the four repos running the poller each happened to reach that step inside the window. The poll cycle, the snapshot build, and the `Publish state snapshot branch` step had all already succeeded in every run, so no orchestration state was lost, but the job still failed and paged. The artifact is a secondary, best-effort copy of the snapshot; consumers read the durable `state-snapshot` branch when its independently retried, best-effort publication succeeds.

| The numbers that matter | Value |
| --- | --- |
| Affected runs | 35669827207, 35669899082, 35669889742, 35669957264 |
| Outage window | roughly 40 seconds, 2026-09-22 00:00 UTC |
| Snapshot persistence channels that are best-effort | 2 (run artifact and `state-snapshot` branch push) |
| Spurious Telegram ERROR alerts this prevents | 4 per artifact-service blip |

What this means for operators: an artifact-service blip no longer fails the poller, and a poll failure alert once again points to the poll cycle, snapshot build, or another fatal workflow step. The snapshot artifact can be missing from a run without the job going red, so prefer the `state-snapshot` branch when reconstructing a tick and check workflow warnings if that tick is absent.

### For contributors

`if-no-files-found: error` is kept on the step on purpose: a genuinely missing `state.json` is a real defect and still surfaces as a failed-step annotation, though it no longer fails the job. `tests/test_state_snapshot.py` pins both settings, and pins that `Publish state snapshot branch` carries no blanket `continue-on-error`; expected branch-push failures remain handled by its existing retry-and-warning path. Consumer repos pick this up on the next `@stable` promotion through the existing `ai-orchestrate-poll.yml` wrapper, whose interface is unchanged.

- **Conflicted pull requests now receive the same automatic close cleanup as conflict-free pull requests.**

Repositories using the cancel-on-close wrapper now recover cleanup work when GitHub suppresses the `pull_request.closed` event for a conflicted pull request. The existing event path still handles ordinary closures immediately. A scheduled fallback validates every linked pull request before cancelling orphaned runs, preserving runs whenever state is missing, partial, or non-terminal. The release smoke test records mergeability diagnostics and recognizes either cleanup path without masking workflow-list API failures.

| The numbers that matter | Value |
| --- | --- |
| Scheduled fallback cadence | Every 5 minutes |
| Pull requests per GraphQL batch | Up to 50 |
| Active-run states scanned | `queued`, `in_progress` |

What this means for operators: conflicted closures converge without administrator policy changes, while uncertain or reopened pull-request associations remain untouched for a later safe retry.

### For contributors

The wrappers continue to use trusted default-branch workflow code, and the scheduled cleanup shares repository-scoped concurrency with the immediate event path.

- **The release gate no longer fails `Cancel-PR .. FAILED (no_run)` when the smoke-test PR has become conflicted with its base.** Phase 7 of `Test & Mark Stable Release` now makes the throwaway PR mergeable before closing it, so the close exercises the real `pull_request.closed` trigger.

Run 35672590166 closed smoke PR #4252 75 seconds after the forward-merge of `stable` into `main` had rewritten the same hunk of `.ai/.workspace_source_manifest.txt`, so the PR was conflicted at close time. GitHub does not run `pull_request` workflows for a conflicted PR, no cancel-on-close run appeared, and the gate blocked the nightly promotion. The scheduled cleanup sweep added for #4258 covers such closures in production, but its observed cadence is longer than the 10-minute Phase 7 budget, so the gate would still have failed. `.github/workflows/test-and-mark-stable.yml` now merges the base into `ai/issue-N` through the merges API before the close; on a conflict it overwrites each PR file the base also changed with the base's version, retries once, waits for GitHub to recompute mergeability, and records the outcome. A PR that still cannot be made mergeable is closed anyway and falls back to the scheduled sweep, with a workflow warning naming the cause.

| The numbers that matter | Value |
| --- | --- |
| Failing run / smoke PR | 35672590166 / #4252 |
| Gap between the conflicting merge and the close | 75 seconds |
| Observed cancel-on-close cron cadence (median, 2026-09-22) | about 12 minutes |
| `PHASE7_WAIT_BUDGET_MINUTES` | 10 (unchanged) |
| Mergeability poll ceiling per check | 90 seconds |

What this means for operators: a `main` commit that lands during the roughly 100-minute gate no longer blocks the nightly `Promote main to stable` cycle through Phase 7. The run log carries `PHASE7_UNCONFLICT_CHECK`, `PHASE7_UNCONFLICT_FILE` and `PHASE7_UNCONFLICT_RESULT` lines and the results table shows `pre-close unconflict=<outcome>`, so a future `no_run` states whether the PR was mergeable when it was closed.

### For contributors

The step only rewrites files on the throwaway smoke branch, with `[E2E Smoke Test]`-prefixed commits (never `[ai-autofix]`, which `review_autofix.yml` treats as self-triggered). API spend is one mergeability poll, one or two `/merges` calls, and on conflict one `/compare`, one `/pulls/N/files` page and up to four calls per overlapping file. `tests/test_test_and_mark_stable_phase7_unconflict.py` pins the ordering, the commit prefix, and the never-fails contract.

- **Repeated review/autofix failures on older PR branches now reach the workflow-heal intake.** The review workflow's failure path skipped its heal report with `reason=reporter_missing` whenever the PR branch predated the reporter script, so those PRs never opened a heal issue.

`review_autofix.yml` stages its support scripts with the `stage_workflow_support.sh` from the PR branch, and a branch forked before #4208 does not know `workflow_failure_heal.py` or `workflow_failure_heal_autofix_report.sh`. Run 35685250882 on PR #4259 was the second consecutive `editor_empty_noop` failure on that PR and should have been reported, but the `Report autofix failure to workflow failure heal` step found no reporter and logged `WORKFLOW_HEAL_AUTOFIX_REPORT skip reason=reporter_missing`. The `Stage workflow support files` step now backfills the pair from the main snapshot, the same way it already backfills the preflight-checked scripts, using the new `REVIEW_HEAL_REPORTER_SUPPORT_SCRIPTS` env list. A branch copy still wins when present, and a miss on both refs only logs a warning.

| The numbers that matter | Value |
| --- | --- |
| Failing run | 35685250882 on PR #4259 |
| Scripts backfilled | `workflow_failure_heal.py`, `workflow_failure_heal_autofix_report.sh` |
| Extra GitHub API calls | 0 (files come from the already checked-out main snapshot) |

What this means for operators: a PR whose AI review keeps failing now files its `ai:workflow-heal` issue after the second failed run regardless of how old its branch is; the reporter no longer depends on the PR branch carrying the script.

### For contributors

The backfill mirrors the `REVIEW_PREFLIGHT_*_SUPPORT_SCRIPTS` loop in the same step. `tests/test_workflow_failure_heal.py` pins the env list and executes the loop against a stale checkout plus a main snapshot.

- **Workflow failure heal reports now reach the intake.** Both heal reporters send the report enveloped under `client_payload.report`, and a rejected dispatch logs why.

GitHub's `repository_dispatch` API accepts at most 10 top-level `client_payload` properties, and the heal report has 20 (issue reporter) or 23 (review/autofix reporter). Every report so far was answered with HTTP 422 and ended as `WORKFLOW_HEAL_AUTOFIX_REPORT skip reason=dispatch_denied` or `WORKFLOW_HEAL_REPORT error dispatch_failed`, so `workflow-failure-heal-intake.yml` never ran from a `repository_dispatch`. `scripts/workflow_failure_heal_report.sh` and `scripts/workflow_failure_heal_autofix_report.sh` now build the body with `workflow_failure_heal.py wrap-dispatch` (`{schema_version, report}`), the intake's "Materialize the report payload" step unwraps it with `unwrap-dispatch`, and a flat payload from a reporter staged at an older release is still accepted. A failed dispatch now appends `detail=` with the first 300 characters of the API error to the existing log line.

| The numbers that matter | Value |
| --- | --- |
| `client_payload` top-level keys before / after | 20 or 23 / 2 |
| GitHub limit (`DISPATCH_CLIENT_PAYLOAD_MAX_KEYS`) | 10 |
| Rejection detail logged | first 300 characters of stderr |

What this means for operators: after the next `@stable` release, a review/autofix run that fails `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK` times in a row on one PR, and every human-needed escalation label, produce an intake run in coding-workflows. If a dispatch is still refused, the reporter's log line names the cause. No variable, secret or wrapper change is needed.

- **Release gates now ignore script paths found only in full-line comments and wait for authoritative review completion before verifying editor output.**

Both stable-release workflows now use the canonical workflow-reference checker instead of maintaining duplicate raw-text scanners. Reviewer-majority log lines remain progress telemetry and no longer allow the E2E release gate to proceed before the review workflow and editor have completed.

- **The weekly Security Audit runs again in coding-workflows itself.** From 2026-07-05 every run died at the Codex call with `Error: No such file or directory (os error 2)`, because Codex was handed a relative model-catalog path.

`.github/workflows/security-audit.yml` passed `--catalog-path "${SECURITY_AUDIT_SUPPORT_DIR:-.}/scripts/codex_model_catalog.json"`. In the source repo the support dir is unset, so `./scripts/codex_model_catalog.json` was written into `~/.codex/config.toml` as `model_catalog_json`, and Codex resolves a relative value there against `CODEX_HOME` rather than the working directory. The workflow now falls back to `${GITHUB_WORKSPACE}`, and `scripts/write_codex_config.sh` turns any relative `--catalog-path` into an absolute path under the caller's working directory before writing it, so no other caller can hit the same failure.

| The numbers that matter | Value |
| --- | --- |
| Consecutive failed runs | 13 (2026-07-05 to 2026-09-23, last one workflow_dispatch run 35821734999) |
| Introduced by | PR #3575 |
| Codex CLI version reproduced on | 0.114.0 |

What this means for operators: the scheduled `Security Audit` in this repo needs no action and will run on its next Sunday 08:00 UTC slot. Consumer repos calling the reusable workflow were not affected, since their support dir is an absolute `$RUNNER_TEMP` path.

- **Workflow failure heal reports preserve harness diagnostics.** Label-triggered reports now prioritize recent comments, compact complete orchestrator-state snapshots, and retain explicit validation run links. The poller posts harness-error detail before applying `ai:harness-broken`, preventing the reporter from racing ahead of the evidence.

- **Conflict resolution now fails closed when Git still reports unmerged paths.** Review/autofix summaries also identify unsuccessful resolver execution as `conflict_resolver_failed`.

Trusted runner staging now surfaces path-specific failures and checks the Git index before creating an `[ai-merge-resolve]` commit. This prevents marker-free modify/delete conflicts or failed staging operations from being reported as clean no-op reviews. Existing resolver isolation and retry behavior remain unchanged.

What this means for operators: resolver failures remain visible and actionable instead of being misclassified as successful clean reviews.

- **Stable-release editor recovery now adopts review work already queued for the smoke PR.** Phase 4b dispatches only when no eligible active run exists, then pins and polls one exact run ID so serialized review queue time is not multiplied by duplicate work.

### For contributors

The truncation helper `_judge_truncate_pr_diff_file` mirrors `RB_JUDGE_PR_DIFF_MAX_BYTES` in `scripts/review_rb_judge.sh` (UTF-8-safe cut via python3, `head -c` fallback). The static prefix of the judge prompt (system instructions, README, agents.md, semble prefetch) is not capped by this change; on the failing run it was about 250 KB, which is why the default shared budget leaves roughly half the cap free.

### For contributors

The merge-conflict resolver deleted `binance-blessings`'s tracked `agents.md`, which carried the production App Platform ID, even though both merge parents still had the file. The next review round's editor tried to restore it, the restore was wiped by the "editor may not create new files" cleanup in `review_commit_changes.sh`, and the run dead-ended with `DID_COMMIT=false` and `EDITOR_CHANGES_LOST=true` (AI Review run 34099352704). `review_commit_changes.sh` already carried the tracked-path guard; the other cleanup sites did not. This is the same bug class as the ~10,700-line deletion in PRs #917/#931, where the remedy was the git-remote-URL gate. That gate only protects the coding-workflows checkout itself, so the per-path guard is the second layer that covers consumer repos legitimately owning one of these names.

- **The AI review workflow no longer reports a partial-clone blob-fetch failure as a merge conflict.** `review_autofix.yml`'s `Detect merge conflicts` step now backfills blobs and retries the merge once on the promisor-fetch signature, and downgrades a surviving failure from a hard `::error::` to a `::warning::` fail-open.

The `codex-agent` checkout uses `filter: blob:none`, so the merge precheck's 3-way merge lazy-fetches blob content from the promisor remote. That batched fetch fails hard with exit 128 when any single object in the batch is unreachable server-side, even though the merge itself is well-formed. The step had no recovery for it, so a green run still showed `::error::Merge precheck failed (exit 128)` in its annotations, and `MERGE_CONFLICT=true` came from an infrastructure failure rather than a merge outcome. In the three observed runs the conflicts happened to be real — `scripts/review_conflict_prepare.sh`'s replay found 3, 8, and 6 unmerged paths on the same inputs the precheck had just failed on — so the harm was a false red annotation on successful runs plus a latent path to invoking the Codex resolver on a PR with nothing to resolve. The two sibling merge probes, the pre-review merge-topology gate in the same workflow and that same replay in `review_conflict_prepare.sh`, already carried this recovery; the late precheck was the only one left without it.

| The numbers that matter | Value |
| --- | --- |
| Merge probes with promisor recovery | 2 of 3 → 3 of 3 |
| Merge retries on the promisor signature | 1 |
| Exit-128 causes that keep the hard `::error::` | all except a surviving promisor fetch failure |
| Consumer runs observed with the signature | 3 (`tele-funtoken-msg-scoring` 31303907566, 31305600310, 31312967313 — all concluded `success`) |
| New contract tests | 7 in `tests/test_review_autofix_merge_precheck.py` |

What this means for operators: a review run whose merge precheck trips over a lazy blob fetch now recovers silently instead of printing a red annotation on a successful run, and the Codex conflict resolver stops being invoked on infrastructure noise. Genuine exit-128 causes — untracked-file collisions, a corrupt index, unrelated histories — still fail loudly with the same annotations as before.

### For contributors

`MERGE_CONFLICT=true` is deliberately kept on the fail-open path: `scripts/review_conflict_prepare.sh` runs its own merge replay with its own blob backfill and clears the flag when that replay finds nothing to resolve, so the resolver step remains the single place that decides whether a conflict is real. The retry builds its own `_merge_backfill_refspecs` array rather than reusing `_merge_base_refspecs`, which is defined only inside the shallow-clone deepen branch and would be unset under `set -u` on a full clone.

- **The editor-changes-lost re-dispatch works again.** Both branch-scoped list-runs probes in `scripts/gh_helpers.sh` now pin `-X GET`, so they stop 404ing and the automated retry is no longer reported as budget-exhausted on a head SHA that was never retried.

`autofix_retrigger_has_inflight_peer` and `autofix_changes_lost_head_retry_consumed` query `/repos/{repo}/actions/runs` with `-f branch=` and `-f per_page=`. `gh api` infers its HTTP method from its arguments: GET by default, but POST as soon as any `-f` / `-F` parameter is present and no method is given. There is no `POST /repos/{repo}/actions/runs` route, so every call 404'd, `_is_gh_permanent_failure` classified the 404 as non-retryable, and both probes returned `reason=api_error` on their first attempt. The peer probe fails open, so its breakage was silent. The budget probe fails closed, so the same 404 reported the per-head-SHA retry budget as consumed and suppressed the `Re-dispatch review on editor-changes-lost` step every single time it was reachable.

The visible effect in consumer repos was a PR that went quiet: the run finished green, posted the CRITICAL `⚠️ Editor changes lost (retry unavailable)` alert and the "retry budget is unavailable or exhausted" comment, blocked auto-merge, and then waited for the orchestrator's generic stall recovery to re-trigger the review roughly two hours later.

| The numbers that matter | Value |
| --- | --- |
| Probes fixed | 2 (`autofix_retrigger_has_inflight_peer`, `autofix_changes_lost_head_retry_consumed`) |
| Automated changes-lost retries previously dispatched | 0 of every occurrence |
| Retry attempts before the fail-closed skip | 1 (404 is non-retryable) |
| Observed stall before generic recovery | 123–137 min, up to attempt 3 (`tele-funtoken-msg-scoring` #3763/#3764, #3761/#3765) |
| Reference run | `tele-funtoken-msg-scoring` 32732281452 |
| New regression tests | 4 in `tests/test_gh_helpers_list_runs_method.py` |

What this means for operators: an editor-changes-lost iteration now re-dispatches its own review immediately instead of dead-ending on a CRITICAL alert and waiting on stall recovery, which removes the two-hour idle window and the repeated stall-recovery escalations on affected PRs. The one-retry-per-head-SHA bound is unchanged — a head that genuinely already consumed its retry still skips, and the probe still fails closed when the API is truly unreachable.

### For contributors

The pre-existing suite could not catch this: its `gh` stub ignores the arguments it is passed, so the method the helper actually requests was never asserted. The new tests add a stub that reproduces gh's method inference, plus a static assertion that both call sites pin GET, so removing the flag fails the suite. `review_autofix_sweep.yml` already used `-X GET` on the same endpoint family; these two helpers were the only REST GETs in `gh_helpers.sh` passing `-f` without a pinned method.

- **The review editor's changelog fragments now survive to commit.** The consumer-repo new-file cleanup in `scripts/review_commit_changes.sh` exempts top-level `changelog.d/*.md` fragments, so a review round whose fix is creating the required fragment commits normally instead of dead-ending on a false "Editor changes lost" alert.

Reviewers flag a missing changelog fragment per the §20 convention, and the editor's usual fix is creating one new file under `changelog.d/`. The commit step's consumer-repo cleanup deleted every editor-created untracked file before staging ("editor may not create new files"), which erased the fragment, left the tree with nothing to commit, and fired `EDITOR_CHANGES_LOST` — blocking auto-merge on a round that had actually produced the requested fix. The failure was deterministic: a retried round recreated the fragment and hit the same deletion. Observed on `tele-funtoken-msg-scoring` PR #3764 (review run 32732281452), where the editor's only claimed change was `changelog.d/3763-uniswap-comp-admin-stats-aggregator.md` and the run's own log shows the cleanup removing that exact path two steps before the changes-lost error.

| The numbers that matter | Value |
| --- | --- |
| Paths exempted | top-level `changelog.d/*.md` fragments |
| Other new-file cleanup behaviour | unchanged (strays still removed, incl. non-`.md` files and nested Markdown under `changelog.d/`) |
| Reference run | `tele-funtoken-msg-scoring` 32732281452 |
| New tests | 5 in `tests/test_review_commit_changelog_fragment_preserved.py` |

What this means for operators: fragment-only review rounds in consumer repos now commit and push like any other fix, so the "Editor changes lost (retry unavailable)" alert no longer fires for this case and auto-merge is not blocked on it. The workflow-source repo path is untouched — it already preserved editor-created files.

### For contributors

The exemption sits in the existing removal-loop `case` statement beside `.serena` / `scripts/` / `prompts/`, and the surviving fragment is staged by the existing consumer untracked-files `git add` pass — no new staging logic. This pairs with the `-X GET` probe fix (fragment `3763-changes-lost-redispatch-get-method.md`): that PR repaired the retry after a changes-lost event; this one removes the pipeline-inflicted cause of the event for fragment-only rounds.

- **Release smoke tests no longer fail on healthy long reviewer steps.** The v1.27.0 release run failed even though its review pipeline was working correctly; the wait-review gate now waits long enough and watches the right job.

On the v1.27.0 release (run 32824674139), `e2e-smoke-test` declared "Review phase stalled — no activity for 40 minutes" 58 seconds before the review run's `Run reviewer models` step completed successfully. Three defects lined up: `promote-main-to-stable.yml` still forwarded `review_timeout=40` after the downstream default had been raised to 60, the per-step stall cap of 50 minutes sat below the 51-minute healthy reviewer-step duration actually observed, and the wait loop's live-log probes read the wrapper run's 4-second `review / gate` job instead of `review / codex-agent`, so its log-based activity signals and early-exit shortcuts never worked. The promote dispatcher default now tracks the downstream 60, the per-step cap default is 75 minutes, and the log probes select the codex-agent job by name with an in-progress-job and `jobs[0]` fallback.

| The numbers that matter | Value |
| --- | --- |
| `promote-main-to-stable.yml` `review_timeout` default | 40 → 60 minutes |
| `test-and-mark-stable.yml` `review_step_timeout` default | 50 → 75 minutes |
| Observed healthy `Run reviewer models` durations | 41m (v1.20.1), 51m (v1.27.0) |
| Margin by which the v1.27.0 gate gave up too early | 58 seconds |

What this means for operators: a slow-but-healthy review run no longer kills a release; re-dispatching `promote-main-to-stable.yml` without overriding `review_timeout` now uses the intended 60-minute inactivity window. When `test-and-mark-stable.yml`'s `review_timeout` default changes again, `promote-main-to-stable.yml`'s default must change with it — its description now says so explicitly.

### For contributors

The wait loop's editor-noop and reviewer-majority shortcuts were inert in every wrapper (`internal-review.yml`) run because `.jobs[0]` is the gate job there; with the codex-agent job selected they can fire again, letting Phase 4 exit success shortly after the reviewer step ends instead of waiting out the editor.

- **Implementation guard-rejection handling now stays below the GitHub Actions step-size limit.** The destructive-delete and scope-block rejection shell has been moved into `scripts/implement_handle_guard_block.sh`, while `implement.yml` keeps the same trigger conditions and environment bindings.

The helper is staged into the runtime directory before fetched-support cleanup, so late guard failures in source and consumer repositories can still latch `ai:destructive-blocked` or `ai:scope-blocked`, post the same issue comments, and send the same direct Telegram fallback alerts after repository support files are removed.

Focused recovery tests now inspect and execute the helper directly, including the cleanup-survival path and the reduced workflow step body size.

- **Direct OpenRouter callers now report usage for every parsed response with choices.** The workflow-log summarizer and release-gate soft-error analyzer emit normalized prompt, completion, total, and cache token telemetry even when response content is empty, while preserving their existing return and exception behavior and avoiding prompt, response, or credential content in logs.

- **Workflow cost reports no longer count malformed MCP log text as runtime traffic.** Structured Semble, Serena, and generic MCP events continue to populate the same telemetry fields.

`scripts/cost_audit.py` now requires each MCP event to carry the fields emitted by its canonical helper before including it in per-run or aggregate totals. `scripts/collect_workflow_logs.py` applies the same validation before retaining structured telemetry lines from full logs, so echoed markers are not reintroduced during wrapper/child deduplication. Counter echoes such as `SERENA_QUERY 0`, prose that merely mentions an event prefix, and partial query, fallback, or probe lines are ignored instead of inflating usage. Valid Semble fallback events retain their existing contract-test versus runtime classification, and malformed telemetry remains fail-open for the workflow.

What this means for operators: workflow analysis reports may show lower, more accurate MCP query, fallback, and probe totals when captured logs contain echoed counters or malformed event text; no emitter format or telemetry key changes.

### For contributors

Treat the existing Semble, Serena, and generic MCP emitter fields as the parser contract; add focused tests before relaxing required telemetry fields.

- **Stall recovery no longer clobbers healthy internal review runs, and issues can no longer be auto-closed by a merged PR that merely mentions them.** Two orchestrator-poller guards that existed for exactly these incidents were structurally unable to fire; both are now effective.

On 2026-08-25 the poller re-triggered review for PR #3823 (issue #3816) while its original review run was 137 minutes into a healthy reviewer pass, pushing an empty commit that tripped the stale-base gate and discarded the whole pass. The in-flight review guard's authoritative fallback, `_direct_inflight_review_run_on_branch`, matched workflow names against `AI Review` / `Internal Review` / `Review Autofix` only — but this repo's review workflows are named `Internal: AI Review & Autofix` (`internal-review.yml`) and `Codex PR Self-Healing Semantic Agent` (`review_autofix.yml`), so the guard could never match an upstream review run. The two real names are now recognized by every review-family name matcher in `scripts/orchestrate_poll_process.sh`. The same day, issue #3817 was auto-closed as "merged" although its scope was never implemented: the merged PR #3825 referenced it with a non-closing `Refs #3817`, and the poller's cross-reference linkage adopted that PR as the issue's implementation PR. Label reconciliation, validation fix-up merged-evidence backfill, and `close_merged_issues_sweep` now verify candidates through the new `_pr_json_is_issue_implementation_pr` helper — head branch `ai/issue-<n>` or a closing-keyword body reference — before forcing `ai:merged` or closing the issue.

| The numbers that matter | Value |
| --- | --- |
| Review-family name matchers extended | 5 sites in `scripts/orchestrate_poll_process.sh` |
| Workflow names added | `Internal: AI Review & Autofix`, `Codex PR Self-Healing Semantic Agent` |
| Merged-state adoption gates added | label reconciliation + validation fix-up backfill + `close_merged_issues_sweep` |
| Extra API cost | Up to 1 `pulls/<n>` fetch per cross-referenced candidate on each protected path |
| Incidents | PR #3823 / issue #3816 (discarded review pass), issue #3817 / PR #3825 (issue closed unimplemented) |

Two follow-on defects from the same 2026-08-25 incident chain are fixed in this PR as well. First, stall recovery itself now targets only the issue's own implementation PR: poller run 32891613081 resolved issue #3816's "linked PR" to the unrelated investigation PR #3828 (whose body said `Refs #3816`), pushed its recovery empty commit onto that PR's branch, and the wrong PR's review pipeline then labeled issues `ai:review-blocked`. `retrigger_review` (managed and standalone), `dispatch_rb_judge` (managed and standalone), and the standalone `attempt_merge` now resolve their target through the new `_resolve_issue_implementation_pr` helper (open `ai/issue-<n>` PR first, then verified cross-references) and skip the destructive push, judge dispatch, or merge when no implementation PR resolves. Second, the review pipeline's "Detect merge conflicts" step in run 32891957030 hard-failed on `! [rejected] main -> origin/main (non-fast-forward)`: its explicit fetch refspec lacked the `+` force prefix that git's default remote-tracking refspec carries, so a stale or divergent local `origin/<base>` ref killed the step (and mislabeled linked issues) instead of being refreshed. Every explicit `refs/heads/X:refs/remotes/origin/X` fetch refspec in `review_autofix.yml`, `implement.yml`, `orchestrate_poll_process.sh`, `review_conflict_prepare.sh`, and `check_external_branch_advance.sh` now carries the force prefix.

What this means for operators: a `Stall recovery: re-triggered review` warning should no longer appear while a review run is visibly in progress on the PR's branch, and an `ai:orchestrator-managed` issue can only be closed by a PR that actually implements it. A merged PR that merely mentions an issue now shows up as `rejected=not_implementation_pr` in the poll log and, for `ai:merged`-labeled issues with no verifiable implementation PR, falls through to the existing stale-label Telegram alert instead of a silent close. Transient PR lookup failures are logged separately as `rejected=pr_fetch_failed` and retry on the next poll cycle instead of masquerading as genuine implementation-PR rejections.

### For contributors

New helper `_pr_json_is_issue_implementation_pr` (rejects on unverifiable input, since adopting merged state is the destructive act), new structured log keys `LINKED_PR_CROSS_REF_REJECTED` and `CLOSE_MERGED_SWEEP … rejected=not_implementation_pr`, and new contract tests in `tests/test_linked_pr_implementation_guard.py` plus regression cases in `tests/test_retrigger_inflight_direct_fallback.py` and `tests/test_orchestrate_poll_process.py`.

- **AI-memory pushes now survive orchestrator dispatch bursts: the default push-retry budget doubled from 8 to 16 attempts.**

Plan run 32849764877 failed before posting an implementation plan because the fail-closed `/answer` claim could not push to the shared `ai-memory` branch: all 8 attempts lost the ref-lock race against a dispatch burst that landed a foreign commit on the ref every 3-5 seconds for about 2 minutes. The 8-attempt loop lasts about 80 seconds, so it exhausted mid-burst and aborted the whole plan phase with `Failed to push memory branch after 8 attempts`. The default `AI_MEMORY_PUSH_RETRIES` is now 16 in `scripts/ai_memory.py`, the four shell callers that embed the same fallback (`review_rb_judge.sh`, `orchestrate_poll_process.sh`, `review_consolidate.sh`, `review_apply_fixes.sh`), and the workflow-log-analysis cache writer in `scripts/collect_workflow_logs.py`, stretching the jittered retry loop to roughly 3 minutes so it outlasts a burst of that shape. Explicit `AI_MEMORY_PUSH_RETRIES` overrides, the 8-second jitter cap, and the fail-closed claim semantics are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Default `AI_MEMORY_PUSH_RETRIES` | 8 → 16 |
| Observed burst push interval on `ai-memory` | every 3-5 s for ~2 min |
| Old retry-loop duration (max) | ~80 s |
| New retry-loop duration (mean/max) | ~3 min / ~3.7 min |

What this means for operators: a plan, clarify, or implement run dispatched inside a busy orchestrator wave no longer hard-fails its claim step just because sibling runs were writing run events to `ai-memory` at the same time; set `AI_MEMORY_PUSH_RETRIES` explicitly to restore the previous budget.

### For contributors

`tests/test_ai_memory_push_retry_backoff.py` gains a 15-rejection/16-budget regression test, and `tests/test_ai_memory_processed_command_entry.py` now pins the default at >= 16. The full-jitter backoff cap stays at 8 s because attempt frequency, not sleep length, wins ref-lock races; the larger budget only extends how long a contended claim keeps trying before giving up.

- **A successful AI implementation run is no longer reported as a failure when the shared `ai-memory` branch loses a write race.** `memory_finalize_task` now fails open like the other post-PR bookkeeping helpers, and a memory-branch rebase conflict now names the files it collided on.

`implement.yml` calls `memory_finalize_task` only after the branch is pushed and the pull request is open, under `set -euo pipefail`. The `ai-memory` branch is a single ref that `implement`, `issue_pr_status` and `orchestrate_poll` all push to concurrently, so a losing writer can hit an add/add rebase conflict on the same `tasks/issue-<n>/lineage/task_lineage.v1.json` file. That conflict exited 2 and marked the whole run `failure`, which posted an "AI implementation workflow failed" comment on the issue, fired a Telegram alert, and recorded a `phase_failed` event in the memory ledger for a run that had actually succeeded. Every sibling post-PR helper — `memory_record_run_event`, `memory_record_candidate`, `memory_processed_command_complete` — already fails open; `memory_finalize_task` was the outlier. It now warns, emits `"fail_open":true` telemetry, and returns 0.

| The numbers that matter | Value |
| --- | --- |
| Observed incident | `tele-funtoken-msg-scoring` run 33231997918, issue #3833 (PR #3837 created, then merged) |
| Gap between the two writers | 38s (finalize commit 04:04:18, competing push 04:04:56) |
| Post-PR bookkeeping helpers that fail open | 3 of 4 → 4 of 4 |
| New regression tests | 5 |

What this means for operators: a run that pushed its branch and opened its PR now finishes green even when its lineage bookkeeping loses the race, so the failure comment, the Telegram alert, and the false `phase_failed` ledger entry stop firing on successful work. Lineage state is unaffected in practice — the writer that wins the race is the one holding the newer state.

### For contributors

`memory_processed_command_claim` is deliberately left strict: it is a mutual-exclusion gate whose failure must stop the caller, unlike the bookkeeping helpers around it. `persist_memory_operation` still treats a rebase conflict as terminal rather than re-running the operation onto the fresh head; for this call site that is the correct outcome, since re-running would rewrite a `merged` lineage state back to a stale `in_progress`. The conflict message now appends `rebase stdout`, where git writes `CONFLICT (add/add): Merge conflict in <path>` — stderr carries only `error: could not apply <sha>` plus generic hints, which is what made the original incident undiagnosable from the run log alone.

- Security-audit prompt and Codex failures now report sanitized phase, working-directory, and required-path context while preserving nonzero exit statuses and keeping credentials and prompt content out of diagnostics.

- **The review autofix sweep no longer deadlocks behind a workflow run GitHub has wedged.** `review_autofix_sweep.yml` now stops treating a `queued` run as active once it passes `SWEEP_STALE_QUEUED_MINUTES` (default 120), so a PR whose review run never started gets picked up on the next 30-minute tick instead of stalling indefinitely.

The sweep skips any PR that already has a `queued` or `in_progress` review run on its head ref, which stops a tick from stomping a synchronize-fired run mid-edit. That check had no time cutoff, and GitHub can leave a run wedged in `queued` with zero jobs while rejecting both `cancel` (409 `Cannot cancel a workflow run that has not been queued yet`) and `rerun` (403 `This workflow is already running`). Nothing could clear such a run, and because the sweep is the only recovery path for the PR behind it, the guard deadlocked the mechanism it protects. On `shubhodeep1/coding-workflows#3841` this stalled the review-blocked judge for over 11 hours while every tick logged `AUTOFIX_SWEEP_SKIP pr=#3841 reason=active_run`. `in_progress` runs are still never discounted, since the codex-agent job legitimately runs well over an hour.

| The numbers that matter | Value |
| --- | --- |
| New repo var | `SWEEP_STALE_QUEUED_MINUTES` (default `120`) |
| Longest observed legitimate concurrency wait | ~94 minutes |
| Sweep cadence | every 30 minutes |
| Time PR #3841 stalled behind the wedged run | 11+ hours |
| New tests | 11 in `tests/test_review_autofix_sweep_stale_queued.py` |

What this means for operators: a review run that GitHub fails to start no longer strands its pull request. The sweep reports each discounted run as an `AUTOFIX_SWEEP_STALE_QUEUED` warning naming the workflow, head ref, and run id, so the wedged run is visible in the tick log rather than silently ignored. Setting `SWEEP_STALE_QUEUED_MINUTES=0` restores the previous always-suppress behaviour.

### For contributors

The cutoff is computed in bash and passed to jq as `--argjson cutoff`, so the reduce stays a pure function of its input and never calls `now`. A run whose `created_at` is missing or unparseable still counts as active, which fails toward the old behaviour rather than toward a duplicate dispatch. `tests/test_review_autofix_sweep_stale_queued.py` extracts the jq program from the workflow file itself and executes it, so an edit that drops the cutoff cannot pass against a stale copy of the reduce.

- **CI finishes again.** The `CI / lint` job was being cancelled at its 30-minute timeout on every run, including on `main`, so the repository had no completing full-test gate. The orchestrate-poll module now runs across four parallel shards and the job budget is 45 minutes.

`tests/test_orchestrate_poll_process.py` is the critical path. Most of the 307 tests in its post-fast-fail sharded subset spawn the real poller as a bash subprocess inside a throwaway sandbox, so they cost seconds each rather than milliseconds — 71 of the first 95 took over 4 seconds, the slowest 27. Run sequentially the module took roughly 35 minutes on a 4-core box, more than the entire job was allowed, so CI never reached the steps after it and every run ended `cancelled`. Each test allocates its own tempdir sandbox, so the module shards with no shared state and no harness change: the runner already accepts test names as arguments.

| The numbers that matter | Value |
| --- | --- |
| New repo var | `CI_POLL_TEST_SHARDS` (default `4`) |
| Job timeout | 30 → 45 minutes |
| Measured speedup on 4 cores | 3.6x (62s → 17s on a 16-test slice) |
| Tests in the sharded CI subset | 307 |
| New test methods | 12 in `tests/test_ci_poll_test_sharding.py` |

What this means for operators: CI reaches its final steps and reports a real result instead of being cut off mid-suite, so a green tick again means the suite passed. Set `CI_POLL_TEST_SHARDS=1` to fall back to sequential; a non-numeric or non-positive value warns and does the same.

### For contributors

The split is `NR % total == n`, and `tests/test_ci_poll_test_sharding.py` extracts that expression from the workflow rather than restating it, then proves it is a true partition across shard counts 1, 2, 3, 4, 5, and 8 and against the module's live test count. A copy of the expression in the test would have agreed with the step only at authoring time; extracting it means a change to the split is exercised. Shards are all waited on before any is judged, so one early failure cannot orphan its siblings on the runner, and a shard whose exit code was never recorded is treated as failed rather than passing silently.

- **The OpenCode live smoke now proves reasoning delivery for adaptive and detail-less models instead of false-failing them.** `opencode-live-smoke.yml` uses a reasoning-demanding probe prompt and accepts provider reasoning text as fallback evidence when the endpoint omits reasoning token counts.

The first all-slug run of the Phase 1 rollout gate (run 33087059507) failed two of eight slots with `no_reasoning_usage` even though both models reason correctly in production. Direct probes against OpenRouter showed two distinct causes: `openai/gpt-5.6-sol` treats reasoning effort as a ceiling and performs zero reasoning on the old trivial `Return exactly OK` prompt on every endpoint and parameter shape, and `deepseek/deepseek-v4-pro` reasons on every call but OpenRouter's `chat/completions` usage omits `completion_tokens_details`, so its count can never exceed zero. The smoke's probe prompt now embeds a small verification task that adaptive models actually reason about, and a zero token count falls back to one non-streaming `chat/completions` probe that accepts non-empty reasoning text for the same model, prompt, and `xhigh` effort. The per-slot table reports the fallback path as `PASS(text)`, and a slot with neither token usage nor reasoning text still fails as `no_reasoning_usage`.

| The numbers that matter | Value |
| --- | --- |
| Slots false-failing before this fix | 2 of 8 (`deepseek/deepseek-v4-pro`, `openai/gpt-5.6-sol`) |
| Extra API calls per fallback | 1 non-streaming `chat/completions` probe |
| Files changed | `.github/workflows/opencode-live-smoke.yml`, `tests/test_opencode_live_smoke_workflow.py` |

What this means for operators: dispatching `opencode-live-smoke.yml` without a model filter can now genuinely go all-green, which is the recorded evidence the opencode cutover's read-side and write-side phases are gated on.

### For contributors

`scripts/write_opencode_config.sh` is unchanged: wire captures confirmed opencode 1.18.23 delivers the configured `reasoning: {effort}` variant to OpenRouter exactly as written, so the P1 configuration writer was never the defect.

- **Release runs no longer fail by timeout while every test is passing.** The `validate-scripts` job in both release gates now shards the orchestrate-poll test module instead of running it serially.

Release v1.27.0 (run 33073743283) failed with zero test failures: the serial unit-test step in `test-and-mark-stable.yml` hit the job's 30-minute cap and was cancelled mid-suite, which skipped `validate` and `release`. The cause was the one PR #3844 had already fixed in `ci.yml` — `tests/test_orchestrate_poll_process.py` spawns the real poller as a subprocess per test and alone consumed ~24 of the 30 budgeted minutes — but the release gates in `mark-stable.yml` and `test-and-mark-stable.yml` were never sharded. Both now run the module across `CI_POLL_TEST_SHARDS` workers (default 4, the same repository variable ci.yml uses) with the same verified `NR % total == n` partition, and their job budgets rise from 30 to 45 minutes for cold-runner headroom.

| The numbers that matter | Value |
| --- | --- |
| Failed release run | 33073743283 (v1.27.0) |
| Poll-module share of the 30-minute budget | ~24 minutes serial |
| Shard count | `CI_POLL_TEST_SHARDS`, default 4 |
| `validate-scripts` budget | 30 → 45 minutes |

What this means for operators: a release run is cancelled only if something is genuinely wrong, not because the test suite grew; a red `validate-scripts` again means a failing test. Setting `CI_POLL_TEST_SHARDS` now affects the release gates as well as `CI / lint`.

### For contributors

`tests/test_ci_poll_test_sharding.py` pins the ported step in both release workflows (partition expression, failure handling, reap-before-judge ordering, serial-invocation removal, 45-minute budget), so the three copies of the shard split cannot silently diverge.

- **Convergence runs of `review_autofix.yml` no longer trip a false-positive editor no-op block.** The editor prompt now states the audit arithmetic invariant the no-op validator enforces.

When every reviewer finding was already fixed on HEAD, the editor could record "confirmed a prior fix is already present" as `issues already applied 1` against `total issues listed 0` in its summary's "Review file issue audit" section. `scripts/validate_editor_audit.sh` correctly flagged the imbalance, set `EDITOR_NOOP_SUSPICIOUS=true`, skipped the "Enable auto-merge on PR" step, and paged the operator, even though the run was a genuine, healthy convergence. This happened on tele-funtoken-msg-scoring PR 3809 (run 33088357425). The editor prompt in `scripts/review_apply_fixes.sh` now spells out that `total issues listed == issues applied + issues already applied + issues ignored` must hold on every audit bullet, and that a review file listing zero new issues gets all four counts emitted as 0, with prior-fix confirmations narrated under "Already satisfied (suggested but already present):" instead.

| The numbers that matter | Value |
| --- | --- |
| Prompt file changed | `scripts/review_apply_fixes.sh` |
| Validator (unchanged, strict on purpose) | `scripts/validate_editor_audit.sh` |
| Incident run | tele-funtoken-msg-scoring Actions run 33088357425 |
| New tests | 2 (`test_editor_prompt_states_audit_arithmetic_invariant`, `test_convergence_shape_total_zero_already_applied_one_is_mismatch`) |

What this means for operators: a review run whose editor honestly concludes "no changes needed" now reaches auto-merge instead of ending in an "Editor no-op suspicious" Telegram warning that asks for a manual re-run. The validator itself is unchanged, so a summary whose counts genuinely do not add up still blocks auto-merge.

### For contributors

The fix is prompt-side only. The validator's arithmetic stays strict because it is what keeps `total issues listed` trustworthy as an auto-merge gate; the new validator test pins the observed false-positive shape (`total 0, already applied 1`) as a mismatch on purpose, and the new cascade-contract test keeps the prompt sentence and the validator in lockstep.

- **python-repo-checks validation no longer aborts as `harness_error` when `.ai/validate.yml` uses a command-style `entry`.** The generated container healthcheck now accepts script paths and commands such as `sh scripts/run_validation_repo_checks.sh`, `bash -lc "scripts/run_validation_repo_checks.sh"`, or `python -m pytest`.

Consumer repos whose `.ai/validate.yml` selects `type: python-repo-checks` with a command-style `entry` previously failed every AI Validate run before their repo checks executed. The template `workflow-templates/validation-harness/python-repo-checks/docker-compose.test.yml.j2` interpolated the entry into `test -f /workspace/<entry>`, so `entry: sh scripts/run_validation_repo_checks.sh` rendered an invalid file test (`test: /workspace/sh: unexpected operator`), the app container stayed unhealthy, and validation reported `harness_error`. The healthcheck now first verifies `/workspace`, then checks each whitespace-separated entry token under `/workspace`; a path-like token must resolve to an existing file or directory, while entries without path-like tokens pass when their command target is on `PATH`. Command execution remains in the existing repo-check test. First observed on shubhodeep1/drhyg_ecommerce_automation run 33128774884.

| The numbers that matter | Value |
| --- | --- |
| Template fixed | `workflow-templates/validation-harness/python-repo-checks/docker-compose.test.yml.j2` |
| Failing consumer run | shubhodeep1/drhyg_ecommerce_automation Actions run 33128774884 |
| Pull request | #3862 |

What this means for consumer repos: after the next `@stable` sync regenerates `validation/docker-compose.test.yml`, python-repo-checks validation with a command-style entry proceeds to the actual repo checks instead of dying unhealthy at container startup. No consumer-side change is required; existing path-style entries behave exactly as before.

### For contributors

Regression coverage lives in `tests/test_family_python_repo_checks.py`, which simulates the rendered healthcheck the way docker runs it for path, unquoted-command, quoted-command, directory-argument, and command-only entries. The golden fixture `tests/fixtures/validation_harness/python_repo_checks/docker-compose.test.yml` was regenerated. The fix landed on `stable`; `forward-merge-stable-to-main.yml` propagates it to `main` automatically on merge.

- **`review_autofix.yml` now installs the OpenCode CLI and warms its models.dev cache before the review pipeline runs.** This fixes every PR targeting `orchestrator/project-3845` failing review with "models.dev cache is not readable".

PRs that target the opencode-cutover integration branch run the reusable workflow pinned at `review_autofix.yml@main`, but stage their support scripts from the PR's own merge ref. Since PR #3864 landed the read-side opencode cutover on that branch, `review_run_reviewers.sh` generates a per-reviewer OpenCode config via `write_opencode_config.sh`, which hard-fails when `~/.cache/opencode/models.json` is missing. Main's workflow never installed opencode, so every reviewer, summariser, and editor slot failed at config generation and the run finished as `editor_empty_noop` (first seen on the run for PR #3867). The workflow now runs the pinned `install-opencode` composite action (version `1.18.23`, overridable via the `OPENCODE_VERSION` repo var) right after the Codex CLI install, while allowing setup failures to continue for staged scripts that remain Codex-backed.

What this means for operators: review runs on integration-branch PRs succeed again without any manual action. Main-target PRs still use Codex for reviewer and editor execution, but their review jobs now also perform the OpenCode install and cache warm, adding setup time and a network-dependent preflight. That setup step fails open so a transient OpenCode or models.dev failure does not abort Codex-only runs; OpenCode-backed branch scripts still fail with their required-cache error when the cache is unavailable.

### For contributors

The `test_production_review_path_remains_opencode_free` guard in `tests/test_opencode_live_smoke_workflow.py` now permits exactly the install-step strings and still rejects any other opencode appearance in `review_autofix.yml`.

- **OpenCode bootstrap failures now emit classified alerts.** Review preflight, reviewer startup, and consensus summarisation report stable `opencode_agent_failure` Telegram errors before preserving the existing fail-closed workflow handling.

- **Review-autofix now records measured OpenCode token and cache usage without confusing missing telemetry with zero.** Reviewer text output and recovery behavior remain unchanged.

Production reviewer calls now retain OpenCode JSON events long enough to reconstruct reviewer text and aggregate `step_finish` token evidence. Existing usage markers gain an availability signal, run summaries distinguish measured zero cache reads from unavailable evidence, and cost reports show unavailable cache telemetry as `N/A` rather than calculating a misleading hit rate.

The cutover plan now records the auditable three-run Codex latency baseline and keeps the pre-P2 cache-read baseline explicitly unavailable. `@stable` remains held until three consecutive post-cutover production runs satisfy the amended parity criterion.

- **The nightly drift audit no longer fires a daily partial-coverage WARNING for cancelled review runs.** Absent logs on completed runs concluded `cancelled` or `skipped` are now classified as expected instead of missing.

The daily `.github/workflows/drift-audit.yml` run scans the last 24 hours of `internal-review.yml` and `review_autofix.yml` logs, and almost every day some of those runs were concurrency-cancelled by a newer push before uploading any logs. `scripts/drift_audit.sh` counted each one as missing coverage, flipped the run to `partial`, and escalated the per-run Telegram summary to WARNING — for example the 2026-08-29 run reported "fetched 62, missing 11" with all 11 missing IDs being cancelled runs. The audit now records those runs as `unscannable` (a new `DRIFT_AUDIT_COVERAGE` field, run-summary key, and "Cancelled/skipped runs without logs" job-summary row), keeps coverage `full`, and the alert stays at DEBUG. A failed log fetch for any other conclusion still reports `partial` coverage and a WARNING alert, and cancelled or skipped runs whose logs do exist are still scanned for drift markers.

| The numbers that matter | Value |
| --- | --- |
| Missing-log runs in the 2026-08-29 audit, all cancelled | 11 of 73 scanned |
| Conclusions treated as expected-no-logs | `cancelled`, `skipped` |
| Alert level for a cancelled-only gap (was WARNING) | DEBUG |

What this means for operators: the ⚠️ "Partial log coverage" Telegram alert now only fires when a log that should exist could not be fetched, so a WARNING from the drift audit is worth reading again instead of daily noise.

### For contributors

The run-summary JSON gains `logs_unscannable` / `unscannable_run_ids`, appended after the existing fields; the shell wrapper's jq TSV appends the new count last so existing field positions are unchanged. `_fetch_run_log` takes a `log_expected` keyword argument that downgrades the fetch-failure annotation from `::warning::` to an info line for cancelled/skipped runs.

- **AI Plan no longer rejects plans that change a consumer repo's own `scripts/` files.** Only runtime-fetched script paths are blocked now, so validation fix-up projects can proceed instead of failing before the plan is posted.

The `Guard plans targeting template-owned paths` step in `plan.yml` rejected every path under `scripts/`, on the stale premise that `implement.yml` excludes that whole directory from Codex commits. `implement.yml` stopped doing that: it builds per-file exclusions for runtime-fetched helpers so consumer repos can commit their own `scripts/` changes. The mismatch deadlocked any project touching `scripts/run_validation_repo_checks.sh`, the validation entry that `validation_template_bootstrap.py` seeds into consumer repos and `.ai/validate.yml` points at, so a repo could never plan a fix for a file this library told it to own. The guard now derives the implementation script paths from the staged `implement.yml`, unions them with the plan job's fetched helpers, and falls back to the old blanket rejection if either source is unavailable or unparseable.

| The numbers that matter | Value |
| --- | --- |
| Workflow changed | `.github/workflows/plan.yml` |
| Still blanket-rejected | `prompts/`, `ai-memory/`, `.github/prompts/`, `.github/scripts/` |
| New test file | `tests/test_plan_template_owned_path_guard.py` |
| Guard tests added | 57 |

What this means for operators: a plan that lists a consumer-owned file under `scripts/` now proceeds to implementation instead of failing the AI Plan run with a template-owned-path error. Nothing the guard previously caught is let through, so plans targeting fetched helpers such as `scripts/render_prompt.sh` are still rejected with the same routing back to this repository.

### For contributors

The guard parses the staged implementation workflow's helper-staging block, including literal `scripts/...` entries written to `FETCHED_MANIFEST`, rather than carrying another copy of that list, then unions those names with the plan job's own `scripts/.gitignore` entries. It mirrors the implementation workflow's ownership exception for a consumer-tracked Serena template while still rejecting an untracked runtime copy, and preserves its blanket exclusions for `prompts/`, `ai-memory/`, `.github/prompts/`, and `.github/scripts/`. `EXCLUDED_RE` is kept as the pre-filter before path extraction; trailing sentence punctuation, redundant path separators, and lexical path segments are normalized before directory classification and exact helper matching. The guard had no test coverage before this change; the consumer-owned and implement-only-helper regression cases fail against the previous body.

- **Reviewer runs no longer fail when a PR diff mentions a nonexistent `REFERENCE_*` placeholder.** `render_prompt.py` now fails open on unresolvable reference placeholders when rendering untrusted assembled prompt bodies.

Run 33245886964 killed all reviewers in the `Run reviewer models` step because the reviewed PR was a plan doc containing a literal braced `REFERENCE_SECURITY_MONEY_LENS` token for a reference file that does not exist yet. The reviewer prompt body embeds the raw PR diff, and reference-placeholder hydration scanned that untrusted content strictly, hard-failing the render on the missing `prompts/references/security-money-lens.txt`. On the `--input-already-assembled --skip-syntax-validation` untrusted assembled-body path the renderer now leaves such tokens unhydrated so they render verbatim, emits a stderr warning, and still hydrates resolvable references like the reviewer checklist's severity-classification block. Trusted template renders keep the strict hard failure, so real prompt-authoring mistakes still fail loudly. This closes the third gap in the same seam, after include-assembly (run 29182737982) and the template-syntax gate (run 28936678508).

| The numbers that matter | Value |
| --- | --- |
| Failing run | 33245886964 |
| Renderer path affected | `--input-already-assembled --skip-syntax-validation` |
| Behaviour on missing reference (untrusted body) | warn + render token verbatim |
| Behaviour on missing reference (trusted template) | unchanged hard failure |

What this means for operators: a docs-only or plan PR that merely mentions a future `REFERENCE_*` placeholder no longer takes down its own review_autofix run; the token passes through to the reviewer prompt as plain text.

### For contributors

`hydrate_reference_placeholders()` gained a `strict` keyword (default `True`); `main()` enables leniency only when both `--input-already-assembled` and `--skip-syntax-validation` identify an untrusted assembled body. Regression test: `test_render_prompt_py_fails_open_on_missing_reference_in_untrusted_assembled_body`.

- **Duplicate "merge conflicts. Review workflow dispatched for resolution." Telegram warnings no longer repeat every poll cycle while a conflict resolver is already running.**

During the PR #3895 forward-merge conflict (2026-08-29), operators received 6+ identical conflict warnings and GitHub accumulated 10 cancelled duplicate internal-review dispatches over ~95 minutes, while the one real resolver run completed successfully. Two blind spots caused it: duplicate dispatches held back by the review_autofix concurrency group report status `pending`, which neither the orchestrator poller's `_has_active_autofix_run` guard nor `review_autofix_sweep.yml`'s snapshot counted as active, and `forward-merge-stable-to-main.yml` plus the sweep dispatched `internal-review.yml` without `--ref`, keying those runs to the default branch where the head-branch-keyed guards could not see them. Both guards now count `pending` runs as active, and both dispatchers now dispatch on the PR's head branch (the sweep falls back to the default branch for fork PRs, whose head ref does not exist in the repo).

| The numbers that matter | Value |
| --- | --- |
| Duplicate dispatches during the incident | 10 |
| Duplicate Telegram conflict warnings | 6+ |
| Real resolver runtime (run 33273396616) | ~102 minutes |
| Expected duplicate warnings after the fix | 0 |

What this means for operators: a forward-merge or standalone PR conflict now produces one alert when the fallback PR opens and one resolver run, instead of a stream of repeated warnings and cancelled runs while the resolver works.

### For contributors

The guard/status contracts are pinned by `tests/test_conflict_dispatch_active_run_visibility.py` as text contracts on the shipped files. A `pending` run is never treated as stale by the sweep's wedged-run cutoff — it is bounded by its running peer's 240-minute job timeout.

- **The review_autofix conflict resolver no longer dies with `helpers_missing` on consumer repos whose `stable` staging list predates the opencode cutover.** Conflicted PRs stopped looping on "PR autofix failed".

`scripts/review_conflict_resolve.sh` is staged main-primary, but the staging list that builds the runtime bundle runs from the consumer's `SCRIPT_REF`. When that ref's `scripts/stage_workflow_support.sh` predates the opencode cutover (#3848), the bundle never contains `opencode_helpers.sh` or `write_opencode_config.sh`, so the resolver exited 1 immediately with `failure_class=helpers_missing`, the editor's committed fixes were never pushed, and every conflicted PR failed with `finalize_reason=push_failed`. The resolver now falls back to the trusted on-disk support checkouts (`.codex-workflow-src-main` first, then `.codex-workflow-src`) for each missing dependency and logs a `::warning::` naming the fallback path. The workflow source repository may additionally use its own canonical `scripts/` directory; consumer repositories fail closed rather than source their PR-modifiable workspace. Both helpers are also added to `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` so future stable refs stage them from the same snapshot as the resolver.

| The numbers that matter | Value |
| --- | --- |
| Failing runs diagnosed | drhyg_ecommerce_automation 33278423340, 33279585316 |
| Resolver failure class eliminated | `helpers_missing` (rc=1 at startup) |
| Dependencies now resolved via fallback | `opencode_helpers.sh`, `write_opencode_config.sh` |

What this means for consumer repos: no action needed. The resolver is fetched fresh from main at run time, so the fix applies to the next `review_autofix` run on a conflicted PR without waiting for a stable re-tag.

### For contributors

The lockstep rule generalises: any new dependency sourced by a main-primary script from `SUPPORT_SCRIPTS_DIR` must either be added to `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` in the same PR or carry its own checkout fallback, because the staging list executing on consumers is the older `SCRIPT_REF` copy.

- **Workflow log analysis now retains startup-failure diagnostics.** Zero-duration `startup_failure` runs are prioritized for log collection and included in categorized error metadata when logs are unavailable.

- **Clarify, plan, implement, and orchestrator clarify-response wrappers now enforce the same trigger predicates as their reusable workflows.**

The `internal-{clarify,plan,implement,orchestrate-clarify-respond}.yml` callers and corresponding `workflow-templates/ai-*.yml` templates now reject unrelated issue comments and untrusted user or bot commands before dispatching reusable workflows. Eligible issue openings, trusted maintainer commands, established GitHub Actions bot markers, tracker exclusions, and non-PR guards remain available. A contract test now keeps each internal and consumer predicate aligned with its reusable workflow counterpart.

What this means for operators: consumer repositories receive fewer immediately skipped workflow runs without losing supported issue-to-PR automation entry points.

- **Consumer workflow updates now send the existing Telegram notification when only changelog assets or assembled fragments changed.**

The `Update Workflows` workflow now uses the same managed-change conditions for commits and notifications. Changelog asset updates list their changed paths, while fragment assembly reports the fragment count and detected changelog layout. The existing `SILENT` default, helper fallback, unchanged notification categories, and true no-op behavior are preserved.

What this means for operators: changelog-only automated commits are no longer silent when workflow-update notifications are enabled.

- **Stall recovery no longer re-approves issues latched with `ai:destructive-blocked` or `ai:scope-blocked`.** The orchestrator poller now pauses stall recovery for those issues the same way it already does for `ai:needs-human`.

When the destructive-commit guard or the scope guard in `implement.yml` rejects an implementation run, it latches `ai:destructive-blocked` (or `ai:scope-blocked`) on the issue and every later `implement.yml` run refuses to redispatch it (`AI_PHASE_GATE_V1 phase=implement gate=phase_validation reason=destructive_blocked outcome=skip`) until a human removes the label. The issue keeps its `ai:awaiting-approval` phase label, so `scripts/orchestrate_poll_process.sh` saw an ordinary approval stall and posted `/approved` once per poll cycle: `auto_approve` three times, then a Codex stall-judge run that chose `retrigger_implement`, each round a refused implement run and a Telegram warning. On `shubhodeep1/tele-funtoken-msg-scoring` issue #3906 that produced four recovery rounds in four hours with no possible progress, and the recovery budget would eventually have closed the issue and let the judge regenerate it under a new number, discarding the human-in-the-loop signal. `orchestrate_lib.detect_stalls` and the standalone stall loop now skip issues carrying a `STALL_RECOVERY_LATCH_LABELS` entry, and the `check-stalls` payload gains an additive `latched` list so the poll log shows `STALL_SKIP issue=<n> reason=human_gated_latch label=<label> phase=<phase> action=none` instead of staying silent.

| The numbers that matter | Value |
| --- | --- |
| Latch labels that pause stall recovery | `ai:destructive-blocked`, `ai:scope-blocked` |
| Recovery rounds burned on issue #3906 before this fix | 4 (`auto_approve` x3, `retrigger_implement` x1) |
| New stable log line | `STALL_SKIP issue=<n> reason=human_gated_latch label=<label> phase=<phase> action=none` |
| Regression tests | `tests/test_orchestrate_lib.py`, `tests/test_orchestrate_poll_process.py` |

What this means for operators: a latched issue now waits quietly for the human review the guard asked for. Remove the latch label (and set `ALLOW_BULK_DELETE=true` or `ALLOW_WORKFLOW_EDITS=true` when the deletions are legitimate), then reply `/approved`, and the pipeline resumes; the stall recovery counter is not consumed while the latch is present.

### For contributors

`detect_stalls()` keeps its return shape; the new `detect_stall_latched_issues()` helper feeds the additive `latched` field of the `check-stalls` CLI payload, and `stall_recovery_latch_label()` is the single predicate both call. The standalone loop in `run_standalone_stall_recovery` resolves the phase and the latch label through `determine_phase()` and `stall_recovery_latch_label()` in one Python call, so `STALL_RECOVERY_LATCH_LABELS` is the single place to add a latch label; if that call produces no output the candidate is skipped for the cycle with a `[standalone-stall]` warning instead of aborting the poller.

- **The project security pass now converges instead of exhausting its fix budget on new findings every cycle.** After a consolidated fix issue merges, the re-audit covers only what changed since the last audited commit, re-verifies the findings it already reported, and asks the implementer to clear a defect class rather than one line.

Until now every re-audit in `orchestrate_poll_process.sh` re-scanned the whole `merge-base..integration-head` range from scratch with no memory of earlier cycles. On tele-funtoken-msg-scoring#3928, fun-token-multi-chain#471 and binance-blessings#249 every `[security-pass]` fix issue merged, no finding ID ever repeated between cycles, and all three projects still ended in `ai:security-pass-failed` because each fresh full-range sample surfaced something the previous one had not, often the same defect class at a sibling location (another exchange venue, another adapter). The poller now records `security_pass_last_audited_sha` and `security_pass_reported_findings` in state; `scripts/security_audit.sh` accepts `SECURITY_AUDIT_DIFF_SINCE` (scope narrows to range files changed since that commit, plus files cited by prior findings) and `SECURITY_AUDIT_PRIOR_FINDINGS` (the earlier findings are appended to the prompt with instructions to re-emit persisting ones under the same ID and to report every remaining instance of the class). The first audit of a project, and every audit after `/re-security-pass`, still covers the full range. A clean pass invalidated by a head advance (sync merge, resolver merge, fix PR) also audits only the new commits. The default `MAX_SECURITY_PASS_CYCLES` rises from `3` to `5`.

| The numbers that matter | Value |
| --- | --- |
| Projects failed on non-repeating findings when this landed | 4 of 4 open tracking issues across 3 consumer repos |
| Fix issues merged before exhaustion (#3928 after its `/re-security-pass`, #471, #249) | 3 each |
| Full-range re-audit cost on #3928 | 154 files, about 13 min of Codex per tick |
| `MAX_SECURITY_PASS_CYCLES` default | `3` before, `5` after |
| New structured log key | `SECURITY_PASS_SCOPE` (`mode=full|delta`, `reason=…`, `since_sha=…`, `prior_findings=N`) |
| New GitHub API calls | 0 |

What this means for operators: a project that reaches the security pass works through its findings on its own. Each fix cycle audits the fix and the findings it was meant to clear, so the loop ends when those are resolved, not when the auditor runs out of new things to say about untouched code. Projects already labelled `ai:security-pass-failed` recover with `/re-security-pass` once this release reaches `@stable`; that reset still runs one full-range audit first.

### For contributors

`run_security_pass_inline` computes the delta pointer next to the merge-base: the recorded `security_pass_last_audited_sha` (or, for state that passed before the field existed, `security_pass_head_sha`) is used only when it resolves and is an ancestor of the current head; anything else logs `mode=full reason=last_audited_sha_not_ancestor_of_head` and audits the full range. The pointer and the findings memory are written by the same `jq` that records `security_pass_head_sha`, cleared by `/re-security-pass` and by the `ENABLE_SECURITY_PASS=false` release path, and the memory is emptied by a clean pass. Memory entries trim `exploit_scenario` and `recommendation` to 600 characters and keep the latest 60 findings. In `security_audit.sh` the explicit range remains the scope contract; `SECURITY_AUDIT_DIFF_SINCE` intersects it and fails closed on a commit that does not resolve or is not an ancestor of the head, and `SECURITY_AUDIT_PRIOR_FINDINGS` fails closed on anything that is not a JSON array of objects citing repository-relative files. Regression coverage lives in `tests/test_orchestrate_poll_process.py` and `tests/test_security_audit_workflow_contract.py`.

- **The `Project security pass exhausted` tracking comment now lists the remaining blocking findings.** Operators asked to intervene after `MAX_SECURITY_PASS_CYCLES` no longer have to guess what the auditor still flags.

When the project security pass in `orchestrate_poll_process.sh` spends its fix-cycle budget it used to post only a count ("still reports 4 blocking finding(s)") and label the tracking issue `ai:security-pass-failed`. The findings-JSON file behind that count lives only in the runner's runtime directory, so the count was the operator's entire record of what blocked completion. The exhaustion comment now carries the same ID / category / severity / confidence / location / exploit / recommendation table that a consolidated `[security-pass]` fix issue would have carried, under a `Remaining blocking findings (integration head <sha>)` heading. Rendering is best-effort: an unreadable findings file, or a table that would push the comment past GitHub's 65536-byte limit, falls back to the count-only comment with a workflow warning instead of skipping the terminal transition.

| The numbers that matter | Value |
| --- | --- |
| Trigger | tele-funtoken-msg-scoring#3928, cycle 3/3 exhausted on 2026-09-06 with 4 unpublished findings |
| Comment budget guard | table dropped above 60000 bytes |

What this means for operators: after `ai:security-pass-failed`, open the tracking issue's `Project security pass exhausted` comment, address the listed rows, then comment `/re-security-pass`.

### For contributors

`create_security_pass_fix_issue` and `security_pass_terminal_failure` now share one renderer, `render_security_pass_findings_table`, so the fix-issue body and the exhaustion comment cannot drift. `security_pass_terminal_failure` takes the findings file as an optional fourth positional argument; existing three-argument callers keep the count-only comment.

- **A security-pass fix issue that fails implementation is now closed and re-issued instead of parking the project forever.** The poller no longer logs `remains in progress` every cycle for a consolidated `[security-pass]` fix issue that `implement.yml` left in `ai:implementation-failed`.

When the mandatory project security pass creates a consolidated fix issue and `implement.yml` fails it in post-Codex validation (or produces no changes), the issue ends in `ai:implementation-failed`. Wave issues in that state are closed and re-issued by the poller's wave loop, and standalone stall recovery deliberately skips the label, but a security-pass fix issue lives outside the wave arrays, so nothing re-dispatched it: `orchestrate_poll_process.sh` kept the project in `security-pass-fixing` and logged `Security-pass fix issue #N remains in progress.` on every poll. The `security-pass-fixing` handler now recognises the label, waits while the failure's `Post-Codex validation diagnosed follow-up fixes` blocker issues are still open, escalates an unchanged deferral after the existing bounded defer window, then closes the failed issue, re-issues it with the same body plus re-issue guidance, points `security_pass_active_fix_issues` at the successor, and posts a `Security-pass fix issue re-issued` tracking comment. Diagnose outcomes with missing or malformed blocker metadata use that same bounded escalation instead of parking silently, and durable issue markers prevent a state-write retry from creating duplicate successors. The re-issue budget is bounded by the new `MAX_SECURITY_PASS_FIX_REISSUES` repository variable; exceeding it removes the dead issue from managed reuse, attempts to close it, and fails the pass as `ai:security-pass-failed`, recoverable with `/re-security-pass` like the other terminal security-pass states.

| The numbers that matter | Value |
| --- | --- |
| Trigger | tele-funtoken-msg-scoring#3928: fix issue #4055 failed on 2026-09-07 14:23 UTC, its fix-up #4101 merged at 15:03 UTC, and the project stayed parked |
| `MAX_SECURITY_PASS_FIX_REISSUES` default | `2` re-issues per fix cycle |
| Wired in | `.github/workflows/orchestrate_poll.yml` (`vars.MAX_SECURITY_PASS_FIX_REISSUES`) |

What this means for operators: a security-pass fix that dies in post-Codex validation recovers on its own once its fix-up merges. If the successor fails again past the cap, the tracking issue gets a `Project security-pass fix could not be implemented` comment and the `ai:security-pass-failed` label; address the findings, then comment `/re-security-pass`.

### For contributors

The new `security_pass_handle_failed_fix_issue` helper mirrors the wave implementation-failed path (blocker parsing via `extract_fix_issues_from_comment`, post-codex versus no-op guidance) and reads the fix issue's comments from the cycle-local GraphQL batch, falling back to REST only when that batch missed the issue. State gains `security_pass_fix_reissue_count` and `security_pass_fix_defer`; both are cleared when a fresh fix issue is created, on `/re-security-pass`, and when the pass is disabled. The successor is created before the failed issue is closed so a transient `gh issue create` failure retries next poll instead of tripping the closed-without-merged-PR terminal path.

- The orchestrator security pass now defers consolidated fix-issue creation when its paginated duplicate lookup fails or returns invalid data, preventing transient GitHub API failures from creating competing managed issues.

- **Project security-pass fix issue creation is now idempotent across lost state checkpoints.** Before creating a cycle issue, the poller reuses an open orchestrator-managed issue carrying the same tracking-issue and cycle Local ID markers, then republishes that issue number in authoritative state instead of creating a duplicate.

- Security-pass cycle exhaustion now preserves prior tracked Telegram alerts for diagnosis while still sending the terminal critical notification.

- **The security-audit engine's findings-JSON preflight now proves the output destination is writable before it spends a model run.** Previously the check trusted the shell `-w` test, which answers "writable" for root on read-only mounts such as `/sys`, so a root-run poller could complete the whole codex audit and only then fail while publishing `SECURITY_AUDIT_FINDINGS_OUT`.

`scripts/security_audit.sh` now backs every writable-destination preflight (`findings-output-preflight`, `prompt-preflight`, `codex-preflight`) with a real write probe: an existing file is opened for append and a missing file's parent directory receives a short-lived temp file that is removed immediately. Hosted `ubuntu-latest` runners never hit the old gap because they run unprivileged; self-hosted runners executing the orchestrator poller as root now fail closed at preflight with the same `destination is not writable` diagnostic instead of after the audit. `docs/how-it-works.md` also gains the `ai:security-pass`, `ai:security-pass-fixing`, and `ai:security-pass-failed` states and the `/re-security-pass` command so the lifecycle doc matches the default-on gate.

What this means for operators: a misconfigured findings path is reported before any model cost is incurred, on every runner type, and the lifecycle documentation now describes the security-pass gate that projects flow through.

- **The plan workflow's orchestrator auto-answer parser now accepts the blockquoted clarification-question format the prompt itself mandates.** False "Auto-answer parser failed … No Q-ID blocks detected" alerts no longer fire on prompt-conformant output.

`prompts/mode-plan.txt` instructs the planner to emit clarification questions inside a markdown blockquote (`> **Q1: <question>**` through `> Reply:`), but the auto-answer parser and the structured-clarification-block detector in `.github/workflows/plan.yml` anchored their regexes on `^\s*` and never matched the `> ` prefix. An emission that followed the template verbatim raised a false parser-failure alert and forced a human `/answer`, as on shubhodeep1/fun-token-multi-chain#434 (run 33355986371). The five regexes now take an optional `(?:>\s*)*` blockquote prefix, the two `Q_COUNT` greps in `.github/workflows/clarify.yml` carry the same tolerance, and so do the two regexes in `extract_recommended_answers` in `scripts/orchestrate_poll_process.sh`, whose stall-recovery auto-answer would otherwise silently extract nothing from a blockquoted clarification comment. `tests/test_plan_auto_answer_recommended_parser.py` pins single- and multi-question blockquoted fixtures, including the `> Reply:` line, across all three parsers.

| The numbers that matter | Value |
| --- | --- |
| Regexes widened in `plan.yml` | 5 |
| `Q_COUNT` greps widened in `clarify.yml` | 2 |
| Regexes widened in `orchestrate_poll_process.sh` | 2 |
| New parser/Q_COUNT tests | 8 (52 total passing) |
| Triggering incident | shubhodeep1/fun-token-multi-chain#434, run 33355986371 |

What this means for operators: orchestrator-managed plan runs whose clarification questions follow the canonical blockquoted template are auto-answered as designed instead of stalling on a false "No Q-ID blocks detected" alert awaiting a human `/answer`, and stall recovery no longer falls back to "No recommended answers could be extracted" on such comments.

- The mandatory orchestrator security pass now recovers when an externally merged final PR deletes its integration branch.

When the integration branch is confirmed absent, the poller audits only the final PR's verified immutable head SHA and rechecks it after analysis. Transient branch-fetch failures still fail closed, and unavailable or mismatched PR-head evidence cannot fall back to the default branch. This keeps external-finalize and validation-complete projects gated until the intended project snapshot receives a clean pass.

- **Plans that report only self-check blockers now stop the pipeline as blocked instead of looping in clarification.** The AI Plan workflow no longer posts an unanswerable "clarification questions" comment when the planner emits `PLAN_SELF_CHECK: BLOCKER:` with `STATUS: NOT_CLEAR` and no Q-ID question block.

Previously, `.github/workflows/plan.yml` routed that output shape to the clarification path, where the orchestrator auto-answer parser failed with `No Q-ID blocks detected` and stall recovery kept re-answering into the same blocked plan, one Codex planning run per cycle. This surfaced on shubhodeep1/bitsafe.io issue 478 (itself a stall-recovery re-issue of issue 471), where the blocker was the `ai:destructive-blocked` label plus a missing `ALLOW_BULK_DELETE` repository variable, a state no `/answer` can clear. The parse step now routes blocker-only output to the existing blocked path: the issue gets `ai:blocked` (which stall recovery skips), loses `ai:clarification`, receives one "Planning blocked: human input required" comment carrying the first blocker line as the reason, and a CRITICAL Telegram alert pages a human once. Plans that pose Q-ID questions or carry a `NEEDS_CLARIFICATION` status alongside blockers still reopen clarification as before.

| The numbers that matter | Value |
| --- | --- |
| Workflow changed | `.github/workflows/plan.yml` (`Parse planning output` step) |
| Reference incident | shubhodeep1/bitsafe.io#478, run 33509691014 |
| Label applied on blocker-only plans | `ai:blocked` (was: stuck in `ai:clarification`) |
| Planning runs saved per stalled issue | 1 per stall-recovery cycle, indefinitely |

What this means for operators: a plan that is blocked on workflow state (destructive guards, missing repository variables) now pages you once via the CRITICAL Telegram alert and waits, instead of repeatedly warning "Auto-answer parser failed; waiting for human /answer". Clear the reported blocker, then reply `/answer` on the issue to resume planning.

### For contributors

The `plan_self_check_*` step outputs, including `plan_self_check_reopen_clarification`, are emitted unchanged; only the final `blocked` / `needs_clarification` routing moved. `tests/test_plan_clarify_blocked_output.py` mirrors the new routing and adds blocker-only and blocker-plus-questions regression tests.

- **Review autofix same-head resume, the review-issue ledger, and validation hints persist across runs again.** The affected `actions/cache` paths no longer embed the per-run workspace directory, so a run can restore what the previous run saved.

Every `review_autofix.yml` run since the workspace-reuse change cached its ledger and partial-resume marker under `${RUNNER_TEMP}/workspaces/<pr>-<run_id>-<run_attempt>/.ai/…`. actions/cache hashes the path list into the cache version and only matches a save whose key and version both agree, so each run computed a new version, logged `Cache not found for input keys: review-ledger-…`, and started from `AUTOFIX_RESUME_ROUND=0`. On PR #4077 (security-pass fix cycle 5 for project #3965) that produced 13 consecutive same-head runs, each ~1.5 h of reviewer and editor spend, each ending with `Resume round: 1 of 3`; the `REVIEW_MAX_RESUME_ROUNDS` bound could never trip. The three ledger cache steps now share one run-independent, repository/PR-scoped path list, and stage steps copy between that directory and the workspace without letting concurrent PRs on one self-hosted runner clobber each other. `validate.yml`'s behavioural-smoke restore uses the identical list, while its validate-hints cache now uses a separate repository/fingerprint-scoped staging path so repeated validation can skip discovery when workspace reuse is off.

| The numbers that matter | Value |
| --- | --- |
| Looping PR | shubhodeep1/coding-workflows#4077 (13 same-head partial-finalize runs) |
| Cache steps aligned | 3 in `review_autofix.yml`, 1 in `validate.yml` |
| Stable staging scopes | Review ledger: repository + PR; validate hints: repository + discovery fingerprint |
| New log lines | `REVIEW_LEDGER_CACHE_STAGE_IN/OUT`, `VALIDATE_HINTS_CACHE_STAGE_IN/OUT` |
| Regression test | `tests/test_review_ledger_cache_path_stability.py` |

What this means for operators: same-head partial-finalize runs now stop after `REVIEW_MAX_RESUME_ROUNDS` (default 3) or on `no_progress`, and the review-issue ledger's `PERSISTING` / `accepted-residual` history carries across autofix iterations again instead of resetting every run.

### For contributors

The stage-in and stage-out steps are inline shell, `continue-on-error: true`, and fail open when `RUNNER_TEMP` or `workspace_path` is unavailable. Keep the ledger path list byte-identical across all four cache steps; the regression test asserts parity, rejects any `workspace_path`, `run_id`, or `run_attempt` reference in cache paths, and round-trips both cache families across distinct workspaces.

- **A routine sync merge no longer terminalizes a project that already passed its security pass.** The bounded security-fix budget now resets when new commits invalidate a recorded clean audit, so findings in freshly-synced code get their own fix cycles instead of failing the project on sight.

The orchestrator's project security pass is SHA-bound: a clean result is valid only for the exact integration head it audited. When the head advances, the pass is invalidated and re-runs. Until now the completed-fix-cycle counter carried across that invalidation, so a project that had already spent its budget getting to a clean pass would hard-fail on the very first finding in code that arrived afterwards, without ever being granted a cycle to fix it. `run_security_pass_inline` now resets `security_pass_cycle` to `0` when the prior status was `passed` and the head has moved, logging `SECURITY_PASS_CYCLE_BUDGET_RESET ... reason=head_advanced_after_clean_pass`. The reset fires only from a `passed` prior status, so an unresolved `blocked` fix chain keeps its spent budget and still terminalizes on exhaustion.

| The numbers that matter | Value |
| --- | --- |
| Fix cycles granted to post-sync findings, before | 0 |
| Fix cycles granted to post-sync findings, after | `MAX_SECURITY_PASS_CYCLES` (default 3) |
| New GitHub API calls | 0 |
| Incident that motivated this | project #3965, passed at `75048a2c`, sync-merged to `56f71c8f`, failed 3/3 |

What this means for operators: a project that reaches a clean security pass and then receives a `chore: sync <default> into <integration>` merge, a resolver or judge conflict resolution, or a merged fix PR will now work through any new findings on its own instead of raising a CRITICAL alert and waiting for `/re-security-pass`. Completion still requires a clean SHA-bound pass at the current head, so no unaudited code reaches the default branch as a result of the fresh budget.

### For contributors

The reset lives next to the `security_pass_current_head_is_valid` early return in `scripts/orchestrate_poll_process.sh`, which is the single point both security-pass entrypoints funnel through. `ensure_security_pass_before_completion` delegates to `run_security_pass_inline`, so patching the inner call site covers both. A `jq` failure while rewriting the budget is non-fatal: the previous budget stands and the run emits a warning. Two tests cover the split — one asserts the fresh budget and the created fix issue after a head advance from `passed`, the other asserts that a `blocked` prior status still terminalizes on exhaustion.

- **The tracking issue body no longer advertises stale security-pass state.** Every security-pass transition that exits before tick-level reconciliation or begins the long-running audit now re-renders the `### Security pass` block before posting its state comment.

On #3965 the issue body still read `Status: passed` with the previously audited SHA while the label said `ai:security-pass-failed` and the alert comment reported exhaustion, because the body was only re-rendered on the merge-conflict and wave-status paths and security-pass transitions left the tick before reaching them. The `running`, `passed`, head-changed `pending`, `blocked`, fail-closed, terminal-failure, closed-fix-failure, stall-recovery successor-adoption, and `/re-security-pass` transitions now call a shared reconcile step between the state write and the state comment, so the persisted body hash rides the comment already being posted.

| The numbers that matter | Value |
| --- | --- |
| Transitions that now re-render the body | 9 |
| Extra API calls when the body is unchanged and `project_body_snapshot` is present | 0 |
| Legacy state without `project_body_snapshot` | 1 live issue-body fetch per transition |
| API calls when the body changes | 1 `gh issue edit` |

What this means for operators: the security-pass block on a tracking issue is trustworthy at a glance. `Status`, `Completed fix cycles`, `Audited integration SHA`, and `Active fix issue` reflect the current state on each covered transition, not the last clean pass or closed predecessor issue.

### For contributors

The reconcile is `reconcile_tracking_body_after_security_pass_transition` in `scripts/orchestrate_poll_process.sh`, a thin wrapper over the existing hash-gated `reconcile_tracking_issue_body_from_state`. It reads `final_merge_pr` and `integration_branch` from state and fails open. Unlike the tick-level callers it does not skip when both are empty: the body render reads only state, and those two values feed only the readiness refresh, which guards itself, so a fail-closed transition on a project with no integration branch still re-renders. Already-stale issues such as #3965 are not self-healed on the steady-state `failed` tick, by design, to avoid a per-tick live-body fetch for legacy states with no `project_body_snapshot`; they re-render on the next transition, for example `/re-security-pass`. Regression coverage includes the audit-start `running`, terminal-failure, `blocked`, and stall-recovery successor-adoption transitions, including a second terminal-state tick that asserts no edit is issued when the body is unchanged.

- **A security-pass fix issue that stall recovery closes and re-issues no longer fails the whole orchestrator project.** The poller now adopts the replacement issue and keeps waiting, instead of terminalizing the project under a fix that is still in the pipeline.

The mandatory project security pass parks a project in `security-pass-fixing` and pins one consolidated fix issue in `security_pass_active_fix_issues`. When that fix issue stalls, orchestrator stall recovery closes it and immediately re-issues the same body as a new issue. The re-issue path (`execute_stall_recovery_action` → `close_and_reissue`) only re-points *wave* state — `.issue_number_map` and `.waves[].issues[].github_issue` — and both writes are gated on a non-null `local_id`. A consolidated security-pass fix issue is not a wave issue, so the stall judge reports `local_id: null`, the re-point is skipped, and the pinned number keeps pointing at the closed predecessor. On the next poll tick the `security-pass-fixing` arm read that issue as closed without merged-PR evidence and `security_pass_closed_fix_failure` marked the entire project `failed` / `ai:security-pass-failed`, requiring an operator `/re-security-pass`. Before terminalizing, `scripts/orchestrate_poll_process.sh` now resolves the live successor through the durable `- Tracking issue: #<N>` and ``- Local ID: `security-pass-fix-cycle-<K>` `` body markers that survive re-issue, adopts it into `security_pass_active_fix_issues`, and posts a tracking comment naming both issues.

| The numbers that matter | Value |
| --- | --- |
| Extra API cost | 1 paginated `GET /issues?state=open&labels=ai:orchestrator-managed`, only on the closed-without-merge branch |
| New structured log key | `SECURITY_PASS_FIX_ISSUE_SUCCESSOR_ADOPTED` |
| Incident | Project #3965: fix issue #3990 closed and re-issued as #3993 → #3996 at 2026-09-04T01:52Z; project failed at 01:59Z; #3996's fix merged at 18:18Z against an already-reset project |
| Fix cycles lost to the incident | 2 of 3 (`security_pass_cycle` went back to 0 on the operator's `/re-security-pass`) |

What this means for operators: a stalled security-pass fix issue is now handled entirely by the existing stall-recovery ladder. `/re-security-pass` is still the recovery command for a fix that genuinely closed without merging, and that path is unchanged — a closed fix issue with no matching open successor still fails the project exactly as before. An inconclusive successor lookup, meaning an API or parse failure, keeps the project in `security-pass-fixing` and retries on the next tick rather than reading a transient read failure as evidence of a failed fix.

### For contributors

New helper `resolve_security_pass_fix_successor` returns 0 on a confirmed successor, 1 on a successful lookup with no successor, and 2 on an inconclusive lookup, mirroring the fail-closed dedupe contract `create_security_pass_fix_issue` already uses for the same managed-issue listing. Matching requires both body markers, so another project's fix issue or a different cycle of the same project is never adopted. Regression coverage lives in `tests/test_orchestrate_poll_process.py`. The equivalent gap on the validation path, where `validation_active_fix_issues` is likewise never re-pointed by `close_and_reissue`, is untouched by this change and still routes a re-issued validation fix-up to `mark_validation_failed`.

- **Security-pass projects no longer hand their fix and follow-up issues to a human.** Advisory follow-ups are filed only after the project merges, and the implement editor edits the branch's own helper scripts instead of `main`'s copies.

Project #3965 showed two ways the security pass produced issues that stopped for human input even though nothing in them needed a person. The exhaustion judge's advisory follow-ups (#4090, #4091) were filed against `main` while the code they cite existed only on `orchestrator/project-3965`, so the planner answered `BLOCKED: PR #3968 is still open` and both issues sat in `ai:blocked`. The cycle-7 fix issue (#4113) halted in `ai:needs-human` because `implement.yml` had replaced the branch's `scripts/codex_helpers.sh` with `main`'s copy before the editor ran, and the editor's change could not be merged back onto the branch's version (477 lines apart). The orchestrator now keeps accepted findings as pending waivers and files their `ai:security` follow-ups from the final-merge arms, with a body line naming the merging PR. The implement workflow now runs `scripts/implement_staged_support_workspace.sh` around the editor and repair loops, so the editor sees `HEAD`'s helpers and its edits commit as plain edits (`IMPLEMENT_STAGED_SUPPORT_EDITED_FROM_HEAD`), while untouched helpers get the staged copy back for the workflow's later steps.

| The numbers that matter | Value |
| --- | --- |
| New repo variable | `SECURITY_PASS_ADVISORY_DEFER_UNTIL_MERGED` (default `true`; `false` restores judge-time filing) |
| Sites that file deferred follow-ups | every `final_merge_status = "merged"` write in `scripts/orchestrate_poll_process.sh` (5) |
| New helper | `scripts/implement_staged_support_workspace.sh restore\|reinstall`, wired at 4 points in `.github/workflows/implement.yml` |
| Issues this reproduces | #4090, #4091 (`ai:blocked`), #4113 (`ai:needs-human`, run 35072286584) |

What this means for operators: a security-pass project that reaches the exhaustion judge no longer leaves `ai:blocked` advisory issues behind while its integration branch is unmerged, and a fix issue on this repository whose edit lands in a staged helper no longer stops for a human merge. Existing pending advisories are filed the first time the project's final merge is recorded after this ships. #4113 itself needs one manual redispatch (remove `ai:needs-human`, restore `ai:awaiting-approval`) once this is on `main`; the retry then commits the editor's helper edit instead of conflicting.

### For contributors

Waiver rows gain `followup_pending`, `audited_head_sha`, and `finding` while deferred; `create_security_pass_advisory_followup` takes an optional sixth `merged_pr` argument and clears the pending fields when it records the issue. `scripts/implement_commit_changes.sh` reads `STAGED_SUPPORT_EDITOR_HEAD_LEDGER` (`${RUNTIME_DIR}/staged_support_editor_head.txt`) and its summary line adds `edited_from_head=<n>`. Consumer repos are unaffected by the implement change (no ledger means the helper is a no-op).

- **The planner prompt now describes what actually happens to a blocker-only plan.** `prompts/mode-plan.txt` told the planner that `PLAN_SELF_CHECK: BLOCKER:` findings reopen clarification, which stopped being true when blocker-only plans were rerouted to the blocked path.

The AI Plan workflow routes a plan that ends with `PLAN_SELF_CHECK: BLOCKER:` and `STATUS: NOT_CLEAR` to the blocked path unless the same output also carries a Q-ID question block or a `NEEDS_CLARIFICATION` status. The prompt still promised the old clarification behaviour, so a planner that wanted a human answer had no reason to write the questions that would actually get it one. The self-check section now states the real routing and tells the planner to pose explicit Q-ID questions whenever a human answer could clear the blocker. Both `prompts/mode-plan.txt` and the `prompts/_templates/mode-plan.txt` source carry the corrected text; no workflow logic changed.

| The numbers that matter | Value |
| --- | --- |
| Files changed | `prompts/mode-plan.txt`, `prompts/_templates/mode-plan.txt` |
| Routing changed | none, the workflow already behaved this way |
| Reference incident | shubhodeep1/tele-funtoken-msg-scoring#3967, run 33721460753 |

What this means for operators: a plan blocked on something a human can answer is more likely to arrive with answerable questions attached, instead of landing on `ai:blocked` with nothing for `/answer` to resolve.

### For contributors

The behaviour this prompt now documents was introduced in the plan-routing fix for issue 3957. That change edited only the `Parse planning output` step in `.github/workflows/plan.yml` and left the prompt contract describing the pre-fix routing.

- **An implement run that finds the work already done now closes the issue instead of failing.** A `BLOCKED:` verdict whose reason says nothing was required is reclassified as a success-no-op.

The implement phase treats `BLOCKED: <reason>` as a terminal failure, which is right for a real obstacle and wrong for the one reason the pipeline already has a success path for: the approved plan needs no edits because the branch already carries the requested state. Issue #3972 asked for an `author_association` gate that `main` already had, the model answered `BLOCKED: Approved plan requires no edits; all actor gates exist and the focused contract test passes (5/5).`, and run 33711184784 failed and posted implementation-failed diagnostics. The retry loop in `.github/workflows/implement.yml` now classifies the reason line before the failure bail and, when it says nothing was required, takes the existing success-no-op path that closes the issue with `ai:closed` and an "Already implemented" comment. `prompts/mode-implement.txt` was the upstream cause and now reserves `BLOCKED:` for real obstacles, directing the model to end with `No changes needed.` for a no-op outcome.

| The numbers that matter | Value |
| --- | --- |
| Motivating issue / run | #3972 / 33711184784 |
| Classifier halves that must both agree | 2 (`BLOCKED_ALREADY_SATISFIED_REGEX`, `BLOCKED_REAL_OBSTACLE_REGEX`) |
| New regression tests | 3 in `tests/test_implement_post_codex_recovery.py` |

What this means for operators: an issue whose work landed via a sibling task, or whose plan was built on stale repository state, now ends as a closed issue rather than a failed run needing triage. Genuine blockers are unchanged.

### For contributors

The classification is deliberately two-sided and asymmetric. The reason must match the positive regex and must not match the real-obstacle veto (`scope-lock-violation`, `cannot`, `unable`, `unavailable`, `denied`, `missing`, `fail*`, `error`, `conflict`, `ambiguous`, `insufficient`, `needs human`, `blocked by`, `out-of-scope`, `timeout`). Anything failing either half keeps the previous failure behaviour, so a miss costs a false failure, never a falsely-closed issue. The already-satisfied path writes `codex_success_noop.flag` and never `codex_blocked.flag`, so the diagnostics comment is not emitted. README section 10j documents the contract.

- Consumer workflow wrappers now pin reusable coding-workflows calls to the immutable release commit SHA while retaining `# stable` for readability. Stable syncs refresh existing wrappers, including the self-updater, and drift audits report stale pins. Refs #3973.

- **The implement phase no longer overflows codex-cli's prompt cap when the Semble overflow fallback fires.** Targeted-file-context chunk blocks are now clamped to the same total byte budget as inlined files, and `implement.yml` logs the assembled prompt size so an overshoot is visible instead of opaque.

An orchestrator security-pass fix issue was closed without a merged pull request because every implementation attempt died before the editor ran. `scripts/targeted_file_context.py` renders Semble chunks for files too large to inline, but chunk size is set by the search index rather than by the file that triggered the query, and the rendered block was never clamped to the remaining budget. One 11,800-byte source file pulled in a 129,000-byte block against a 102,400-byte budget, and each subsequent overflowing path added another unclamped block on top. The assembled prompt passed codex-cli's 1,048,576-character `turn/start` stdin cap, so both attempts failed with `Input exceeds the maximum length`, the retry loop read that as an ordinary no-actionable-output bail, stall recovery retriggered the same deterministic failure twice, and the stall judge closed the issue. The poller then correctly fail-closed the project on `ai:security-pass-failed`.

| The numbers that matter | Value |
| --- | --- |
| Semble block size per overflowing file, before | ~129,000 bytes |
| `TARGETED_FILE_CONTEXT_MAX_BYTES` default | 102,400 bytes |
| Overflow blocks emitted in one prompt | 6+ |
| codex-cli `turn/start` stdin cap | 1,048,576 characters |
| Affected implement run / issue | 33796624872 / #3990 |

What this means for operators: `TARGETED_FILE_CONTEXT_MAX_BYTES` is now enforced as a true total across inlined files and Semble chunk blocks alike. A clamped block says so in its header and tells the model to read the rest with its read tool, and once the budget is spent no further Semble query is issued. The implement job also prints `Implement prompt assembled size: NN characters (MM bytes)` on every run and raises a workflow warning when the character count is at or over the cap, so a future overshoot is diagnosable from the run log rather than from a stalled issue.

### For contributors

The new `IMPLEMENT_PROMPT_CODEX_STDIN_CAP_CHARS` repo variable (default `1048576`) only tunes that warning threshold; it never truncates the prompt and never fails the step. The guard compares in characters because that is the unit codex-cli enforces; `wc -m` runs under `C.UTF-8`, and a non-UTF-8 locale degrades it to a byte count, which is never smaller than the character count and so can only warn early. The clamp is the prevention, the warning is the diagnosis. This mirrors the guards already in `scripts/review_run_reviewers.sh` and `scripts/review_rb_judge.sh`. The issue-comment block in the implement prompt remains uncapped and is a separate latent overflow source.

- **Stall recovery no longer closes unrelated pull requests that merely mention a stalled issue, and standalone re-triggers of a failed implementation now actually run.** Only the issue's own implementation PR is closed, and the standalone `retrigger_implement` action swaps the phase label before posting `/approved`.

When orchestrator stall recovery decides to close and re-issue a stuck task, `close_linked_pr` in `scripts/orchestrate_poll_process.sh` enumerates linked PRs from three sources, one of which is the issue's timeline cross-references. GitHub records a cross-reference for any PR whose body mentions `#<issue>`, and the `Refs #<issue>` linkage this repo requires produces exactly that event, so a rule-following tooling fix that cited a stalled issue was treated as the issue's implementation PR and closed unmerged. Every open candidate now passes through `_linked_pr_is_issue_implementation`: it is closed only when its head branch follows the orchestrator convention (`ai/issue-<n>`, `ai/<n>`, `ai-implement-<n>` or `ai-<n>`, optionally with a non-word suffix such as `ai/<n>-<slug>`) or its body carries a GitHub close keyword for the issue. Cross-reference-only PRs stay open and are logged with a new `close_linked_pr: skipping PR #<pr> … (cross-reference only; …)` line. The check reads head, base, and body from the same `pulls/<n>` request that already read the PR state, so it adds no API calls.

| The numbers that matter | Value |
| --- | --- |
| Extra GitHub API calls per candidate PR | 0 |
| Lookup strategies still consulted | 3 (timeline, branch name, body keyword) |
| Branch patterns accepted as implementation PRs | `ai/issue-<n>`, `ai/<n>`, `ai-implement-<n>`, `ai-<n>` (plus a non-word suffix such as `ai/<n>-<slug>`) |
| Label swaps before a `retrigger_implement` `/approved` (both arms) | 1 `gh issue edit` call |

The second fix is in the same script. The standalone stall-recovery loop (`run_standalone_stall_recovery`) re-triggered a stalled implementation by posting `/approved` while the issue still carried `ai:implementing` from the failed run, so `implement.yml`'s precheck skipped every re-trigger with `reason=already_implementing` and the run finished in seconds with conclusion `success`, no PR and no diagnostics. The orchestrator-managed loop already swapped the label first. Both arms now call one shared helper, `_reset_implementing_to_awaiting_approval_for_retrigger`, which moves the issue back to `ai:awaiting-approval` with a single `gh issue edit` call before the comment is posted; if the swap fails it logs a `::warning::` and the `/approved` comment is still posted.

What this means for operators: a PR on a `claude/…` or feature branch that references an orchestrator issue with `Refs #<n>` survives that issue's stall recovery. PRs the orchestrator opened on `ai/issue-<n>`, and PRs whose body says `Closes #<n>`, are closed exactly as before. A standalone issue whose implementation run failed is retried by stall recovery instead of burning its stall budget on no-op runs and being closed and re-issued.

### For contributors

`surface_reissue_closed_without_pr` still consults the broad, unfiltered linked-PR set on purpose: it only surfaces a warning and never blocks recovery, so it does not spend one request per candidate to apply the same filter. Its docstring records that choice. Two tests in `tests/test_orchestrate_poll_process.py` run the real bash functions against a stubbed `gh` and pin the close, skip, unknown-state, and one-request-per-candidate behaviours plus the predicate's accept and reject cases. Two further tests pin the label-swap helper's contract (one call, warning and return code 1 on failure) and that both `retrigger_implement` arms call it before posting.

- **A converging orchestrator project no longer ends in "Manual intervention required" because of its own bookkeeping.** The poller now files the judge's fix-up issues even when the recovery budget is exhausted, only charges that budget on a repeated finding, and stops counting its own `chore: sync main` merges toward integration backpressure.

Project tele-funtoken-msg-scoring#3928 fixed a different real defect on each of five consecutive judge cycles and was still stopped by `orchestrate_poll.yml`: the recovery-exhausted branch posted `## Project Failed` without creating the two fix-up issues the judge had just produced, `MAX_RECOVERY_ATTEMPTS` had been consumed by findings that were never repeated, and the tracking issue carried `ai:integration-backpressure` because 15 sync-merge commits were counted as drift. Operators had to re-derive the judge's diagnosis by hand and would have been blocked from merging the resulting fix even after `/judge_resume`. All three behaviours are corrected in `scripts/orchestrate_poll_process.sh`; the exhausted branch now lists the filed issues in the `## Project Failed` comment and `/judge_resume` picks them up with no further diagnosis.

| The numbers that matter | Value |
| --- | --- |
| New repo var | `RECOVERY_COUNT_DISTINCT_FINDINGS` (default `false`) |
| Recovery budget charged when | the judge fingerprint is already in the project's `judge_failed_fingerprints` ledger, or is empty |
| Backpressure figure | non-merge commits ahead of default (raw `ahead_by` fallback when the compare commit list is missing or exceeds 250) |
| #3928 shape now evaluated as | 11 work commits against a threshold of 25, instead of 26 |
| Loop breaker unchanged | `JUDGE_REPEAT_FINGERPRINT_MAX` (default `2`) |
| Absolute cap unchanged | `MAX_JUDGE_CYCLES` |

What this means for operators: a project that keeps fixing distinct findings keeps running until `MAX_JUDGE_CYCLES`, identical-finding loops still trip the repeat-fingerprint breaker, and a project that does exhaust recovery leaves its fix-up issues filed and tracked so `/judge_resume --reset-recovery` resumes work immediately. Set `RECOVERY_COUNT_DISTINCT_FINDINGS=true` on a consumer repo to restore the previous every-failed-verdict accounting.

### For contributors

`_integration_branch_ahead_of_default` accepts an optional OUTVAR third argument and sets `INTEGRATION_AHEAD_BY_WORK_COMMITS` from the same compare call; `compute_cycle_integration_ahead_by` exposes `CWS_WORK_AHEAD_BY` and `CWS_BACKPRESSURE_AHEAD_BY`, and only the backpressure gate reads the latter. `BACKPRESSURE_TRIGGERED` and `BACKPRESSURE_CLEARED` log lines carry `raw_ahead_by=` and `work_ahead_by=`; each failed verdict logs a `RECOVERY_BUDGET_ACCOUNTING` line. The judge prompt (`prompts/mode-orchestrate-poll-judge.txt`) now requires the judge to quote the plan sentence a contradiction violates before flagging it.

- **A large editor log no longer costs a job its entire budget.** The run-substate ledger helper now reads only the tail of the token log it parses, and bounds the ai-memory write it makes.

Implement, review-autofix and validate runs call `scripts/ledger_emit_substate.sh` after every editor attempt to record token counts. The helper scanned the whole editor log for a JSON `usage` object, probing every `{` in the document. `json.JSONDecodeError` counts newlines from byte 0 of the document each time a probe fails, so the scan is quadratic in file size: a 30 MB codex log costs over an hour of CPU, twice per attempt, with no log line to say why. Run 34074649678 on issue #4017 lost a 3-hour implement job to exactly that. Codex had finished editing at 02:36 and the job was cancelled at 04:57 with no branch, no pull request and no diagnosis. The helper now reads the trailing `LEDGER_TOKENS_LOG_MAX_BYTES` (default 1 MiB, snapped to a line boundary) instead. Every parser in it keeps its last match and editors write their usage summary at the end, so the tail carries the same answer. The `record-run-event` write it makes is now bounded by `LEDGER_EMIT_TIMEOUT_SECONDS` too, because it runs while the helper holds an exclusive lock.

| The numbers that matter | Value |
| --- | --- |
| Log that triggered the stall | 31,922,047 bytes |
| Time the two parses consumed | ~141 min of a 180 min job budget |
| Same log parsed under the new default | under 2 s |
| `LEDGER_TOKENS_LOG_MAX_BYTES` default | `1048576` (`0` reads the whole file) |
| `LEDGER_EMIT_TIMEOUT_SECONDS` default | `120` (`0` disables the bound) |

What this means for operators: an implement, review-autofix or validate run whose editor produces a very large log now finishes and opens its pull request instead of being cancelled at the job timeout with its work discarded. Both bounds fail open, so telemetry that cannot be written is still never able to fail or stall the run that produced it.

### For contributors

The fix lives in `scripts/ledger_emit_substate.sh`, so all six callers that pass `--tokens-log-file` are covered without touching their call sites. `implement.yml`, `review_autofix.yml` and `validate.yml` export both variables from repository variables with the defaults above, so an operator override set in the repo reaches the helper. The tail read looks one byte behind its window and keeps a complete first line when the seek lands exactly on a line boundary. Token values are unchanged for any log at or under the bound, which is every log the helper saw before this class of run. `tests/test_run_substate_ledger.py` covers the tail bound, its configurability, the disable path, and the emit timeout.

- **The review-autofix editor can now run a repository's pytest suites instead of improvising import shims.** `review_autofix.yml` installs pytest when the repository declares pytest configuration and the runner's interpreter cannot import it.

The "Install project dependencies (best-effort)" step could report success while installing nothing. A `pyproject.toml` that carries only a tool table, such as `[tool.pytest.ini_options]`, has no `[project]` table, so setuptools' legacy fallback builds an `UNKNOWN-0.0.0` package and `pip install -e .` exits 0. The step's `install_failed` guard stayed false, no warning was logged, and pytest was still missing when the editor started. Every autofix round then reported "pytest is unavailable" and validated its own edits through a hand-rolled no-install import shim rather than the repository's real suites. The step now detects declared pytest configuration, installs pytest through `python3 -m pip` so the install and the importability probe share one interpreter, and warns when the bootstrap still does not take.

| The numbers that matter | Value |
| --- | --- |
| Workflow fixed | `.github/workflows/review_autofix.yml` |
| Autofix rounds that hit the gap | 8 of 8 on PR #4029, 2026-09-07 05:52 to 23:45 UTC |
| Config markers detected | `pytest.ini`, `conftest.py`, `[tool.pytest.ini_options]`, `[tool:pytest]`, `[pytest]` |
| Regression tests | 5, in `tests/test_review_autofix_review_pipeline_contract.py` |

What this means for consumer repos: an AI review-autofix round on a Python repository that declares pytest now validates its fixes against the suites the repository actually ships, so a regression the tests would catch is caught before the round pushes. Repositories with no pytest configuration are untouched and no extra install runs for them.

### For contributors

The gap is silent by construction, which is why it survived: pip's exit code says success, so neither the workflow log nor the `::warning::` path showed anything wrong, and only the editor's own prose reported it. The bootstrap is deliberately gated on declared configuration rather than on the presence of `tests/test_*.py`, so a unittest-only repository does not pay for an install it will not use. `implement.yml` handles the same need differently, by instructing the editor to run `pip install pytest` itself, and is left as is.

- **Two no-op plans no longer fail their pipeline phase.** The plan phase stops rejecting files a plan explicitly promised *not* to change, and the implement phase recognises a validation-only `BLOCKED:` verdict as a success-no-op instead of a hard failure.

Both defects turned a correct outcome into an ERROR Telegram alert and sent a working issue back to a human. In `plan.yml`, the template-owned path guard scanned the whole "Files likely to change" section, including the "No changes are expected to:" list that plans use to name the files they deliberately leave alone. In `implement.yml`, the BLOCKED verdict classifier recognised "no changes are required" but not "requires validation only ... no repository edit is permitted", so a plan whose work had already landed failed the run instead of closing the issue. The guard now suppresses negative sublists and resumes scanning at a later `Files ... change` lead-in regardless of indentation, and the classifier gained a `validation-only` branch that still yields to the existing real-obstacle veto.

| The numbers that matter | Value |
| --- | --- |
| Plan-phase false positive | binance-blessings issue #268, run 34127286952 |
| Implement-phase false failure | tele-funtoken-msg-scoring issue #4090, run 34125645374 |
| Path wrongly rejected | `scripts/build_static_context.sh` |
| Regression coverage | Verbatim production inputs plus plan and classifier edge cases |

What this means for operators: a plan that lists unchanged files as context now reaches the implementation phase, and an issue whose fix already shipped closes itself through the existing no-op path. Both previously produced an ERROR alert that needed manual triage.

### For contributors

The plan guard stays conservative in the other direction: a path mentioned positively with a negation later in the same line is still treated as an edit target, and suppression ends at a later "Files to change:" lead-in even when it is indented. The implement classifier remains two-sided, so `BLOCKED_REAL_OBSTACLE_REGEX` still vetoes a validation-only verdict that names a failure, an unavailable tool, or a denied permission.

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

- **Editor-created files now survive to commit in consumer repos.** The review autofix commit step reconciles new files against a pre-editor snapshot instead of deleting every file the editor created, so a review round whose fix is a new file (a `db/contracts/*.yml` contract, a regression test) commits normally instead of dead-ending on "Editor changes lost".

Reviewers regularly ask for a new file, and the editor creates it, but the consumer-repo branch of `scripts/review_commit_changes.sh` removed every editor-created untracked path before staging under the old "editor may not create new files" policy (with a single carve-out for `changelog.d/*.md` fragments from the #3763 fix). The tree was clean at commit time, the run fired `EDITOR_CHANGES_LOST`, the automatic re-dispatch produced the same file and the same deletion, and once the one-shot retry budget was spent the workflow blocked auto-merge on a PR that looked green. On `tele-funtoken-msg-scoring` PR #4287 this took three consecutive editor rounds (runs 34196277121 and 34198075113) that each reported "Added `db/contracts/settings.yml`" while the run log shows the cleanup removing that exact path two steps later. The editor prompt also told the model not to create files at all, contradicting the reviewers it was applying.

Now the "Apply fixes with editor model" step records the paths that were already untracked before the editor ran (`PRE_EDITOR_UNTRACKED_FILE`, both repo kinds), and the consumer cleanup keeps any new path that is absent from that list, removes paths that were untracked beforehand (strays, leftovers) and pipeline-owned artifacts (`.ai/`, `pre_assembled_static.txt`, fetched support files) even when new, and falls back to the legacy delete-all behaviour when the snapshot is missing. Every removed path is written with its reason to `REVIEW_REMOVED_NEW_FILES_FILE`, and the "Editor changes lost" PR comment lists them, so a future stall names its cause on the PR. The editor prompt's file-creation policy now allows a new file when a reviewer finding, the PR's scope, or a documented repository convention requires it, and requires every created file to be listed by path.

| The numbers that matter | Value |
| --- | --- |
| Reference PR / runs | `tele-funtoken-msg-scoring` #4287, runs 34196277121 and 34198075113 |
| Editor rounds lost to the deletion there | 3 (one committed partially, two produced no commit) |
| New env inputs | `PRE_EDITOR_UNTRACKED_FILE` (snapshot), `REVIEW_REMOVED_NEW_FILES_FILE` (removal log) |
| Paths still removed when new | `.ai/**`, `.github/ai/**`, `.github/prompts/**`, `.github/scripts/**`, `ai-memory/**`, `.codex-workflow-src*`, `node_modules`, `pre_assembled_static.txt`, `unattended_system_instructions.md`, `ai_pipeline.md`, `agents.md` |
| New tests | 5 in `tests/test_review_commit_new_files_preserved.py` |

What this means for operators: after the next `@stable` promotion, a consumer-repo review round that creates a contract, test, or other convention-required file commits and pushes like any other fix, and the "Editor changes lost (retry unavailable)" alert no longer fires for it. When the cleanup does drop a new file, the PR comment now says which path and why, instead of only "no commit was produced". The workflow-source repo path is unchanged; it already staged only editor-touched files.

### For contributors

The reconciliation lives in the existing removal loop in `scripts/review_commit_changes.sh`; the `NEW_FILES_BEFORE_COMMIT_FILE` block boundaries are preserved so the existing fragment-preservation tests keep extracting it. Preserved files are staged by the existing consumer untracked-files `git add` pass and remain subject to the write guard and protected-path resets. The snapshot is taken in the same step that already snapshots the workflow-source repo (`PRE_EDITOR_STATE_FILE` is untouched), and the fallback keeps consumers on an older wrapper on the old behaviour rather than failing open.

- **A watchdog kill of the review-autofix editor no longer aborts the whole editor step.** After a wall-time or idle kill the editor now moves on to the next attempt (or the fallback summary) instead of the run surfacing as "AI review/autofix produced no output — will retry".

When the editor watchdog killed attempt 1 at the 55-minute wall cap on PR #4071 (AI Review run 34397466777), `scripts/review_apply_fixes.sh` exited 1 a few milliseconds after restoring editor isolation: no attempt 2 or 3, no fallback-model attempt, no fallback summary, and no archived editor stderr. The "Post editor summary comment" step then classified the run as an empty no-op, posted the "will retry" comment, and the review was deferred to the stall poller's next cycle, costing the run's remaining budget and another full reviewer pass. The watchdog subshell exits on its own after it kills the editor, so the parent's `kill "${wd_pid}"` can hit a process that is already gone and return 1; under `set -euo pipefail` that unguarded failure aborted the script. The reap is now `kill ... || true; wait ... || true` at the editor kill site and at both reviewer watchdog kill sites in `scripts/review_run_reviewers.sh`.

| The numbers that matter | Value |
| --- | --- |
| Incident | AI Review run 34397466777, PR #4071, editor attempt 1 killed at 3300s |
| Time from kill to silent exit | ~5 s (the watchdog's TERM, `sleep 5`, KILL sequence) |
| Kill sites fixed | 1 in `review_apply_fixes.sh`, 2 in `review_run_reviewers.sh` |
| Regression test | `tests/test_editor_watchdog_kill_contract.py` |

What this means for operators: an editor that overruns `EDITOR_MAX_WALL` or goes idle now costs one attempt, not the whole review run. The retry loop keeps its remaining attempts (including the `MODEL_EDITOR_FALLBACK` switch on the final one), the editor's stderr is archived as `editor_attempt_<n>.err` in the failure-log artifact, and the fallback summary is written so the downstream steps see a real disposition instead of an empty no-op.

### For contributors

On `main` the race is narrow because the stall guard kills a runner-owned editor process within about a second of the watchdog's TERM, so the parent usually reaps the watchdog while it is still in its `sleep 5`. The `orchestrator/project-3965` branch runs the editor under `sudo -u nobody` isolation, where the runner-owned stall guard cannot signal the editor's process group, the guard survives until the watchdog's KILL five seconds later, and the parent then always reaps an already-exited watchdog. That is why run 34397466777 hit the abort deterministically. The signal-permission gap in the isolated editor path is a separate defect on that branch and is not addressed here.

- **Implementation commits on an orchestrator integration branch no longer revert the branch's own copies of the runtime helpers.** In this repository the implement workflow installs `main`'s support scripts over the checkout before the editor runs, and the commit step now puts every staging-only modification back before it commits.

The "Stage workflow support files" step of `.github/workflows/implement.yml` installs `SCRIPT_REF`'s copies of `scripts/*.sh`, `scripts/*.py`, `prompts/*` and `ai-memory/schemas/*` into the checkout so the job runs the freshest tooling. Consumer repos exclude those files at commit time, but in `shubhodeep1/coding-workflows` they are tracked, and when the issue targeted `orchestrator/project-3965` the implementation commit carried `main`'s versions and silently dropped the branch's security-pass fixes from PRs #4029, #4052, #4057 and #4071. PR #4079 lost 1,117 lines across eight helpers that way and its own review editor then failed with `model_provider_broker_start: command not found` on every hourly retry; PR #4071 had spent eight autofix rounds restoring the same files. The staging step now inventories its explicit install destinations rather than parsing status lines, records modified helpers and copies recreated over branch-side deletions, preserves immutable runtime copies, and invokes the commit helper from that runtime directory. `scripts/implement_commit_changes.sh` restores untouched copies to `HEAD`, removes untouched staging recreations, re-bases editor content edits onto the branch version with a 3-way merge, preserves editor-selected modes, and fails closed when any ledger input is missing or cannot be reconciled. The preflight scope guard projects only entries whose content and mode still match the installed baseline back to `HEAD`, so editor content, mode, and deletion changes remain visible to `files_touched` enforcement.

| The numbers that matter | Value |
| --- | --- |
| Helpers reverted in PR #4079 | 8 files, 1,117 deletions |
| Failed review-autofix rounds on PR #4079 before this fix | 10 (hourly, run 34669207742 and earlier) |
| Autofix rounds PR #4071 needed to restore the same files | 8 |
| Ledger location | `${RUNTIME_DIR}/staged_support_overwrites.txt` |

What this means for operators: an implementation PR against an integration branch now contains only what the editor changed. Look for `IMPLEMENT_STAGED_SUPPORT_LEDGER` and `IMPLEMENT_STAGED_SUPPORT_RESTORE restored=<n> rebased=<n> conflicts=<n>` in the implement job log. An unresolved staged-support path fails closed, attempts and verifies the `ai:needs-human` latch, posts the affected paths and latch status, and sends the configured CRITICAL alert instead of entering generic diagnosis and re-issue handling.

### For contributors

The job executes `SCRIPT_REF` copies from `IMPLEMENT_STAGED_SUPPORT_RUN_DIR` after ledger restoration begins, so `main`-side tooling fixes keep applying to wedged integration branches without dirtying committed content or replacing a running script. Consumer repositories never set the staged-support overrides and are unaffected. `tests/test_implement_post_codex_recovery.py` covers restore, re-base, conflict, branch deletion, immutable execution, and handler routing.

- **Planning is no longer skipped by a PR that merely mentions the issue, and `Target branch:` now routes an issue to its integration branch.** Two pipeline defects that together stalled issue #4073 and blocked its re-issue #4075.

`plan.yml` ("Skip when issue already has a PR") and `implement.yml` ("Safety check for existing PR") counted every open pull request that cross-referenced the issue as the issue's own PR. PR #4072, a `main` fix that cited #4073 as context, kept #4073's three planning runs from doing anything, with no comment and no label change, until stall recovery closed it 76 minutes later and re-issued it as #4075. Both gates now skip only for the issue's own implementation PR: an open PR on the conventional `ai/issue-<N>` head, or an open PR whose body closes the issue with a supported short, qualified, or GitHub issue URL reference (the same boundaries `_pr_json_closes_issue` uses in `scripts/orchestrate_poll_process.sh`). Mention-only PRs are logged as ignored. The re-issue #4075 then planned on `main` because its body names the branch as ``**Target branch:** `orchestrator/project-3965` ``, which `scripts/resolve_integration_ref.sh` did not recognise, and the planner correctly emitted `BLOCKED: integration branch mismatch`. `Target branch:` is now an accepted alias of `Integration branch:` in child and tracking issue bodies, in both the shell resolver and `scripts/orchestrate_lib.py`; the canonical line wins when both are present.

| The numbers that matter | Value |
| --- | --- |
| GitHub API calls per gate | 2 (timeline + paginated open-PR inventory), was 1 |
| Planning runs silently skipped on #4073 | 3 (runs 34426969864, 34431425213, 34435890933) |
| New resolver fixtures | 4 under `tests/fixtures/integration_ref_resolver/` |

What this means for operators: an issue that an unrelated open PR references with `Refs #N` now plans and implements normally, and a human-written issue can steer the pipeline to an integration branch with a `Target branch:` line instead of the orchestrator's metadata footer. A `Target branch:` that names a branch that does not exist fails safe the same way a missing `Integration branch:` does instead of falling back to the default branch.

### For contributors

`tests/test_stall_recovery_pr_lookup.py` now exercises the plan.yml gate as well as the implement.yml one, with a `MOCK_GH_PULLS_JSON` hook in the shared `gh` stub for the paginated open-PR inventory. The alias pattern lives in two places by design (shell and Python parity is pinned by `test_resolve_integration_ref_parity_for_fixtures`). Workflow shell steps receive the resolved ref through step-local environment variables, so valid Git ref characters cannot be reinterpreted as shell syntax when the ref is logged or stored as the implementation PR base.

- **The "Editor no-op suspicious" alert now says when the editor simply failed.** When every editor attempt fails, the Telegram warning and PR comment report the attempt count and the final attempt's provider error instead of claiming the editor "claimed no changes were needed".

`review_autofix.yml` blocks auto-merge whenever the editor's no-op disposition cannot be verified, which is correct, but one of the cases it covers is the `recoverable_failure` partial-finalize summary that `scripts/review_apply_fixes.sh` writes after all editor attempts fail. In that case the editor never produced output, yet the operator alert read "Editor claimed no changes needed but disposition could not be verified. Manual review or re-run required." On PR #4077 the editor's model broker answered HTTP 429 on every attempt for 21 consecutive runs (Actions runs 34692519987, 34700918528 and 34702442346 among them) while the alert kept asking for a re-run. The `Validate editor no-op disposition` step now sets an additive `EDITOR_NOOP_RECOVERABLE_FAILURE=true` when it sees the partial-finalize sentinel, and the `Telegram editor-noop-suspicious warning` step uses it to post "Editor failed on all N attempts" with the last `Error:` line from the final attempt's stderr, clipped to 240 characters. `EDITOR_NOOP_SUSPICIOUS`, `EDITOR_NOOP_REFUSAL`, the auto-merge gate and the PR-comment literal the orchestrator poller greps for are unchanged.

| The numbers that matter | Value |
| --- | --- |
| New env var | `EDITOR_NOOP_RECOVERABLE_FAILURE` (default `false`) |
| Sentinel matched | `partial finalize requested after a recoverable editor failure` |
| Error excerpt limit | 240 characters, final attempt only |
| Incident PR / runs | #4077 / 34692519987, 34700918528, 34702442346 |

What this means for operators: a Telegram warning that begins "Editor failed on all N attempts" means the editor never ran to completion and quotes the provider error to chase; a re-run only helps when that error was transient. The generic "Editor no-op suspicious" wording is now reserved for runs where the editor did produce a summary that could not be verified.

### For contributors

The sentinel is matched verbatim from the `Runtime failure path:` line of the recoverable_failure fallback summary in `scripts/review_apply_fixes.sh`; `tests/test_review_autofix_editor_noop_cascade_contract.py` pins it in lockstep across the script and the workflow. The soft-deadline fallback summary is deliberately not classified here.

- **review_autofix now flags an editor that failed or refused on every attempt as no-op suspicious even when no reviewer succeeded, and records recoverable failures in the run summary.** Validator Checks 1b and 1c set `EDITOR_NOOP_SUSPICIOUS=true` themselves, and `REVIEW_AUTOFIX_RUN_SUMMARY_V1` gains `recoverable_failure` / `editor_noop_recoverable_failure`.

Two gaps left out of PR #4083 (the "Editor no-op suspicious" alert fix for PR #4077) are closed. First, `scripts/review_run_reviewers.sh` exits green with `REVIEWERS_SUCCESSFUL=0` when every reviewer slot is skipped fail-open (circuit breaker open, or a retryable failure with no failback mapping), and the `Apply fixes with editor model` step has no reviewer-count clause, so the editor still runs. When it then fails or returns a safety-policy refusal, only Check 2 of `Validate editor no-op disposition` used to flag the resulting partial-finalize summaries, and Check 2 is skipped at zero reviewers: the Telegram / PR-comment alert never fired, the merge-conflict detect/resolver chain ran with nothing to land, and the run summary recorded the editor slot as `success`. Check 1b now sets `EDITOR_NOOP_SUSPICIOUS=true` alongside `EDITOR_NOOP_REFUSAL=true`, and Check 1c does the same alongside `EDITOR_NOOP_RECOVERABLE_FAILURE=true`; the validator also keeps these checks reachable for late refusal and recoverable-failure partial finalization when too little hard-timeout headroom remains for the validation/publish tail. Soft-deadline and reviewer-phase partial finalization retain the existing skip behavior. On paths where the validator already ran, behavior with reviewers present is unchanged. Second, the `Append review pipeline iteration summary` step classified a recoverable failure as `unexpected_noop` / `editor_noop_suspicious`; it now records `slot_results.editor.failure_class=recoverable_failure` and, when no partial finalize was requested, `finalize_reason=editor_noop_recoverable_failure`. `refusal` / `editor_noop_refusal` keep precedence and every existing value is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Validator checks that now set `EDITOR_NOOP_SUSPICIOUS` | Check 1b (refusal), Check 1c (recoverable failure) |
| Reviewer statuses that reach the editor with `REVIEWERS_SUCCESSFUL=0` | `skipped_open`, `skipped_unmapped` |
| No-tail-budget partial finalize | refusal/recoverable failure classified; soft deadline and reviewer phase still skipped |
| New editor `failure_class` | `recoverable_failure` |
| New `finalize_reason` | `editor_noop_recoverable_failure` |
| Soft-deadline fallback summary | still not matched (budget exhaustion, not an editor failure) |

What this means for operators: a review run whose editor never produced validated output now posts the specific refusal or "Editor failed on all N attempts" alert and blocks the resolver chain, regardless of how many reviewers succeeded. Recoverable failures are named as `recoverable_failure` instead of an unexpected no-op. Auto-merge behaviour does not change: the partial-finalize request already held those gates closed on these paths.

### For contributors

The Check 1 regex and its `::warning::` literal (grepped by the e2e poller in `test-and-mark-stable.yml`) are byte-for-byte unchanged; the refusal and recoverable-failure summaries at zero reviewers are both pinned with `EDITOR_NOOP_SUSPICIOUS=true` and their respective specific flags. The workflow-gate contract additionally evaluates the late editor-failure path where `AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE=false`, including the unchanged soft-deadline exclusion. `determine_finalize_reason` still returns `partial_finalize` ahead of every `editor_noop_*` outcome, so on the real recoverable-failure path the editor slot's `failure_class` is the field to read. Pinned by `tests/test_review_autofix_editor_noop_cascade_contract.py` and `tests/test_review_autofix_review_pipeline_contract.py`; runbook §20.10.2 in `probably_unnecessary_but_read_if_stuck.md` carries the reachability trace.

- **The review workflow no longer sends a CRITICAL Telegram alert on every re-dispatch while a review-blocked judge decision waits for human approval.** One alert is sent when the approval request is created; later runs that reuse the pending request log a suppression line instead.

`review_autofix.yml` runs `@main`, but `scripts/review_rb_judge.sh` executes from the PR head's `SCRIPT_REF`. A PR whose branch carries the trusted-human-approval gate for terminal judge decisions therefore reports `judge_action=approval_pending` to a workflow that had no arm for it, so the generic `Review-blocked judge action: approval_pending` message fired on every 30-minute review-sweep re-dispatch. The `Telegram review-blocked judge decision` step now handles `approval_pending`: it alerts on `judge_skip_reason=approval_request_created` and exits quietly on `approval_pending`.

| The numbers that matter | Value |
| --- | --- |
| Incident | `shubhodeep1/coding-workflows` PR #4079, run 34721550146 |
| Duplicate CRITICAL alerts before the fix | 24 in eleven hours (one per sweep tick) |
| Regression test | `tests/test_review_rb_judge_self_run_exclusion.py::test_workflow_suppresses_repeat_approval_pending_alert` |

What this means for operators: a pending approval produces one Telegram alert, not one every half hour, and the alert text now says the recommendation needs trusted human approval rather than echoing the raw action name.

### For contributors

The arm is byte-identical to the one `orchestrator/project-3965` already carries in its `review_autofix.yml`, so the project's next `chore: sync main` merge sees the same insertion on both sides. The branch also suppresses `judge_action=skip`; that arm is deliberately not ported because on `main` a handled `skip` still covers `invalid_pr_number` and `pr_not_open`, whose alerts are unrelated to the approval gate.

- **The review gate now skips `workflow_dispatch` reruns of a same-head review cycle that has already ended.** A PR whose newest trusted `<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->` comment says `resume_should_continue=false` for its current head no longer gets a full codex-agent job every time the 30-minute sweep fires.

Operators saw the cost on PR #4077: after the same-head cycle went terminal on `0d30bc69` at 2026-09-12T15:55Z (`resume_state=no_progress`, round 3 of 3), `Internal: AI Review Autofix Sweep` kept dispatching `internal-review.yml` for it every 30 minutes for 12.5 hours. Each of those runs (for example 34703936345) restored the cached terminal state and exited neutral after about 6 minutes of runner setup, doing no review work. The `Evaluate review gate` step in `review_autofix.yml` now resolves the account authenticated through `GH_PAT`, trusts only that account's newest marker for the PR's current head, and sets `should_run=false` with `skip_reason=terminal_same_head` before the codex-agent job starts. Only a PR GitHub reports `mergeable=true` is skipped, so a conflicted head still reaches the resolver; `pull_request` events, `force_rb_judge` dispatches, `[force-review]` in the title and the `force-review` label all bypass it, and an identity, comments API, or parser failure logs `AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED` and runs normally.

| The numbers that matter | Value |
| --- | --- |
| Same-head reruns dispatched after PR #4077 went terminal | about 25 over 12.5 hours |
| Runner time per no-op rerun | about 6 minutes |
| New repo variable | `AUTOFIX_SKIP_TERMINAL_SAME_HEAD` (default `true`) |
| New API calls per skipped dispatch | 2 (`/user` plus paginated `/issues/{n}/comments`); `head_sha` rides on the existing `/pulls/{n}` fetch |
| Skip log line | `AUTOFIX_GATE_SKIP reason=terminal_same_head pr=<n> head_sha=<sha> resume_state=<state> resume_round=<n> resume_round_limit=<n> marker_comment_id=<id> mergeable=true` |

What this means for operators: a PR that exhausted its same-head resume rounds stays quiet until a new head is pushed, it becomes conflicted, or someone forces a review. Set `vars.AUTOFIX_SKIP_TERMINAL_SAME_HEAD=false` to restore the previous unconditional re-dispatch.

### For contributors

The decision logic is an embedded Python heredoc in the gate step, extracted and executed by `tests/test_review_autofix_terminal_same_head_gate.py` against marker fixtures. It keys on the fenced `key=value` block of the partial-finalize comment (`partial_finalize=true`, `head_sha=`, `resume_should_continue=`), picks the newest trusted marker by `created_at` then comment id, exits non-zero on malformed JSON so the shell emits the structured parse-error diagnostic, and counts rejected current-head markers in `AUTOFIX_GATE_NO_SKIP_TERMINAL_SAME_HEAD`. Every value reaching a log line is reduced to `[A-Za-z0-9_.-]`; `AUTOFIX_GATE_TERMINAL_SAME_HEAD_UNCHECKED` and `AUTOFIX_GATE_TERMINAL_SAME_HEAD_OVERRIDE` record the other non-skip decisions.

- **The test suite no longer commits into the real repository when a workflow launches it with `GIT_DIR` / `GIT_WORK_TREE` set.** A session-wide fixture in `tests/conftest.py` strips the repo-pinning git variables before the first test runs.

The implement, review_autofix, and validate workflows export `GIT_DIR` and `GIT_WORK_TREE` into `$GITHUB_ENV`, and the codex editor inherits them when it runs `pytest` to validate its own change. Git honours those variables ahead of the working directory, so a test that builds a scratch repository under a temp dir and only sets `cwd` was rebound to the live checkout. During the implement run for issue #4092, `tests/test_assemble_changelog.py` did exactly that: its `git init` re-initialised the real repository, `git add -A` staged the whole live work tree (including the support scripts the staging step had installed over the branch's own files), and `git commit -m base` landed commit `37c72a5` on `ai/issue-4092` under the throwaway identity `test <test@example.invalid>`. That commit reverted 1,654 lines across 32 files, including eight runtime helpers; `scripts/codex_helpers.sh` lost `model_provider_broker_start`, and every review round of PR #4093 then died in the editor with `model_provider_broker_start: command not found`. The fixture removes `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY`, and `GIT_ALTERNATE_OBJECT_DIRECTORIES` for the whole session and restores them afterwards, so every git subprocess a test spawns resolves its repository from `cwd`.

| The numbers that matter | Value |
| --- | --- |
| Stray commit | `37c72a5` on `ai/issue-4092`, PR #4093 |
| Lines reverted by that commit | 1,654 across 32 files |
| Review runs lost to the missing helper | 34982425230 and the retries that followed |
| Regression test | `tests/test_pytest_git_env_isolation.py` |

What this means for operators: an implement run that lets the editor execute the repository's own tests can no longer produce a foreign-identity commit that silently reverts the branch, and the review editor that follows it keeps its broker helpers.

### For contributors

Individual tests already stripped these variables by hand (`test_pr_merge_status_guard.py`, `test_orchestrate_poll_process.py`, `test_detect_editor_changes_lost.py`, and others); those per-test scrubs stay and remain the right pattern for tests that also sanitise `BASH_ENV` or `WORKSPACE_PATH`. Tests that intentionally exercise `GIT_DIR` handling keep working because they set the variables inside the test body. The regression test launches a nested pytest run of the changelog tests with the variables pointed at a sentinel repository and asserts the sentinel's branch, head, and commit count are unchanged.

- **Review auto-merge now refuses to merge a PR head that differs from the head the workflow authorized.**

Deterministic small/docs-only skips and completed review runs pass their observed head SHA to `gh pr merge --match-head-commit`. Missing snapshots fail closed, and concurrent pushes are left for their own `synchronize` review run instead of inheriting an earlier decision. The workflow now withholds `ai:review-skipped` and `ai:ready-to-merge` when bound merge authorization fails, preventing downstream automation from acting on the rejected head. Forward-merge fallback PRs retain real merge commits, while regular PRs retain squash merges. This change binds the initial auto-merge request only; GitHub may still keep auto-merge enabled across later pushes while required checks are pending, which remains separate lifecycle hardening work.

What this means for operators: a concurrent push that invalidates the initial merge request remains open and visibly outside the ready-to-merge phase until a workflow run evaluates that exact head.

- **The release gate's `e2e-smoke-test` job cap is now 180 minutes (was 120), so a healthy but slow smoke run is no longer cancelled while Phase 6 is still inside its own 30-minute window.**

Release v1.29.7 failed on `Test & Mark Stable Release` run 35479338161 with every phase healthy: Phase 4 (wait for review & autofix) spent exactly its 75-minute `REVIEW_STEP_TIMEOUT`, Phase 6 (review-blocked simulation) started at +99m, and the 120-minute job cap cancelled it at idle 1222s of its 1800s `PHASE_TIMEOUT` window. The cancel also skipped `Deep verify`, so `Verify all phases passed` blocked the release on `Internals` even though a Phase 6 timeout is scored as a non-blocking warning. Phase 6 could not finish sooner because its poller dispatch queued behind a scheduled `Internal: AI Orchestrate Poller` run that held the `ai-orchestrate-poll` concurrency group for 40 minutes while judging two live projects, and was then displaced from the single pending slot by later force-tick dispatches. The 180-minute cap in `.github/workflows/test-and-mark-stable.yml` is sized for this evidenced healthy-run envelope, including the 75-minute review step and Phase 6's 30-minute inactivity window; it does not claim to contain the sum of every phase's fail-fast inactivity limit. The three comments that cited the old 120-minute cap now say 180 minutes.

| The numbers that matter | Value |
| --- | --- |
| `e2e-smoke-test` `timeout-minutes` | 120 → 180 |
| Phase 4 per-step cap (`REVIEW_STEP_TIMEOUT`, unchanged) | 75m, clamped at 90m |
| Phase 6 inactivity window (`PHASE_TIMEOUT`, unchanged) | 30m |
| Failing run | 35479338161 (v1.29.7) |

What this means for operators: a release-gate run whose review phase legitimately takes the full 75 minutes now gets its review-blocked simulation and deep verification instead of a `cancelled` gate, at the cost of a genuinely hung run taking up to 60 minutes longer to fail. Phase scoring, phase timeouts, and promotion dispatch behavior are unchanged.

- **Stable releases now recover safely from ambiguous Git tag push failures.**

Release operators no longer lose an otherwise valid release when GitHub accepts a tag push but times out before confirming it. `.github/workflows/mark-stable.yml` and `.github/workflows/test-and-mark-stable.yml` now retry tag publication with exponential backoff and inspect the exact remote tag after each failed push. A matching remote object confirms that publication succeeded despite the failed response. A conflicting immutable version tag still fails without force, while only the existing `stable` and major pointers retain force-update behavior.

| The numbers that matter | Value |
| --- | --- |
| Maximum publication attempts per tag | 3 |
| Retry delays | 2 seconds, then 4 seconds |
| Remotely verified tags | Version, `stable`, and major-version tags |

What this means for release operators: transient, ambiguous GitHub responses can recover automatically, while genuine immutable-tag conflicts and unverifiable remote state continue to block the release.

- **Stable releases now push the assembled changelog.** The `release` job named the source branch as a bare `stable`, which git refuses because `stable` is also the moving release tag.

Every release cut from the `stable` branch retried the changelog push four times, logged "dst refspec stable matches more than one", and then released without folding `changelog.d/` at all. The v1.29.7 gate (run 35570966035) left 113 fragments unfolded that way. The push in `test-and-mark-stable.yml` and `mark-stable.yml` now targets `HEAD:refs/heads/<branch>`, and both `git fetch` calls in the same step use an explicit `+refs/heads/<branch>:refs/remotes/origin/<branch>` refspec, so the retry rebase and the fail-open reset see the real branch tip instead of the tag.

What this means for operators: the next stable release folds the accumulated fragments into `CHANGELOG.md` on `stable` and extracts the release notes from the assembled file. Releases cut from `main` were never affected, because no tag is named `main`.

- **README core-wrapper examples now match the shipped dispatch predicates.**

The Quickstart now includes the canonical job-level `if:` predicates for `ai-clarify`, `ai-plan`, and `ai-implement` and describes their distinct issue-open, trusted-user, and marked-bot routes accurately. Consumers who hand-copy the examples no longer receive guidance that omits wrapper-side dispatch filtering or overstates the bot route for `/reclarify`. The Contributing section now links to the actual workflow-dispatch release flows instead of the removed `docs/release-policy.md` file.

What this means for operators: README-based wrapper installation and release guidance now agree with the checked-in templates and workflows.

- **A security-pass fix issue merged by the review-blocked judge no longer fails its project as "closed without a merged PR", and judge-created follow-up issues now plan and implement on the project's integration branch.** Two paths in the per-PR review-blocked judge and one check in the orchestrator poller are corrected; a third guard gains diagnostics.

When the per-PR review-blocked judge (`review_autofix.yml`, `scripts/review_rb_judge.sh`) merges a fix PR and then swaps the linked issue to `ai:ready-to-merge`, it raced the `AI Issue PR Status` handler that had already labelled the issue `ai:merged` and closed it. On `shubhodeep1/tele-funtoken-msg-scoring#4379` the swap landed six seconds after `ai:merged`, and the poller's security-pass check, whose GraphQL batch never sees a linked PR for a merge into an integration branch, failed project #3928 with `fix_issue_closed_without_merged_pr`. The judge's phase swap now adds a non-terminal target without replacing the complete label set, removes only non-terminal phases observed before the write, and rechecks terminal precedence so a concurrent `ai:merged` or `ai:closed` label is never deleted; the poller also consults the same issue-timeline merged-PR evidence its cache-miss path already used before declaring a closed fix issue unmerged, then backfills `ai:merged` so the closed issue's labels match that evidence. Separately, follow-up issues the judge opens under `merge_with_followup` carried only Source PR / Parent issue / Type, so `scripts/resolve_integration_ref.sh` sent their plan and implement runs to the default branch (`tele-funtoken-msg-scoring#4386` blocked on "integration branch mismatch"; `binance-blessings#290` implemented against `main` while the missing file sat on `orchestrator/project-249`). Those follow-ups now carry `- Tracking issue: #N` and `- Integration branch: <branch>`, copied from the actual parent issue's metadata or from a PR base matching `ORCH_INTEGRATION_BRANCH_PATTERN`; canonical `orchestrator/project-<N>` bases also supply the tracking issue number, invalid metadata falls back safely, and an explicit `(default branch)` suppresses PR-base fallback instead of being emitted as a real ref.

| The numbers that matter | Value |
| --- | --- |
| Consumer issues that surfaced the defects | `tele-funtoken-msg-scoring#3928` / `#4379` / `#4386`, `binance-blessings#290` |
| Extra API calls on the poller's happy path | 0 (the timeline read runs only for a closed, unlabelled, unlinked fix issue) |
| Extra API calls in a non-terminal judge phase swap | 2 on the ordinary one-old-phase path; 3 when a concurrent terminal label wins |
| New regression tests | 14 in `tests/test_review_rb_judge_label_propagation.py`, 3 in `tests/test_orchestrate_poll_process.py`, 2 in `tests/test_retrigger_inflight_direct_fallback.py` |

What this means for operators: a project whose security-pass fix PR was merged by the review-blocked judge advances to its re-audit instead of parking in `ai:security-pass-failed`, and a judge-created follow-up issue lands on the integration branch its parent belongs to. Projects already parked by the race still need `/re-security-pass`; follow-ups already opened without lineage need the two metadata lines added to their body by hand.

### For contributors

The stall recovery's direct in-flight review check (`_direct_inflight_review_run_on_branch`) now treats concurrency-held `pending` review runs as active and logs `STALL_INFLIGHT_DIRECT_CHECK branch=<b> rc=<n> runs=<n> live=<n> matched=0 outcome=<listing_unavailable|no_fresh_review_run>` on stderr whenever it returns nothing. Poller run 35230465327 pushed a recovery commit onto `ai/issue-4367` while review run 35226455269 was live and left no trace of why; the fail-open contract is unchanged, the miss is now attributable.

- **The implement retry loop now stops on a deliberate `BLOCKED: <reason>` verdict instead of retrying it five times under a "you must modify files" nudge.**

`prompts/mode-implement.txt` tells the implement model that `BLOCKED: <reason>` is a valid terminal deliverable, but `.github/workflows/implement.yml` treated that answer as an anonymous "returned output but produced no file changes" attempt. Every remaining attempt re-ran with the retry nudge, whose "the implementation plan requires repository modifications" wording contradicts issues that forbid file edits, and the issue received a generic "Codex implement failed after 5 attempts" diagnostics comment. The "Run Codex implementation" step now detects any final-output line starting with optional whitespace followed by `BLOCKED:` when the worktree delta is empty, even when summary text precedes it. It logs the model's reason, writes it to `${RUNTIME_DIR}/codex_blocked.flag`, emits the `Failed` substate, and breaks out of the loop on that attempt, the same shape as the existing `request_user_input` bail. The diagnostics comment on the source issue now opens with "Codex bailed on attempt N: deliberate BLOCKED verdict" so an operator can tell a model verdict from a stuck loop at a glance.

| The numbers that matter | Value |
| --- | --- |
| Motivating run | shubhodeep1/multi-user-ai-agent issue #246, run 33470149029 |
| Attempts spent on one deterministic verdict before this fix | 5 of 5 |
| Tokens spent on those attempts | ~201K |
| Attempts spent after this fix in the motivating case | 1 |

What this means for operators: an implement run that receives a `BLOCKED:` verdict stops on the first attempt that returns it. The issue comment identifies the deliberate verdict and points to the final assistant output captured in the workflow log; the included per-attempt tails contain stderr only. Downstream handling is unchanged: the step still exits 1 and the run takes the existing implementation-failed path, so orchestrator recovery behaves as before, just without four wasted attempts in the motivating cycle.

### For contributors

The check sits in the empty-delta branch ahead of the success-no-op regex (Guard 0, README 10f) and never fires when the attempt also produced real file changes. `tests/test_implement_post_codex_recovery.py::test_codex_blocked_verdict_bail_and_flag` pins the anchor regex, its position relative to the delta check and the success-no-op regex, the flag-then-break ordering, the stale-flag cleanup before the loop, and the `diag_reason` branch; `test_codex_blocked_verdict_regex_matches_line_start_only` exercises the live grep pattern against positive and negative fixtures.

- **The merge train's per-run file-list cache now actually caches, and `curl_gh_api` honours `Retry-After` on secondary rate limits.** Both changes reduce how much of the shared GitHub API budget a single poll tick or review run spends, following the rate-limit alert fired from tele-funtoken-msg-scoring AI Review run 34168241128.

`scripts/review_merge_train.sh` documents a per-run cache of each PR's changed-file list so that a `release` tick evaluating several queued PRs against the same older set fetches every list once. That cache never held: every caller invoked `_mt_pr_files` through a `$(...)` command substitution, which runs the function in a subshell and discards the cache write on return. With `MERGE_TRAIN_MAX_OLDER_PRS` at its default of 20, a tick with Q queued PRs could issue up to Q × 21 paginated `GET /pulls/{n}/files` calls where the number of distinct PRs would do. The file-list, own-files and blocker helpers now have output-variable forms (`_mt_pr_files_into`, `_mt_own_files_into`, `_mt_blockers_for_into`) that run in the caller's shell, and the gate and release paths use them; the original printing names are kept and still fill the cache when called directly.

Separately, the rate-limit branch of `curl_gh_api` in `scripts/gh_helpers.sh` computed its wait only from `X-RateLimit-Reset`, the primary-window reset. GitHub answers secondary rate limits with `Retry-After` in seconds, and the primary reset on that same response can be up to an hour away, so the helper slept the full 600 s cap where GitHub had asked for a few seconds. `_parse_reset_header` now prefers a numeric `Retry-After`, falls back to `X-RateLimit-Reset`, and always returns 0 so it is safe under `set -euo pipefail` outside an `if` / `||` context.

| The numbers that matter | Value |
| --- | --- |
| `GET /pulls/{n}/files` calls per merge-train `release` tick | up to Q × (1 + `MERGE_TRAIN_MAX_OLDER_PRS`) → one per distinct PR |
| Wait on a secondary-limit 403 | up to 600 s → the `Retry-After` value (cap, floor and 30 s fallback unchanged) |
| Test files added to `ci.yml` | `tests/test_review_merge_train.py`, `tests/test_gh_helpers_parse_reset_header.py` |
| Triggering run | tele-funtoken-msg-scoring AI Review run 34168241128, rate limit at 00:32:43Z with the reset 24 s out |

What this means for operators: nothing to configure. Merge-train release ticks in `orchestrate_poll.yml` and `cancel_on_pr_close.yml` spend fewer API calls per queued PR, and a secondary-limit 403 inside any `curl_gh_api` caller resumes after the seconds GitHub asked for instead of ten minutes. Consumer repos pick both up on the next `@stable` sync.

### For contributors

The regression test `test_release_fetches_each_pr_file_list_once_per_run` drives the real script through the fake `gh` and asserts each `/pulls/{n}/files` path appears exactly once in the call log; it fails on the previous code with two fetches of the shared older PR. New callers inside `review_merge_train.sh` must use the `_into` forms; the comment above `_mt_pr_files_into` explains why the `$(...)` form silently defeats the cache. `_parse_reset_header` ignores a non-numeric `Retry-After` (the HTTP-date form) and falls through to `X-RateLimit-Reset`; the `gh_retry` family is unchanged and still reads the reset from `GET /rate_limit`.

- **The merged-PR commit guard no longer goes quiet when GitHub is unreachable, and it now covers the GitHub MCP push tools.** In Claude Code Web sessions where the API proxy answers HTTP 403, the guard used to allow every commit and push with a warning the model never saw; it now decides from git history, and asks a human before any push it cannot prove safe.

Consumer-repo sessions were pushing follow-up commits onto branches whose pull request had already merged. The `.claude/hooks/pr_merge_status_guard.py` hook that enforces `CLAUDE.md` §21 was installed, but in a Claude Code Web session without the GitHub App connected both of its transports (`gh api` and `gh pr list`) fail with HTTP 403, and its fail-open contract turned that into a silent allow. The hook now falls back to the git remote, which works in exactly those sessions: it fetches `origin/<default>` and inspects where the branch forks off it. A branch sitting on merge-commit side history whose remote tip is fully contained in the default branch is blocked with the usual `git checkout -B <branch> origin/<default>` remediation. Anything git cannot settle, including an absent remote ref that could be deleted or never pushed, routes a `git push` through the harness permission prompt so a human confirms the PR is still open; a bare `git commit` is still allowed with a warning, since work only strands once pushed.

Two further gaps closed in the same change. Pushes made through `mcp__github__push_files` and `mcp__github__create_or_update_file` never touched the Bash hook at all; a second `PreToolUse` matcher in `.claude/settings.json` now runs the same guard on them, using the fetched remote branch tip in place of `HEAD`. And in repositories that merge with merge commits, the merged head is an ancestor of the default branch, so the guard's ancestry test blocked the very reset §21.A prescribes; the fork point on the default branch's first-parent chain now tells a rebuilt branch from a stranded one.

| The numbers that matter | Value |
| --- | --- |
| GitHub API calls added by the fallback | 0 (one `git ls-remote`, one `git fetch`) |
| Hook timeout in `.claude/settings.json` | 60s → 90s |
| MCP tools newly guarded | `mcp__github__push_files`, `mcp__github__create_or_update_file` |
| Test file now run by `ci.yml` | `tests/test_pr_merge_status_guard.py` |
| Consumer repos reached on next `@stable` sync | 12 (`.github/ai/consumer_repos.json`) |

What this means for operators: in a session where GitHub is unreachable you will now see a permission prompt on `git push` naming the branch and the reason; allow it only if the PR for that branch is still open. Nothing to install: the hook, its settings wiring, and the updated §21 text reach consumer repos through the existing `workflow-templates/.claude/` mirror in `update_workflows.yml`.

### For contributors

The refinement and the fallback share one primitive, `on_first_parent_chain`, bounded by excluding the candidate's parents from the `git rev-list --first-parent` walk. A stacked branch (forked off another branch that has since merged by merge commit) is reported inconclusive when it has unmerged commits on origin or has never been pushed. For an MCP push whose target repository is not the local checkout, or whose remote tip cannot be fetched for ancestry verification, the guard asks instead of blocking because a block could not self-clear safely. `tests/test_pr_merge_status_guard.py` gained an offline end-to-end fixture with a bare origin reached through `url.<path>.insteadOf`, so fetch and ls-remote run without network, and `ci.yml` now runs the file.

- **review_autofix no longer loses a whole reviewer pass to the shared attempt-prompt race.** Each reviewer slot now copies the pass prompt to its own per-slot attempt file, and an empty effective prompt is restored (or loudly flagged) before codex launches.

In `scripts/review_run_reviewers.sh`, every concurrent reviewer worker used to derive the same `<pass prompt>.attempt_1` path whenever no model-family overlay existed, then `cp`-truncate, nag-append, and sanitize-rewrite that one file while codex read it as stdin. One bad interleaving left the file empty, every reviewer failed non-retryably with `No prompt provided via stdin`, and the review_autofix job failed with "All reviewers failed" even though the assembled pass prompt was intact. Observed on consumer run tele-funtoken-msg-scoring `actions/runs/32222803753` (PR #3721, pass 2: 6 of 6 reviewers failed ~1.5s after launch). The attempt path now embeds the per-slot `safe_name`, and a pre-launch guard restores an unexpectedly empty effective prompt from the base prompt with a `::warning::` instead of failing the slot silently.

| The numbers that matter | Value |
| --- | --- |
| Affected script | `scripts/review_run_reviewers.sh` |
| Failure signature | `No prompt provided via stdin` on every slot of one pass |
| Local race repro (shared path) | 41 of 1200 reads empty |
| Local race repro (per-slot path) | 0 of 1200 reads empty |

What this means for operators: a review_autofix run can no longer fail an entire reviewer pass because parallel reviewer slots raced on one temp prompt file; a Telegram "PR autofix failed" alert from this signature should not recur, and any residual empty-prompt condition now shows up as an explicit `::warning::` in the job log.

### For contributors

`tests/test_review_reviewer_attempt_prompt_isolation.py` pins both halves: the attempt path must embed `${safe_name}`, and the empty-prompt guard must precede the codex launch redirect. `${safe_name}` is `run_reviewer`'s local, visible inside `execute_reviewer_attempt` via bash dynamic scoping (sole call site), matching the existing pattern in `emit_reviewer_substate`.

- **The weekly `AI Security Audit` runs again in consumer repos.** Every scheduled run had been cancelled at startup with zero jobs since the reusable workflow and its consumer wrapper started sharing one concurrency group.

Consumer repos call `security-audit.yml` through the synced `ai-security-audit.yml` wrapper. Both files declared `concurrency.group: security-audit-${{ github.repository }}`, and GitHub treats a called workflow that waits on the group its caller already holds as a deadlock, cancelling the run before any job starts. On `shubhodeep1/tele-funtoken-msg-scoring` runs 34747244353, 34021337015 and 33301125251 all ended that way with the banner "Canceling since a deadlock was detected for concurrency group ... between a top level workflow and 'security-audit'". The reusable workflow now uses `security-audit-reusable-${{ github.repository }}`; the wrapper template is unchanged, so consumers pick the fix up on the next `@stable` sync without a wrapper rewrite.

| The numbers that matter | Value |
| --- | --- |
| Files changed | `.github/workflows/security-audit.yml`, `tests/test_security_audit_workflow_contract.py` |
| Consumer wrapper changes required | 0 |
| Regression test | `test_security_audit_reusable_concurrency_group_differs_from_consumer_wrapper` |

What this means for operators: the Sunday 08:00 UTC audit produces findings issues again instead of a zero-job failure, with no repo-var or wrapper change on the consumer side.

### For contributors

A reusable workflow must never declare the same workflow-level concurrency group as the wrapper that calls it. The contract test pins the two groups apart so a future edit cannot reintroduce the deadlock.

- **Three orchestrator stall-recovery dead ends are closed: clarification auto-answers now parse the option formats Codex actually emits, the editor-changes-lost review recovery fires on every trigger path with a per-head retry bound, and consumer poll runs get the integration-readiness script they call.**

Orchestrator-managed clarifications no longer park for an hour and then discard the model's recommended answers. The `(RECOMMENDED)` parsers in `plan.yml` and in the poller's `extract_recommended_answers` accept bullet-less option lines (`A. text (Recommended)`), and clarification comments are matched by their body marker instead of a `[bot]` author login — pipeline comments are posted with the `GH_PAT` under a human login, so the login filter meant the poller's `auto_respond_clarify` never found the questions at all. On `tele-funtoken-msg-scoring#3754` that combination stalled a financial-payout clarification for 67 minutes and then answered it with "the issue description is deemed sufficient", leaving the planner to invent payout tables.

The `review_autofix.yml` editor-changes-lost recovery was unreachable on `workflow_dispatch` runs, which is every autofix iteration after the first, so a lost editor pass posted "All automated retry attempts have been exhausted" without making any attempt and the PR sat blocked until generic stall recovery re-triggered it about two hours later (`tele-funtoken-msg-scoring#3757`, run 32659591000). The step now keys on the job-level PR number and bounds itself with `autofix_changes_lost_head_retry_consumed` (one branch-scoped list-runs call, fail-closed): one automated retry per head SHA, since a changes-lost run pushes no commit and the head cannot advance.

`orchestrate_poll.yml` also stages `scripts/check_integration_pr_readiness.py`, which `orchestrate_poll_process.sh` already invoked; consumer poll runs previously failed the call with "No such file or directory" and never refreshed the `orchestrator/integration-pr-not-ready` commit status from the poller (run 32657328962).

| The numbers that matter | Value |
| --- | --- |
| Clarification stall before forced auto-answer (#3754) | 67 minutes |
| Review dead-end before generic stall recovery (#3757) | 123 minutes |
| Automated editor-changes-lost retries per head SHA | 1 |
| Extra API calls per changes-lost re-dispatch decision | 1 |

What this means for operators: the two Telegram warnings that motivated this change ("Stall recovery: auto-responded to clarification", "Stall recovery: re-triggered review") should become rare; when a clarification does auto-answer it now carries the model's own recommended `Q1: A` selections instead of "deemed sufficient", and an "Editor changes lost … retry unavailable" PR comment now means the retry budget was unavailable or exhausted instead of silently unreachable.

### For contributors

`tests/test_plan_auto_answer_recommended_parser.py` pins the bullet-less forms (including the verbatim #3754 Q1 block) and the marker-only comment selection end-to-end; `tests/test_editor_changes_lost_redispatch_budget.py` pins the re-dispatch guard and the budget helper; `tests/test_orchestrate_poll_stages_readiness_script.py` pins that every unguarded `python3 scripts/<f>.py` call in the poller is staged.

### Added
- `test-and-mark-stable.yml`: E2E smoke test workflow that exercises all pipeline phases
  (clarify → plan → implement → review/edit) before marking a version stable. Creates a
  test issue, polls each phase to completion, verifies success, cleans up, then proceeds
  with the standard release process. Supports `skip_e2e`, `dry_run`, configurable
  `phase_timeout`, and testing against external repos via `test_repo`.

### Fixed
- Reviewer and editor runs no longer hard-fail when a reviewed PR touches a Jinja/HTML template that contains a standalone double-quoted `{% include "..." %}` line.

  The AI Review and autofix pipelines embed the raw PR diff and the full content of changed files into the reviewer/editor prompt body, then render that body through `scripts/render_prompt.py` with `--skip-syntax-validation` set (the reviewer/editor call sites in `scripts/review_run_reviewers.sh` and `scripts/review_apply_fixes.sh`). That flag already suppressed the strict `{{...}}` syntax gate for embedded diff tokens, but the earlier `{% include "..." %}` assembly pass had no matching guard: a standalone double-quoted include line in a reviewed template matched `INCLUDE_DIRECTIVE_PATTERN`, was treated as a prompt-fragment directive, failed to resolve on disk, and exited the whole reviewer stage with `PromptAssemblyError: Included prompt fragment not found`. Because `render_prompt.py` is staged main-primary (`scripts/stage_workflow_support.sh`), the failure reached consumers pinned to `@stable` too. `render_prompt.py` now skips include resolution for the same already-assembled untrusted bodies the syntax gate already skips, emitting `{% include %}` lines verbatim so the reviewed diff survives into the prompt; include resolution for real prompt templates (rendered without the opt-out) is unchanged, including the hard-fail on a genuinely missing fragment.

  | The numbers that matter | Value |
  | --- | --- |
  | File fixed | `scripts/render_prompt.py` (`assemble_prompt_fragments` / `_assemble_prompt_fragment` gain a `resolve_includes` flag; `main` passes `resolve_includes=not args.skip_syntax_validation`) |
  | Trigger | a standalone double-quoted `{% include "..." %}` line in any reviewed template |
  | Regression tests | `tests/test_render_prompt_foundation.py` — `test_render_prompt_py_skip_syntax_validation_preserves_standalone_include_lines`, `test_render_prompt_py_resolves_standalone_include_for_trusted_templates` |

  What this means for operators: PRs that add or change templates carrying double-quoted `{% include %}` lines now pass AI Review instead of dying in the reviewer stage with an empty editor no-op. No configuration change is needed; the fix ships from `main` via the main-primary staging path.

  For contributors: the single-quoted `{% include '...' %}` form never matched the double-quote-only `INCLUDE_DIRECTIVE_PATTERN`, so it slipped through before this fix — the real defect was scanning the untrusted body for include directives at all, not the quote style, so the regex was left unchanged and the scan is now skipped for untrusted bodies. Surfaced by `shubhodeep1/tele-funtoken-msg-scoring` PRs `shubhodeep1/tele-funtoken-msg-scoring#3548` (`_partials/site_footer.html`) and `shubhodeep1/tele-funtoken-msg-scoring#3549` (`_partials/header-cro-v2.html`).

### Security

- **Check-failure triage now verifies trigger inputs and PR origin before credential-bearing work begins.** Malformed identifiers, unsupported conclusions, invalid SHAs, and fork-origin PRs stop in a credential-minimal prerequisite job.

The reusable `AI Check Failure Triage` workflow now passes check metadata through step environments and references quoted shell variables only. Its read-only prerequisite validates input shape, confirms the PR head repository, and gates the secret-bearing `triage` job on a successful same-repository result. Check names are sanitized and bounded before Actions-log or Telegram display, and fork-skip notices retain the rejected PR and head-repository context for auditability. Existing same-repository triage and concurrency behavior remains unchanged.

What this means for consumer-repo operators: untrusted check-run metadata cannot reach checkout, model execution, or PAT-backed processing until the workflow has validated the trigger and rejected fork-origin PRs.

- **Workflow dispatch inputs no longer enter Bash source code.** Release, validation, refresh, and workflow-log analysis jobs now bind untrusted inputs through step environments and validate them before use.

Direct interpolation previously let shell metacharacters be parsed before the surrounding Bash validation ran. The affected dispatch paths in `comprehensive-test-and-release.yml`, `mark-stable.yml`, `promote-main-to-stable.yml`, `test-and-mark-stable.yml`, `validate.yml`, `validation-refresh.yml`, and `workflow-log-analysis.yml` now reject malformed repository names, numeric values, SHAs, version tags, repository-relative paths, and branch refs with actionable errors. Invalid release timeout values that previously fell back silently now fail the run, while valid inputs, defaults, names, and outputs remain unchanged.

| The numbers that matter | Value |
| --- | --- |
| Workflows hardened | 7 |
| Existing input names changed | 0 |
| Valid-input defaults changed | 0 |

What this means for operators: malformed manual or reusable-workflow dispatch inputs fail before any command or API operation uses them, and valid dispatches continue with the same arguments and outputs.

- **review_autofix now merges only the head it evaluated.** Every auto-merge call owned by the workflow, including deterministic skip, the clean-review helper, and the review-blocked judge, passes `--match-head-commit` so a later push cannot merge under an earlier decision.

On 2026-09-09 and 2026-09-16 two PRs in shubhodeep1/fun-token-multi-chain (#498 and #527) merged into `main` with `ai:review-skipped` and no reviewer run. Each was opened with a diff under the 10/10 line threshold, the gate approved the deterministic skip, and a code push that landed 13 to 19 seconds later was what `gh pr merge --squash --auto` shipped, because the merge was issued by PR number with no head binding. The second one auto-deployed the session-server and the lobby Worker. The `synchronize` run for the real diff correctly refused the skip but found the PR already closed. The workflow now exports the gate's head SHA as the `head_sha` output, `deterministic-skip-merge` binds its merge to it, and `scripts/review_enable_auto_merge.sh` binds the reviewed-path merge through the existing `INITIAL_HEAD_SHA` input. Writable heads refresh `INITIAL_HEAD_SHA` after fetching their live branch; read-only paths retain the commit checked out before their early exit. An unknown or malformed SHA refuses the merge with a `::warning::` rather than issuing it unbound, matching `scripts/review_rb_judge.sh`.

| The numbers that matter | Value |
| --- | --- |
| Merge call sites now head-bound | 9 (`review_autofix.yml` ×2, `review_enable_auto_merge.sh` ×2, `review_rb_judge.sh` ×5) |
| Incident PRs | shubhodeep1/fun-token-multi-chain#498 (+117/−4 merged, +2/−2 evaluated), #527 (+324/−16 merged, +4/−3 evaluated) |
| Tracking issue | #4109 |

What this means for consumer repos: nothing to configure. The fix arrives with the next `@stable` sync of `review_autofix.yml`. A PR whose head moves after the gate ran now stays open with a `Could not enable auto-merge` warning naming the moved head, its linked issues do not receive `ai:ready-to-merge`, and its next `synchronize` event re-evaluates the new diff. `ai:review-skipped` is still applied by the skip job before the merge attempt.

### For contributors

Read-only review paths capture `INITIAL_HEAD_SHA` before their early exit, while the review-blocked judge embeds and binds to its checked-out `RB_JUDGED_HEAD_SHA`. Audit lines: `AUTOFIX_DET_SKIP_MERGE_BOUND pr=<n> head_sha=<sha> action=<squash|merge_commit|refuse>` (skip job) and `AUTOFIX_AUTO_MERGE_HEAD_BOUND ...` (helper). Residual not covered here: when `--auto` enrols instead of merging immediately, the binding covers the enrolment only; GitHub keeps auto-merge enabled across later pushes by write-access users. That is tracked separately in #4109.

## [v1.1.0] - 2026-03-22

### Fixed
- `memory_maintenance.yml`: replaced inline Python with CLI command to fix branch-safe persistence
- `review_autofix.yml`: fixed ghost reference to removed `ai-auto-review-and-edit.yml`
- `ai_pipeline.md`: updated stale workflow name references to current names
- `research/`: updated pre-refactoring workflow names to current names
- `docs/compatibility-matrix.md`: corrected misleading setup-runtime action references
- `cancel_on_pr_close.yml`: fixed 404 due to incorrect workflow reference
- `ci.yml`: fixed JSON validation step failing when `schemas/` dir doesn't exist

### Added
- `ci.yml`: basic CI workflow with YAML lint, Python syntax check, JSON validation, and shell lint
- `CHANGELOG.md`: release tracking per `docs/release-policy.md`
- README quickstart section with full secrets/vars reference and all workflow wrappers
- Serena MCP integration across all workflows
- Internal caller workflows so all AI workflows run on this repo
- Comprehensive repository audit report

### Changed
- Disabled URL previews in all Telegram notifications
- Consolidated `TG_ADMIN_USERID` and `TG_ADMIN_CHAT_ID` into single variable
- Added yamllint config to disable line-length, document-start, and truthy rules
