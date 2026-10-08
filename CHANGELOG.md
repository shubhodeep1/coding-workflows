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

- **Review editor attempts now log one `EDITOR_REVIEWER_CHECKSUM_SUMMARY` warning that counts the reviewer-file hashes the editor miscopied.**

`scripts/review_apply_fixes.sh` already logs `EDITOR_REVIEWER_CHECKSUM_UNVERIFIED` for each reviewer file whose sha256 the editor summary got wrong, and accepts the attempt on the file path and issue audit. Each attempt with at least one mismatch now also logs `EDITOR_REVIEWER_CHECKSUM_SUMMARY attempt=<n> files_checked=<n> checksum_mismatches=<n> validation_ok=<true|false>`. Replaying the three editor attempts from release gate run 35802596362 gives `checksum_mismatches=2`, `2` and `1` out of 6 reviewer files. Nothing that passed or failed before changes outcome.

What this means for operators: search for `EDITOR_REVIEWER_CHECKSUM_SUMMARY` to see how often the editor miscopies hashes, and treat an attempt where every file mismatches as a sign the editor made up its reviewer list rather than read the files.

- **Review/autofix stops retrying a failure that repeats identically on one head.** After three review runs on the same PR head fail with the same fingerprint, the gate ends the next run before any model call and escalates the PR to `ai:review-blocked`.

Every failure comment `review_autofix.yml` posts now ends with a hidden `<!-- review-autofix-failure:v1 head=… reason=… fp=… -->` marker. The fingerprint combines the failure reason with the normalised stderr of the editor and "Collect PR metadata" stages, so run ids, SHAs and temp paths do not change it. The `gate` job counts the trailing markers for the current head and, at `REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL`, skips the run with `skip_reason=fingerprint_cap`. A new `fingerprint-cap-block` job then labels the linked issues `ai:review-blocked` (or the PR when it has none), posts one "AI review/autofix stopped: identical failure repeated" comment, sends an `identical_failure_cap` report to workflow failure heal, and sends a Telegram WARNING. PR #4259 failed seven times this way on one head, spending about 75 minutes of reviewer and consolidator time per run before the editor exited in one second.

| The numbers that matter | Value |
| --- | --- |
| Identical failures that trip the cap (`REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL`) | 3 |
| Extra GitHub API calls in the gate | 0 when the terminal same-head skip already ran, else one `GET /user` plus one paginated comments call |
| Kill switch | `REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED=false` |
| New log prefixes | `AUTOFIX_FINGERPRINT`, `AUTOFIX_FINGERPRINT_CAP_TRIPPED`, `AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED`, `AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED` |

What this means for operators and consumer repos: a PR stuck on a deterministic review failure now lands in `ai:review-blocked` after its third identical failure instead of being re-dispatched by the sweep and stall poller for hours. The review-blocked judge still runs, because `force_rb_judge` dispatches bypass the cap, and pushing a fix resets the count. Consumers receive the cap with the next `@stable` release, with no wrapper, secret or variable change.

- **The Actions pipelines can now run a role on the Claude Code CLI, but nothing uses it yet.** Phase 3 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` adds the engine plumbing with every role still defaulting to codex, so no run changes until a role's cutover.

`scripts/ai_engine.sh` picks a role's engine (the `ai:codex` / `ai:engine-claude` labels, then `AI_ENGINE_<ROLE>`, then `AI_ENGINE`, then the default in `.github/ai/claude_engine.json`) and runs `claude -p` through `claude_run`, which writes the final answer to the same output file the codex path writes. A usage-limited or rejected account moves the run to the next account; when Claude cannot run at all it logs `AI_ENGINE_FALLBACK`, sends one Telegram note per job and returns 75 so the caller runs codex. Every run gets the P5 permission policy from `scripts/claude_settings.json.tmpl`: `gh pr merge`, `gh api … DELETE`, force pushes, remote branch deletes and edits to the checkout's `.github/workflows/**` and `.claude/**` are denied, and `gh_api_write_guard.py` checks every Bash call. The sandboxed clarify and review-editor scripts gain a Claude branch behind `scripts/claude_anthropic_relay.py`, which keeps the OAuth token on the host.

| The numbers that matter | Value |
| --- | --- |
| Pinned CLI | `@anthropic-ai/claude-code` 2.1.289 |
| Roles with an engine switch | 30 (the six reviewer slots have none) |
| Roles defaulting to Claude | 0 |
| Fallback exit code | 75 |
| Context gate | start-up input below 25,000 tokens, `CLAUDE.md` not visible |
| New GitHub API calls | 0 |

What this means for operators: nothing changes on any run yet. `claude-engine-smoke.yml` can be dispatched by hand; until the token broker ships it reports `available=false` and checks the codex fallback. `codex_stall_guard.sh` and `codex_heartbeat.sh` accept `--engine claude`, which only adds `engine=claude` to their log lines, and `scripts/cost_audit.py` totals Claude usage in a new "Claude engine usage" table.

### For contributors

Call sites source `scripts/ai_engine.sh`, call `ai_engine_for_role <ROLE>`, and on `claude` run `claude_run <ROLE> <prompt> <out> <workdir> [session_id]` with `AI_ENGINE_MODEL_HINT` / `AI_ENGINE_EFFORT_HINT` set to the role's existing model and reasoning values; exit 75 means "run the codex command". The account pool is `$CLAUDE_ENGINE_POOL_DIR` (default `$RUNNER_TEMP/claude-pool`) with an `order` file and `tokens/<NAME>` files. `clarify_isolated_run.sh` takes optional `claude <ROLE>` arguments and `review_untrusted_sandbox.sh` takes `prepare claude` and a seventh `claude` argument to `run`; the codex and OpenCode command lines are unchanged. Tests: `tests/test_ai_engine.py`, `tests/test_claude_engine.py`, `tests/test_claude_settings_policy.py`, `tests/test_claude_anthropic_relay.py`, each in its own `ci.yml` step.

- **A token broker now hands Claude account tokens to trusted Actions jobs, and every AI wrapper grants the `id-token: write` permission it needs.** No role uses Claude yet; until the account secrets exist in `shubhodeep1/claude-workers`, the broker answers `503 pool_empty` and every role stays on codex.

Phase 4 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` adds the `claude-pool-broker` Cloudflare Worker (source `tools/claude-pool-broker/`, deployed at `https://claude-pool-broker.shubhodeep.workers.dev`). It verifies the caller's GitHub OIDC token (signature, issuer, audience `coding-workflows-claude-pool`, expiry, at most 10 minutes old), requires `repository_owner` `shubhodeep1`, a repository that is coding-workflows or listed in `.github/ai/consumer_repos.json`, and a `job_workflow_ref` under `shubhodeep1/coding-workflows/.github/workflows/`, and refuses anything else with `403` and a reason code. The new `.github/actions/claude-pool-token` action fetches the pool once per job, masks every token, probes each account with Haiku 4.5, writes the accounts under the `0.9` gate (least used first) to `$RUNNER_TEMP/claude-pool` for `scripts/ai_engine.sh`, and deletes them in its post step. It never fails a job: any problem is `available=false` with a reason, and the job runs codex.

| The numbers that matter | Value |
| --- | --- |
| Wrapper templates gaining `id-token: write` | 11 `workflow-templates/*.yml` |
| coding-workflows callers and workflows gaining it | 14 |
| OIDC token maximum age | 10 minutes |
| Consumer registry cache in the Worker | 10 minutes, last good copy kept |
| New GitHub API calls | 0 |

What this means for consumer repos: the next `@stable` sync adds `id-token: write` to the AI wrappers; nothing else changes until a role's cutover. Repos whose sync is stale keep running codex.

### For contributors

`scripts/claude_pool_token.sh` does the work for the Node 24 action (`main.js` / `post.js`); the OIDC and pool tokens never reach argv. `.github/ai/claude_engine.json` `broker_url` now points at the Worker. The account secrets (`CLAUDE_POOL_TOKEN_<NAME>`) and `CF_BROKER_DEPLOY_TOKEN` live only in `shubhodeep1/claude-workers`, whose `claude-pool-key-sync.yml` writes the Worker's `CLAUDE_POOL_TOKENS` secret; `tests/test_claude_pool_token.py` fails if any coding-workflows workflow or template mentions them. Worker tests run with vitest in `ci.yml`.

- **`ai-orchestrate.yml` takes an optional `engine` input, and the project's engine label now follows its work to every issue and PR.** Dispatching with `engine=claude` puts the whole project on Claude Opus 5.5 at `high` effort once its roles are cut over; `engine=codex` pins it to codex.

Phase 6 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` propagates `ai:engine-claude` / `ai:codex` through project work; role cutovers are a separate Phase 5 task. The orchestrator turns its `engine` input into a label on the tracking issue and the wave-1 issues, and rejects any value other than empty, `claude` or `codex` before running the decomposer. The poller copies the tracking issue's engine label onto every issue and PR it creates (all 15 creation sites), and `implement.yml` copies an issue's label onto its PR. When both labels are present, `ai:codex` wins and is the one copied. `/implement-plan-claude` dispatches with `engine=claude`; against a wrapper without the input it dispatches without it and labels the tracking issue afterwards, so wave-1 issues created before that write do not inherit the label.

| The numbers that matter | Value |
| --- | --- |
| Poller issue and PR creation sites that copy the label | 15 |
| Accepted `engine` values | empty, `claude`, `codex` |
| New GitHub API calls | 0 |

What this means for operators: `gh workflow run ai-orchestrate.yml -f engine=claude …` starts a Claude project in one step, with no labelling afterwards. Leaving `engine` empty behaves exactly as before.

### For contributors

New log prefixes: `AI_ENGINE_PROJECT_LABEL` (orchestrate) and `AI_ENGINE_PR_LABEL` (implement). The poller helper `engine_label_create_args` reads `TRACKING_LABELS` (or a labels JSON argument) and prints `--label <name>` lines for `mapfile`; it prints nothing on an empty or unreadable list. `tests/test_engine_label_propagation.py` covers the mapping, every creation site, early input validation and the helper, and runs in its own `ci.yml` step. The consumer permission guard now recognizes vetted literal-ID read loops without prompting; unvetted loops remain undecided and writes still ask. Reusing an existing implementation PR applies the issue's engine label or fails the run.

- **Blocked work no longer waits for a person.** An unblock judge now picks up every issue, pull request and orchestrator project that stopped at a point that needed a human, and gets it moving again or closes it with a report.

Once per poll tick, `orchestrate_poll_process.sh` searches for items carrying a block label (`ai:blocked`, `ai:needs-human`, `ai:scope-blocked`, `ai:destructive-blocked`, the `ai:*-failed` labels, the escalated triage, heal and resolver labels) and for failed projects, and dispatches `unblock_judge_dispatch.yml` for the oldest ones. The judge (`scripts/unblock_judge.sh`, role UNBLOCK_JUDGE) reads the evidence and picks one verdict: retry with a narrower fix, answer the open question, descope, override the scope or bulk-delete guard, reissue, accept with a follow-up, record an operator step, or close. The verdict is carried out with the commands a person would use (`/revalidate`, `/re-security-pass`, `/judge_resume`, `/approved`, `/answer`, `/reclarify`, a review dispatch). Hard limits are enforced in code. The judge never repeats a verdict for the same failure, never waives a security finding or a failed validation, and never extends an issue's scope to `.github/**`, `.claude/**` or `workflow-templates/**` in any repository, or `scripts/**` in this repository.

| The numbers that matter | Value |
| --- | --- |
| Judge runs started per poll tick | at most 1 (`UNBLOCK_JUDGE_MAX_DISPATCH_PER_TICK=0` disables dispatch) |
| Time blocked before the judge looks | 30 minutes (`UNBLOCK_JUDGE_MIN_BLOCKED_MINUTES`) |
| Time between verdicts on one item | 6 hours (`UNBLOCK_JUDGE_RETRY_HOURS`) |
| Rounds per item / per project | 2 / 6 |
| Still blocked after the last round | closed after 24 hours |
| Fix-up wait before deciding again | 72 hours (`UNBLOCK_JUDGE_FIXUP_WAIT_HOURS`) |
| Isolated model timeout | 1500 seconds (`UNBLOCK_JUDGE_TIMEOUT_SECS`; invalid values fall back to 1500) |
| New label | `ai:unblock-closed` |
| New wrapper | `workflow-templates/unblock_judge_dispatch.yml` (standard and full profiles) |

What this means for operators: blocked items resolve themselves or end closed with `ai:unblock-closed` and one Telegram CRITICAL. If the close succeeds but applying that label fails, the CRITICAL alert still fires so a closed item is not lost without notice. If a project's label write fails, a CRITICAL alert reports that closure is pending while it remains blocked. A closed project is marked `abandoned` and its tracking issue closed. Steps only you can take arrive in the `ai:operator-step` issue, with the work kept off behind a flag until you act. You can still act on any blocked item yourself. Set `UNBLOCK_JUDGE_ENABLED=false` to turn the judge off.

This release also adds activation verification after default-branch merges and orchestrator project completion. `scripts/activation_verify.sh` grades merged work `LIVE` or `DORMANT`, files code gaps for the normal pipeline, and records human-only gaps in the repository's single `ai:operator-step` issue. The model reads merged code without GitHub or Telegram credentials; `ACTIVATION_VERIFY_ENABLED=false` disables the check.

### For contributors

Three give-up exits that used to end in a Telegram alert alone now hand over to the scan.
- **Project judge.** `JUDGE_OUTPUT_FAILURE_MAX` (default 3) consecutive runs with no usable output fail the project with `ai:blocked`.
- **Merge deferrals.** The first time `MAX_MERGE_DEFERRALS` is reached, the PR gets `ai:needs-human`.
- **Review-blocked judge.** Its terminal skips (`llm_failed`, `json_parse_failed`, `missing_followup_details`, `merged_pr_unsafe_action`, `auto_merge_disabled`) add `ai:needs-human` to the PR.
- **Workflow-failure heal.** An escalation with no prior issue labels the failure report itself.

Malformed pipeline-authored project fix-up requests are skipped with an `UNBLOCK_PROJECT` diagnostic naming the comment ID, rather than disappearing silently from the poll log.

`override_guard` on the destructive latch leaves a one-shot `override=bulk_delete` marker. `implement.yml` spends it on the issue's next run, and only when no canonical workflow source is among the deletions. A failed review dispatch retains the PR's block label; failed prerequisite writes prevent later resume actions and success notifications. If posting a standalone fix-up's wait marker fails, the judge closes that new issue instead of leaving an untracked open fix-up (and logs a close failure for recovery). An issue must carry `ai:orchestrator-managed` and appear in the project's V2 state before its body can route a fix-up into that project; unavailable state defers the verdict. A PR reissue creates its replacement before closing the original PR so close-event cleanup cannot cancel the replacement. Parsed model verdicts redact the OpenRouter key before GitHub writes, including when the configured key has surrounding whitespace; an incomplete 30-comment window defers dispatch until a paginated REST history read verifies one rotating candidate per tick, so an older wait marker refreshed in place cannot be missed. The selection and action logic is pure Python (`scripts/unblock_scan.py`, `scripts/unblock_ledger.py`, `scripts/unblock_actions.py`). `tests/test_unblock_judge.py` and `tests/test_unblock_scan.py` run in their own `ci.yml` step.

Activation verification uses the ACTIVATION_VERIFY role from `prompts/mode-activation-verify.txt`. A merge that closes an activation-fix issue is not verified again, preventing recursive fix issues. Operator-step updates append keyed comments rather than patching a shared issue body; the highest-id trusted comment for a key is current, and earlier comments and legacy body sections remain as history. If a new tracker's label listing stays stale, the writer stops instead of posting to an unverified duplicate; `tests/test_activation_verify.py` covers the verifier and operator-step writer.

Issue and project resume commands are posted before their block label is removed, except `/approved`: the block label is removed before posting that command so the implementation gate sees no guard label. If posting fails, the judge restores the block label for the next scan even when another actor removed it first (HTTP 404), and alerts CRITICAL if restoration also fails.

The merged-PR guard also keeps numeric push refspecs in its branch check when output is redirected, while still recognizing adjacent unquoted file descriptors. Env-wrapped commits whose working directory cannot be resolved ask for confirmation instead of checking a different checkout.

Project state and reset commands now verify their comment authors: only the authenticated pipeline account can write state, and only that account or a repository-associated human can reset a failed project. The implement workflow also uses a collision-checked random `GITHUB_ENV` delimiter for issue text, so an issue containing `EOF` cannot inject job variables.
When the pipeline account cannot be verified, the poller pauses project processing and sends one CRITICAL Telegram alert per tick if configured.

- **Standalone PRs now get a security audit before they auto-merge.** After a clean review, a PR into the default branch waits until a security audit of its head comes back clean, the same gate an orchestrator project already passes.

Port P1 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` (Phase 8a). Eligible standalone PRs take the review path even for doc-only or tiny diffs, so the deterministic-skip job cannot enable auto-merge before the security pass. The new "Single-issue security pass" step in `review_autofix.yml` runs right before "Enable auto-merge on PR" and holds both merge steps until the authenticated pipeline account has posted a clean marker for the current head. If none exists, it dispatches `security-audit.yml` (consumers: `ai-security-audit.yml`) for the PR's head branch with the new `pr_number` input. The audit posts its result only for the live PR head and re-runs the review on clean or failed; disabled or summary-less audits report failed, not clean. Findings become follow-up issues that target the PR branch, so their merges start the next cycle. The pass skips orchestrator child and integration PRs, fork heads, `e2e-smoke-test` PRs, and PRs with exactly one linked issue when that issue is a verified automation follow-up (`scripts/security_pass_skip.py`), so follow-ups never recurse without exempting mixed-issue changes.

| The numbers that matter | Value |
| --- | --- |
| Audit cycles per PR | 5 (`MAX_SECURITY_PASS_CYCLES`) |
| A pending audit is treated as lost after | `SECURITY_PASS_PENDING_STALE_HOURS` (default 6 hours) |
| New repository variable | `SINGLE_ISSUE_SECURITY_PASS_ENABLED`, default `true` |
| New `security-audit.yml` / `ai-security-audit.yml` input | `pr_number` |
| New log prefix | `SINGLE_ISSUE_SECURITY_PASS` |

What this means for operators: a standalone PR merges only after its own audit is clean, with no waiver path. If posting an audit result fails, the audit does not re-dispatch review without a persisted marker; the pending marker stays in place until a later review event retries the stale audit. A PR still failing after five cycles is labelled `ai:security-pass-failed` for the planned Phase 7 unblock judge; until Phase 7 ships, it remains held. Until a consumer's `ai-security-audit.yml` wrapper is synced with the `pr_number` input, the dispatch fails and the PR merges as before, with a warning. Set `SINGLE_ISSUE_SECURITY_PASS_ENABLED=false` to turn the pass off.

### For contributors

`scripts/review_single_issue_security_pass.sh` has two modes: `gate` (review job; it reuses the PR payload and comments the job already fetched) and `report` (the new "Report single-issue security pass" step in `security-audit.yml`, which reads the audit's summary line from the tee'd run log). The project-level security-exhaustion judge works on orchestrator project state, so a single PR goes straight to the unblock judge on exhaustion. `tests/test_single_issue_security_pass.py` runs in its own `ci.yml` step.

- **Standalone issues no longer wait an hour for their clarify questions to be answered.** `clarify.yml` now answers a standalone issue's questions as soon as it posts them, with each question's RECOMMENDED option, and logs every pick in one `<!-- ai:auto-decisions:v1 -->` comment that the PR body repeats.

Port P3 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` (Phase 8b). The new "Standalone auto-decide" step runs right after "Post clarification questions" on any issue that is not `ai:orchestrator-managed`. It posts the answer through the orchestrator's existing poster, `scripts/orchestrate_parse_and_post_answer.sh`, so the same `/answer [auto-answered-by-orchestrator]` comment, `ORCHESTRATOR_MAX_CLARIFY_CYCLES` loop guard and `ai:blocked` escalation apply. Each pick becomes an `AD-<n>` entry (question, pick, why, alternatives) in the issue's auto-decisions comment, which later clarify cycles edit in place, and `implement.yml` copies the entries into the PR body under "Auto-decisions" with issue references broken up. Clarify reuses one paginated comment fetch for the full decision history and backup loop guard while limiting prompt context to the oldest 50 comments; a failed fetch stops clarification before an automatic answer. The step is skipped after a human `/reclarify`, when any question lacks a RECOMMENDED option, or when `STANDALONE_AUTO_DECIDE_ENABLED` is `false`; the 60-minute stall-ladder auto-answer stays as the backstop.

| The numbers that matter | Value |
| --- | --- |
| Time from posted questions to `/answer` | same clarify run (was the 60-minute stall threshold) |
| New repository variable | `STANDALONE_AUTO_DECIDE_ENABLED`, default `true` |
| Extra GitHub API calls per clarify round | 2 writes (the answer and the AD comment), 0 additional read operations; the existing comment read now paginates (one call per page) |
| New log prefix | `STANDALONE_AUTO_DECIDE` |

What this means for operators: a standalone issue moves from clarify to plan without waiting, and every decision the pipeline took for you is listed on the issue and in its PR. Post `/reclarify` to see the questions again and answer them yourself; set `STANDALONE_AUTO_DECIDE_ENABLED=false` to restore the previous wait.

### For contributors

`scripts/auto_decisions.py` (`parse`, `render`, `pr-section`) accepts the same RECOMMENDED bullet drift as `extract_recommended_answers` and the plan.yml parser, and trusts only comments by an `OWNER`/`MEMBER`/`COLLABORATOR` or a `[bot]`. `tests/test_auto_decisions.py` covers the parser, the AD numbering, the trust and delimiter rules, the PR-body section and the workflow wiring, and runs in its own `ci.yml` step.

- **Merged work is now checked for whether it will actually run.** After a PR merges into the default branch, and when an orchestrator project completes, an activation verifier grades the work LIVE or DORMANT and acts on every gap it finds.

Port P4 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` (Phase 8c). The new `activation-verify` job in `issue_pr_status.yml`, and the poller at every project completion path, run `scripts/activation_verify.sh` with the ACTIVATION_VERIFY role (`prompts/mode-activation-verify.txt`, built from the activation scope of `/verify-activation`). The verdict is posted on the linked issue or the tracking issue. Gaps that code in the repository can close become one standalone issue that the normal pipeline implements. Gaps only a person can close (a secret, a repository variable, a release) go to the repository's single `ai:operator-step` issue, and the project keeps going. The model reads the merged code read-only and gets no GitHub or Telegram credentials.

| The numbers that matter | Value |
| --- | --- |
| Verdicts | `LIVE`, `DORMANT` |
| New repository variables | `ACTIVATION_VERIFY_ENABLED` (default `true`), `ACTIVATION_VERIFY_MODEL`, `THINKING_LEVEL_ACTIVATION_VERIFY` |
| New label | `ai:operator-step` |
| Model time limit at project completion | 15 minutes |
| New log prefix | `ACTIVATION_VERIFY` |

What this means for operators: watch the `ai:operator-step` issue. It lists, per merge or project, the exact steps only you can take, and each step names the flag that keeps its feature off until you do. Everything else is fixed by the pipeline. Set `ACTIVATION_VERIFY_ENABLED=false` to turn the check off.

### For contributors

`scripts/operator_step_issue.py upsert` is the only writer of the `ai:operator-step` issue (one keyed section per source, replaced in place; the unblock judge's `operator_step` verdict reuses it). It reconciles observed duplicate trackers, but the issue-body PATCH is not atomic across concurrent writers and can still lose an entry. Failed code-gap searches leave a non-terminal comment instead of creating an unverified duplicate issue. Completed-project poll ticks retry only a trusted partial verdict while fewer than three partial comments exist and within 30 minutes of the first; failed comment writes may cause another attempt in that window but cannot keep the model running indefinitely. A merge that closes an activation-fix issue is not verified again, so fixes never loop. `tests/test_activation_verify.py` runs in its own `ci.yml` step.

- **Work stacked on a branch whose PR already merged now goes to that PR's base.** `implement.yml` and the review gate stop targeting a finished branch.

Port P6 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md` (Phase 8d). GitHub retargets a stacked PR on its own only when the merged branch is deleted. When the branch is kept, new work and open PRs used to keep pointing at it. The new `scripts/retarget_merged_base.sh` treats an existing branch as finished only when a merged PR has it as its head and the branch tip is still that PR's head commit, so a branch name reused for new work is never touched. A confirmed deleted head ref follows its merged PR's base too. `implement.yml` maps the issue's integration branch through it before checkout, and the review gate retargets an open PR whose base is such a branch with one `PATCH`.

| The numbers that matter | Value |
| --- | --- |
| New repository variable | `RETARGET_MERGED_BASE_ENABLED`, default `true` |
| Hops followed | up to 3 (`RETARGET_MERGED_BASE_MAX_HOPS`) |
| API calls when the base is the default branch | 0 |
| API calls per hop otherwise | 2 reads, plus 1 `PATCH` when a PR is retargeted |
| New log prefix | `RETARGET_MERGED_BASE` |

What this means for operators: a fix-up issue or PR that would have landed on a branch nobody merges any more lands on the live base instead. Deleted merged branches also resolve to their former base; transient ref lookup failures keep the original branch. After a PR base changes, review runs against the new base rather than trusting earlier skip or resume state; legacy partial markers without a base reference cannot suppress review. A missing optional helper checkout skips retargeting with a warning, while an identity mismatch fails the gate. Set `RETARGET_MERGED_BASE_ENABLED=false` to turn both paths off.

### For contributors

The review gate gets the helper through a sparse checkout from the same protected commit as `codex-agent`, verified like the fingerprint-cap helper, and reads the base from the PR fetch it already makes. `implement.yml` runs the helper from the staged clone that already provides `resolve_integration_ref.sh`. `tests/test_retarget_merged_base.py` uses a fake `gh` and runs in its own `ci.yml` step.

- Track Semble bootstrap time and context contribution, including unused bootstraps and rejected cross-run telemetry echoes.

- **Interactive Claude Code sessions now read OpenRouter spend themselves with the `OR_MGMT_KEY` management key (CLAUDE.md §29).** Questions like "what did the review panel cost this month" get answered from OpenRouter's own numbers, not from list-price estimates.

When a task needs OpenRouter data, the session calls the management API directly, without asking first: `GET /api/v1/activity` for per-model daily cost and tokens, `GET /api/v1/keys` for per-key spend totals, and `GET /api/v1/credits` for account credit. Creating, deleting, disabling, or limiting keys and any billing change still need the user's approval in the §2 Q/A format. The key lives only in the session environment. No Actions workflow reads it, and §29.D forbids adding one that does. `README.md` lists it next to the other session-only credentials.

| The numbers that matter | Value |
| --- | --- |
| `/activity` history | last 30 completed UTC days (older dates return HTTP 400) |
| `/activity` granularity | day × model × provider endpoint, account-wide |
| `/keys` `usage_monthly` | current calendar month, not a rolling 30 days |

What this means for operators: add `OR_MGMT_KEY` to the Claude Code session environment to enable this. Without it, sessions say so once and carry on. The management API's account-wide totals do not provide per-workflow or per-repo splits; cost is pinned to a pipeline role only when that role is the sole user of a model slug. Some direct requests set `HTTP-Referer` or `X-Title` (`scripts/analyze_soft_errors.py`, `scripts/summarize_unselected_runs.py`), but those headers do not add a workflow or repo breakdown to the management totals.

- **Consumer repositories receive the pipeline's secrets automatically.** A new `Propagate consumer secrets` workflow copies `CHECK_TRIAGE_ISSUES_TOKEN`, `GH_PAT`, `OPENROUTER_API_KEY` and `TG_BOT_SECRET` from this repository into every consumer the moment its registration lands in `.github/ai/consumer_repos.json`.

Until now a `/seed-repo` run ended with a manual checklist: an operator had to open each new consumer's Settings → Secrets page and paste four values, and the check-failure triage posting token was not even on that list. `.github/workflows/propagate-consumer-secrets.yml` runs on every push to `main` that changes the registry and targets only the entries that push added; `scripts/propagate_consumer_secrets.sh` writes each secret through `gh secret set` with the value on stdin, then confirms the names with `gh secret list`. A `workflow_dispatch` with an empty `targets` input backfills every registry entry once for the consumers registered before this workflow existed. Targets outside the registry are refused, an empty library secret is skipped with a warning, and any failed or unverified write leaves the run red with a Telegram CRITICAL so the workflow-failure heal intake picks it up.

| The numbers that matter | Value |
| --- | --- |
| Secrets copied per consumer | 4 |
| Trigger path | `.github/ai/consumer_repos.json` on `main` |
| Required `GH_PAT` scope on consumers | `repo` (unchanged; the `@stable` dispatch already needs it) |
| Tests | 18 in `tests/test_propagate_consumer_secrets.py` (own `ci.yml` step) |

What this means for operators: issue the triage posting token once with "All repositories" access, store it in this repository, and never visit a consumer's secrets page again; the seed checklist now says so.

### For contributors

Log keys are `CONSUMER_SECRETS_PROPAGATE repo=… secret=… status=<set|skipped_empty|failed|verify_missing>`, `repo=… status=verify_list_failed` when the post-write listing fails, plus a `summary` line. `gh_retry` re-invokes a small helper that pipes the value afresh on every attempt; retry diagnostics echo only the command words, never the stdin value. The workflow has no concurrency group (a replaced pending run would lose the consumers its push added) and diffs the registry only against the push's own `before` tip, targeting every entry when that commit is outside the shallow checkout.

### For contributors

The marker is computed by `scripts/workflow_failure_heal.py autofix-failure-fingerprint`, and the gate count by `autofix-identical-failure-count`. Only markers written by the account `GH_PAT` authenticates as are trusted, several comments from one run count once, and an editor summary or a failure comment without a marker ends the scan. The gate checks out only `scripts/workflow_failure_heal.py` (sparse) and prefers the copy that carries the subcommand, so self-repo branches forked before this change use the main snapshot. `workflow_failure_heal_autofix_report.sh` now honours an explicit `AUTOFIX_FAILURE_REASON` and carries the marker's fingerprint as the optional `failure_fingerprint` payload field.

- **Workflow failure heal now routes a self-inflicted review/autofix failure to the branch that caused it.** When a coding-workflows pull request breaks its own review run, the diagnosis is posted on that PR instead of opening a heal issue. When the PR's base branch introduced the break, the heal issue targets that base branch.

The autofix heal reporter (`scripts/workflow_failure_heal_autofix_report.sh`) now sends the PR's base branch, the script ref the run staged, the PR's changed files, and the crash file named by the run's stderr (a `…/scripts/<name>: line N:` shell error or an `::error::` line naming a `scripts/` or `.github/workflows/` path). For a report from this repository, the intake (`scripts/workflow_failure_heal_intake.sh`) decides who changed the crash file before the model runs. It checks the PR's own diff first, then `git diff --name-only origin/main origin/<base>` on its checkout. It logs `WORKFLOW_HEAL crash_ownership=<pr|base|none>` and passes the facts to the diagnosis prompt, which gains two tokens. `pr-self-inflicted` posts the diagnosis on the PR under "Workflow failure heal: this failure is caused by this pull request's own changes" and opens no issue. `base-self-inflicted` opens the `ai:workflow-heal` issue with `Target branch:` set to the base branch. For an `orchestrator/project-<N>` base, that issue also carries `Tracking issue: #N`, `Integration branch:`, `Refs #N` and the `ai:orchestrator-managed` label.

| The numbers that matter | Value |
| --- | --- |
| New classification tokens | `pr-self-inflicted`, `base-self-inflicted` |
| New payload fields (optional, `autofix_failure` only) | `base_branch`, `script_ref`, `changed_files` (at most 200 paths of 120 characters), `crash_file` |
| New GitHub API calls | 0. The PR diff comes from the report and the base diff from two depth-1 `git fetch`es |
| Kill switch | `WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED=false` |

What this means for operators: a PR like #4259, whose own change broke the editor guard, gets its diagnosis as a PR comment that the review-blocked judge can act on. An integration-branch defect like the one on `orchestrator/project-4139` lands as a child issue of that project. Neither case produces a fix aimed at `stable`. A token the computed ownership does not back is logged as `classification_remapped … to=workflow-defect` and takes the existing `workflow-defect` route. Consumer reports are not affected.

### For contributors

`scripts/workflow_failure_heal.py` adds `extract_crash_file`, `classify_crash_ownership` and the `classify-crash-ownership` subcommand, plus `build-autofix-payload --base-branch / --script-ref / --changed-files-file` and `compose-issue --integration-branch`. The reporter sends the new flags only when the staged helper advertises `classify-crash-ownership`, so an older helper still gets its report through. The reporter's evidence now includes the `failure_evidence_tail.txt` stage stderr that "Assemble failure evidence" writes, which is where the crash line appears. The base comparison is a tree diff between two depth-1 fetches, so it needs no merge base. It is skipped when the base is `main` or `WORKFLOW_HEAL_TARGET_BRANCH`.

- **Lessons learned now reach the prompts that plan, implement and review, and the orchestrator writes a lessons retrospective when every project completes, clean ones included.**

Lesson records from the judge, the review-blocked judge and review autofix were saved to the `ai-memory` branch but never read: memory retrieval only loaded candidate and canonical records. The planning, implementation and reviewer prompts now get a `LESSONS LEARNED` block with up to 5 of the newest lessons whose text or tags match the issue, and the block uses at most a quarter of the role's memory budget. The orchestrator also stops learning only from failures. `scripts/orchestrate_poll_process.sh` records causes as they happen: a judge fix-up issue, a validation `needs_fixes` diagnosis, reported security findings, and a stall recovery. When the project completes, it writes one lesson per cause type, plus a summary of the recovery, stall, review-blocked, validation and security counters. The wave judge skips clean projects, but this retrospective still runs for them; a project with nothing to report writes nothing.

| The numbers that matter | Value |
| --- | --- |
| Roles that see lessons | `planning`, `implementation`, `reviewer` |
| Lessons per prompt | at most 5, newest keyword matches first |
| Budget share | at most 25% of the role's token budget, only when lessons match |
| Causes kept in orchestrator state | newest 20 `lesson_events`, 400 characters each |
| New GitHub API calls | none |
| Kill switches | `AI_MEMORY_ENABLED`, `LESSONS_LEARNED_ENABLED` (default `true`) |

What this means for operators: planners and reviewers now see what earlier projects learned, and each orchestrator project adds a short record of what went wrong on the way to done. A lesson whose text trips the memory prompt-injection patterns is never shown to a prompt. Set `LESSONS_LEARNED_ENABLED=false` in the job environment to turn lessons off.

### For contributors

Retrieval lives in `retrieve_memory_context` (`scripts/ai_memory_lib.py`). With no matching lesson its output is unchanged, and the retrieve JSON and telemetry gain `lessons_selected`. Completion lessons are built by `orchestrate_lib.build_completion_lessons` and written by `emit_orchestrator_completion_lessons` on all four `status = "complete"` paths. They use phase `orchestrator_completion`, kind `project_retrospective` and deterministic record ids, so a repeated completion tick writes nothing.

- **Review/autofix now checks the editor's preconditions before the reviewers run.** A run whose staged `scripts/review_apply_fixes.sh` would fail a required-variable guard now fails in the preflight step within seconds, instead of after a full reviewer and consolidator pass.

The "Preflight: Verify required files before reviewer invocation" step of `review_autofix.yml` now runs `review_apply_fixes.sh --preflight` after its file checks. The new `review_apply_fixes_preflight()` function checks every `: "${VAR:?…}"` guard the editor script declares, the OpenCode helpers and config writer, the `opencode` binary, and write access to `RUNTIME_DIR`, with no model or network call. It logs one `REVIEW_EDITOR_PREFLIGHT check=<name> result=<ok|fail>` line per check and a final `REVIEW_EDITOR_PREFLIGHT result=<ok|fail> checks=<n> failed=<m>`. A failure sets `EDITOR_PREFLIGHT_FAILED=true` and takes the ordinary failure path: failure comment with its fingerprint marker, `ai:review-blocked` on the linked issues, and a heal report with `failure_reason=editor_preflight_failed`. `scripts/stage_workflow_support.sh` also logs `::notice::STAGE_MAIN_PINNED_DIVERGENCE script=<name> script_ref=<ref>` when a main-pinned support script's branch copy differs from the `main` copy it stages instead.

| The numbers that matter | Value |
| --- | --- |
| Time to fail on a broken editor guard | seconds, before reviewers (PR #4259 spent about 75 minutes per run, seven runs) |
| New failure reason | `editor_preflight_failed` |
| New log prefixes | `REVIEW_EDITOR_PREFLIGHT`, `STAGE_MAIN_PINNED_DIVERGENCE` |
| New GitHub API calls | 0 |
| Kill switch | `REVIEW_EDITOR_PREFLIGHT_ENABLED=false` |

What this means for operators: a deterministic editor precondition failure, like the unset `OPENROUTER_API_KEY` on PR #4259, now costs one short run per head, and the identical-failure fingerprint cap stops the retries after three. The preflight is skipped (`skip reason=unsupported`) when the staged editor script predates `--preflight`, and (`skip reason=editor_not_scheduled`) in Claude-branch review mode or a terminal resume, where the editor does not run.

### For contributors

Adding a `: "${VAR:?…}"` guard to `scripts/review_apply_fixes.sh` now requires the same name in `preflight_required_vars` inside `review_apply_fixes_preflight()`; `tests/test_review_autofix_review_pipeline_contract.py` fails otherwise. The variable must be set when the preflight step runs, because that step sees the job env plus the editor step's explicit env entries (`GH_TOKEN`, `REPOSITORY`, `TOOL_CALL_BUDGET_JUDGE`). A second contract test compares each `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` entry with its `origin/main` copy and fails when the branch copy writes a `${RUNTIME_DIR}/…` or `${…_FILE}` output the main copy does not (the PR #4273 class). It fetches `origin/main` only when the ref is missing and skips with a warning when it cannot.

- **`/implement-plan-claude` stages now use allowlisted helpers for workflow dispatches and comment edits, and every permission prompt a session hits is logged and filed as an issue.** The command also requires Auto mode and stops before any phase that must edit `.claude/**`.

Stage sessions used to find a dispatched run with `gh run list -L 1`, which can return the previous run, so they wrote polling loops with `sleep` and `$(gh api ...)`. They also edited the progress comment with `gh api ... > $S/body.md && python3 - <<'EOF' ... && gh api -X PATCH`. Both shapes stopped unattended sessions at permission prompts. Three helpers replace them, each allowed by an exact rule in `.claude/settings.json`: `.claude/scripts/dispatch_workflow.py` dispatches an allowlisted workflow and prints the id of the run it started, `.claude/scripts/edit_comment.py` edits one comment in place, and `.claude/scripts/permission_prompts.py` reports prompts. A new hook, `.claude/hooks/permission_prompt_logger.py`, records every permission prompt and Auto-mode denial. At the end of each stage, `permission_prompts.py file` lists them in the report and, in coding-workflows, files each new pattern as an `ai:permission-prompt` issue that clarify routes to the Claude issue implementer. `/implement-plan-claude` step 0 now refuses to start outside Auto mode, and a phase that edits `.claude/**` stops at `Status: BLOCKED` and asks how to run it, because Claude Code never auto-approves those edits.

| The numbers that matter | Value |
| --- | --- |
| Workflows `dispatch_workflow.py` accepts | 6, the same files allowed as `gh workflow run <file> *` |
| Wait for the dispatched run | polls every 5 s, up to 90 s |
| Issue per prompt pattern | 1, later occurrences comment on it; no cap on open issues |
| Command text kept in an issue | 2,000 characters, heredoc bodies removed, token-like strings masked |
| Repos that file issues | coding-workflows only; the 13 consumers in `.github/ai/consumer_repos.json` log and report |

What this means for operators: run `/implement-plan-claude` from a session in Auto mode, or it stops at step 0 and asks you to switch. Expect `ai:permission-prompt` issues in coding-workflows whenever a stage still hits a prompt; each starts a Claude implementation project whose fix usually edits `.claude/`, so it will stop and ask you before that phase. Close an issue as not planned when the prompt is by design (a protected-path edit or an ask-first operation). Plans that edit `.claude/**` now need you once per such phase: answer in the blocked stage whether to run it in a session you watch.

### For contributors

CLAUDE.md §23.I documents the helpers and reports, and §28.C the protected-path stop. The logger writes `~/.claude/permission-prompts/<session id>.jsonl`, never decides, and makes no API calls. `permission_prompts.py` keeps `filed-state.json` next to the logs so a later run in the same session files only new occurrences, and dedupes against existing issues by the `<!-- ai:permission-prompt:v1 sig=<sig> -->` marker. The `ai:permission-prompt` label is in `.github/ai/label_contract.v1.json` and `scripts/label_helpers.sh`. Tests: `tests/test_dispatch_workflow.py`, `tests/test_edit_comment.py`, `tests/test_permission_prompts.py`, in one `ci.yml` step.

- **Pushed pull requests now get a Sonnet check-in, and `/implement-plan-claude` runs every stage in its own fresh session through to `/verify-activation` and `/deploy-activate`.** A new CLAUDE.md §26 has every interactive session start a small Sonnet checker for each PR it pushes, and the same checker drives `/implement-plan-claude` from phase to phase without ever waking the expensive session.

Until now a session pushed a branch, opened its pull request, and went quiet, and `/implement-plan-claude` waited on each phase with a Sonnet Routine that, it turns out, could not act: sessions started by a Routine with `create_new_session_on_fire` get no MCP tools and no repository, so its checker could never start the next step. Both now use a Sonnet session started with `create_session`, which does get the repository, `gh`, and the claude-code-remote tools. It runs `.claude/scripts/check_in_status.py` every 3 hours (re-armed with `send_later`), and the script, not the model, decides whether the PR merged, closed, got blocked, or is stuck. For a §26 check-in the checker writes the terminal report itself from next steps the pushing session gave it, renames itself `PR #<n> merged — …`, and sends one push notification. For `/implement-plan-claude` it starts the next stage session, titled `implement-plan <slug> — phase 2/4` (or `security-pass`, `validation`, `verify-activation`, `deploy-activate`), which archives the previous stage unless that one is waiting on you. After the completion PR merges the command runs `/verify-activation`, loops on its fix PRs, and starts `/deploy-activate` in its own session when the verdict is DORMANT. A 24-hour safety net restarts a stalled chain.

| The numbers that matter | Value |
| --- | --- |
| Check-in interval | 180 minutes |
| Checker model | `claude-sonnet-5`, in Auto mode |
| GitHub REST calls per check | 1 in terminal-only mode; non-terminal checks add 1 per 100 check runs and up to 3 for an old failure |
| "Stuck" threshold | conflict or failed check, head older than 6 hours, no workflow run active |
| Safety net | one wake of the last stage session after 1440 minutes, only if the chain stalled |
| Verify-activation cycles before asking | 3 |
| New `CLAUDE.md` section | §26 |
| Pull request | #4304 |

What this means for operators: run `/implement-plan-claude` in Auto mode (the command asks for it) and leave it; the session list shows one open session per plan, named after its current stage, and you are notified when a stage starts, when the project is LIVE, or when `/deploy-activate` is waiting for you at Step 1. After any other push, the Sonnet checker tells you when the PR lands and what is left. Neither touches CI or review comments on its own. `.claude/settings.json` now pre-approves the tools these flows call, so sessions stop asking at start-up.

### For contributors

- `.claude/scripts/check_in_status.py` (new, mirrored to `workflow-templates/.claude/scripts/`) takes `--pr N [--terminal-only]`, `--run ID`, or `--issues a,b`, reads over REST only, prints one JSON line, and exits 2 on a failed read; `tests/test_check_in_status.py` runs in its own `ci.yml` step.
- `permissions.allow` adds file edits, `claude/*` pushes, `gh` REST and run reads, the four security-audit / validate dispatches, the GitHub MCP and claude-code-remote tools, `CronCreate` / `CronDelete` / `PushNotification`, and the helper. `permissions.ask` keeps `gh api` writes behind a prompt. The claude-code-remote write tools (`send_later`, `create_session`, `archive_session`, the trigger tools) ask on every call outside Auto mode whatever the allowlist says, which is why stage and checker sessions need Auto mode and the checker runs on Sonnet.
- `.claude/hooks/pr_check_in_reminder.py` is a `PostToolUse` hook under the anchored matcher `^(?:Bash|mcp__.*__create_pull_request|mcp__.*__push_files|mcp__.*__create_or_update_file)$`; its reminder text now points at the Sonnet checker session.

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

- **The security audit now opens an `ai:security` follow-up issue for every new finding.** The limit of 3 follow-up issues per UTC week is gone, so no finding is silently deferred.

`scripts/security_audit.sh`, which runs weekly from `.github/workflows/security-audit.yml` in this repo and in every consumer through `workflow-templates/ai-security-audit.yml`, used to stop filing follow-ups once 3 `ai:security` issues with a finding marker existed for the current UTC week. It then logged the rest on the tracker as "Findings deferred by the weekly cap". Deferred findings were never filed later: on 2026-09-24 run 35996690244 surfaced 5 findings on tracker #3576, opened #4398, #4399 and #4400, and left 2 that had to be filed by hand as #4431 and #4432. Every finding that passes the confidence gate and the exclusion catalog now gets its own issue unless an `ai:security` issue, open or closed, already carries its `<!-- ai:security-finding:<id> -->` marker. The existing-issue lookup now reads every `ai:security` issue through one paginated REST listing instead of the first 200. Without the cap, a repo can pass 200 such issues, and a truncated list would re-file old findings.

| The numbers that matter | Value |
| --- | --- |
| Follow-up issues per week | was at most 3, now one per new finding |
| Existing issues read for the duplicate check | was the first 200, now all (1 REST call per 100 issues) |
| Tracker comment lines removed | "Existing follow-up issues this UTC week", "Findings deferred by the weekly cap" |
| Repos affected | this repo and the 13 consumers in `.github/ai/consumer_repos.json`, on the next `@stable` sync |

What this means for operators: expect one `ai:security` issue per distinct finding after each audit, including weeks with more than three. There is no knob to bring a cap back. `SECURITY_AUDIT_ENABLED=false` still turns the audit off for a repo. Findings deferred before this change are not filed retroactively. They show up again only if a later audit re-reports them.

- **The `CI` workflow's `lint` job may now run for 60 minutes instead of 45.**

The single `lint` job in `.github/workflows/ci.yml` grew to 40–45 minutes. On 2026-09-28, runs on `main` were cancelled at the 45-minute limit while every test was still passing. That left pull requests marked "unstable" and made clean Claude-fixer reviews miss their ready check snapshot. This is a stopgap until the job is split into parallel jobs.

| The numbers that matter | Value |
| --- | --- |
| `lint` `timeout-minutes` | 60 (was 45) |
| `main` runs cancelled at 45 minutes | 36367681221, 36368393442 |

What this means for operators: `CI` completes again instead of being cancelled near the end. A full run still takes about 45 minutes until the split lands.

- **`CI` now runs as parallel jobs instead of one 40–45 minute `lint` job.** The aggregate status is still called `CI / lint`, and it fails when any job fails.

`.github/workflows/ci.yml` used to run about 120 steps one after another in a single `lint` job. On 2026-09-28 that job was cancelled on `main` at its 45-minute limit while every test was still passing. The steps now run in parallel jobs: `static-checks`, four test jobs that each take a slice of the old step order, and a four-group `orchestrate-poll` matrix. The final `lint` job needs all of them and runs with `if: always()`. It fails unless every job succeeded, so a failure or cancellation can never read as a skipped, passing check. No test was dropped, and no step body changed except the orchestrate-poll split. The release gates' `validate-scripts` jobs in `mark-stable.yml` and `test-and-mark-stable.yml` keep one runner, and their budget goes from 45 to 60 minutes.

| The numbers that matter | Value |
| --- | --- |
| Old `lint` job | 1 job, 40–45 minutes; `timeout-minutes: 45`, raised to 60 by the #4706 stopgap |
| New CI jobs | `static-checks` (15 min budget), 4 test jobs (20 each), `orchestrate-poll` × 4 groups (20 each), `lint` aggregate (5) |
| Orchestrate-poll split | 4 matrix groups × `CI_POLL_TEST_SHARDS` (default 4) local shards |
| First split run (run 36523765261) | 9.0 minutes wall-clock; critical path `orchestrate-poll (0)` at 8.7 minutes, slowest test job `tests-promote-stall-and-review` at 8.2 minutes |
| Release `validate-scripts` | 37 minutes measured (run 36374918973); budget 45 → 60 minutes |

What this means for operators: CI results arrive in about 9 minutes instead of most of an hour. That shortens every Claude-fixer review round, and clean reviews are more likely to find a ready check snapshot within `CHECK_RUNS_WAIT_TIMEOUT_SECS`. The Checks tab shows each job separately; `CI / lint` still summarises them.

### For contributors

Add a new CI step to one of the existing jobs. A new job must also go into the `lint` job's `needs` list, which `tests/test_ci_job_split_contract.py` enforces. `tests/test_ci_poll_test_sharding.py` checks that the group split and the local shard split, alone and together, run every orchestrate-poll test exactly once.

- **The `gh api` permission guard now approves narrowly scoped read-only loops over literal IDs.** A complete loop can inspect `gh api`, `gh run`, or `gh pr` results without an unattended permission prompt; writes and unvetted commands remain subject to the existing safeguards.

- **The `gh api` permission guard now denies a call that passes jq's own command-line options to `--jq`, instead of stopping at a permission prompt.** The deny reason says how to fix the command, so an unattended session corrects it in the same turn.

On 2026-09-28 an unattended session stopped at a prompt for `gh api "…/runs/$r/jobs?per_page=50" --jq --arg r "$r" '<program>'` (#4891). `gh api` has no `--arg`: `--jq` took `--arg` as its program, and `gh` would have rejected the call before sending any request. `.claude/hooks/gh_api_write_guard.py` read it as an unreadable call, treated it as a write, and asked. The guard now returns `permissionDecision: deny` when a `-q`/`--jq` value matches `^--?[A-Za-z]`, and the reason tells the session to put the value into the jq program, pipe the output to `jq` with its own options, or wrap a program that starts with a minus sign in parentheses. CLAUDE.md §23.H documents the new outcome.

| The numbers that matter | Value |
| --- | --- |
| Forms caught | `--jq <v>`, `--jq=<v>`, `-q <v>`, `-q<v>` |
| Value pattern | `^--?[A-Za-z]` (`--arg`, `-r`, `--raw-output`, `-c`) |
| Still allowed | `--jq '-.size'`, `--jq -1`, `--jq '(-.size)'` |
| Precedence | deny, then ask, then allow; the unparseable-command and hidden-call asks run first, unchanged |

What this means for operators: sessions no longer wait for a human on a `gh api` call that could never work. Every other guard decision is unchanged, and a deny runs nothing, so no permission is widened.

### For contributors

The check is a new `MalformedJq` subclass of `Unreadable` raised in `parse_gh_api_args`; `evaluate` collects it before the generic write path. `tests/test_gh_api_write_guard.py` replays the #4891 command verbatim and covers each form, the valid programs above, and the precedence. The hook is mirrored byte-for-byte under `workflow-templates/.claude/hooks/`.

- **The `gh api` permission guard now approves a single read-only REST call in a double-quoted `echo` substitution.** Literal-ID `for` loops may use their variable in the endpoint path; unsafe substitutions still ask, with a fixed example of a separate read command.

- **The review panel drops `x-ai/grok-4.20` for `openai/gpt-6-luna`, swaps `google/gemini-3.1-flash-lite` for `google/gemini-3.8-flash`, and every reviewer attempt now has a hard 120-turn cap and a repeated-tool-call stop.** A reviewer can no longer loop on one tool call for hours.

Three `x-ai/grok-4.20` reviewer passes in `coding-workflows` looped on a single repeated tool call: 2,205 turns and 746M tokens (run 35949371968), 1,468 turns and 663M tokens (run 36483245451), and 234 turns and 123M tokens (run 36522631293). That came to about $311 in five days, and the first two ran until the 2-hour `HEARTBEAT_MAX_WALL` kill because every call looked like progress to the idle watchdog. Across 11,463 reviewer slots from Sep 22 to Sep 29 in `coding-workflows`, `digital_pa`, `fun-token-multi-chain`, and `tele-funtoken-msg-scoring`, real reviewer passes peaked at 101 turns. The watchdog in `scripts/review_run_reviewers.sh` now reads each attempt's OpenCode JSON event stream: an attempt that starts more than `REVIEWER_MAX_STEPS` turns is killed and its slot fails with no retry or failback, and `REVIEWER_TOOL_REPEAT_LIMIT` identical consecutive tool calls (same tool, same input) end the attempt as a retryable `tool_repeat` failure. Grok leaves every default roster: the six-reviewer panel, the `standard` review-tier default, and `opencode-live-smoke.yml`.

| The numbers that matter | Value |
| --- | --- |
| `REVIEWER_MAX_STEPS` default | 120 turns (highest real pass: 101) |
| `REVIEWER_TOOL_REPEAT_LIMIT` default | 10 identical consecutive calls |
| Largest Grok loop | 2,205 turns, 746M tokens |
| Grok 4.20 reviewer spend at OpenRouter list prices, Sep 22 to Sep 29 | $758 ($311 of it in the three loops) |
| Estimated cost per reviewer call, Grok 4.20 slot to `gpt-6-luna` | about $0.52 to about $0.07 |
| New failback chains | `gpt-6-luna -> gpt-5.6-luna`, `gemini-3.8-flash -> gemini-3.1-flash-lite` |

What this means for operators: a looping reviewer now costs at most one 120-turn attempt instead of up to two hours of turns, and the panel continues with the remaining reviewers. Override `vars.REVIEWER_MAX_STEPS` or `vars.REVIEWER_TOOL_REPEAT_LIMIT` per repo if needed. A repo whose `vars.REVIEW_TIER_STANDARD_REVIEWER_SLUGS` still names a Grok slug gets a full-panel review instead of the `standard` subset, because the resolver fails open when a slug is not on the panel; drop Grok from that variable to keep the three-reviewer tier. The Grok catalog and failback entries stay in place.

### For contributors

OpenCode's own agent `steps` setting is deliberately unused: in 1.18.23 it only injects a "maximum steps reached" instruction and keeps offering tools with `tool_choice: auto`, which a looping model ignores. The repeat check compares full tool inputs, so paged `read` calls on one file with different offsets never match. A check against the stderr permission log, which records only the path, would have stopped 24 normal attempts in the same week. The guards apply to the review panel only; the judge, consolidator, consensus summariser, and smoke reviewer-role callers are not capped. `google/gemini-3.8-flash` gets a new catalog entry.

- **Reduced shared GitHub PAT traffic and made failed clarification routing recover automatically.** High-volume jobs report start/end PAT quota snapshots (shared-budget deltas are estimates), and the poller batches clean-PR reads and skips draft Claude PRs. Review watchdogs and comment pagination avoid redundant requests. A trusted failed `/reclarify` is requeued by the existing poller only after the PAT budget recovers; the failure path creates the discovery label if it has not yet been synced.

The existing Workflow Log Analysis collector now publishes a ranked hourly summary of the previous UTC day's measured jobs at 06:00 UTC in the `gh-pat-budget-day` artifact and Actions job summary. Missing logs, unknown deltas, and truncated run listings are flagged; no historical ranking is invented before a day of instrumented runs exists. Operators can consider separating low-volume routing from high-volume review credentials if that measured report shows persistent contention; this change keeps one PAT and does not modify repository secrets.

- **A reviewer slot that fails for infrastructure reasons no longer blocks a clean `claude/*` review round.** When at least half of the active reviewer panel returns `success` and those reviewers report no findings and no task gaps, the round is clean and auto-merge proceeds; before, one failed slot handed the whole PR to a Claude session.

In Claude-fixer mode the consensus ledger used to carry the failure notice of every reviewer slot that failed (non-retryable error, output-token cap, empty output, retryable-failure limit) or was skipped for an unmapped model. The summariser turned that notice into a `FINDINGS FROM <slot>` block that was not `(No findings reported.)`, so the round counted as not clean and cost a Claude fix session. `scripts/summarize_reviewer_consensus.sh` now skips every reviewer input whose `status_<prefix>_<slot>.txt` is not `success`; an input with no status file is kept as before. `scripts/review_autofix_step_claude_fixer_handoff.sh` then requires `REVIEWERS_SUCCESSFUL * 2 >= active` before it calls a round clean, and logs `CLAUDE_FIXER_PANEL_FLOOR successful=<n> active=<m> floor_met=<true|false|unknown>`.

| The numbers that matter | Value |
| --- | --- |
| Clean-round floor | at least 50% of the active panel returned `success`, rounded up |
| 6 active reviewers | 3 successes needed |
| 5 active reviewers | 3 successes needed |
| Active panel size | larger of the `status_review_*.txt` count and the `reviewer_active_models.txt` line count |
| Workflow files changed | 0 (`review_autofix.yml` is untouched) |

A reviewer slot skipped for budget is not covered on its own. When it is the pass's only non-success slot, the runner still requests a partial finalize before any consensus ledger is written, so the hand-off fails closed and the round goes to the Claude session, as it did before. Beside a hard failure it is dropped like the others.

What this means for operators: `claude/*` PRs whose reviewers hit a rate limit or a token cap should auto-merge when enough of the panel still reviewed them clean, with no Claude fix session. A real finding or task gap from any successful reviewer still blocks, and so does a round below the floor. When the active panel size cannot be read (`floor_met=unknown`) the floor is not applied.

### For contributors

The summariser change applies to every caller, not only Claude-fixer mode: the pass-1 cross-pollination ledger and the pass-2 ledger that feeds the GPT editor and the memory-record step no longer include failure notices for non-`success` slots. The old `failed after retries` filter is kept. `tests/test_review_autofix_claude_fixer_mode.py` runs the real `run_reviewer_pass` to pin the budget-skip boundary. Tests: `tests/test_review_autofix_claude_fixer_mode.py` and `tests/test_summarize_reviewer_consensus_prompt.py`.

- **New standalone issues go to the Codex pipeline by default again.** With `AI_ISSUE_IMPLEMENTER` unset, or set to anything other than `claude`, clarify now routes a new issue to clarify → plan → implement instead of the Claude issue queue.

This is Phase 0 (freeze) of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md`, which retires the claude.ai-session-driven Claude automation. Before this change, `scripts/claude_issue_route.py` defaulted to `claude`, so every new issue without a label landed in the `ai:claude-issue-queue` that the retired sessions used to drain. Now the default is `codex`, and an invalid value also falls back to `codex`. An `ai:claude` label or `AI_ISSUE_IMPLEMENTER=claude` still routes an issue to Claude while that path exists. Orchestrator-managed issues, tracking issues and `[E2E …]` fixtures stay on Codex, as before.

| The numbers that matter | Value |
| --- | --- |
| Default of `AI_ISSUE_IMPLEMENTER` | `codex` (was `claude`) |
| Fallback for an invalid value | `codex` (was `claude`) |
| New GitHub API calls | 0 |

What this means for operators: new issues no longer wait on the Claude pickup, and repos that set `AI_ISSUE_IMPLEMENTER=codex` see no change. A repo that wants the old behaviour sets `AI_ISSUE_IMPLEMENTER=claude`, but Phase 2 of the same plan removes the Claude queue entirely.

- **Clarify, clarify-respond and plan now run on Claude by default.** These are the first roles cut over from codex to the Claude Code CLI, with codex kept as the automatic fallback.

The CLARIFY role in `clarify.yml`, the CLARIFY_RESPOND role in `orchestrate_clarify_respond.yml` (the answer, the self-critique and the revision) and the PLAN role in `plan.yml` (through `scripts/run_plan_codex.sh`) now default to Claude Opus 5.5 at each role's existing reasoning level. Each job first resolves the role's engine. It installs the Claude CLI and fetches an account from the token pool only when that engine is Claude. When Claude cannot start (no credential, every account at its usage limit, the CLI missing, or a clarify image build failure), the same attempt runs the unchanged codex call and the rest of the job stays on codex. Clarify keeps its sandbox: the Claude CLI runs inside the same container, and the real token stays on the host.

| The numbers that matter | Value |
| --- | --- |
| Roles moved to Claude | `CLARIFY`, `CLARIFY_RESPOND`, `PLAN` |
| Model on Claude | `claude-opus-5-5` (a role variable starting with `claude-` overrides it) |
| Roles still on codex | every other role, until Phases 5b–5d |
| Exit code that falls back to codex | `75` (logged `AI_ENGINE_FALLBACK role= reason=`) |

What this means for operators: clarification questions, orchestrator answers and implementation plans are written by Claude from now on. To put one role back on codex, set the repository variable `AI_ENGINE_CLARIFY`, `AI_ENGINE_CLARIFY_RESPOND` or `AI_ENGINE_PLAN` to `codex`. `AI_ENGINE=codex` does it for every role, and an `ai:codex` label does it for one issue. No code change is needed.

### For contributors

The defaults live in `.github/ai/claude_engine.json`; the code defaults in `scripts/claude_engine.py` stay `codex`, so a missing config file still means codex everywhere. The codex commands are unchanged: `tests/test_plan_codex_step_extraction.py` runs `run_plan_codex.sh` against a fake `claude_run` and a fake `codex`, and `tests/test_plan_clarify_blocked_output.py` runs the clarify retry loop with a stand-in sandbox, each checking the Claude branch, the exit-75 fallback and the exact codex call.

- **Implement, implement-repair and implement-diagnose now run on Claude by default.** The implementation editor and its two recovery roles join clarify and plan on the Claude Code CLI, with codex kept as the automatic fallback.

In `implement.yml`, the IMPLEMENT role (the main editor), IMPLEMENT_REPAIR (the syntax and validation repair passes) and IMPLEMENT_DIAGNOSE (the post-failure diagnosis in `scripts/implement_diagnose_post_codex_failure.sh`) now default to Claude Opus 5.5 at each role's existing reasoning level. A new "Resolve AI engine" step resolves all three roles once per job. It installs the Claude CLI and fetches an account from the token pool only when at least one of them is on Claude. Retries keep their context: `scripts/codex_thread_reuse.sh` resumes the role's Claude session across attempts, the way it resumes a codex thread today. When Claude cannot start (no credential, every account at its usage limit, the CLI missing), the same attempt runs the unchanged codex call.

| The numbers that matter | Value |
| --- | --- |
| Roles moved to Claude | `IMPLEMENT`, `IMPLEMENT_REPAIR`, `IMPLEMENT_DIAGNOSE` |
| Model on Claude | `claude-opus-5-5` (a role variable starting with `claude-` overrides it) |
| Roles still on codex | orchestrator, judges, review, validate and utility roles, until Phases 5c–5d |
| Exit code that falls back to codex | `75` (logged `AI_ENGINE_FALLBACK role= reason=`) |

What this means for operators: implementation PRs, repair commits and fix-up issue diagnoses are written by Claude from now on. To put one role back on codex, set the repository variable `AI_ENGINE_IMPLEMENT`, `AI_ENGINE_IMPLEMENT_REPAIR` or `AI_ENGINE_IMPLEMENT_DIAGNOSE` to `codex`. `AI_ENGINE=codex` does it for every role, and an `ai:codex` label does it for one issue. No code change is needed.

### For contributors

`codex_thread_reuse_direct_run` branches to `codex_thread_reuse_claude_direct_run` when `CODEX_THREAD_REUSE_ENGINE=claude`. The Claude session UUID lives in `states/claude-<key>.session` next to the codex thread state and is dropped after a failed run, so the next attempt starts a fresh session. `tests/test_codex_thread_reuse_core.py` and `tests/test_implement_post_codex_recovery.py` run both call sites against a fake `claude_run` and a fake `codex`, each checking the Claude branch, the exit-75 fallback, a Claude crash that does not fall back, and the unchanged codex call.

- **The orchestrator decomposer, its judges and the review write roles now run on Claude by default.** `CLAUDE_FIXER_ENABLED` is the switch for the review side again: `false` keeps the review editor, consolidator, conflict resolver and review-blocked judge on OpenCode.

`orchestrate.yml` decomposes projects with Claude Opus 5.5 (ORCHESTRATE). The poller's wave, stall, integration, security-pass and review-blocked judges in `scripts/orchestrate_poll_process.sh` also run on Claude, and each judge reads its project's `ai:engine-claude` / `ai:codex` label from the cached tracking-issue labels without an API call. The poller's review-blocked judge runs in the credential-free review sandbox, read-only for verdicts or with validated transfer for combined fixes. In `review_autofix.yml`, the editor runs the Claude Code CLI inside the same network-isolated sandbox, and the consolidator, conflict resolver and review-blocked judge also use the isolated review runner. When Claude cannot start, the decomposer uses its unchanged Codex command while poller judges retry isolated OpenCode and review roles use their OpenCode fallback. A review editor whose Claude sandbox image fails to build uses the OpenCode image instead of failing the job.

Ephemeral OpenCode judge and conflict-resolver sandboxes build without the Claude CLI or engine support files. A missing Claude dependency cannot prevent an explicitly OpenCode-selected attempt or a fresh isolated OpenCode retry after Claude is unavailable; failed OpenCode isolation still never falls back to a credentialed host agent. The integration judge diagnoses conflicts read-only and dispatches the existing resolver, which reads its PR-head- and default-branch-tip-bound diagnosis as untrusted advisory context; an unconfirmed guidance comment defers dispatch without consuming budget. If a resolver is already running, the poller skips the judge call without spending another lifetime dispatch. Combined review-blocked fixes use validated sandbox transfer. A commit wrapped in `env -C` now requests confirmation rather than checking the wrong checkout when the target directory cannot be resolved.

| The numbers that matter | Value |
| --- | --- |
| Roles moved to Claude | `ORCHESTRATE`, `WAVE_JUDGE`, `STALL_JUDGE`, `INTEGRATION_JUDGE`, `SECURITY_JUDGE`, `REVIEW_EDITOR`, `REVIEW_CONSOLIDATOR`, `CONFLICT_RESOLVER`, `RB_JUDGE` |
| `CLAUDE_FIXER_ENABLED` default | `true` (Claude-fixer mode on) |
| Roles still on codex | validate, security audit, triage, workflow heal, log analysis and the utility roles, until Phase 5d |
| Exit code that falls back | `75` (logged `AI_ENGINE_FALLBACK role= reason=`) |
| Sandbox progress line | `CLAUDE_ENGINE progress role=REVIEW_EDITOR transcript_bytes=<n>`, every `REVIEW_SANDBOX_PROGRESS_SECS` (60) when the transcript grew |

What this means for operators: project decomposition, judge verdicts, review fixes, consolidated findings and conflict resolutions are written by Claude from now on. To keep the review side on OpenCode, set the repository variable `CLAUDE_FIXER_ENABLED` to `false`. It wins over the `ai:engine-claude` label and `AI_ENGINE`. To move one role back, set `AI_ENGINE_<ROLE>` to `codex`, for example `AI_ENGINE_WAVE_JUDGE=codex`. PR labels are honored even on dispatch-triggered reviews; when a PR's labels cannot be verified, its review write roles stay on OpenCode. The poll job installs the CLI and fetches the account pool only on ticks with active projects, including those whose `ai:engine-claude` label overrides a global codex setting. Rejected poller judge transfers remove only newly untracked files; failed sandbox cleanup or an unverifiable removal stops that poll tick before another issue can stage those files. The sandbox relay drains valid, bounded rejected POST bodies before responding, with a one-second limit for clients that stop uploading.

### For contributors

`scripts/claude_engine.py` applies `CLAUDE_FIXER_ENABLED=false` first in `resolve_role` (`source=var:CLAUDE_FIXER_ENABLED`), so every call site honours it. `claude_run` gains `AI_ENGINE_READ_ONLY=true`, which narrows a write role to the read tool profile. The review-blocked judge's verdict pass uses it to match the OpenCode `reviewer` role, and the switch never widens a role. The engine files ride `OPTIONAL_BOOTSTRAP_SCRIPTS` in `scripts/stage_workflow_support.sh`, so a support checkout without them keeps every review role on OpenCode. Tests: `tests/test_orchestrator_judges_claude_engine.py` (new) covers the poller helper, the five judge sites and the decomposer. `tests/test_review_autofix_claude_fixer_mode.py` is rewritten for the switch and the four review sites, and `tests/test_ai_engine.py` and `tests/test_claude_engine.py` cover the read-only switch and the resolver order.

The security-pass cap now terminalizes on a high, critical or unrated `keep_fixing` finding once the extra fix-cycle budget is spent, instead of waiving it or creating unlimited fix issues. Low/medium findings can still become advisories. Location-based waiver suppression requires the same exploit scenario and severity, not just a nearby line and category; legacy waivers without a scenario suppress by exact ID only. Standalone stall judges select from their target issue's labels, and the poller's review-blocked judge uses the linked PR's already fetched labels. The decomposer Claude launcher strips GitHub and OpenRouter token environment variables before starting its isolated engine.

- **CLAUDE.md §25 now allows a scheduled pull request status check when the user asks for one.** Interactive sessions still never subscribe to PR activity and never act on a PR's CI or review activity unprompted.

Since §26 retired on 2026-10-03, §25 read as banning every scheduled look at a PR, including an hourly check the user explicitly requested ("check hourly and get these PRs to completion"). §25.B now bans only polls the user did not ask for. §25.C allows a requested check through `send_later` or a Routine. The check acts only within the request, under plain §12, is never armed unprompted, and stops when the PR merges or closes or when the user says to stop. The `subscribe_pr_activity` ban and `.claude/hooks/pr_watch_guard.py` are unchanged.

What this means for consumer repos: sessions there follow the same rule after the next `@stable` sync, because `workflow-templates/CLAUDE.md` points at this file.

- **Standalone issues now get their clarification questions answered by the Claude clarify-respond worker, and nobody is paged for questions the pipeline answers itself.** Security-audit findings, workflow heals and ordinary issues use the same `CLARIFY_RESPOND` worker as orchestrator-managed issues, and credentials or setup steps become placeholders instead of a stop.

Until now a standalone issue's questions were answered by picking each question's RECOMMENDED letter. That pick could not check GitHub state, and it posted dead answers when the RECOMMENDED option asked a human to "provide" something. `clarify.yml` also sent the `🚨 CRITICAL` "Clarification required" Telegram alert after its own auto-decide step had already answered, which is what happened on issue #6262 (run 37251621451). Now `clarify.yml` hands the questions to `orchestrate_clarify_respond.yml` and skips the alert. The worker answers from the repository plus a GITHUB FACTS block, the state of the PRs, issues, branches and runs the issue references, read on the host before the network-isolated model runs. A credential, token, account or other setup the work needs is decided as an UPPER_SNAKE_CASE placeholder secret or variable: the code reads it with no default and skips or fails closed until it is set. The placeholder is listed under "Setup required" in the `<!-- ai:auto-decisions:v1 -->` comment and in the PR body. If the worker fails, each question's RECOMMENDED option is posted instead.

The `core` install profile now includes the issue-comment responder wrapper, so its default delegation does not silently leave standalone questions unanswered. Existing core-profile repositories receive the wrapper on their next automatic workflow sync.

| The numbers that matter | Value |
| --- | --- |
| Clarification questions surveyed (19 Apr to 5 Oct 2026, excluding the release-gate fixture) | 136 on 72 issues |
| Of those, answerable from the repository or GitHub state | 125 (92%) |
| Of those, needing a credential, identity or external evidence (now placeholders or fetched facts) | 11 (8%) |
| RECOMMENDED options that asked a human to provide data | at least 6 |
| New repository variable | `STANDALONE_CLARIFY_RESPOND_ENABLED`, default `true` |
| GitHub API calls added per clarify-respond run | 1 GraphQL call plus at most 5 run reads; standalone mode adds 1 paginated comment read |

What this means for operators: the "Clarification required" alert now means a human is really needed. It comes from the worker as `WARNING` when it escalates (an issue with no stated intent) or its loop guard blocks, and as `CRITICAL` only when the worker fails and a question has no RECOMMENDED fallback. Placeholders to provision are listed under "Setup required" on the issue and in its PR; the feature that needs one stays off until you set it. Post `/reclarify` to answer an issue's questions yourself (the worker then stays away), or set `STANDALONE_CLARIFY_RESPOND_ENABLED=false` to go back to RECOMMENDED-only answers.

### For contributors

`orchestrate_clarify_respond.yml` "Check orchestrator metadata" now outputs `mode` (`orchestrator`, `standalone` or `skip`) and `respond`; `is_orchestrator` is unchanged and every later step gates on `respond`. Plan-stage questions on standalone issues are not answered by the worker (their comment does not start with `<!-- ai:clarification-questions -->`). `scripts/auto_decisions.py` gains `from-answers` and `SETUP-<n>` items; `scripts/clarify_github_facts.py` is new. `prompts/mode-clarify.txt` keeps `BLOCKED:` only for auth-walled content the task depends on. The workflow now also stages `clarify_data_provision_guard.py`, which consumer repositories never received, so their data-provision guard had been skipped silently. Tests: `tests/test_clarify_respond_standalone.py`, `tests/test_clarify_github_facts.py`, `tests/test_auto_decisions.py`.

Standalone clarify-respond bypasses the semantic answer cache because its key excludes live GitHub facts. Orchestrator-managed responses keep the cache path.

- Skip the roughly 30-second Semble setup on clarify, plan, orchestrate and clarify-respond runs that have no Semble query.

- Bootstrap Semble only at the first task-text query in implement, review, poll and validate jobs; retain eager mode as a rollback.

- Oversized targeted files now use read-tool markers by default instead of repeated off-file Semble chunks; opt in with `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED=true` and eager bootstrap.

- **Review sandbox rejections now say which rule fired, without printing the path.** Unsafe directories emit `reason=unsafe_directory`, a fixed `category=` token and bucketed `depth=`. Annotated snapshot, refresh and transfer failures emit fixed reason tokens; unannotated errors retain their legacy reason or `unknown`. No previously refused directory, symlink or file is accepted now. Before this change, the failure behind issue #6424 (PR #6288, run 37264822053) could not be traced to a rule.

  | Field | Values |
  |---|---|
  | `category=` | `symlink`, `invalid_name`, `dot_github_subtree`, `env_like`, `sensitive_name`, `key_material_suffix`, `excluded_name_variant`, `other` |
  | `depth=` | `1`, `2`, or `3+` path segments |

  What this means for operators: the review/autofix failure headline and workflow-failure-heal fingerprint distinguish the rejection class while keeping the sandbox-controlled path out of logs. The line's prefix, exception name and exit codes are unchanged.

  For contributors: `UnsafeWorkspaceDirectory` records only the fixed category and depth bucket that `main()` emits.

- **`CLAUDE.md` and the shipped slash commands no longer describe retired machinery.** `/deploy-activate` now runs Cloudflare steps itself under `CLAUDE.md` §24, the way it already ran DigitalOcean steps under §22.

A prompt audit found several commands that contradicted the repository. `/implement-plan-ai` called `/implement-plan-claude` an in-session implementer, but it is the Claude-engine orchestrator hand-off. `/implement-issue-claude` said the engine label stays inert until Phase 6, which has shipped. `/analyze-log` and `/investigate-issue` treated `gh auth status` as the auth check, which §23 says fails behind the Claude Code Web proxy. `/write-plan` sent implementation to `/investigate-issue` instead of the orchestrator. Commands that load project context now read the relevant sections of `README.md` and `agents.md` and no longer re-read `CLAUDE.md`, which every session already loads, and the log commands name whichever GitHub workflow-log tool the session exposes. `/audit-plans` stopped screening for `docs/implement-plan/` progress logs and `claude/implement-plan-*` PRs, which no longer exist since the session chain was retired. In `CLAUDE.md`, the §12 trigger list no longer names a PR comment, the inactive §12.G body is reduced to a stub, and the pre-task step reads the relevant sections of `README.md` and `agents.md` instead of both files in full.

| The numbers that matter | Value |
| --- | --- |
| Commands changed | `analyze-log`, `apply-analysis`, `apply-url`, `audit-plans`, `deploy-activate`, `implement-issue-claude`, `implement-plan-ai`, `investigate-issue`, `validate-consumer-issue`, `verify-activation`, `write-plan` |
| Template copies changed | `workflow-templates/.claude/commands/{apply-analysis,apply-url,audit-plans,deploy-activate,implement-plan-ai,verify-activation,write-plan}.md` |
| `CLAUDE.md` sections edited | PRE-TASK, §0, §12, §12.G, §19, §27 (numbering unchanged) |
| Cloudflare credentials | `FUNTOKEN_IO_CF` (funtoken.io), `FT_GAMES_CF` (ft.games, 5m.fun) |
| Additional template copies corrected | `validate-consumer-issue`, `implement-issue-claude` |

What this means for consumer repos: the next `@stable` sync delivers the updated `CLAUDE.md` and template commands. Section numbers are unchanged, and no rule was loosened. `/validate-consumer-issue` now searches only relevant context, and `/implement-issue-claude` makes clear that the Claude label does not switch review off OpenCode. With a Cloudflare credential present, `/deploy-activate` runs Cloudflare reads directly and runs Worker deploys after you approve each step. When Wrangler and a safe sandbox are available, a failed dry run blocks the deploy; without isolation, the command relies on GitHub check-runs. Failed or unverified commit and pre-deploy checks block Worker deployment rather than handing out manual deploy commands; a missing or rejected Cloudflare session credential also blocks the Cloudflare step. The deploy process receives only the matching Cloudflare credential in an allowlisted environment, not other session secrets that Wrangler build hooks could read. You still set Worker secret values yourself, and §24.D operations still need a Q/A approval first.

### For contributors

`tests/test_audit_plans_command.py` no longer asserts the legacy `/implement-plan-claude` screening text; it asserts that text stays out of the command.

- **The review panel drops `z-ai/glm-5.2` for `mistralai/mistral-small-2603`, every review tier now draws its reviewers at random from all six panel models, and a `claude/**` push with no PR is no longer reviewed.** Small and mid-sized PRs can now get any panel model, not just a fixed four.

`mistralai/mistral-small-2603` takes glm-5.2's slot (#2) in `REVIEWER_MODELS` in `.github/workflows/review_autofix.yml`, and in `opencode-live-smoke.yml` to match. `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` now defaults to empty. With that, the `standard` tier (diffs of 200 lines or fewer) draws 4 reviewers and the `lite` tier (50 lines or fewer, no protected path) draws 1 from the whole panel. The draw is seeded by the PR number, so a PR keeps the same reviewers on every autofix round. Before, `standard` always ran minimax-m3, deepseek-v4-pro, qwen3.7-plus and gpt-6-luna, and gemini-3.1-flash-lite and glm-5.2 ran only on larger PRs. `internal-review.yml` loses its `push: claude/**` trigger and the `resolve-claude-branch-pr` / `review-claude-branch-push` jobs, so a `claude/**` branch is reviewed once it has a PR, like every other branch.

| The numbers that matter (OpenRouter `/activity` and editor audit comments, Sep 5 to Oct 5) | Value |
| --- | --- |
| glm-5.2 reviewer spend | $2,250 ($2.13 per review run, $12/M output tokens) |
| mistral-small-2603 reviewer spend, Sep 5 to 21 | $65 ($0.04 per review run, $0.60/M output tokens) |
| Applied fixes no other reviewer in the same run matched | glm-5.2: 37 in 1,058 runs; mistral-small: 187 in 1,558 runs |
| Runs where mistral-small produced findings | 59% of 1,279 (minimax-m3: 63%, deepseek-v4-pro: 53%) |
| Panel size by tier (unchanged) | lite 1, standard 4, full 6 |

What this means for operators: review spend per run drops, and every panel model now sees small and mid-sized PRs. A repo that sets `vars.REVIEW_TIER_STANDARD_REVIEWER_SLUGS` keeps its pinned list, and a list naming `z-ai/glm-5.2` now fails open to the full panel with a warning, because glm is no longer on the panel. Mistral's context window is 262K tokens, against 1M for the rest of the panel. A context overflow fails its slot on multi-reviewer tiers; if Mistral was the only reviewer and was skipped (`skipped_unmapped` or `skipped_open`) or reported a context overflow, the round retries with the live `openai/gpt-6-luna` slot unless its circuit breaker is open. Without that model, with its circuit breaker open, or if a started retry fails, a single-reviewer round fails rather than silently passing without a reviewer, including when size tiers are disabled; a budget-skipped retry after either a skip or context overflow instead requests partial finalize. Mistral has no same-family failback chain. `CLAUDE_BRANCH_PUSH_PR_GRACE_SECONDS` is no longer read.

### For contributors

The `claude-branch-review` mode stays in `review_autofix.yml` behind the `force_claude_branch_review` input, with nothing calling it. `tests/test_internal_review_push_pr_grace.py` and its `ci.yml` step are removed with the jobs they tested. The glm-5.2 catalog entry and failback chain stay so a repo can still opt back in. The merged-PR guard asks for confirmation when an `env`-wrapped commit's Git directory cannot be resolved, rather than checking the session checkout's PR history.

When a skipped or context-overflowed sole lite-tier Mistral slot falls back to GPT too late to start another reviewer, the review requests soft-deadline partial finalize rather than reporting a reviewer failure. Other unsuccessful lite passes still fail.

- **The security audit now runs on the Claude engine, with codex as the fallback.** Each audit runs on Opus 5.5 at `high` effort first. It reruns on codex (`openai/gpt-6-sol`) only when Claude cannot produce a usable result.

This covers both callers: the weekly and dispatched `security-audit.yml`, and the orchestrator project security pass in `orchestrate_poll.yml`. The `SECURITY_AUDIT` role in `.github/ai/claude_engine.json` now defaults to `claude`. `scripts/security_audit.sh` resolves it with `ai_engine_for_role` and runs Claude through `claude_run`, in the same credential-free, network-isolated container that codex uses. When Claude fails, the same prompt reruns on codex and the log records `AI_ENGINE_FALLBACK role=SECURITY_AUDIT reason=<reason>`. Fallback triggers include every pool account being at the 90% usage gate (`all_gated`), no account, a crash, a timeout, and output that is missing or is not a JSON array of objects. An orchestrator project labelled `ai:codex` keeps its audit on codex. A valid empty result from Claude counts as clean, as an empty codex result does today.

| The numbers that matter | Value |
| --- | --- |
| Claude model and effort | `claude-opus-5-5`, `high` |
| Codex fallback model | `openai/gpt-6-sol` (`xhigh`, unchanged) |
| Usage gate that forces codex | every account at or above `gate_utilization` `0.9` |
| Fallback reasons logged | `all_gated`, `no_credential`, `isolation_unavailable`, `all_accounts_failed`, `timeout`, `crashed_rc_<n>`, `missing_output`, `malformed_output`, `schema_mismatch` |

What this means for operators and consumer repos: audits are billed to the Claude account pool instead of OpenRouter whenever the pool has capacity. Consumer repos pick this up on the next `@stable` sync. To keep a repository on codex, set the repo variable `AI_ENGINE_SECURITY_AUDIT=codex`. Each run's log says which engine produced its findings (`security-audit: engine=claude|codex`).

### For contributors

`claude_run` in `scripts/ai_engine.sh` accepts `AI_ENGINE_INCLUDE_PATHS` (newline-separated, default empty), which mounts trusted runtime paths read-only, as codex's `--include` does. The audit uses it for the oversized-file chunks. The output check strips at most one outer ```` ```json ```` fence before parsing; any other fence falls back to codex. `security-audit.yml` adds `Resolve AI engine`, `Install Claude Code CLI` and `Resolve Claude credential` steps, which run only when the role resolves to Claude. `orchestrate_poll.yml` adds `SECURITY_AUDIT` to its `any_claude` role loop and passes the pool step's reason as `CLAUDE_POOL_REASON`. Tests: the Claude engine cases in `tests/test_security_audit_workflow_contract.py`, plus `tests/test_claude_engine.py` and `tests/test_orchestrator_judges_claude_engine.py`.

- **Standalone PR security audits are now opt-in.** `SINGLE_ISSUE_SECURITY_PASS_ENABLED` defaults to `false`, so eligible PRs can proceed through the normal review and merge path without a per-PR security audit.

This default applies in this repository and to consumer repositories after their next `@stable` sync. Reviews no longer hold a standalone PR for an existing pending or findings marker while the pass is disabled. Previously filed security findings remain open as issues. Orchestrator projects still use the separate `ENABLE_SECURITY_PASS` gate.

| Security pass | Default |
| --- | --- |
| Standalone PR (`SINGLE_ISSUE_SECURITY_PASS_ENABLED`) | `false` |
| Orchestrator project (`ENABLE_SECURITY_PASS`) | `true` |

What this means for operators: set the repository variable `SINGLE_ISSUE_SECURITY_PASS_ENABLED=true` to require the standalone audit before merge again.

- **Review tiers are now on by default: small PRs get one or four reviewers instead of the full six-model panel, and the two most expensive models run only on the full panel.** A diff of up to 50 lines that touches no protected path runs one reviewer, any diff of up to 200 lines runs four, and larger diffs keep the full panel.

`review_autofix.yml` now defaults `REVIEW_TIER_RESOLVER_ENABLED` to `true`, so every repo on `@stable` picks this up on the next sync. The tiers also no longer depend on folder names. Until now the resolver was off, and turning it on gave three reviewers only when every change sat in one of `scripts/`, `prompts/`, `.github/workflows/` or `tests/`, and one reviewer only for docs-only diffs, so application code in a consumer repo still got all six. A small diff that touches a protected path (the same list the deterministic skip gate uses: `agents.md`, `CLAUDE.md`, `.github/`, `.claude/`, `scripts/`, `prompts/`, `workflow-templates/`, `db/contracts/`, build, dependency and config files, both sides of a rename) never drops below four reviewers. The four-reviewer tier runs the four cheapest panel models by list price, `minimax/minimax-m3` ($0.23/$0.96 per M tokens), `deepseek/deepseek-v4-pro` ($0.435/$0.87), `qwen/qwen3.7-plus` ($0.32/$1.28) and `openai/gpt-6-luna` ($0.10/$0.50), so `google/gemini-3.8-flash` ($0.75/$3.75) and `z-ai/glm-5.2` ($0.65/$2.04) run only on the full panel. The one-reviewer tier picks one of those four by `sha256("<PR number>:<model>")`, so a PR keeps the same reviewer on every fix round and rerun while different PRs spread evenly across the four.

| The numbers that matter | Value |
| --- | --- |
| `lite` tier | 1 reviewer, diff of at most `REVIEW_TIER_LITE_MAX_LOC` (50) lines, no protected path |
| `standard` tier | 4 reviewers, diff of at most `REVIEW_TIER_STANDARD_MAX_LOC` (200) lines, any folder |
| `full` tier | all 6 `REVIEWER_MODELS`, larger diffs, `[force-review]` / `force-review`, fail-open cases |
| `REVIEW_TIER_LITE_REVIEWER_SLUG` default | empty (was `qwen/qwen3.7-plus`): random pick from the standard list |
| `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` default | `minimax/minimax-m3,deepseek/deepseek-v4-pro,qwen/qwen3.7-plus,openai/gpt-6-luna` (was the same without `qwen/qwen3.7-plus`) |

What this means for operators: most small PRs now cost one or four reviewer calls per pass instead of six, and never call the two most expensive models. Set `vars.REVIEW_TIER_RESOLVER_ENABLED=false` to keep the full panel on every PR (unless `vars.REVIEWER_RISK_TIER_ENABLED` is also on, whose own selection, which can be smaller, then stands), set `vars.REVIEW_TIER_STANDARD_REVIEWER_SLUGS` to choose the four-reviewer set (it is also the one-reviewer pool), or set `vars.REVIEW_TIER_LITE_REVIEWER_SLUG` to pin the one reviewer. Docs-only PRs and PRs of at most 10 added and 10 removed lines that touch no protected path still skip review entirely through `AUTOFIX_SKIP_DOC_ONLY` and `AUTOFIX_SKIP_MAX_ADDITIONS` / `AUTOFIX_SKIP_MAX_DELETIONS`, and the release-gate smoke PRs still get the full panel through their `force-review` label.

### For contributors

The `REVIEW_TIER:` log line gains `protected=<bool>` and, when set, `protected_path=<path>`; `models_source` gains `random_lite` and `random_standard` (the latter only when `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` reaches the script empty, which draws four reviewers from the whole panel; an empty repo variable falls back to the workflow default instead), and `reason` gains `code_<=50_loc_unprotected`, `protected_path_<=50_loc` and `code_<=200_loc`. Existing reason values are kept where they still describe the decision. A full panel already forced by the risk-tier resolver (`REVIEWER_RISK_TIER_ENABLED`, for example its `REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX`) is never shrunk by the size tiers (`reason=risk_tier_forced_full`), and a random pick that returns fewer reviewers than the tier asks for (for example without `sha256sum`) fails open to the full panel (`reason=random_reviewer_pick_failed`, `models_source=fallback_full_random_pick_failed`). The protected-path lists in `scripts/review_run_reviewers.sh` mirror the gate's `PROTECTED_SKIP_SUPPRESSED` patterns, and `tests/test_review_autofix_review_pipeline_contract.py` fails if the two drift apart. When `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` names a slug that is not on the panel, the standard tier fails open to the full panel as before and an unpinned lite tier draws from the whole panel with a warning.

- **The review panel's Gemini slot is back on `google/gemini-3.1-flash-lite`.** `google/gemini-3.8-flash`, which replaced it on 2026-09-29, had become the largest single line on the OpenRouter bill.

`REVIEWER_MODELS` in `.github/workflows/review_autofix.yml` and the live-smoke roster in `.github/workflows/opencode-live-smoke.yml` now list `google/gemini-3.1-flash-lite` instead of `google/gemini-3.8-flash`. Gemini 3.8 Flash cost $2,399 of the $5,387 spent on OpenRouter from 2026-10-01 to 2026-10-04 (45%), over 53,177 requests averaging about 303K prompt tokens each. Gemini 3.1 Flash Lite lists at a third of its price. The review tiers are unchanged: the `standard` tier still runs `minimax/minimax-m3,deepseek/deepseek-v4-pro,qwen/qwen3.7-plus,openai/gpt-6-luna`, and the Gemini slot runs only on the `full` panel.

| The numbers that matter | `gemini-3.8-flash` | `gemini-3.1-flash-lite` |
| --- | --- | --- |
| List price, prompt / completion per 1M tokens | $0.75 / $3.75 | $0.25 / $1.50 |
| Cached-read price per 1M tokens | $0.075 | $0.025 |
| Observed cache hit rate | 90% (2026-09-30 to 2026-10-04) | 2% (2026-09-21 to 2026-10-03) |
| Observed requests per day as a reviewer | about 12,000 | about 175 |

What this means for operators: full-panel reviews should cost noticeably less. In its earlier stint, Flash Lite made far fewer calls per review than Flash 3.8 does, so expect shorter Gemini findings.

### For contributors

`scripts/reviewer_failback_chains.json` and `scripts/codex_model_catalog.json` keep their `google/gemini-3.8-flash` entries for operator overrides; the live slot fails back to `google/gemini-3-flash-preview`, as it did before 2026-09-29. Flash Lite's 2% cache hit rate in its earlier stint was not investigated here. `README.md` and `agents.md` no longer call the `standard` tier "the four cheapest panel models", because Flash Lite is now cheaper by list price than some of them.

### For contributors

The follow-up planner in `scripts/security_audit.sh` no longer takes the `MAX_FOLLOWUP_ISSUES_PER_WEEK` argument, and it skips pull requests returned by the issues endpoint. `tests/test_security_audit_workflow_contract.py` covers the uncapped filing, duplicate checks across more than one page, and the absence of the cap. The planned `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP` in `docs/plans/security-pass-convergence-plan.md` is dropped as well, so pre-existing advisories are planned without a cap.

- **`/implement-plan-claude` now checks the merged project against its plan before the security pass, and the lessons each project learns reach AI memory.**

Until now, the audit that maps every plan acceptance criterion to the merged code ran only after the completion PR had moved the plan to `docs/completed/`, so its fix PRs skipped the security audit and runtime validation. A new `conformance <k>/3` stage now runs `/verify-activation — scope conformance` right after the last phase merges, before the security pass. It runs again before completion if validation needed a Claude-written fix. After completion, the `verify-activation` stage checks only activation (`— scope activation`). A project that was already in flight has no conformance run on record, so it gets the full check at the end, as before. Each stage also records surprises (a conformance finding, a security follow-up, a validation fix, a blocked-PR intervention, a departure from the plan) in a new `## Lessons` section of `docs/implement-plan/<slug>.md`. `issue_pr_status.yml` copies those lessons into the `ai-memory` branch when the PR carrying them merges.

| The numbers that matter | Value |
| --- | --- |
| Conformance runs per project | at most 3, shared by the pre-security and post-validation runs |
| New `/verify-activation` scopes | `— scope conformance`, `— scope activation` (no scope: full check, unchanged) |
| Ingestion trigger | merge of a `claude/implement-plan-*` or `claude/verify-activation-*` PR |
| Lesson record | `lessons_learned_record.v1`, phase `implement_plan`, kind `project_retrospective` |
| Kill switches | `AI_MEMORY_ENABLED`, `LESSONS_LEARNED_ENABLED` (repo vars, default `true`) |

What this means for operators: conformance defects are now fixed before the security audit and validation, and before the plan is archived, so no unaudited fix lands after the project is marked done. Lesson ingestion is fail-open and never turns the `AI Issue PR Status Sync` job red. Set `LESSONS_LEARNED_ENABLED=false` to stop it.

### For contributors

`scripts/ingest_implement_plan_lessons.py` reads every `docs/implement-plan/*.md` (except `README.md`) at the merge commit, so a lesson lost to an `ai-memory` push race is written by the next implement-plan merge. Record ids hash the plan slug, source and text, and `record_lessons_learned` accepts an optional `record_ids` list, skipping ids already written, so re-reading every log never duplicates. Prompt retrieval does not read `lessons_learned/` records yet.

- **The AI pipeline moves to the GPT-6 family, and GPT reasoning defaults drop from `xhigh` to `high`.** `WORKFLOW_EDITOR_MODEL` now defaults to `openai/gpt-6-sol`, the capacity fallback to `openai/gpt-5.6-sol`, and every lightweight utility role to `openai/gpt-6-luna`.

Every phase that follows the editor default (clarify, plan, orchestrate, the orchestrate_poll judge, clarify-respond, implement and its diagnose/repair sub-phases, the review_autofix editor, consolidator and review-blocked judge, the conflict resolver, validate, validation-refresh discovery, check-failure triage, workflow heal, security audit and workflow-log-analysis) now runs on `openai/gpt-6-sol`. `WORKFLOW_EDITOR_FALLBACK_MODEL`, which the plan, implement and review_autofix retry loops switch to on their final attempt, moves from `openai/gpt-5.5` to `openai/gpt-5.6-sol`, the previous primary. The log analyser, reviewer-consensus summariser, materiality fallback, behavioural smoke, retro and unselected-run summary roles move from `openai/gpt-5.6-luna` to `openai/gpt-6-luna`, and AI-memory keyword extraction (`AI_MEMORY_KEYWORD_MODEL`) moves from `openai/gpt-5.4-nano` to `openai/gpt-6-luna`. At the same time, 18 `THINKING_LEVEL_*` / reasoning repo-var defaults for GPT phases drop from `xhigh` to `high`, along with the matching script fallbacks.

| The numbers that matter | Value |
| --- | --- |
| New editor default | `openai/gpt-6-sol` (1.05M context, $2/$10 per Mtok up to 272K prompt tokens) |
| New capacity fallback | `openai/gpt-5.6-sol` (was `openai/gpt-5.5`) |
| New utility-role default | `openai/gpt-6-luna` ($0.10/$0.50 per Mtok, down from $0.20/$1.20 on `gpt-5.6-luna`) |
| Reasoning defaults lowered `xhigh` → `high` | 18 repo vars (`THINKING_LEVEL_CLARIFY`, `_CLARIFY_ORCHESTRATOR`, `_CLARIFY_RESPOND`, `_PLAN`, `_ORCHESTRATE`, `_JUDGE`, `_IMPLEMENT`, `_IMPLEMENT_REPAIR`, `_DIAGNOSE`, `_EDITOR`, `_REVIEW_BLOCKED_JUDGE`, `_VALIDATE`, `MODEL_REASONING_EFFORT_DISCOVER`, `_CHECK_TRIAGE`, `_WORKFLOW_HEAL`, `_ANALYSIS`, `REVIEW_CONSOLIDATOR_REASONING`, `VALIDATION_DISCOVERY_REASONING_EFFORT`) |
| Kept at `xhigh` | `THINKING_LEVEL_REVIEWER` and the pass-2 large-diff level (non-GPT reviewer slots); the hardcoded security-audit and workflow-log-analysis analyze/audit passes |
| New catalog entries | 2 (`openai/gpt-6-sol`, `openai/gpt-6-luna`); the `gpt-5.6-sol` and `gpt-5.6-luna` entries stay for operator overrides |

What this means for operators: repos that do not set `WORKFLOW_EDITOR_MODEL`, `WORKFLOW_EDITOR_FALLBACK_MODEL`, the utility-role model vars, or the `THINKING_LEVEL_*` vars pick up the new models and the `high` reasoning level on the next `@stable` sync. A repo var that is already set still wins, so repos that pinned `openai/gpt-5.6-sol` or `xhigh` keep that value until the var is cleared. To restore the previous reasoning depth for one phase, set its `THINKING_LEVEL_*` var to `xhigh`.

### For contributors

`scripts/codex_model_catalog.json` gains `openai/gpt-6-sol` and `openai/gpt-6-luna` with the same fields as their gpt-5.6 counterparts (OpenRouter reports identical context windows and supported parameters), and `docs/codex-model-reference.md` is regenerated from it. The `_PROMPT_BUDGET_TOTAL_BYTES` default (800000) is unchanged; its comments now note that both the primary and fallback have 1.05M windows. The E2E smoke overrides (`low` for clarify, plan and reviewers, `medium` for the editor), the `medium` utility-role levels, and the `low` interim judge are unchanged. `CHANGELOG.md`, `analysis/` and `docs/plans/` keep their historical `gpt-5.6-sol` references.

- **`/implement-plan-claude` now builds each project on its own branch and merges it into the default branch only after every stage passed, and on its PRs the Claude session fixes what the review panel finds instead of the GPT editor.**

New `/implement-plan-claude` projects open a project branch `claude/implement-plan-<slug>` with a draft final PR into the default branch, the way the orchestrator uses `orchestrator/project-<N>`. Phase, conformance-fix, validation-fix, and completion PRs target that branch, and the security audit and runtime validation run against it. The final PR is then marked ready, reviewed as a whole, and auto-merged. `review_autofix.yml` gains a Claude-fixer mode for PRs whose head starts with `claude/implement-plan-`. The reviewer panel still reviews every head, but the GPT editor, the GPT conflict resolver, and the review-blocked judge no longer run. The workflow hands findings and conflicts to a fresh Claude stage session, re-reviews each push, and auto-merges once no valid finding is left. Check-in sessions, including the CLAUDE.md §26 post-push check-in, now run every hour on Sonnet at low effort instead of every 3 hours.

| The numbers that matter | Value |
| --- | --- |
| Check-in interval | 60 minutes (was 180) |
| Claude-fixer rounds before the PR is labelled `ai:review-blocked` | `MAX_AUTOFIX_ITERATIONS` (default 5), counting `[claude-autofix]` commits |
| Claude interventions per blocked PR before the command asks | 3 |
| New workflow inputs | `security-audit.yml` and `ai-security-audit.yml`: `ref`; `validate.yml`, `internal-validate.yml` and `ai-validate.yml`: `target_ref`; `review_autofix.yml` and `ai-review.yml`: `claude_fixer_converged_head` |
| New repo var | `CLAUDE_FIXER_ENABLED` (default `true`) |

What this means for operators: nothing reaches the default branch from an `/implement-plan-claude` project until conformance, security, validation, and the completion PR have all landed on the project branch and the final PR has cleared review. Projects whose progress log already exists on the default branch finish the old way. Consumer repos need the next `@stable` wrapper sync for `ai-security-audit.yml`, `ai-validate.yml`, and `ai-review.yml` before the command can dispatch the new inputs; until then the command stops with `Status: BLOCKED` rather than auditing or validating the wrong branch. Set `CLAUDE_FIXER_ENABLED=false` to send `claude/implement-plan-*` PRs back through the GPT editor path.

### For contributors

- The hand-off comment is `<!-- ai:claude-fixer-handoff:v1 kind=<findings|conflict> head=<sha> round=<n> -->`, posted by `scripts/review_autofix_step_claude_fixer_handoff.sh`. The session answers with `<!-- ai:claude-fixer-verdict:v1 head=<sha> -->`. The gate accepts a `claude_fixer_converged_head` dispatch only when the workflow's hand-off and a collaborator's verdict both name the current head. `.claude/scripts/check_in_status.py` reads the same markers and reports `state: review-round` / `state: conflict`.
- `/effort low` is applied only when it is the whole starting prompt of a `create_session` child. That was verified with `get_session` on 2026-09-25, so checkers start with `/effort low` and receive their instructions through a one-shot `create_trigger` two minutes later.
- `/implement-plan-claude` no longer passes `pr_number` to the consumer `ai-validate.yml`, which has no such input and rejected the dispatch.
- In this library the convergence run is dispatched on `review_autofix.yml` directly, not through `internal-review.yml`: that wrapper calls `review_autofix.yml@main`, so forwarding an input `main` does not define yet turns every review run on the PR into a zero-job `startup_failure`.

- **`/audit-plans` now screens its recommendation against running `/implement-plan-claude` projects, not just AI-orchestrator projects.** It no longer recommends a plan that overlaps one of those projects or that one is already implementing, and it never archives a plan such a project still owns.

Until now the step-6 merge-conflict gate only looked at open `ai:orchestrator-tracking` issues and their `orchestrator/project-*` PRs. A project driven by `/implement-plan-claude` has no tracking issue, so the audit could not see it. Step 6 now also finds those projects from two sources: progress logs `docs/implement-plan/<slug>.md` with `Status: IN_PROGRESS` or `BLOCKED`, and open PRs whose head or base branch starts with `claude/implement-plan-`. It counts each project's open-PR files and the plan files of its unticked phases, or every file the plan names once all phases have merged. A plan either runner is already implementing is reported as in flight instead of being recommended. Step 7 skips archiving such a plan, because the project's own completion PR moves it to `docs/completed/`.

| The numbers that matter | Value |
| --- | --- |
| Project runners screened | 2 (AI orchestrator, `/implement-plan-claude`) |
| Extra GitHub calls per run | 0 new listings; the existing open-issue and open-PR listings serve both |
| `/implement-plan-claude` sources | `docs/implement-plan/*.md` logs (`IN_PROGRESS` / `BLOCKED`) and open `claude/implement-plan-*` PRs |

What this means for operators: a `/audit-plans` pick is now safe to start while `/implement-plan-claude` projects are running, and a running project's plan stays in `docs/plans/` until its completion PR moves it. When GitHub cannot be read, the audit still screens against the local progress logs but marks the pick UNSCREENED. Both command copies (`.claude/commands/` and `workflow-templates/.claude/commands/`) carry the change.

### For contributors

`tests/test_audit_plans_command.py` pins the command text: that the two copies are identical, the two in-flight sources, batched GitHub reads, set-aside of in-flight plans, the archive skip, and screening from the logs when GitHub is unreadable. It runs in its own `ci.yml` step.

- **`/implement-plan-claude` no longer stops mid-project to ask clarification questions.** It picks the RECOMMENDED answer, records it, and lists every such decision for human review at the verify-activation and deploy-activate stages.

A new CLAUDE.md §28 covers every `/implement-plan-claude` session after its start-up checks (step 0 permission mode, step 1 plan resolution, step 3 phase checklist). Inside that scope, an intent or design question that §0/§2 would turn into a stop is written in the usual Q/A format and answered with its `(RECOMMENDED)` option. That includes an ambiguous plan step, an edge case the plan leaves open, and a `/verify-activation` finding that needs a decision (the chain now passes `— unattended` to its conformance and activation runs). The work then continues. Each pick is written as an `AD-<n>` line in a new `## Auto-decisions` section of `docs/implement-plan/<slug>.md` and listed in the body of the PR that carries it. The step-12 `/verify-activation` report and the `/deploy-activate` opening message show the whole list and never ask about it. A `change AD-<n> → <letter>` reply is implemented as one PR on `claude/implement-plan-<slug>-decision-changes`, and `/deploy-activate` holds its runbook until that PR merges.

| The numbers that matter | Value |
| --- | --- |
| Questions that still stop the chain | start-up checks (steps 0, 1, 3); used-up caps; security or validation runs that did not succeed; terminal validation classes; §22.B / §23.C / §24.D operations |
| Where decisions are recorded | `## Auto-decisions` in `docs/implement-plan/<slug>.md`, plus the carrying PR's body |
| Where they are reviewed | `/verify-activation` activation-stage report, `/deploy-activate` opening message |
| Notifications per decision | none; the completion `PushNotification` carries the count |

What this means for operators: once you start a Claude-orchestrated project and answer the start-up checks, you are needed at completion, plus any failure escalation. Read the auto-decision list there. Reply `change AD-<n> → <letter>` for any choice you want different; unchanged entries are marked `confirmed` when `/deploy-activate` reaches LIVE. Consumer repos get the same behaviour through the `@stable` sync of `CLAUDE.md` and `.claude/commands/`.

### For contributors

`scripts/ingest_implement_plan_lessons.py` reads only `## Lessons`, so auto-decisions never reach AI memory (covered by `tests/test_ingest_implement_plan_lessons.py`). `tests/test_implement_plan_claude_command.py` pins the §28 text and the review wording in both copies of `verify-activation.md` and `deploy-activate.md`. The AI orchestrator's own clarify auto-answer (`[auto-answered-by-orchestrator]`) is unchanged.

- **Standalone issues are now implemented by Claude Code by default, in every repo.** A new repository variable, `AI_ISSUE_IMPLEMENTER` (default `claude`), picks the implementer, and setting it to `codex` switches a repo back to the clarify → plan → implement pipeline.

Every issue the AI orchestrator does not manage now goes to one "Claude issue dispatcher" routine. `clarify.yml` routes the issue, claims it with the `ai:claude` label, and sends a `claude-issue` dispatch to coding-workflows. The new `claude-issue-intake.yml` checks the repo against `.github/ai/consumer_repos.json` and fires the routine, which starts an Opus session running `/implement-issue-claude`. That session writes a single-phase plan and continues as `/implement-plan-claude` in the new issue mode: project branch, Claude-fixer review rounds, conformance, security pass, validation, completion, and the final merge, which closes the issue. The project is built on the branch the issue names in an `Integration branch:` or `Target branch:` line (security follow-ups, heal issues), otherwise on the default branch. Orchestrator issues, tracking / security-audit / retro issues, and `[E2E …]` release-gate fixtures always stay on Codex. Codex `plan.yml`, `implement.yml`, and standalone stall recovery skip Claude-claimed issues, so the two pipelines never work the same issue.

| The numbers that matter | Value |
| --- | --- |
| Routines needed | 1, for every repo (new consumers are covered once listed in `consumer_repos.json`) |
| Per-repo switch | `AI_ISSUE_IMPLEMENTER=codex` |
| Per-issue switch | `ai:codex` or `ai:claude` label, then `/reclarify` |
| New labels | `ai:claude`, `ai:codex`, `ai:claude-handoff-failed`, `ai:claude-blocked` |
| New coding-workflows settings | variable `CLAUDE_ISSUE_ROUTINE_ID`, secret `CLAUDE_ISSUE_ROUTINE_TOKEN` |
| Fire retries | 408 / 429 / 5xx / network errors, backoff 2, 4, 8, 16 s |

CLAUDE.md §28 (unattended auto-decisions) now also covers `/implement-issue-claude` sessions, start-up checks included, because nobody is at the keyboard. Every judgement call is recorded as an `AD-<n>` entry and listed on the issue at the end. The project stops only on a §28.C failure: an exhausted cap, a failed security or validation run, an ask-first operation, or a missing base branch. It then comments on the issue, adds `ai:claude-blocked`, and sends a push notification.

What this means for operators: create the dispatcher routine and set `CLAUDE_ISSUE_ROUTINE_ID` / `CLAUDE_ISSUE_ROUTINE_TOKEN` in coding-workflows before this reaches `@stable` (README, "Claude issue implementer"). Until then, Claude-routed issues get `ai:claude-handoff-failed` and a comment explaining how to retry or switch to Codex. Nothing is dropped silently.

### For contributors

Routing lives in `scripts/claude_issue_route.py`. A routing error falls back to Codex. The handoff and intake drivers are `scripts/claude_issue_handoff.sh` and `scripts/claude_issue_intake.sh`, with stable log prefixes `CLAUDE_ISSUE_HANDOFF` and `CLAUDE_ISSUE_INTAKE`. Tests: `tests/test_claude_issue_route.py`, `tests/test_implement_issue_claude_command.py`, and `test_standalone_stall_recovery_skips_claude_claimed_issues`.

- **The action-needed report for a pushed PR now comes from the session that opened it, not from the Sonnet checker, and the Routines these check-ins leave behind are swept.** When a PR reaches a terminal state, the CLAUDE.md §26 checker hands the verdict back to the pushing session, which writes the next steps with the context it already holds.

Until now the Sonnet checker wrote the §26 terminal report itself, from next steps the pushing session guessed at when it armed the check-in. Now the pushing session creates a hand-back Routine bound to itself, due in 7 days, before starting the checker. The checker renews it at every 3-hourly check-in; when `.claude/scripts/check_in_status.py` reports the PR merged or closed, it pulls the Routine forward to one minute out (only its `run_once_at`, never its prompt) and confirms with `get_trigger` 10 minutes later that it ran in the pushing session. The pushing session wakes once, re-reads the PR state with the same script, writes the report, renames itself `PR #<n> merged — <no action needed | action needed>` (or `PR #<n> closed — decision needed`), renames and archives the checker, and sends the one push notification. `/implement-plan-claude` does the same for a blocked, closed, or stuck PR: the stage session that opened it runs the intervention instead of a fresh `… — blocked PR` session, while merged phase PRs still start a fresh stage session. A new sweep, `.claude/scripts/stale_routines.py`, deletes fired check-in reminders, Routines whose session is gone, and hand-backs for PRs that finished more than a day ago, every time a check-in is armed or reported.

| The numbers that matter | Value |
| --- | --- |
| Wakes of the pushing session per PR | 1, when the PR is terminal |
| Hand-back horizon (renewed at each check-in) | 7 days |
| Delivery check after the hand-back | 10 minutes |
| Sweep grace for a finished PR's hand-back | 24 hours |
| Check-in interval (unchanged) | 180 minutes |

What this means for operators: open the session that pushed the PR to see what is left; it is titled with the PR's outcome, and its checker is archived under `… — handed to …`. If that session was archived first, the checker writes the report itself and adds ` (pushing session unreachable)` to its title. If a checker dies, the unrenewed hand-back fires within 7 days and the pushing session, finding the PR still open, starts a fresh checker. The sweep only deletes Routines named `PR #<n> status check-in…`, `PR #<n> hand-back`, or `implement-plan <slug>: …`, and never one you paused. Routines left from earlier check-ins are cleared by the first sweep after this ships.

### For contributors

- Routine names are capped at 60 characters and truncated with `…`, so the sweep identifies a hand-back and its PR from the PR URL in the Routine's prompt, never from its name.
- The checker only moves the hand-back's `run_once_at`. `update_trigger` tells models not to rewrite a Routine's prompt because another session asks, and checkers told to put the verdict in the prompt refused (observed 2026-09-25), so the prompt is fixed and the woken session reads the PR state itself.
- The hand-back must be a scheduled fire. `fire_trigger` ignores `persistent_session_id` and starts a fresh session with no repository or context, whether the bound session is active or archived (verified 2026-09-25), so no flow calls it. A scheduled fire into an archived session ends with `auto_disabled_session_gone`, which the checker's `get_trigger` check reads as a failed hand-back.
- `.claude/scripts/stale_routines.py` (mirrored to `workflow-templates/.claude/scripts/`) reads a `list_triggers` result from a file (the harness usually saves that result to a file itself), uses only `id`, `name`, `enabled`, `ended_reason`, and the prompt, makes one REST read per distinct enabled hand-back PR, and prints `{"delete": [...], "kept", "not_ours", "errors"}`. A failed read keeps the Routine. `tests/test_stale_routines.py` runs in its own `ci.yml` step, and `.claude/settings.json` pre-approves the script and `get_trigger`.
- `.claude/hooks/pr_check_in_reminder.py` (and its `workflow-templates/` copy) now tells the session to sweep, create the bound hand-back Routine, and never use `fire_trigger`; `tests/test_pr_check_in_reminder.py` asserts the new reminder text and the CLAUDE.md §26 wording. CLAUDE.md gains §26.G for the sweep; §26.A–F keep their letters.
- `/implement-plan-claude` adds a **Hand-back** section, a `Hand-back trigger` field in the `— resume.` block and the progress log `Check-in:` line, and a hand-back branch plus a 10-minute delivery check in the checker prompt; step 0 deletes the previous stage's hand-back Routine and step 2 runs the sweep.

- **Review/autofix failure comments now say what failed, and heal no longer accepts a retry as the fix for a repeating failure.** A new CI check also rejects duplicate `CLAUDE.md` section numbers.

The "AI review/autofix failed" comment now names the failed step and quotes the first specific error line, with credentials redacted. Before, it said only that the workflow "encountered an error". The conflict resolver's stderr is now captured and fingerprinted like the editor's, so a resolver failure is no longer reported as a generic `workflow_failure`. Workflow failure heal treats a failure as deterministic when the identical-failure cap tripped or an earlier heal of the same lineage merged a fix and the failure came back. For those failures the intake never files a `transient` verdict, and the heal issue requires a fix that removes the cause, backed by a regression test that reproduces it. `tests/test_claude_md_section_numbers.py` fails CI when two `## §N.` headings share a number.

| The numbers that matter | Value |
| --- | --- |
| Identical failures on PR #4443 before a human found the cause | 6 |
| Extra API calls per failed review run | 1 (jobs list, fail-open) |
| Longest first-error line quoted in a comment | 300 characters |

What this means for operators: a blocked PR's comment names the failing step and error, so the cause is visible without opening the run log. A heal issue for a repeating failure asks for a real fix with a test, not a retry.

### For contributors

New `scripts/workflow_failure_heal.py` subcommands: `failure-headline` and `is-deterministic`. New stable log prefix: `AUTOFIX_FAILURE_HEADLINE`. The intake logs `classification_remapped from=transient to=inconclusive reason=deterministic_failure`.

- **CI now runs `tests/test_workflow_overlay_core.py`.**

The "Validation bootstrap and family direct-run tests" step in `.github/workflows/ci.yml` now runs `tests/test_workflow_overlay_core.py` directly, right after its sibling `tests/test_workflow_overlay_orchestrator.py`. PR #4507 corrected this test to pin `validate.yml`'s trusted-checkout `stage_workflow_support.sh` invocation from #4463, but no workflow ran the file, so it had been failing on main since 56fcde4 without CI noticing. It covers WORKFLOW.md overlay loading, prompt-override rendering, and the overlay staging in `clarify.yml`, `plan.yml`, `implement.yml`, `review_autofix.yml`, and `validate.yml`.

What this means for contributors: a pull request that breaks the overlay loader or the staging of `stage_workflow_support.sh` now fails CI instead of passing unnoticed. The release gates in `mark-stable.yml` and `test-and-mark-stable.yml` are unchanged, and consumer repos are not affected.

- **`/implement-plan-claude` no longer stops a project just because its third conformance audit found something to fix.** The fix PR that audit opens now gets a narrower `/verify-activation — scope fix-check #<PR>` of its own diff instead of a fourth audit, and the chain continues to security and validation when the fix holds.

The conformance cap is 3 runs per project, and every fix PR a run opens used to be re-audited by a new run. So when run 3 opened a fix PR, the project was certain to stop at `Status: BLOCKED` and wait for a human, however small the fix. Issue #4545's project hit exactly that at stage `conformance 4/3` after fix PR #4616, a one-line prompt rule plus its test. The new `conformance 3/3 — fix check` stage checks only the files that fix PR changed against the findings its body lists. It opens no fix PR and does not count toward the cap. It still stops the project when a listed finding is unresolved or the diff introduced a defect (`FIX-DEFECTIVE`). Defects it notices in code the fix did not touch go into the progress log and the final PR's body instead of starting another round.

The conformance audit also gains a **Sibling paths** check. It covers every template, prompt, or config variant the changed code selects between, and every child process that inherits an environment variable exported around a subprocess. These are the two kinds of defect that runs 2 and 3 of issue #4545's project found one at a time.

| The numbers that matter | Value |
| --- | --- |
| Conformance runs per project | 3 (unchanged) |
| Fix checks after the third run's fix PR | 1, not counted toward the cap |
| Stage that stalled issue #4545's project | `conformance 4/3` after PR #4616 |
| Files updated | `.claude/commands/implement-plan-claude.md`, `.claude/commands/verify-activation.md` (and their `workflow-templates/` copies) |

What this means for operators: a `/implement-plan-claude` project now asks you about its conformance cap only when a fix really is broken. `/verify-activation` also accepts `— scope fix-check #<PR>` when you want to re-check a single merged fix PR by hand.

- **`gh api` reads and routine writes no longer stop Claude Code sessions at a permission prompt.** A new hook, `.claude/hooks/gh_api_write_guard.py`, replaces the seven `gh api` `permissions.ask` rules in `.claude/settings.json` and prompts only for writes that are not routine.

The old ask rules matched any `gh api` call carrying `-X`, `--method`, `-f`, `-F`, `--field`, `--raw-field` or `--input`, so a read such as `gh api -X GET search/issues -f q=...` prompted exactly like `gh api -X DELETE`. An ask rule prompts even in Auto mode, which stopped unattended `/implement-plan-claude` stage sessions on searches, PR-description updates, progress-comment edits and security-audit dispatches. The hook works out each call's real HTTP method and lets reads and CLAUDE.md §23.B routine writes to the session's own repository through: creating a PR, editing a PR's or issue's title or body, adding or editing comments, replying to review threads, adding or removing a label, requesting reviewers, and dispatching the six workflows already allowed as `gh workflow run <file> *`. It approves the whole command when the rest is only `cd`, `sleep`, `echo`, `2>&1` or a pipe into `head`, `tail`, `wc -l` or `sort`. Every other write, including closing a PR or issue, merging, deleting a branch, changing settings, dispatching any other workflow, or writing to another repository, still prompts in every permission mode. It also prompts for `gh api` that could run out of the hook's sight, such as inside `$(...)`, `bash -c`, `sudo`, `xargs` or a heredoc fed to `python3`.

| The numbers that matter | Value |
| --- | --- |
| `gh api` ask rules removed from `.claude/settings.json` | 7 |
| Workflows whose `gh api` dispatch is routine | 6 (`security-audit.yml`, `ai-security-audit.yml`, `internal-validate.yml`, `ai-validate.yml`, `review_autofix.yml`, `ai-review.yml`) |
| GitHub API calls the hook makes | 0 |
| Repos affected | this repo and the 13 consumers in `.github/ai/consumer_repos.json`, on the next `@stable` sync |

What this means for operators: unattended stage sessions stop waiting on prompts for reads and routine writes. Destructive or administrative `gh api` calls still ask a human. Commands that combine `gh api` with loops, `python3` scripts, `$VAR` paths or file redirects are left to the allow list or the Auto-mode classifier, so they can still prompt outside Auto mode. The hook fails closed: if it cannot read its input or hits an internal error, it prompts. Do not add `gh api` ask rules back, because an ask rule overrides the hook.

### For contributors

CLAUDE.md §23.H documents the classification and §23.D how to shape `gh api` calls so the hook can approve them. `tests/test_gh_api_write_guard.py` runs in its own `ci.yml` step and covers the commands from the observed prompts, each class, the helper allowlist, the fail-closed contract, and the wiring. It also checks that the dispatchable workflows match the `gh workflow run` allow rules. `workflow-templates/.claude/hooks/gh_api_write_guard.py` and `workflow-templates/.claude/settings.json` must stay byte-identical to the root copies.

- **The Claude issue pickup now checks the producer runs of up to 30 queue targets per wake, three times the 10 it starts.** Queue items that fail the binding check no longer hold back the bound items queued after them.

Items that fail the binding check (`unbound`, `binding_mismatch`, `binding_untrusted`) stay open for the watchdog instead of being closed. If the pickup read the producer runs of only as many targets as it starts per wake, 10 such items at the front of the queue would take every read, and every bound item behind them would be deferred on every wake. `claude_issue_route.py queue-pending --fetch-repo` now reads the runs named by the first `QUEUE_BINDING_SCAN_FACTOR` × `--limit` targets (3 × 10 = 30) and still starts at most `--limit` of them. Items of a target past that window are deferred to the next wake, as before.

| The numbers that matter | Value |
| --- | --- |
| Targets whose producer runs are read per wake | 30 (`QUEUE_BINDING_SCAN_FACTOR` 3 × `QUEUE_PICKUP_LIMIT` 10) |
| Targets started per wake | 10, unchanged |
| Stuck items at the head of the queue that still leave all 10 start slots to bound items | 20 |
| Reads per completed producer run in the window | 1 compare read and 1 artifact download (per-run fallbacks when a listing misses) |

What this means for operators: nothing to configure. A few queue items stuck without a valid binding no longer stall the queue while they wait for someone to act on the watchdog alert. The worst case adds reads for the extra 20 targets on a wake with that many items open.

- **Claude fixer and `/implement-plan-claude` sessions post PR and issue comments through the GitHub MCP tools.** Hand-built `gh api … --input <file>` or heredoc JSON bodies always stop at a permission prompt, which stalls a session nobody is watching.

CLAUDE.md §23.D item 4 and the Rules of `.claude/commands/fix-claude-pr.md` and `.claude/commands/implement-plan-claude.md` now require `mcp__github__add_issue_comment` / `mcp__github__update_issue_comment` for comments, and forbid `gh api … --input <file>`, `-F body=@<file>`, and heredocs that build a JSON body. The prompt that motivated this came from a fixer session on PR #4601 that built its finding-by-finding reply with a Python heredoc piped into `gh api --input`.

What this means for operators: fewer unattended Claude sessions stopping at a permission prompt; nothing to configure.

- **Claude, not GPT, now fixes every `claude/*` pull request, and the session that pushed it is woken to do the fix.** Merge conflicts, failed checks, review findings, and block labels on these PRs reach the pushing Opus session through its §26 check-in, and an hourly catch-all starts a fresh fixer for any PR nobody handled.

`review_autofix.yml` used to run Claude-fixer mode only on `claude/implement-plan-*` heads. Every other Claude PR went through the GPT editor and conflict resolver, and its §26 checker only reported when the PR merged or closed. Now every PR-backed `claude/*` head runs in Claude-fixer mode: the reviewer panel still reviews, and the workflow hands the round to Claude. The hourly Sonnet checker runs `.claude/scripts/check_in_status.py --hand-back` and, when a fix is due, wakes the session that pushed the PR. That session follows the new `/fix-claude-pr` command: it claims the head, fixes, verifies, and pushes. When that session is gone, the checker starts a fresh Opus 5.5 session at high effort instead. The new `claude-pr-catch-all` job in `.github/workflows/review_autofix_sweep.yml` checks this repo and every registered consumer hourly. For any fix that has been due for 2 hours with no live claim, it queues a fixer on the Claude issue queue, and the Claude issue pickup session starts it. `/implement-plan-claude` stage, fixer, and `/deploy-activate` sessions now always start on Opus 5.5 at high effort.

| The numbers that matter | Value |
| --- | --- |
| PRs in Claude-fixer mode | every PR-backed `claude/*` head (was `claude/implement-plan-*` only) |
| Checker cadence | hourly, unchanged |
| Catch-all | hourly, cron `17 * * * *`, this repo plus the 13 repos in `.github/ai/consumer_repos.json` |
| Catch-all wait | `CLAUDE_PR_SWEEP_MIN_AGE_HOURS`, default 2 (a `claude/implement-plan-*` failed check waits the chain's 6 hours), then up to one pickup wake (hourly) |
| Claim lease | `CLAUDE_FIX_CLAIM_LEASE_HOURS`, default 3, ended early by a push |
| Conflict, CI, and block fixes per PR before a hold | `CLAUDE_FIX_HAND_BACK_CAP`, default 3 |
| Stage and fixer sessions | `claude-opus-5-5`, `/effort high` |

What this means for operators: a Claude PR that goes red or conflicts is fixed by Claude within about an hour, usually by the session that wrote it. After three conflict, CI, or block fixes on one PR, the fixer parks it with a hold claim, sends one push notification, and asks you. The catch-all needs the Claude issue pickup session to be running (`/claude-issue-pickup start`); the queue watchdog flags its items when the pickup stops. `CLAUDE_FIXER_ENABLED=false` sends every Claude PR back to the GPT path. A Claude PR opened before this change, whose checker still runs the old `--terminal-only` instructions, is picked up by the catch-all.

### For contributors

Claims are PR comments ending in `<!-- ai:claude-fix-claim:v1 head=<sha> kind=<conflict|ci|review|blocked|hold> by=<claimant> -->`, written by `.claude/scripts/claude_fix_claim.py` and read by `read_fix_claims` in `check_in_status.py`. Only owner, member, and collaborator comments count, timed by `created_at`. A PR keeps one §26 checker; a second interested session registers with it through a `PR #<n> status check-in: subscriber` trigger. Queue items for PR fixes are titled `[claude-issue-queue] fix <owner>/<repo>#<n>` and carry a `claude_pr_fix.v1` block (`scripts/claude_issue_route.py`); `queue-pending` returns them as `item_type: pr_fix`. The dispatcher steps (`.claude/commands/claude-issue-dispatch.md`) accept that payload next to `claude_issue.v1`, and start every session in two steps (`/effort high` alone, then a one-shot `dispatch <owner>/<repo>#<n>: start` trigger). An issue-mode `/implement-plan-claude` project whose base branch merges while it is in flight now moves onto the branch its base merged into and retargets its final PR. `stale_routines.py` now also sweeps those ended start triggers. New tests: `tests/test_check_in_status_hand_back.py` and `tests/test_claude_pr_sweep.py`.

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

- **The claude.ai-session Claude automation is gone: no more issue queue, pickup, stage chain, §26 checkers or Claude-fixer hand-offs.** `claude/*` pull requests are now reviewed, fixed and auto-merged by the normal review pipeline like every other PR, and every standalone issue runs clarify → plan → implement.

This is Phase 2 of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md`. The Claude issue intake and queue watchdog workflows, the `claude-pr-catch-all` sweep job, the Claude routing in `clarify.yml`, the `ai:claude` skip gates in `plan.yml`, `implement.yml` and the stall poller, the implement-plan lessons step in `issue_pr_status.yml`, and the Claude-fixer hand-off in `review_autofix.yml` are removed, together with their scripts, `.claude/` helpers, hooks and tests. `/implement-plan-claude` and `/implement-issue-claude` are now short hand-offs to the Actions pipeline (the Claude-engine label they add takes effect in a later phase), and `/verify-activation` and `/deploy-activate` lose their unattended mode. CLAUDE.md §23.I, §26 and §28 keep their numbers as "Retired" stubs.

Consumer repos are cleaned on the next `@stable` sync. `scripts/ai_labels.py sync-labels` deletes the labels listed in the contract's new `retired_labels` array, and `update_workflows.yml` gains a "Remove retired upstream files" step that deletes each file in `workflow-templates/retired_files.txt` whose sha256 matches a released version, keeping and logging any copy a consumer changed.

| The numbers that matter | Value |
| --- | --- |
| Retired labels deleted by the label sync | 6 (`ai:claude`, `ai:claude-handoff-failed`, `ai:claude-blocked`, `ai:claude-issue-queue`, `ai:claude-issue-queue-stale`, `ai:permission-prompt`) |
| Consumer `.claude/` files the sync removes when unmodified | 11 |
| `review_autofix.yml` size | 442,031 bytes (was 458,436) |
| New GitHub API calls per label sync | 1 `DELETE` per retired label |

What this means for operators: a `claude/*` PR no longer waits for a Claude session; the GPT editor, conflict resolver and review-blocked judge handle it, and auto-merge applies as usual. `claude_fixer_converged_head` is still accepted by `review_autofix.yml` and the review wrapper but ignored, and `CLAUDE_FIXER_ENABLED` is read but unused until the Claude-engine review roles land. The retired repo variables and secrets (`CLAUDE_ISSUE_*`, `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`, `CLAUDE_FIXER_VERDICT_BOT_LOGIN`, `CLAUDE_FIX_CLAIM_LEASE_HOURS`, `CLAUDE_FIX_HAND_BACK_CAP`, `CLAUDE_PR_SWEEP_*`, `CLAUDE_REVIEW_STALL_HOURS`, `AI_ISSUE_IMPLEMENTER`) are no longer read and can be deleted from repository settings.

### For contributors

`tests/test_no_session_automation.py` fails CI if `send_later`, `create_session`, `create_trigger`, `claude-issue-queue`, `check_in_status`, `claude_fix_claim`, `stale_routines` or `claude_session_janitor` reappear under `.github/`, `scripts/`, `.claude/` or `workflow-templates/`. `security_pass_skip.py` moved from `.claude/scripts/` to `scripts/` for the single-issue security pass of a later phase. The security dependency hold of #4934 (a generated `ai:security` follow-up waits for the issue named on its `Depends on: #N` line) moved unchanged from the retired Claude issue router to `scripts/security_dependency.py`, with the same `security-dependency` command line; clarify, implement and the stall poller call it. The `claude-fixer-auto-merge` job id is kept but never runs, in case a required check names it.

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

- **The review editor no longer throws away a correct fix because it miscopied a reviewer file's sha256, and the release gate's editor retry now waits on the review run that is already queued instead of queueing a new one behind it.**

The nightly promote cycle's smoke gate (`Test & Mark Stable Release` run 35802596362) failed at Phase 4b because the editor restored `tests/e2e_smoke_canary.txt` correctly on all three attempts, yet `scripts/review_apply_fixes.sh` rejected each attempt: the model had copied one reviewer file's 64-character hash wrong each time, and the run then discarded the edit. A missing or wrong hash now logs `EDITOR_REVIEWER_CHECKSUM_UNVERIFIED` and the attempt continues. A reviewer file with no entry, or with no issue-audit counts, still fails the attempt. Phase 4b's retry in `.github/workflows/test-and-mark-stable.yml` also stopped dispatching a new review run when one is already queued or running for the bait commit, since the new dispatch shared that run's concurrency group and sat pending past its 25-minute budget.

| The numbers that matter | Value |
| --- | --- |
| Review runs since 2026-09-20 that reached the editor | 16 |
| Of those, runs with at least one attempt rejected only for a checksum | 6 |
| Of those, runs that lost the edit on every attempt | 1 (the smoke gate) |
| Retry dispatch wait on run 35802596362 | 41 minutes pending, against a 25-minute budget |

What this means for operators: review runs finish in fewer editor attempts, and a failed first canary check in the release gate can now actually recover. Watch for `EDITOR_REVIEWER_CHECKSUM_UNVERIFIED` warnings if you want to track how often the model miscopies hashes.

- **A review-blocked spot-fix reissue can now list the new files its follow-up must create, so the implement run's scope guard no longer refuses a required changelog fragment or new test fixture.**

When the review-blocked judge closes a PR with `reissue_mode: spot-fix`, `scripts/review_rb_judge.sh` writes a `files_touched` allowlist into the replacement issue, and `implement.yml` refuses any commit outside it. That list could only hold files the judge cited or files the closed PR changed, and both had to exist at the closed PR head. A follow-up that had to add a file was therefore blocked. #4664, the reissue of #4605 / PR #4607, latched `ai:scope-blocked` on a new changelog fragment and four new `tests/fixtures/integration_ref_resolver/*.json` fixtures. The judge contract (`prompts/mode-judge-review-blocked.txt`) now has an optional `new_output_paths` array, and each declared path that passes validation is appended to the allowlist.

| The numbers that matter | Value |
| --- | --- |
| Paths #4664's scope guard refused | 5 (1 changelog fragment, 4 new fixtures) |
| Declared paths read per reissue | at most 10 |
| New GitHub API calls | 0 |

What this means for operators: a spot-fix reissue that has to add files no longer stops at `ai:scope-blocked`. The guard itself is unchanged. A declared path is dropped when it fails the path validator, is not printable ASCII or has a leading or trailing space, contains a glob character or trailing `/`, has a `.git` segment at any depth, already exists at the closed PR head, cannot be looked up there, or has no file extension in its last segment, so this cannot exempt an existing file or directory, or a new extensionless directory path. Extensionless new files (`Dockerfile`, `.gitignore`) are dropped too and still need the human-gated procedure, and a new directory whose name carries a dot (`conf.d`) is the one accepted residual: one brand-new subtree the judge named. The new `REISSUE_FILES_TOUCHED_NEW_OUTPUTS` log line shows how many paths the judge declared, added, and skipped, and each skip is logged with its reason. #4664 still has to be released through the existing human-gated procedure.

- **Release smoke PRs now reach review even when they overlap an older PR.** The merge-train gate no longer queues a PR marked `IS_SMOKE_TEST=true`; it also clears a prior queue label after retiring its marker. Ordinary overlapping PRs remain queued.

- **The merged-PR guard now asks for confirmation when `env -C` names an unresolved commit directory.**

An `env -C` wrapped `git commit` no longer checks the session checkout when the requested directory is missing or cannot be resolved. That checkout might be a different branch, so its PR history cannot authorize the commit. Other ambiguous shell-control commits retain their warning-only behavior; unresolved pushes still check the session checkout before requesting confirmation. Consumer repos receive the matching live and template hook changes at the next `@stable` sync.

What this means for operators: an unresolved explicit `env -C` directory cannot be mistaken for a verified checkout during the merged-PR check.

- **The orchestrator no longer marks a project validated because of another project's validation run.** Validate runs now carry their tracking issue in the run name, and the poller's workflow-run fallback only credits a success from a run for the same project.

When the `ai:validated` label is missing, the poller falls back to the conclusion of the latest completed validation run created after its dispatch. That lookup did not check which project a run belonged to: on 2026-09-26, project #3965 was marked validated, and moved on to its security pass, because a standalone validation of `main` (tracking issue 0, run 36242757577) finished after #3965's dispatch, while #3965's own three runs had failed. `internal-validate.yml` and the consumer template `workflow-templates/ai-validate.yml` now set `run-name` to `... [tracking:<N>]`, and `get_last_validation_run_info` in `scripts/orchestrate_poll_process.sh` filters on it using the run listing it already fetches, with no extra API calls.

| The numbers that matter | Value |
| --- | --- |
| Run name | `AI Validate [tracking:<N>]` / `Internal: AI Validate [tracking:<N>]` |
| Success credited from | runs marked with the project's own tracking issue only |
| Unmarked runs (wrapper not synced) | may report a failure, never a success |
| New log line | `VALIDATION_RUN_ATTRIBUTION tracking=<N> … selected_run=<id\|none>` |

What this means for consumer repos: the updated `ai-validate.yml` wrapper arrives with the next workflow sync. Until then, a lost `ai:validated` label is no longer recovered from an unmarked successful run; the project waits for the label or the next validation cycle instead of completing on unproven evidence.

### For contributors

`has_active_validation_run` still counts any in-progress validation run in the repository, so another project's run can delay a dispatch; it cannot produce a verdict, so it is unchanged here. Tests: the `test_validation_run_*` cases in `tests/test_orchestrate_poll_process.py`.

- **Reviewer heal evidence now names a summariser that returned nothing.** When the consensus summariser's OpenCode call exits 0 with an empty final message, the `reviewers_failed` evidence says so instead of reducing to `reviewers_failed=true`.

`workflow_failure_heal.py reviewer-failure-evidence` now reads the summariser's `attempt N produced empty stdout` lines. It emits `summariser_exit rc=0`, `summariser_empty_stdout prefix=<prefix>` and `dominant_rc=0`, so the heal report and the failure fingerprint name the failure mode. Attempt counts stay out of the output, so repeated failures fingerprint the same. The `Internal: AI Review & Autofix` / `AI Review` failure-log artifact (`codex-review-autofix-failure-logs-<run>-<attempt>`) now also uploads `summariser_pass1.log` and `summariser_review.log`, which hold each attempt's stderr tail. The summariser's behaviour is unchanged: it still retries 10 times and fails closed on empty output.

| The numbers that matter | Value |
| --- | --- |
| Source failure | PR #4607, run 36317817104: 10 of 10 pass-1 attempts empty |
| Evidence before | `reviewers_failed=true`, `dominant_rc=unknown` |
| Evidence after | `summariser_exit rc=0`, `summariser_empty_stdout prefix=pass1`, `dominant_rc=0` |

What this means for operators: the next heal issue for this failure names the empty summariser output, and its failure-log artifact has the OpenCode stderr needed to find the cause.

### For contributors

A recurrence of this failure class gets a new fingerprint, because the evidence gains lines. The regression test `test_reviewer_failure_evidence_names_summariser_empty_stdout` in `tests/test_workflow_failure_heal.py` fails on the previous parser and passes with this change.

- **The `gh api` permission guard no longer prompts on `gh api` text inside single quotes, and now prompts on a `gh api` call hidden in unquoted backticks.**

`.claude/hooks/gh_api_write_guard.py` (CLAUDE.md §23.H) looked for command substitutions in each word after the shell's quotes were stripped. That caused two errors:

- A single-quoted `sed` expression, commit message or `echo` that merely mentioned a backticked `` `gh api user` `` was treated as a hidden call, so it stopped at a permission prompt. On 2026-09-28 this hit a `/deploy-activate` session updating its activation log.
- `` echo `gh api -X DELETE …` `` with unquoted backticks got no decision from the guard at all.

The guard now reads the raw command the way Bash does. Backtick substitutions outside single quotes, and `$(...)` inside double quotes, count as hidden calls when their body holds `gh api`. Single-quoted text is data.

| The numbers that matter | Value |
| --- | --- |
| New guard test cases | 27 (8 new prompts, 5 new no-decision cases, 14 quoting cases for `substitution_bodies`) |
| GitHub API calls added | 0 |

What this means for operators: fewer unattended stops on log and commit edits that quote a `gh api` command, and no silent pass for a backticked `gh api` write. The fix reaches consumer repos on the next `@stable` sync of `.claude/`.

### For contributors

`has_hidden_gh_api` takes the command as a new optional third argument. Without it, it keeps the older token-level reading, so existing callers are unaffected. An unquoted `$(...)` is still split into its own segments and classified directly, as before.

- **Cancelled CI no longer leaves a PR stuck indefinitely.** The 30-minute review sweep can re-run failed jobs for eligible cancelled or startup-failed `ci.yml` runs on the current PR head.

Open, non-draft PRs in coding-workflows are checked during the existing review sweep. The sweep refreshes PR heads before requesting a rerun so a newer push supersedes the cancelled run. Real test failures do not trigger this recovery, and a failed-jobs rerun is not replaced by a full workflow rerun. Set `CI_CANCELLED_AUTO_RERUN_ENABLED=false` to disable the recovery path without stopping review dispatch.

| Recovery limit | Value |
| --- | --- |
| Sweep cadence | 30 minutes |
| Eligible attempts | First attempt only |
| CI lookups per tick | One bounded completed listing, three active-status listings |
| PR freshness check | One paginated open-PR listing |

What this means for maintainers: cancelled CI gets one automated failed-jobs retry when its PR head is still current; otherwise the sweep logs why it skipped the run.

- **The release smoke test no longer reports Clarify, Plan, or Implement success without that phase's run ID.** `test-and-mark-stable.yml` now finds each phase's run past the first 100 runs, only for the smoke issue's own title, and when no run can be found the phase fails at capture with `status=run_id_missing`.

Run 36374918973 blocked the stable release at `Internals .. FAILED` even though its totals showed 0 failed steps. The Plan wait step read one 100-run page of `actions/runs`. As skipped `issue_comment` runs piled up, the smoke issue's Plan run slid to index 96 of that page and then off it, so the step wrote `status=success` with an empty `run_id`. That skipped `Phase 2b: Soft-error analyser (plan)` and left deep verification to fail the release as `Plan: run ID not found`. Run 36504041362 failed the same way at Implement: the smoke issue's Implement run sat at index 137 of the window when the PR appeared. The Clarify and Implement captures also had no issue-title filter, so a newer run from the parallel alt-model smoke job could be taken as ours. All three captures now walk later pages of the same query, still matching only non-skipped runs of that phase whose `display_title` is the smoke issue's title. A missing ID now stops the gate at that phase's line. The check that keeps the Plan step waiting while another Plan run for the issue is still active pages the same way, so an older active run past page 1 no longer ends the step as `plan_failed`. When that check cannot read a page, it retries only until the Plan phase's inactivity limit and then fails with `status=timeout`, instead of polling until the job's 300-minute limit.

| The numbers that matter | Value |
| --- | --- |
| Pages read by each run-ID capture (Clarify, Plan, Implement) | 1 normally, at most 10 per attempt (GitHub's 1,000-result ceiling); a walk that reads all 10 full pages without a match is not retried |
| Pages read by each 10-second Plan status poll | 1 (unchanged) |
| Pages read by the "other active Plan runs" check (Plan completed without its label) | 1 when page 1 holds an active run, otherwise up to 10, stopping at a short page |
| New status value for `wait-clarify`, `wait-plan`, and `wait-implement` | `run_id_missing` |
| Retries of an unreadable runs page in the "other active Plan runs" check | until `PLAN_PHASE_TIMEOUT` (default 60 minutes) without activity, then `status=timeout` |
| Runs created in the first failed window (03:46–03:57 UTC) | 166 |

What this means for release operators: a busy repository no longer turns a successful Clarify, Plan, or Implement phase into a release block with no failed steps, and deep verification checks the smoke issue's own runs rather than the alt-model job's. If a phase's run really cannot be found, the gate prints `FAILED (run_id_missing)` on that phase's line and the wait step names the issue and title it searched for.

### For contributors

The Plan change is in the `wait-plan` step (`fetch_plan_runs_page_json`, `latest_scoped_run_field`, `require_plan_run_id`, `fail_plan_confirm_retry_if_idle`). `wait-clarify` and `wait-implement` keep their own `capture_run_id`, now paged and title-scoped, plus `require_scoped_run_id`, and take `ISSUE_TITLE` from `steps.create-issue.outputs.title`; like `wait-plan`, they fail before polling when that title is empty, because an empty title would match a run with an empty `display_title`. `tests/test_test_and_mark_stable_plan_polling_guard.py` runs the real step scripts against a stubbed `gh` to cover the missing-ID, later-page, other-issue, page-cap, and unreadable-page cases for all three captures, and the empty-title case for `wait-clarify` and `wait-implement`.

- **Security follow-ups touching the same file now wait for the previous fix to merge.** The weekly and dispatchable security audit adds a `Depends on: #N` line to later findings for that file, including when an earlier follow-up was already filed before a retry. Unrelated files remain independent. Claude queue pickup and Codex clarification and implementation hold a dependent issue until its prerequisite is closed with `ai:merged`; the scheduled poller re-triggers waiting Codex issues when that condition is met. A closed issue without `ai:merged` remains held and is reported as such in the queue watchdog.

- **A PR whose description quotes the skip-AI marker is now reviewed, every review-gate skip says why, and a `claude/*` PR whose review never happened is handed to a fixer after 2 hours.** Before, PR #4807 sat unreviewed for about 16 hours and nothing noticed.

The review gate in `.github/workflows/review_autofix.yml` skipped a review whenever `[skip ai]` appeared anywhere in the PR title or body. PR #4807 (phase 1 of #4785) only quoted the marker in its description, so run 36414886805 skipped it with `skip_reason=skip_ai_marker`. The skip logged no reason and left no comment or label. `check_in_status.py --hand-back` saw no hand-off, block, conflict, or failed check, so the checkers reported "waiting" every hour.

Now the marker counts only when it is intentional: in the PR title, or on a description line holding nothing but the marker, outside a code fence. The gate, `review_autofix_sweep.yml`, and the §26.H catch-all (`scripts/claude_pr_sweep.py`) share this rule, and one test holds all three copies to the same cases. Every gate skip logs `AUTOFIX_GATE_SKIP reason=<skip_reason> pr=<n> head_sha=<sha>`. An open `claude/*` PR skipped for the marker, or for `pr_skip_ai=true` on a non-draft, gets one comment per head saying how to undo it. The gate serializes notices per PR and verifies the head again before posting. `check_in_status.py --hand-back` reports a new `review-stalled` state for a `claude/*` head that has no review trace after `CLAUDE_REVIEW_STALL_HOURS`. A review trace is a hand-off, a skip notice, auto-merge, the marker, a draft, `ai:merge-queued`, or an active workflow run. `/fix-claude-pr` answers `review-stalled` by re-dispatching the review once.

| The numbers that matter | Value |
| --- | --- |
| Incident | PR #4807, about 16 hours unreviewed (run 36414886805) |
| Stall window (`CLAUDE_REVIEW_STALL_HOURS`) | 2 hours after the head commit |
| Skip notices per head | at most 1 |
| Re-dispatches per stalled head before a hold | 1 |
| New API calls for the stall check | none before its no-call checks; then at most the head-commit read and active-run reads the hand-back mode already budgets |
| Workflows `dispatch_workflow.py` allows | 7 (adds `internal-review.yml`) |

What this means for operators: a PR description can now document the marker without losing its review. A deliberate title marker still skips, and the skip is now visible in the log and, on `claude/*` PRs, as a comment. A `claude/*` PR whose review silently never ran now reaches a fixer within about 2 to 3 hours with no human involved. Set the repository variable `CLAUDE_REVIEW_STALL_HOURS` to change the window. The `.claude/settings.json` and `gh_api_write_guard.py` allow lists gain `internal-review.yml`, and consumer repos receive the change with the next `.claude/` sync.

### For contributors

The Python rule is `has_skip_ai_marker` in `.claude/scripts/check_in_status.py`, and the workflows carry an identical `SKIP_AI_BODY_AWK` program; `tests/test_skip_ai_marker_rule.py` checks the three copies against one case table and runs the gate's notice block in bash against a fake `gh`. The notice ends in `<!-- ai:claude-fixer-review-skipped:v1 reason=<reason> head=<sha> -->`, which the existing `gate_fetch_marker_comments` lookup returns, so dedupe costs no extra call. No label is added, because `ai:review-skipped` means the deterministic doc-only or size skip. `review-stalled` reuses the `review` claim kind. The verdict's `stall_redispatched` field tells a second fixer that the head's review was already re-dispatched, so it holds instead of looping. The stall check is off when `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` is unset. The project checker's plain `--pr` mode is unchanged; the hourly catch-all covers `/implement-plan-claude` PRs.

- **`gh_retry` no longer hands callers the error bodies of attempts that failed.** When a GitHub call was retried and then succeeded, the caller got the failed attempts' response bodies followed by the real response. It now gets only the successful response.

`gh api` prints the error response body to stdout when a call fails, and `gh_retry` in `scripts/gh_helpers.sh` redirected only stderr for each attempt. On 2026-09-30, clarify run 36670937896 was rate-limited twice while fetching #5016 and then succeeded. Its metadata file held two rate-limit error objects before the issue, `jq -r '.number'` printed `null`, `null`, `5016`, and the step failed with `Invalid format 'null'`. The `/reclarify` it was handling was lost. `gh_retry` now buffers each attempt's stdout and prints only the buffer of the attempt that succeeds. A call that never succeeds prints nothing to stdout and still returns 1. Retry counts, rate-limit waits, the Telegram alert, the circuit breaker, and the existing stderr lines are unchanged, and no caller had to change. Return codes are unchanged with one exception: when the reader closes the pipe before the successful output is delivered (for example `| head -1` on a large response), `gh_retry` now returns `cat`'s non-zero status at once instead of retrying a command that already succeeded.

| The numbers that matter | Value |
| --- | --- |
| Failing run | clarify run 36670937896, step **Fetch issue metadata** (`clarify.yml:392`) |
| `gh_retry gh` occurrences covered, with no call-site edits | 557 across `.github/workflows/` and `scripts/` |
| New stderr line per failed attempt with output | `gh_retry: dropped <N> bytes of stdout from failed attempt <a>/<m>` |

What this means for operators: a workflow step that hits a rate limit and recovers now continues with correct data instead of failing later on malformed output. For a failed attempt, the new warning line gives the size of the output that was dropped, and gh's own stderr message still names the error.

### For contributors

`gh_retry_to_file`, `gh_api_json_to_file`, `_safe_gh_jq`, and `curl_gh_api` were checked and left unchanged: each already keeps a failed attempt out of a successful result. `gh_retry_to_file` still leaves the last error body in its output file on failure, because callers print it as a diagnostic. If the successful attempt's output cannot be delivered (the reader closed the pipe), `gh_retry` returns non-zero and does not run the command again; before this change, `gh` took the SIGPIPE itself and the command was retried. The two inline `gh_retry()` retry loops in `.github/workflows/review_autofix.yml` (the fallback in **Dispatch standalone validate for orchestrator short-circuit issues** and the wrapper in the deterministic-skip-merge step, whose `$(gh_retry gh api …)` head-SHA capture gates the merge-authorization labels) now buffer stdout the same way, with the same dropped-bytes warning. `tests/test_gh_retry_stdout_isolation.py` drives the helper and both inline wrappers through a fake `gh` and runs in the `ci.yml` gh_helpers test step.

- **The `gh api` permission guard prompts when an unquoted argument expansion could inject a flag, and no longer hides calls behind quoted heredoc markers.**

An unquoted variable appended to a read endpoint could split into `-Fbody=@<file>` or `-XDELETE` after the guard classified it. A quoted or escaped `<<EOF` could instead make the guard discard the following command as a heredoc body, even though Bash would execute it. The hook now checks raw argument quoting and only strips actual heredoc bodies; the consumer hook copy stays identical. Double-quoted dynamic read endpoints keep their existing no-decision behavior in loops. No GitHub API calls or new runtime dependencies were added.

- **The coding-workflows merge-conflict resolver no longer fails closed when its model stages the file it resolved.** The resolver model now works on a private copy of the merge index, so `git add` inside the model leaves the real index untouched.

In this repository, `Internal: AI Review & Autofix` failed six times on PR #5596 at `Run Codex resolver, validate, stage, commit` with `Resolver scope check failed closed (ValueError).` The model had run `git add` on the permitted conflicted file, which changed the live merge index that both attempt-scope guards in `scripts/review_conflict_resolve.sh` require to stay unchanged. Each attempt now gives the model a fresh copy of the captured index through `GIT_INDEX_FILE`, and the script still stages and commits the accepted resolution itself. A model that writes the real index anyway still stops the run, and out-of-scope edits are still restored and rejected by `check_resolver_diff.sh`. The resolver's own OpenCode config also sets `snapshot: false`, because OpenCode's snapshot git calls inherit the environment and would otherwise overwrite the model's index copy.

| The numbers that matter | Value |
| --- | --- |
| Failed review runs on PR #5596 before the fix | 6 |
| Private index path | `${RUNTIME_DIR}/resolver_model_index` |
| Repos affected | coding-workflows only (`IS_WORKFLOW_SOURCE_REPO=true`) |

What this means for operators: conflicted PRs in coding-workflows, such as `stable` forward-merges, resolve instead of dead-ending in repeated `conflict_resolver_failed` runs and workflow-heal issues. Consumer repositories see no behaviour change.

### For contributors

`tests/test_review_conflict_resolve_retry_prelude_render.py` builds a real merge conflict and runs a stub model through the retry loop's own private-index block. It covers permitted staging, out-of-scope staging, a model that bypasses the copy, a fresh copy on every attempt, and the snapshot opt-out.

- **Security audit failures now show a redacted Codex error tail and provider status.** When Codex exits nonzero, the audit log includes up to 40 sanitized lines (4 KiB) and a `provider=402|401|429|5xx|unknown` classification on the existing failure line. Prompt/config echoes and credential-shaped values stay out of the published tail; successful audits are unchanged.

- **Malformed check-run API output no longer looks like a clean review snapshot.** Empty or invalid responses are retried within the collector's wait budget; if still invalid, the review continues with an `api_error` snapshot rather than reporting zero checks as ready.

The collector validates every paginated check-run response before counting runs. A failed GitHub attempt followed by a successful retry keeps only the successful response, so the collector sees all checks on the PR head. Valid responses with an empty `check_runs` list remain supported.

- **A change to the shipped Claude command files now starts a stable release, and both release gates test the Claude assets before tagging.** Consumers receive `CLAUDE.md` and `workflow-templates/.claude/` on the same `@stable` sync as the workflow wrappers, and the release path now treats them that way.

The daily promote cycle (`scripts/promote_main_cycle.sh`) counted root `.claude/` as code but not the `workflow-templates/.claude/` twin that `update_workflows.yml` actually copies to consumers. A release window that changed only shipped command files (`workflow-templates/.claude/commands/*.md`) logged `PROMOTE_CYCLE_SKIPPED reason=no_code_changes` and waited for an unrelated code change. The same gap in the poller's untested-commit check (`comprehensive_cycle_is_code_path` in `scripts/orchestrate_poll_process.sh`) let a bot commit touching only those files pass as non-code. Both now count any `.claude/` directory as code. Separately, `test-and-mark-stable.yml` (`validate-scripts`) and `mark-stable.yml` gain a `Claude asset tests (CLAUDE.md, .claude hooks, scripts, commands)` step running the suites `ci.yml` already runs on PRs.

| The numbers that matter | Value |
| --- | --- |
| Test files added to each release gate | 21 (20 pytest files plus `tests/test_session_start_extract_repo_slug.py`) |
| Added gate time (local run) | about 40 seconds |
| Path pattern added to both classifiers | `*/.claude/*` |

What this means for operators: edits to Claude commands, hooks, scripts, or `settings.json` reach consumers on the next daily promotion even when nothing else changed, and a regression in those files fails the release instead of shipping. Coding-workflows' own Claude automation (issue pickup, the `claude-pr-catch-all` sweep) still runs from `main`, unchanged.

- **Workflow failure heal no longer files unrelated review/autofix failures under one fingerprint.** The intake now ignores the reporter's own header lines when it fingerprints an `autofix_failure` report, and the reporter puts the run's first error into the evidence.

`scripts/workflow_failure_heal_autofix_report.sh` opens every evidence file with `failure_reason=`, `finalize_reason=`, `consecutive_failed_runs=` and a `flags: AUTOFIX_REVIEWERS_FAILED=…` line. That `flags:` line matched the `*_FAILED` signature pattern on every report, so any failure whose evidence had no `::error::` line got the same signature. Three heal issues about one resolver bug (#4411, #4447, #4459) carried fingerprint `218ad70d…`, and #4465 continued that lineage through its `root=` marker. The unrelated forward-merge failure on PR #5892 got the same fingerprint, so its first report continued the lineage of #4459 and escalated at generation 4 without any heal attempt. `scripts/workflow_failure_heal_intake.sh` now passes `--strip-autofix-header` to `workflow_failure_heal.py error-signature`, and the reporter writes `AUTOFIX_FAILURE_FIRST_ERROR` (the `**First error:**` of the PR failure comment) into the evidence as an `::error::` line, which the signature ranks first. An `identical_failure_cap` report, whose evidence is the gate's marker data and matches no signature pattern, is now fingerprinted by its validated `failure_fingerprint` instead of the shared `no-error-lines` signature.

| The numbers that matter | Value |
| --- | --- |
| Heal issues that carried the header fingerprint | 3 (#4411, #4447, #4459) |
| Generation PR #5892's first report escalated at | 4 (cap 3) |
| Header lines ignored by the signature | 4 |

What this means for operators: a review/autofix failure now starts its own heal lineage unless it is the same error, or the same pull request (the `source=` marker, unchanged). Fingerprints of `autofix_failure` reports change once, so an open heal issue filed before this release is still matched for its own PR, but a new PR with the same error opens a new issue instead of an occurrence comment on it.

### For contributors

`strip_autofix_evidence_header()` removes only the leading run of header lines; the same text later in the evidence is kept. Older reporters still pinned in consumer repos send no first-error line, so their signature comes from the evidence tail instead of the header. Tests: `tests/test_workflow_failure_heal.py` (`test_strip_autofix_evidence_header_keeps_reporter_flags_out_of_signature`, `test_intake_autofix_fingerprint_ignores_reporter_header_lines`, `test_autofix_report_puts_first_error_into_evidence`).

- **The release smoke test scopes its Clarify, Plan and Implement run lookups to its own issue again.** The stable → main forward merge #5892 had dropped those lookups in `test-and-mark-stable.yml`, and `main`'s CI failed from 2026-10-01 08:59 UTC until this fix.

The conflict resolver of #5892 kept `stable`'s side of every conflicting hunk in `.github/workflows/test-and-mark-stable.yml` and removed `main`'s three `find_latest_scoped_run_field` call sites. The workflow now calls the shared helper again, keeping `stable`'s 10-page cap, `require_plan_run_id` and `run_id_missing` exits. The helper in `scripts/comprehensive_test_and_release_gh_api.sh` gains an optional page cap that returns exit 2 when every page is full, and returns exit 1 for a page that is not a runs listing. Callers that pass no cap keep the 5-page default and never get exit 2. A page that is not a runs listing now returns exit 1 for every caller, where a no-cap caller used to treat it as the last page and return 0; every caller already falls back on a non-zero exit.

What this means for operators: PRs into `main` that failed `tests-release-and-log-analysis` and `lint` on `tests/test_release_smoke_run_lookup.py` since 08:59 UTC go green again after a `main` merge. An empty smoke-issue title still ends the wait steps with `run_id_missing`.

- **The `python-repo-checks` validation harness now runs its import audit inside the app container.** Before, `ai-validate.yml` failed with `No module named 'yaml'` on any runner without PyYAML.

The generated `validation/tests/20_import_audit.sh` ran `import_audit.py` with the runner's own `python3`. That script checks `yaml` and `jinja2`, which `Dockerfile.app` installs only in the app image, so the audit failed on hosts without those packages even when the app image was correct. Orchestrator project #6031 stopped at runtime validation for this reason (run 37315007990, labelled `ai:harness-broken`). The template now runs the audit with `docker compose exec` against the `app` service, the same way the `python-mongo-flask` family does. It keeps the isolated-subprocess check and the TAP result, and on failure it now prints the helper's output as TAP comments.

What this means for consumer repos: projects whose `.ai/validate.yml` selects `python-repo-checks` stop hitting this false validation failure after the next `@stable` sync. Nothing needs changing in the repo.

- **Validation no longer fails during setup when the target branch is older than main.** The validate job's support staging now runs the overlay loader that ships with the staging helper, not the target checkout's copy.

`.github/workflows/validate.yml` clones the default branch and runs its `scripts/stage_workflow_support.sh`. In coding-workflows itself that helper treats the target checkout as its support root, so it ran the target branch's `scripts/load_workflow_overlay.py`. Since #6135 the helper passes `--trusted-source-repo` and `--trusted-root`, and a target branch cut before #6135 rejects them with exit 2. The run is then reported as "Validation harness generation failed". This hit orchestrator project #6031 (run 37430977754). The helper now prefers the loader next to itself, which always matches its flags and never comes from the target branch.

| The numbers that matter | Value |
| --- | --- |
| Failing run | 37430977754 (`Internal: AI Validate [tracking:6031]`) |
| Loader exit status before the fix | 2 (`unrecognized arguments: --trusted-source-repo ...`) |
| Flags added by | #6135 |

What this means for operators: orchestrator projects whose branch predates a main-side change to the overlay loader can be validated again without first merging main into the project branch.

### For contributors

`STAGE_SUPPORT_HELPER_DIR` is the helper's own directory, resolved from `BASH_SOURCE` at load time. `run_overlay_loader` falls back to the relative `scripts/load_workflow_overlay.py` when no sibling copy exists. `tests/test_validate_workflow_validate_bootstrap.py` runs the function against an older target loader to cover both paths.

The merged-PR guard also asks for confirmation instead of checking the session checkout when an `env`-wrapped commit has an unresolved directory. Its live and consumer-template copies are kept in sync.

- **Review autofix edits are no longer lost on the way out of the isolated editor workspace.** The sandbox now snapshots from, and copies results back into, the work tree the review job's git commands read, so editor fixes are committed and pushed again instead of ending in `editor_changes_lost`.

The review job works in a per-run copy of the checkout (`WORKSPACE_PATH`, under `/home/runner/work/_temp/workspaces/`), with `GIT_WORK_TREE` pointing at it and `GIT_DIR` at `${GITHUB_WORKSPACE}/.git`. `scripts/review_untrusted_sandbox.sh` took `GITHUB_WORKSPACE` as its host directory instead, so every edit the editor made in the sandbox was copied into a directory git no longer read. The editor's diff check then saw no change on every attempt, the commit step found a clean tree, and the run failed with `editor_changes_lost` (issue #6055: PRs #6041, #4877, #6126, #6130 and #6133). The sandbox's `prepare` step now takes `WORKSPACE_PATH` as the host, rejects it unless it sits directly under `${RUNNER_TEMP}/workspaces` next to a real `${GITHUB_WORKSPACE}/.git`, and records it for the `run` step, which transfers only into that recorded path. `scripts/review_untrusted_workspace.py snapshot` takes an optional host Git dir and lists the work tree with `--git-dir`/`--work-tree`. The same fix was made twice before (#4478, #4585), but both merged into project branches that never reached `main`.

| The numbers that matter | Value |
| --- | --- |
| Host directory before | `${GITHUB_WORKSPACE}` (the original checkout) |
| Host directory now | `WORKSPACE_PATH` when set (validated under `${RUNNER_TEMP}/workspaces`), else `${GITHUB_WORKSPACE}` as before |
| Failed runs inspected | 36966343894 (PR #6041), 37166027253 (PR #6133) |

What this means for operators: PRs stuck on repeated `editor_changes_lost` failures can be re-reviewed once this reaches `@stable`, and their autofix rounds should push commits again. The changes-lost guard, the transfer safety checks, and the auto-merge gates are unchanged.

### For contributors

The workspace path is chosen once, in `prepare`, and written to `${REVIEW_SANDBOX_ROOT}/workspace`; `run` refuses to start without it, so a model-controlled environment cannot redirect the transfer. The synthetic repository inside the sandbox still runs with the scrubbed `git_env`; only the host `git ls-files` calls in `snapshot` get the explicit Git dir. Regression test: `test_review_isolation_transfers_into_active_work_tree` in `tests/test_review_autofix_review_pipeline_contract.py` drives the real `prepare` and `run` actions with Docker stubbed, checks that a workspace outside `${RUNNER_TEMP}/workspaces` is rejected, and fails on the old code exactly as production did (exit 0, no transfer-failure marker, edit missing from the work tree).

- **Failing CI on pull requests now reaches check-failure triage, and a failing CI on `main` now opens a heal issue.** Check-failure triage had never run, because GitHub sends no `check_run` event for checks created by GitHub Actions.

`internal-check-failure-triage.yml` and the consumer wrapper `ai-check-failure-triage.yml` gain a `triage-workflow-run` job on `workflow_run: completed`. Each failed (`failure` or `timed_out`) pull-request run is evaluated, but repeated failures of the same workflow on a PR share one open triage issue. The diagnosis reads every failing check on the PR head, so a roll-up job that only fails because another job did gets no separate issue. This repo listens to `CI`; consumer repos listen to every workflow and skip only the shipped pipeline wrapper paths, not custom workflows with matching names or case variants. The exclusion uses the workflow definition path rather than the run path, which may carry an `@refs/heads/...` suffix. `workflow-failure-heal-intake.yml` now also takes failed `CI` runs on pushes to the default branch and files the fix against that branch. The `check_run` job stays for checks from apps other than GitHub Actions.

| The numbers that matter | Value |
| --- | --- |
| Check-failure triage runs in this repo before the change | 0 (since the workflow landed in June) |
| Open triage issues per PR and failing workflow | At most 1 |
| `main` CI push runs that failed, 2026-10-03 09:51 to 2026-10-04 17:56 UTC | 35 of 35 |

What this means for operators: a red CI on a PR now opens an `ai:check-triage` issue when no matching one is open, and a red `main` now opens an `ai:workflow-heal` issue against `main`, so a broken base no longer sits unnoticed. Fix PRs for workflow-heal issues, including release-workflow heal issues, must pass the head-bound security audit. In consumer repos every finished workflow leaves a skipped `AI Check Failure Triage` run in the Actions tab; `CHECK_FAILURE_TRIAGE_ENABLED=false` still turns triage off.

Workflow-heal reports only follow comment run links confirmed by the recent failed-run listing. Only heal-labeled issues with a canonical marker header and a trusted author pass lineage markers forward. Intake-issued branch routing cannot be overridden by untrusted log evidence or diagnosis text.

- **Implement, repair and diagnose now actually run on Claude, and planning against `stable` no longer crashes.** Since the Phase 5b cutover, every implement job picked Claude and then fell back to codex (`openai/gpt-6-sol` on OpenRouter), because the Claude Code CLI never installed. A plan that checked out `stable` failed outright, because it ran `stable`'s older stall guard.

`.github/workflows/implement.yml` sets `BASH_ENV` in "Activate workspace shell context", which moves every later bash step into the materialized `WORKSPACE_PATH` copy. That copy leaves out `.codex-workflow-src`. The bash step of `.github/actions/install-claude` read its relative `config_path` (`.codex-workflow-src/.github/ai/claude_engine.json`) from that directory, logged `install-claude: no claude_version input and … is missing`, and skipped the install. `claude-pool-token` then reported `available=false reason=cli_missing`, and `IMPLEMENT` logged `AI_ENGINE_FALLBACK reason=cli_missing` (or `no_credential` when a `claude` binary was already on `PATH`). The action now reads a relative `config_path` from `GITHUB_WORKSPACE`, where the caller's `uses:` path and the npm cache key's `hashFiles()` already resolve it. Absolute paths are unchanged.

`plan.yml`, `clarify.yml` and `orchestrate_clarify_respond.yml` copied `ai_engine.sh` from the support ref but left `scripts/codex_stall_guard.sh` to the checked-out branch, and `claude_run` runs that guard with `--engine claude`. Workflow-heal issues plan against `stable`, whose guard predates `--engine`, so every Claude planning attempt ended `codex_stall_guard.sh: unknown option: --engine` and the run failed after three attempts. The three workflows now copy the guard from the support ref too, as `implement.yml` already did, so the job no longer runs a guard script from the checked-out branch.

| The numbers that matter | Value |
| --- | --- |
| Implement runs sampled (2026-10-05 03:40Z to 2026-10-06 03:40Z) | 81 |
| Runs that resolved `IMPLEMENT` to Claude and then fell back to codex | 78 (65 `cli_missing`, 13 `no_credential`) |
| Runs whose `IMPLEMENT` ran on Claude | 0 |
| Plan runs in the same window that failed on the stall-guard error | 64 of 148 |
| Callers of `install-claude` covered by the fix | 5 (`clarify.yml`, `claude-engine-smoke.yml`, `implement.yml`, `orchestrate_clarify_respond.yml`, `plan.yml`) |

What this means for operators: implement, implement-repair and implement-diagnose now use Claude Opus 5.5 from the account pool, which moves their editor calls off OpenRouter. The D1 fallback to codex is unchanged when the broker or every account is unavailable. Consumer repos get the fix with the next `@stable` promotion.

### For contributors

`tests/test_install_claude_action.py` runs the action's install step with a fake `npm` and `claude` under a `BASH_ENV` that changes into a directory without `.codex-workflow-src`, and asserts that both Claude steps in `implement.yml` run after the `BASH_ENV` switch. CI runs it in the "Install Claude CLI action tests" step of `ci.yml`. `tests/test_ai_engine.py::test_every_workflow_staging_ai_engine_also_stages_its_stall_guard` fails when a workflow copies `ai_engine.sh` without the guard; it is the same test as in #6433.
The merged-PR guard now requests confirmation when an `env`-wrapped commit has an unresolved directory, rather than checking the session checkout's PR history; its live and consumer-template copies remain identical.

- **Template/live `.claude/` parity failures are corrected, template-only changes now sync to this repo's live copy automatically, and rejected relay requests no longer break their client connection.** This addresses the CI failures seen on `main` since 2026-10-03 09:51 UTC.

Two merged PRs updated a template under `workflow-templates/.claude/` without the live copy under `.claude/`: #6133 (the merged-PR guard hook `pr_merge_status_guard.py`) and #6176 (the `audit-plans`, `apply-url`, `implement-plan-ai` and `implement-issue-claude` commands). The pipeline's editors cannot edit `.claude/**`, so AI fixes leave the live copy behind. This change updates the hook and three live commands; `audit-plans.md` has no change in this PR. CI prepares eligible template-only command copies in its disposable checkout for PRs targeting `main`, but always checks security hooks and `settings.json` against committed live copies; push CI still checks committed parity. A new CI test fails for missing or differing live files unless they are listed in `.github/ai/claude_template_divergence.json` with a reason, and `sync-claude-live-copies.yml` opens a PR for template-only drift without overwriting newer live-file edits. The Anthropic relay also drains a valid, bounded request body before rejecting invalid authorization or an oversized forwarded header so `http.client` receives the intended HTTP 400 instead of `BrokenPipeError`.

| The numbers that matter | Value |
| --- | --- |
| Failed `CI` push runs on `main`, 2026-10-03 09:51 to 2026-10-04 17:56 UTC | 35 of 35 |
| Live copies updated in this change | 4 (1 hook, 3 commands) |
| Command files allowlisted as maintained separately | 6 |
| Anthropic relay focused suite | 31 tests passing |

What this means for operators: CI on new PRs no longer fails from these template/live parity mismatches or the relay's invalid-authorization regression. If a sync PR from `ai/sync-claude-live-copies` appears, it copies template content and executable mode into `.claude/`; it goes through the normal review. Symlinked template or live paths fail rather than copying checkout credentials or writing outside `.claude/`. To keep a file intentionally different, add it to `.github/ai/claude_template_divergence.json` with a reason.

- **This repository's merged-PR commit guard is identical to the copy consumer repos receive again.** The live hook now has the push and redirect parsing fixes that #6133 put only into the template copy, and both copies ask for confirmation when a push source or destination cannot be resolved instead of checking the wrong branch.

#6133 changed `.claude/hooks/pr_merge_status_guard.py` and `workflow-templates/.claude/hooks/pr_merge_status_guard.py` differently. Only the template copy got redirect-target skipping, the `cd … || exit` worktree handling, `VAR+=` prefix handling and `--repo=` push parsing. The live hook is now byte-identical to the template. A spaced redirect no longer erases a numeric branch refspec; an unresolvable push source now requires confirmation. The live `.claude/commands/audit-plans.md` is also synced with its template: #6176 (replace-claude-sessions Phase 2) reworded only the template copy, so `tests/test_audit_plans_command.py::test_template_parity` failed too, hidden behind the guard failures in the same CI step. The CI result must be checked on the published PR head.

What this means for operators: interactive sessions in this repository stop misreading commands such as `git push origin HEAD 2>&1` or `cd dir || exit; git push`, and ask before a push to a shell-expanded destination; consumer repos receive the same additional push safety checks on their next template sync.

- **Review editor transfer failures now retain a sanitized reason in workflow failure evidence.** Rejected transfers still stop the editor instead of falling back after an incomplete result.

When the isolated review editor cannot transfer its result, the workflow reports a fixed, path-free reason code, or `unknown` when no approved reason is available. The reason is also preserved in the editor attempt's stderr artifact for diagnosing repeated `Internal: AI Review & Autofix` failures. Transfer validation and the fail-closed exit remain unchanged; the new evidence does not imply that a rejected result was safe to accept.

What this means for operators: a failed review run can identify which transfer check rejected the result without exposing raw sandbox paths or untrusted error text.

- **Claude write roles now start with eight named tools instead of the CLI's default set.** That brings a no-op start-up back under the 25,000-token context budget.

`claude_run` in `scripts/ai_engine.sh` and the review editor's sandbox in `scripts/review_untrusted_sandbox.sh` used to pass `--tools default` to `claude -p` for every write role (PLAN, IMPLEMENT, the judges, the review editor and the rest). On Claude Code CLI 2.1.289 that set loads about 35 tools, most of which a headless pipeline role never uses, and the `write` job of `claude-engine-smoke.yml` failed its context gate at 29,369 start-up tokens. Write roles now get `Read`, `Grep`, `Glob`, `Bash`, `Edit`, `Write`, `WebFetch` and `WebSearch`. Read roles keep their four tools, and the P5 deny rules and `bypassPermissions` mode are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Context gate budget (plan Q32) | < 25,000 start-up input tokens |
| Write-job start-up before the fix (run 37191845530) | 29,369 tokens |
| Read-job start-up (same run) | 16,804 tokens |
| Tools loaded by `--tools default` (CLI 2.1.289) | 35 |
| Tools in the write profile now | 8 |

What this means for operators: the `write` job of `claude-engine-smoke.yml` should pass its context gate again, and Claude write roles spend fewer input tokens on every run. A write role that needs another built-in tool has to have it added to the list.

### For contributors

The list is set in three places: `scripts/ai_engine.sh`, `scripts/review_untrusted_sandbox.sh` and `PROFILE_TOOLS["write"]` in `scripts/claude_engine.py`. `tests/test_ai_engine.py::test_profile_tool_lists_match_claude_engine` fails if they drift apart or if `default` comes back.

- **The review editor can now repair `.claude/commands/` template parity failures instead of failing the run.** Its sandbox admits each `.claude/commands/<name>.md` whose `workflow-templates/.claude/commands/<name>.md` twin exists in both the PR checkout and the verified workflow-support checkout.

Before this, the sandbox held no `.claude/commands/` files. When a PR's CI failed `tests/test_audit_plans_command.py::test_template_parity`, `Internal: AI Review & Autofix` handed the failure to the editor, the editor created the missing directory, and the result transfer refused it with `reason=unsafe_directory`. That failed the editor step and discarded the editor's other, valid edits (runs 37251542418 on #6204 and 37255381476 on #6208). The admitted set is frozen at snapshot time, so a PR-added twin absent from verified support or a twin added during transfer cannot admit a command on retry. Missing or malformed admission state stops transfer rather than falling back to the mutable host checkout.

| The numbers that matter | Value |
| --- | --- |
| Command files admitted in this repo | 13 of 13 under `.claude/commands/` |
| Command files admitted in a repo without `workflow-templates/` | 0 |
| Failing runs this addresses | 37251542418, 37255381476 |

What this means for operators: review autofix no longer fails on a `.claude/commands/` parity test; it edits the live copy in the PR like any other source file. Other `.claude/` paths stay outside the sandbox. An editor write to an excluded file in an admitted directory is still dropped, and a new directory outside the admitted ones still fails the transfer.

- **A failed standalone security-audit dispatch no longer bypasses the merge gate.** Eligible PRs remain on hold instead of merging unaudited.

A dispatch failure records a failed cycle in a PR comment, and a later review run retries the audit. Once the five cycles and the per-head retry attempts (`SECURITY_PASS_EXHAUSTED_HEAD_AUDIT_ATTEMPTS`, default 2) are used, the PR is labelled `ai:security-pass-failed`; a failed retry dispatch also counts as a used attempt. If that comment cannot be posted, the PR still stays on hold and the review run fails, so the unrecorded failure is visible and workflow recovery retries it. A consumer repository whose `ai-security-audit.yml` does not accept `pr_number` will keep failing dispatch until its wrapper is synced.

What this means for operators: sync the consumer wrapper via `ai-update-workflows.yml` to restore the audit, or set `SINGLE_ISSUE_SECURITY_PASS_ENABLED=false` to opt out of the pass.

- **Unblock judge Codex no longer receives the OpenRouter key.** Its read-only model call runs in a network-isolated container through a host-side broker; verdicts containing literal or encoded credentials are rejected instead of posted. If isolation fails, the judge waits for another run rather than executing Codex on the host.

- Security audits no longer record a clean result when an explicitly scoped tracked file is omitted from the isolated agent's view.

Oversized scoped files are supplied as bounded read-only chunks, including prior-finding and fix-cycle files in full scans. A filtered scoped file or a file exceeding the export caps stops the audit before the model runs. Full scans continue to report other oversized files as a coverage note.

| Limit | Default |
| --- | --- |
| Read-only snapshot file threshold | 2 MiB |
| Scoped oversized per-file export cap | 16 MiB |
| Scoped oversized total export cap | 64 MiB |

What this means for operators: an audit that cannot inspect an explicitly scoped file fails instead of publishing a clean finding set; unscoped oversized files remain visible in the coverage note.

- Integration-conflict judge resolutions can no longer push unrelated file changes.

The poller records the merged index before invoking the isolated judge and rejects changes outside the conflicted paths. Conflicted workflows and actions must use lines from the merge sides in their original relative order without extra duplicates; the committed tree must match the validated staged tree before a push. Rejected resolutions remain subject to the existing bounded escalation path.

What this means for operators: a judge can resolve genuine conflicts without gaining a route to publish unrelated workflow changes.

- **Claude review roles no longer run PR-derived prompts with host credentials in reach.** The consolidator, conflict resolver and review-blocked judge use the network-isolated review sandbox and credential-free model relay. Read-only verdict and consolidation passes cannot transfer workspace edits; resolver and judge fix passes transfer only validated changes. If isolation is unavailable, these roles use the existing OpenCode fallback, never host Claude. Ephemeral judge and resolver sandboxes do not install project dependencies.

- **Review preflight Python probes no longer import modules from a PR checkout.** The review job checks for safe-path support before running host-side PR-tree helpers.

Dispatched reviews now run Semble and Serena bootstrap probes from neutral directories, rather than the PR checkout. Shared pre-review Python calls, conflict prompt rendering, the optional break-glass scan, and the partial-finalize timeout extractor use `PYTHONSAFEPATH=1` to exclude checkout modules from interpreter startup. Serena's handshake probe uses safe-path, but its server does not inherit it so installed server scripts can import sibling modules. If a neutral directory is unavailable, the affected bootstrap remains fail-soft and reports its tool unavailable. The isolation checks in `tests/test_review_pr_tree_python_isolation.py` run in `.github/workflows/ci.yml`; checkout credentials are unchanged.

What this means for operators: host-side Python probes, prompt rendering, and break-glass detection avoid loading modules from the PR tree, while existing installation fallback behavior remains in place.

- **Review editor sandbox failures now identify a safe rejected directory.** The isolated editor is told which paths it cannot access, and transfer still rejects attempts to create excluded directories.

The audit-plans command now matches its consumer template. If the editor tries to create a forbidden directory, the failure log includes a bounded relative directory name or `redacted`, without accepting the unsafe result. This helps diagnose sandbox-only test failures without weakening the transfer gate.

- **A judge merge held for the security audit no longer sends a CRITICAL alert unless it needs a human.** When the review-blocked judge approves a merge and the single-issue security pass is still auditing the head, the "Telegram review-blocked judge decision" step in `review_autofix.yml` now stays silent.

On 2026-10-06 PRs #6288 and #6209 each sent "🚨 CRITICAL: Review-blocked judge action: security_hold". In both, the judge had chosen merge, the security gate had already dispatched a retry audit, and that audit re-runs the review on its own. The gate (`scripts/review_single_issue_security_pass.sh`) now writes `hold_reason=` next to `hold=true`, and `scripts/review_rb_judge.sh` passes it on as `judge_skip_reason=security_hold_<reason>`. Holds that clear without a human send nothing. Every other hold is still a CRITICAL page, now naming the reason and the next step.

| Hold reason | Alert |
| --- | --- |
| `audit_dispatched`, `audit_pending`, `awaiting_followups` | none |
| `dispatch_failed`, `exhausted_without_completed_audit`, `cycles_exhausted`, `label_write_failed`, `markers_unverifiable`, `extensions_unverifiable` | CRITICAL, with the reason |
| `gate_failed`, `security_mode_unverified`, `unknown` (judge side) | CRITICAL, with the reason |

What this means for operators: a `security_hold` page now means the merge is stuck. Check the security-pass comments on the PR, then re-run the audit or decide the merge.

### For contributors

The gate step's outputs gain `hold_reason`; `hold` and `exhausted` are unchanged, and `judge_action=security_hold` is still emitted. A missing or malformed `hold_reason` (for example from an older staged gate script) is reported as `unknown` and pages.

- **The merged-PR guard now treats redirected `cd` and `exit` commands as uncertain when their redirects may fail.**

Pushes after an uncertain directory change are checked against the session checkout rather than an assumed working directory. If that checkout is on a merged PR, the guard blocks the push; otherwise it requests confirmation before the push proceeds. Redirects to literal `/dev/null` retain their existing behavior, as do warning-only checks for uncertain commits. Consumer repos receive the updated guard through the template sync.

What this means for operators: a failed shell redirect cannot silently bypass the merged-PR push check.

- **The merged-PR push guard now checks numeric branch names before output redirects.**

Commands such as `git push origin 123 > /dev/null` now check branch `123` rather than the session's current branch. Attached file-descriptor redirects such as `2>/dev/null` are still treated as redirects. If an explicit push destination cannot be determined, the guard asks for confirmation instead of checking an unrelated branch. When a push includes several unknown targets, it emits one confirmation only after checking known targets for merged PRs. With `--repo` and a positional remote, it checks the refspecs after that remote instead of treating the remote name as a branch.

What this means for operators: numeric branch pushes cannot bypass the merged-PR check through a separate redirect, and uncertain destinations require approval before pushing.

- **Check-failure triage now runs diagnosis without GitHub credentials and posts issues with a separate, narrowly scoped token.**

The workflow collects failure context before running Codex read-only from trusted support, with checkout credentials removed. It treats PR-head instructions as diagnostic data and redacts known secrets from the issue body before posting. Credential-bearing collection, posting, and failure-notification steps use only trusted helpers, including their event-emission dependencies. If trusted support is missing, triage stops rather than executing PR-head scripts.

What this means for consumer maintainers: add `CHECK_TRIAGE_ISSUES_TOKEN` to each repository before the next `@stable` sync. It must be a fine-grained PAT with Issues: write and Metadata: read on that repository; without it, the required reusable-workflow secret prevents triage from starting. The separate token preserves downstream `issues: opened` automation without giving the diagnosis step access to `GH_PAT`.

- **Review autofix no longer imports PR files as host Python modules.** The credential-bearing review job excludes the PR checkout and its workspace copy from Python's implicit import path. Workspace initialization also uses isolated Python with bytecode disabled, and the memory CLI loads its sibling modules from the trusted support directory.

- **The merged-PR guard checks numeric branch refspecs even when prefixed with empty quotes.** It only treats a whole, unquoted numeric word next to a redirect as a file descriptor, so quoted or escaped numeric branch names cannot bypass the merged-PR check.

The live hook and consumer template use the same word-boundary test; ordinary numeric file-descriptor redirects still work as before.

The guard also preserves a numeric push target across chained redirects instead of mistaking it for a file descriptor.

What this means for operators: pushes such as `git push origin ''2>/dev/null` and `git push origin 2 2>&2>/dev/null` check branch `2` before proceeding instead of silently checking only the current branch.

- **Merged-PR guard confirmations now survive multiple warnings in one hook call.** The guard combines its warnings and confirmation reasons into one JSON response, so a push with an unresolved repository still asks for permission instead of producing output the hook cannot parse.

The live and consumer-template hooks now emit at most one JSON object per invocation. A blocked push retains its stderr block and warning without an ask decision; a non-canonical API write retains its confirmation request. No scheduler or operator action is required.

- The orchestrator review-blocked judge no longer runs PR-derived prompts on a credentialed host agent when sandbox preparation fails. It defers and escalates repeated isolation failures on the same PR head, and uses a fresh isolated OpenCode sandbox when Claude is unavailable.

- Dependency installs for isolated implement and review agents can no longer reach arbitrary network services from attacker-controlled build backends.

Both dependency containers now run with `--network none`. An allowlisted HTTPS CONNECT proxy on the host permits only public registry addresses. Repositories can replace the default PyPI/npm/Yarn allowlist with `DEPENDENCY_PROXY_ALLOWED_HOSTS`; unlisted or privately resolved mirrors fail to install rather than falling back to unrestricted egress. On runners that require an upstream corporate proxy, installs may be unavailable and affected checks remain unverified.

- **Orchestrator poller judges no longer run on the credentialed host.** Wave, stall, integration and security-pass judges use read-only, credential-free review sandboxes on both engines. Isolation failures defer and escalate instead of invoking a host agent; integration conflicts are diagnosed by a judge and re-dispatched to the existing resolver.

- **A failed isolated review fix cannot push a partial host edit.** Sandbox result transfer validates destination parents, stages payloads and rolls back host writes if applying them fails. The review-blocked judge refuses to commit, push or mark a PR merged when its Claude fix or transfer fails.

- Isolated implementation agents no longer expose repository source files to the networked dependency installer.

Dependency preparation stages only allowlisted Node manifests and filtered third-party Python requirements for the networked install. Editable source builds run in a separate container without network access; build outputs remain confined to the disposable sandbox.

- **Live `.claude/` sync now holds template changes without verifiable PR provenance.** Authorized paths continue through the normal sync PR; malformed merge timestamps and other unverifiable paths use a separate draft PR and alert for human review.

- Review-blocked fixes now require a same-repository PR head and a verified origin-branch commit before preparing a writable worktree. Fork heads cannot drive a judge decision; moved branches or failed fetches defer the judge without consuming a fix retry and are checked again on the next poll.

- **Conflict resolution no longer runs an OpenCode writer on the credentialed host.** Both resolver engines use isolated snapshots and validated transfer; unsupported conflict paths are refused before any model runs and count toward integration-sync resolver escalation.

- **Live `.claude/` sync holds untrusted PR sources for human review.** The automatic sync now requires a merged PR targeting the sync base, from this repository and a non-bot OWNER/MEMBER/COLLABORATOR author. Refs #3576.

- **Failed clarify, plan and implement runs now open workflow heal issues, and Claude-engine planning no longer crashes on heal issues.** Every failed review/autofix run is reported too, not only the second one in a row.

Until now a failed clarify, plan or implement run reached workflow failure heal only if the issue later picked up an escalation label such as `ai:needs-human`. The stall poller never adds one by default, so issues #6413, #6373 and #6392 failed planning four times each on 2026-10-05 and no heal issue was opened. `clarify.yml`, `plan.yml` and `implement.yml` now carry a `heal-report` job that runs after the phase job fails and dispatches a `phase_failure` report to `workflow-failure-heal-intake.yml`. The intake reads the failed job's log, keeps one open heal issue per source issue, and escalates a heal issue whose own run fails with the failure it was filed for (`ai:workflow-heal-escalated`, CRITICAL Telegram alert). Those three plan failures came from `plan.yml` copying main's `ai_engine.sh` but not `codex_stall_guard.sh`: heal issues plan against `stable`, whose older guard rejected `--engine`. `plan.yml`, `clarify.yml` and `orchestrate_clarify_respond.yml` now copy the guard too.

| The numbers that matter | Value |
| --- | --- |
| `WORKFLOW_HEAL_PHASE_FAILURE_STREAK` (new) | default `1` |
| `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK` | default `2` → `1` |
| API calls per phase report | 1 issue read, 1 identity read, 1 paginated comment read, 1 dispatch |
| Planning attempts lost per run before the fix | 3 of 3 (`exit_code=2`, `reason=no_transcript`) |

What this means for operators: expect a heal issue (or an occurrence comment on an open one) after the first failed clarify, plan, implement or review/autofix run. To leave the first failures to the stall poller as before, set `WORKFLOW_HEAL_PHASE_FAILURE_STREAK` or `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK` to `2`; `WORKFLOW_HEAL_ENABLED=false` still turns heal off for a repository. Implement does not report a guard block (its label already reports), a failure turned into fix-up issues, or a `BLOCKED` verdict, and a cancelled run is never reported.

### For contributors

The reporter is `scripts/workflow_failure_heal_phase_report.sh` (log prefix `WORKFLOW_HEAL_PHASE_REPORT`); the streak and payload live in `scripts/workflow_failure_heal.py` (`phase_failure_streak`, `build_phase_failure_payload`, `build-phase-payload`). Streaks exclude untrusted comments, cancellations and successful implementation. The job runs separately because the intake cannot read the log of a job that is still running. It declares `permissions: {}` so it stays inside any caller's grant. `tests/test_ai_engine.py` fails when a workflow stages `ai_engine.sh` without the guard.

- **Rejected review-editor transfers now retain validated failure diagnostics with path-free directory classification.** When the review/autofix editor's sandbox result is refused, the helper's `::error::` line includes a fixed `reason=` token for known transfer rejections; unsafe directories also include fixed `category=` and bucketed `depth=` tokens. The wrapper validates and repeats the reason, then archives both the transfer diagnostic and the attempt's stderr. Exact excluded build and cache directories are skipped; case variants such as `Build/`, `Dist/`, and `Coverage/` still abort the transfer. Unadmitted directories under `.github/` and `.claude/`, secret-like names, and directory symlinks also abort.

  | Item | Value |
  |---|---|
  | `reason=` (transfer) | `admitted_inventory_missing`, `symlink_path`, `unsafe_file`, `file_changed`, `entry_limit`, `unsafe_directory`, `unsafe_result_path`, `size_limit`, `host_baseline_changed`, `result_conflicts_host`, `transfer_rollback_failed`; unclassified errors use `unknown` |
  | `category=` (unsafe directories only) | `symlink`, `invalid_name`, `dot_github_subtree`, `env_like`, `sensitive_name`, `key_material_suffix`, `excluded_name_variant`, `other` |
  | `depth=` (unsafe directories only) | `1`, `2`, `3+` |
  | Runtime file | `${RUNTIME_DIR}/review_sandbox_transfer_reason_<output-basename>` (per editor attempt) |
  | Archived copies | `review_sandbox_transfer_reason_<attempt>.txt` and `editor_attempt_<attempt>.err` under the previous-reviews directory |

  The editor prompt identifies the specific `.claude/` and `.github/ai/` paths that are admitted and warns against creating other paths there.

  What this means for operators: the next rejected transfer identifies its reason and rule class in the job log and archived attempt files without exposing a sandbox-controlled path.

- **Check-failure triage no longer loads PR-authored agent instructions into its diagnosis sandbox.**

The PR-head snapshot omits agent instruction files at every depth before Codex or Claude starts. Root agent files remain available to the diagnosis only as explicitly untrusted prompt data. Clarify and clarify-respond keep their existing snapshot behavior.

- **A merge conflict the resolver sandbox cannot carry now stops a PR once, instead of failing review/autofix every 30 minutes.** The generated workspace manifest is merged deterministically again, and a host-only conflicted file stops the head after a single failure with a comment that names it.

PR #6438 failed the "Run Codex resolver, validate, stage, commit" step about 28 times on one head between 2026-10-06 16:55 and 2026-10-07 02:51 UTC. Two of its five conflicted paths could not enter the resolver sandbox: `.ai/.workspace_source_manifest.txt` and `.claude/hooks/pr_merge_status_guard.py`. The manifest should never have reached the resolver. The stage check in `scripts/review_conflict_prepare.sh` used the pattern `*' 2 '*' 3 '*`, which never matches the stage list `1 2 3`, so the deterministic union merge never ran. It now matches `' 2 3 '`. For host-only files such as the guard hook, `scripts/review_conflict_resolve.sh` now logs `Conflict resolver: host-only conflicted path(s) need a manual merge: <paths>` and fails closed with the new reason `sandbox_path_host_only` before any model call. Every resolver fail-closed reason now appears in the failure marker as `conflict_resolver_<reason>`. The gate's identical-failure cap stops a head on the first marker whose reason is non-retryable. It no longer waits for `REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL`. The cap comment is titled "non-retryable failure" and quotes the failed run's first error.

| The numbers that matter | Value |
| --- | --- |
| Failed runs on PR #6438's head `0c32cb6` | about 28 in 10 hours |
| Failures before a non-retryable reason is capped | 1 (other reasons: `REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL`, default 3) |
| Non-retryable reasons | `conflict_resolver_sandbox_path_host_only`, `conflict_resolver_sandbox_path_unsupported`, `conflict_resolver_sandbox_support_missing` |
| New gate output | `fingerprint_cap_non_retryable` |
| New `check-paths` argument | optional report file (`scripts/review_untrusted_workspace.py check-paths <host> <paths> [<report>]`) |

What this means for operators: a PR whose merge with the base touches a host-only file now gets one `ai:review-blocked` label and one cap comment naming the file. Resolve that merge by hand and push; the new head runs normally. The cap, including the new non-retryable stop, is still governed by the repository variable `REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED`. In coding-workflows it was `false` from 2026-09-30 to at least 2026-10-07. That is why PR #6438 was never capped, so check that the variable is unset or `true`.

### For contributors

The resolver sandbox boundary from #6187 is unchanged: host-only paths are never handed to the model, and no host fallback was added. `check-paths` echoes a rejected name only when it is a plain relative path (`[A-Za-z0-9_.][A-Za-z0-9._/-]*`) that is a regular file or is absent on the host. Every other rejection is reported as a nameless `unsafe` entry and keeps `sandbox_path_unsupported`, so no untrusted name reaches the log. Integration-sync PRs do not export the specific reason. Their resolver failures must keep counting toward the resolver retry-state escape threshold (`RESOLVER_ESCAPE_THRESHOLD_N`, default 5), whose escalation drives the orchestrator's automatic branch rebuild.

- **Stable-release smoke tests no longer count a pre-bait review as proof that the editor saw the bait.** When a review was already running at bait injection, the release gate verifies its checked-out commit from its job log before accepting its result. Unverified or pre-bait checkouts keep the gate waiting for the bait-head review within the existing budget.

- **Review-blocked fixes now reach the isolated writer.** The judge passes its per-PR work tree through the sandbox's validated workspace path while keeping the checkout's Git directory available for snapshotting.

The judge previously set `GITHUB_WORKSPACE` to the per-PR work tree, which has no `.git`. Sandbox preparation rejected it before the fix writer could start. The checkout layout also ignores a stale inherited workspace path, so snapshots and transferred edits follow the tree the judge is editing. The sandbox's path checks and transfer rules are unchanged.

- **Workflow failure heal no longer exposes repository credentials to its editor.** Heal issues now receive verified, redacted failure evidence, an intake-authored file scope and a disposable editor container. An unverified scope stops implementation at `ai:needs-human` instead of running an untrusted editor on the host.

- **Security audit dispatch no longer silently stalls when its pending comment cannot be confirmed.** The single-issue security gate retries the comment write a bounded number of times and fails the review closed with `pending_marker_failed` if no response confirms the marker. The review-blocked judge preserves that alertable reason; duplicate pending comments for one exhausted-head cycle count as one attempt.

- Poller stall and review-blocked judges now honor `ai:codex` on the judged issue. Standalone stalls use only verified issue labels; managed judges combine issue and tracking labels, falling back to codex when the issue's label snapshot is unavailable or incomplete.

- **Workflow log analysis can read full run logs inside its isolated container.** The analyzer mounts the downloaded log artifact read-only, with an errors-first bounded subset and omission list when it exceeds 200 MiB. If staging fails or the artifact is unavailable, analysis continues using its summary context.

- **Workflow failure heal now checks run provenance before reading job logs.** Phase, review and release reports must reference runs in the claimed repository with the expected workflow and failure state; phase and review reports also require an issue or PR link. Unverified reports are skipped without fetching logs.

- **Orchestrator state comments from other users no longer control project progress or resolver context.** The poller reads V1/V2 state only from the account authenticated by `GH_PAT`, and skips the tick rather than reconstructing state when it cannot verify that account. Integration-sync conflict preparation ignores other users' V1 state comments and stops resolution when it cannot verify the pipeline identity.

After rotating `GH_PAT` to a different account, re-post old state comments from the new account (or retain the original account); otherwise legacy projects without trusted state may enter the existing reconstruction path. No state schema or scheduler changes are required.

- **Unrelated AI PRs no longer queue behind the generated workspace inventory.** The merge train ignores `.ai/.workspace_source_manifest.txt` by default, and implementation and review commits no longer stage its regenerated contents.

The manifest is removed from the tracked tree while remaining a gitignored workspace runtime file. Older branches that still track it retain deterministic conflict handling; real shared paths still queue as before. Set `MERGE_TRAIN_IGNORE_PATHS=none` to restore the former overlap comparison.

The merged-PR commit guard now asks for confirmation when an env-wrapped commit's working directory cannot be resolved, instead of checking PR history in the session checkout.

- **Validation now prepares the template renderer even when an unrelated earlier step failed, and reports a skipped preparation as itself.**

The reusable validate workflow ran `Run validation process` under `always()`, but `Install Python dependencies for validation renderer` ran only when every earlier step had succeeded. A failure in an unrelated step, such as a behavioural-smoke cache step, therefore skipped the preparation while validation still ran, and validation failed with "Trusted renderer runtime is unavailable" (exit 14; heal of #6031). The preparation step now also runs under `always()`, but only when the trusted runtime and the trusted support staging (`Fetch workflow support files`, now `id: support_staging`) succeeded, and not when the `after_create` hook failed. It checks for the renderer under `WORKSPACE_PATH`, the tree `scripts/validate_process.sh` runs it from, instead of the checkout root, and fails with an `::error::` when the renderer is missing. The step records `renderer_state=prepared|absent`, and validation treats dependencies as ready only when preparation succeeded with `renderer_state=prepared`. When preparation did not succeed and the renderer is present, `scripts/validate_process.sh` reports `Template renderer dependency setup did not succeed (step outcome: <outcome>)` in the log and as an `::error::` line before it checks the runtime directory. The exit code is still 14, and the outcome comes from the new `VALIDATION_RENDERER_DEPENDENCIES_OUTCOME` variable, which defaults to `unknown`.

What this means for operators: a validation run whose earlier steps partly failed no longer fails on a renderer runtime that was never prepared. Cancellation prevents dependency installation from starting after the workflow is cancelled. A skipped or failed preparation reports its step outcome when the renderer file is present; a missing renderer file fails the preparation step with its path in the error annotation.

### For contributors

Verification: `test_renderer_dependency_step_runs_after_unrelated_earlier_failure`, `test_renderer_dependency_step_checks_renderer_in_workspace_path`, and `test_skipped_renderer_preparation_surfaces_dependency_failure` each failed with its corresponding pre-fix hunk reconstructed from the PR diff. With the corrected code, `TMPDIR=/source PYTHONDONTWRITEBYTECODE=1 pytest -q tests/test_validate_workflow_validate_bootstrap.py` passed all 18 tests. The review sandbox mounts `/tmp` with `noexec`, so this run used `/source` for executable temporary fixtures.

- **Small orchestrator integration PRs no longer bypass the final-merge gates.** The deterministic review-skip path now refuses auto-merge and merge-authorization labels for integration branches, including when the configured branch pattern is invalid. An unavailable head ref also blocks merge authorization until the PR can be identified.

- **Validation in this repository now renders its harness templates from the verified support commit, not from the branch under validation.**

In coding-workflows itself, `scripts/stage_workflow_support.sh validate` used the validation checkout (often an orchestrator integration branch) as its primary support source. Templates under `workflow-templates/validation-harness/` were therefore never replaced, so a branch carrying an older `20_import_audit.sh.j2` rendered an import audit that ran on the runner's Python instead of the app container's, and validation failed with `ModuleNotFoundError: No module named 'yaml'` (runs 37315007990 and 37492941663; heal of #6031). Every such template listed in the validate manifest is now copied from a checkout pinned to the verified `WORKFLOW_SUPPORT_REF` commit. A template that differs on the branch is logged as `VALIDATE_TRUSTED_TEMPLATE_OVERRIDE path=<path> ref=<sha>`, and one summary line `VALIDATE_TRUSTED_TEMPLATES staged=<n> ref=<sha> source=<repo>` follows. When that commit cannot be checked out, a listed template is missing from it, or the checkout's destination path is a symlink or non-file, staging fails with an `::error::` instead of using or overwriting through the branch's copy.

What this means for operators: a branch that changes a validation-harness template is validated against the template in the trusted support commit until the change reaches `main`. Consumer repositories and validation runs with an explicit `target_ref` are unchanged.

- **The release smoke gate no longer fails before it starts, and runner queue time no longer counts against the Phase 4b retry.** Both of the last two nightly main→stable promotion cycles failed in `test-and-mark-stable.yml`, which kept `stable` at 2026-10-03.

"Phase 0a: Hot orchestrate-poll regression guard" runs `tests/test_orchestrate_poll_process.py` with the runner's `python3`, and that file has imported pytest since #6187. Gate run 37554001238 failed there with `ModuleNotFoundError: No module named 'pytest'` before any phase ran. Phase 0a now installs pytest when it is missing, as Phase 4b already did, and reports `status=pytest_install_failed` if it cannot. In gate run 37395952357 the Phase 4b retry review run waited for a hosted runner (`status=queued`) for most of its 25-minute `EDITOR_RETRY_BUDGET_MINUTES` window and succeeded six minutes late. Queued time seen on consecutive polls now extends that deadline, capped so the job still leaves Phase 6, Phase 7 and the finalization reserve inside `E2E_JOB_TIMEOUT_MINUTES`.

| The numbers that matter | Value |
| --- | --- |
| Seconds into the 2026-10-07 gate when Phase 0a failed | 10 |
| Retry run queued time in the 2026-10-06 gate | about 24 of 25 minutes |
| Latest point a queue-time extension can reach, default budgets | 240 minutes after the job starts (300 − 30 − 10 − 20); the base 25-minute retry budget is never shortened |
| Change to `E2E_JOB_TIMEOUT_MINUTES` or any job timeout | none |

What this means for operators: the nightly `promote-main-to-stable.yml` cycle can get past the smoke gate again, so consumer repos can receive the work merged since 2026-10-03 once a proving cycle completes. A Phase 4b `retry_timeout` now logs how many seconds the run spent queued and how far the deadline was extended.

### For contributors

The job start is recorded as `E2E_JOB_STARTED_EPOCH` in the e2e job's "Validate prerequisites" step; without it Phase 4b adds no extension. Tests: `tests/test_test_and_mark_stable_review_blocked_budget.py` (`test_phase4b_queued_retry_run_extends_the_deadline`, `test_phase4b_queue_extension_needs_a_recorded_job_start`, `test_phase4b_queue_extension_stops_at_the_job_budget_ceiling`, `test_phase0a_installs_pytest_before_running_the_hot_poller_test`).

- **A delete/modify conflict on the workspace manifest no longer blocks review/autofix.** When one side of a merge deleted or untracked `.ai/.workspace_source_manifest.txt` and the other side changed it, the conflict-resolver preparation step used to pass the file to the resolver. A resolver sandbox that excludes `.ai/` then refused the whole run (`sandbox_path_unsupported`), and every retry failed the same way until the identical-failure cap tripped (PRs #6594, #6209, #6146). Preparation now keeps the deletion (`git rm --cached`), but only when the merged `.gitignore` ignores the manifest. When the manifest was the only conflict, preparation commits the `[ai-merge-resolve]` merge itself and the resolver model is skipped.

  When the kill switch is off, the branch is an integration sync, or the conflict is unsafe to resolve deterministically, preparation instead fails with `::error::Manifest union-merge: unhandled reason=<disabled|integration_sync|stage_shape|not_gitignored> stages=<…>`. It does not dispatch a resolver that cannot access `.ai/`. The kill switch and integration-sync exclusion remain in force; such conflicts require correcting the merge or enabling safe deterministic resolution before re-running review.

  What this means for operators: review runs execute the support scripts from `main`, so affected PRs recover once this fix reaches `main`.

- **The release gate's hot poller check now installs `pytest` before it runs, and the standalone validate check has time to report its own timeout.**

`Test & Mark Stable Release` run 37624152181 failed twice. `Phase 0a: Hot orchestrate-poll regression guard` ran the poller test module before anything in the E2E job installed `pytest`, so it stopped with `ModuleNotFoundError: No module named 'pytest'`. The step now installs `pytest` with the same pip-then-apt sequence Phase 4b uses, checks the import, and fails closed with `status=pytest_unavailable` and the install log when neither works. Separately, the `validate-standalone-test` job's 30-minute cap matched the watcher's 1,800-second wait, so GitHub cancelled the job while its child validate run was still in progress and before the watcher could report. The dispatch step now has a 35-minute cap and the job a 45-minute cap, which leaves time for the soft-error analyser. The watcher's waits are unchanged, and why that child run stayed in progress is not yet known.

What this means for operators: a gate run no longer fails Phase 0a on a missing test dependency, and a stuck standalone validate run now fails with the watcher's own `timed out` message instead of a bare cancellation. The soft-error analyser runs only when the dispatch step emits a run ID.

### For contributors

`test_hot_poller_guard_installs_and_verifies_pytest_before_running` and `test_validate_standalone_job_budget_exceeds_watcher_wait` in `tests/test_test_and_mark_stable_review_blocked_budget.py` pin the install order and the step and job caps.

- **A failed review sandbox cleanup no longer skips the commit and shows up as `editor_changes_lost`.**

In `review_autofix.yml`, `Clean up isolated review workspace` ran before `Commit changes`. When cleanup exited 1, the commit step's implicit `success()` skipped it, and the `!cancelled()` uncommitted-changes detector then reported the editor's work as lost (PR #6484, run 37666355049). Cleanup now runs immediately after `Commit changes`. It still runs with `always()` and its failure still blocks the push, auto-merge and ready labels. `scripts/review_untrusted_sandbox.sh cleanup` now names the first failing check or the removal cause in a path-free `REVIEW_SANDBOX_CLEANUP reason=...` line, which is appended to the editor's stage stderr so the failure headline and fingerprint carry it. Before failing, it repairs read-only directories inside the validated sandbox root once (no symlinks followed) and tries the removal again.

What this means for operators: a cleanup failure now reports its own cause instead of a misleading changes-lost diagnosis, and unpublished edits stay blocked before push.

- **The release gate's editor smoke check can restore its canary again: the deterministic pre-write now goes into the isolated review workspace instead of the host checkout.**

`Test & Mark Stable Release` run 37669315093 failed `Phase 4b: Verify editor restored canary` because its retry review run 37674139451 ended with `Review sandbox result transfer was incomplete; refusing editor fallback. reason=host_baseline_changed`. The review sandbox snapshots the checkout before the editor step, and the smoke-only pre-write in `scripts/review_apply_fixes.sh` then rewrote `tests/e2e_smoke_canary.txt` on the host. The transfer correctly refused to publish over that change, so the canary kept its bait. When a review sandbox is prepared, the pre-write now hands the canonical three-line canary to the new `seed` action of `scripts/review_untrusted_sandbox.sh`, which writes it into the sandbox copy after checking that the host still matches the snapshot. The normal validated transfer then publishes it. The host-baseline check is unchanged. Without a prepared sandbox the old host write is kept, and a seed failure logs a warning and leaves the restoration to the model.

What this means for operators: nothing to change. The fix reaches the release gate once it is on `main`, because review runs the helpers from `review_autofix.yml@main`. If the editor model fails on every attempt, the seeded canary is not transferred and the gate reports an editor failure.

### For contributors

`scripts/review_untrusted_workspace.py seed <host> <workspace> <manifest> <path> <payload>` accepts only an allowed path already in the snapshot, refuses host drift (`host_baseline_changed`), symlinks (`symlink_in_path`) and unknown paths (`unsafe_result_path`), and never writes the host or the manifest. `tests/test_review_autofix_review_pipeline_contract.py` covers the seed-then-transfer regression and the rejections, and `tests/test_review_apply_fixes_smoke_deterministic.py` pins the sandbox routing of the pre-write.

- **The review-autofix loop stops after 5 rounds again, and a PR whose security audit runs out of cycles now goes to the review-blocked judge instead of waiting for a human.** A judge merge also passes the single-issue security pass first.

`MAX_AUTOFIX_ITERATIONS` (default 5) used to count only consecutive `[ai-autofix]` commits, so any other commit reset it to zero: an `[ai-merge-resolve]`, a Claude-session or human push, or a merged security-audit follow-up. On a busy default branch the review-blocked judge therefore rarely ran, and every new commit bought another full reviewer-panel round. The count now covers the PR's own history since its last `[judge-fix]` commit and skips other commits instead of resetting. When the single-issue security pass (`scripts/review_single_issue_security_pass.sh`) uses all `MAX_SECURITY_PASS_CYCLES`, the review run hands the PR to the judge with the open `[security-audit]` findings in its prompt. The judge can merge with medium/low findings still open as issues for fixes against the default branch, or push a fix that earns one more audit cycle. High/critical/unrated findings block a merge; they become fix attempts while retries remain, then hold the PR for a clean audit or human decision. A close-and-reissue verdict is available before the final retry. Separately, a merge the judge decides after autofix exhaustion no longer skips the security pass: it merges on a clean audit of the head and otherwise waits for the audit. At the final retry, a `fix` verdict is treated as a merge without creating a fix commit only when no blocking findings remain; a pending audit holds that merge. An audit extension counts only if its fix commit is on the audited branch, so a failed push cannot consume an extra audit cycle, and duplicate extension comments for one fix SHA count once.

| The numbers that matter | Value |
| --- | --- |
| Autofix rounds since the last judge fix on PRs #6178 and #6187 (2026-10-05) | 18 each, while the old count read 0 |
| Open PRs already past the cap of 5 under the new count | 9 of 50 |
| Most autofix rounds per PR before the final judge decision or security hold | 15 (5 per budget, `MAX_REVIEW_BLOCKED_RETRIES` = 2 judge fixes) |
| Extra audit cycles per security-mode judge fix | 1 |

What this means for operators: long-running PRs reach the judge after 5 autofix rounds instead of cycling through reviews for days. PRs labelled `ai:security-pass-failed` can merge with only medium/low findings open; high/critical/unrated findings that persist past the judge retry budget still need a clean audit or human decision. Expect `Review-Blocked Judge Decision` comments with `**Mode:** single-issue security pass exhausted`, and `judge_action=security_hold` when a judge merge waits for an audit.

### For contributors

- The "Count autofix iterations" step body moved verbatim to `scripts/review_autofix_step_count_iterations.sh` (CLAUDE.md §27 pattern) before the count changed; `review_autofix.yml` is 438,478 bytes. When the PR's base branch cannot be fetched or its merge-base is unreachable, the step warns and falls back to the old consecutive count (`AUTOFIX_COUNT_MODE=legacy_consecutive`).
- `review_single_issue_security_pass.sh` gains a `status` mode that writes no GitHub state or step output (but may fetch missing Git history), an `exhausted=true` output, and the `<!-- ai:single-issue-security-pass-extension:v1 head=<sha> -->` marker (trusted only from the pipeline account). The judge-side logic lives in the new `scripts/review_rb_judge_security_pass.sh`, sourced by `scripts/review_rb_judge.sh`; an older staged bundle without it keeps the previous behaviour. The judge fails closed on incomplete security findings and publishes an extension before pushing a judge fix, leaving the commit unpushed when publication fails.
- Tests: `tests/test_review_autofix_iteration_count.py`, `tests/test_review_rb_judge_security_pass.py` (both wired into `ci.yml`) and new cases in `tests/test_single_issue_security_pass.py`.

- **A push to a `claude/**` branch no longer gets a second full reviewer run when its PR opens a few seconds later.**

Claude sessions push a new branch and open its pull request right after, a median of 20 seconds later. The push route of `.github/workflows/internal-review.yml` (`resolve-claude-branch-pr`) looked for an open PR once, at push time, found none, and started the no-PR reviewer panel. The PR's own `pull_request: opened` run then reviewed the same commit again. The step now re-checks every 60 seconds for up to `CLAUDE_BRANCH_PUSH_PR_GRACE_SECONDS` (default 300) and skips as soon as a PR appears. A branch that still has no PR when the window ends is reviewed as before, up to five minutes later.

| The numbers that matter | Value |
| --- | --- |
| Same-commit double reviews, 2026-09-23 to 09-30 | 151 |
| PR opened within 60 s / 90 s / 5 min of the push | 131 / 133 / 133 |
| glm-5.2 reviewer tokens spent on the push-leg duplicates | about 206M of 2,295M (9%) |
| New repo variable | `CLAUDE_BRANCH_PUSH_PR_GRACE_SECONDS`, default `300`, `0` restores the single lookup |

What this means for operators: every reviewer model on the panel runs about once less for each Claude branch that gets a PR within five minutes. A branch that never gets a PR is reviewed up to five minutes later than before. Only this repository is affected, since consumer repos have no push-to-`claude/**` review route.

### For contributors

Values that are not an integer from 0 to 3600 fall back to 300 with a `::warning::`. A failed PR lookup counts as "no PR yet" and is logged as `RESOLVE_CLAUDE_BRANCH_PR_LOOKUP_FAILED` with gh's error text (`error="..."`, one line, at most 200 characters; a `GH_PAT` rate limit shows up here), so the review still runs once the window ends. A newer push to the same branch cancels the waiting run through the existing push concurrency group. `tests/test_internal_review_push_pr_grace.py` runs the step's real shell body against stubbed `gh` and `sleep`.

- **The review editor's output is no longer rejected when it shortens the audit count labels.** Every `claude/*` PR reviewed with the Claude editor since 2026-10-06 was failing all three editor attempts and ending `ai:review-blocked` after the identical-failure cap.

The editor summary's `Review file issue audit:` bullets carry four counts per reviewer file. `scripts/review_apply_fixes.sh` only accepted the full labels (`total issues listed`, `issues applied`, `issues already applied`, `issues ignored`), and the Claude editor writes `total 5; applied 0; already applied 0; ignored 5`. Each attempt was rejected as a format failure, the run finalized with "editor no-op suspicious", and the third identical round tripped the fingerprint cap (PR #6605, run 37565800725; the same fingerprint on #6606 and #6614). The prompt now shows the exact bullet shape with the full labels, and both validators (`review_apply_fixes.sh` and `scripts/validate_editor_audit.sh`) accept the short labels as aliases, with the arithmetic check unchanged. The audit-convergence check in `scripts/review_commit_changes.sh`, which lets a converged editor round skip the review-blocked judge, reads the short labels too.

| The numbers that matter | Value |
| --- | --- |
| Editor attempts rejected per review round | 3 of 3 |
| Pool quota spent per rejected round on #6605 | about 2M cached input tokens per attempt |
| Rounds before the identical-failure cap labels the PR | 3 |

What this means for operators: PRs that were stuck on the `editor no-op suspicious` comment with fingerprint `29efdb47…` complete their editor round on the next review run. A push or a review re-dispatch starts that run.

### For contributors

Tests: `tests/test_review_apply_fixes_reviewer_manifest_validation.py` (`test_short_count_labels_from_claude_editor_are_accepted`, `test_short_labels_still_need_all_four_counts`, `test_prompt_spells_out_the_audit_bullet_shape`) and `tests/test_validate_editor_audit.py` (`test_short_labels_from_claude_editor_balance`, `test_short_labels_mismatch_still_fails`, `test_short_total_label_needs_its_own_number`, `test_short_total_label_glued_to_a_digit_is_not_a_count`).

- **The live merged-PR guard hook matches its template copy again (second time today).** #6605's review autofix round could only edit `workflow-templates/.claude/hooks/pr_merge_status_guard.py`, because the review sandbox refuses the live `.claude/hooks` path, so #6605 merged with the two copies 29 lines apart and `tests-hooks-and-orchestrator` has failed on every push to `main` since 12:58 UTC.

The live copy is now byte-identical to the template, which carries the reviewed heredoc-reader narrowing and the `|&` operator from that autofix round. All 483 guard tests pass with the copies in step. Until the review sandbox can carry both copies, any autofix that edits the template needs this same sync; the `Sync live .claude copies` workflow exists for it and its run on the #6605 merge is the item to check.

What this means for operators: CI on `main` is green again and auto-merge can resume on the open PRs.

- **The live merged-PR guard hook matches its template copy again.** Since #6509 merged, `.claude/hooks/pr_merge_status_guard.py` on `main` lacked the seven-line block that `workflow-templates/.claude/hooks/pr_merge_status_guard.py` carried, so interactive sessions ran a hook that let a shell-controlled commit with per-command Git configuration (`git -c`, `--config-env`, `GIT_CONFIG_*`) through with a warning where #6509's decision Q30 says it asks for confirmation.

The live copy is now byte-identical to the template, which is what `tests/test_pr_merge_status_guard.py::test_template_copies_are_identical` requires. On `main` that test and seven guard behaviour tests failed; all 454 pass with the copies in step. Consumers receive the same file through the `.claude/` sync, so nothing changes for them.

What this means for operators: a commit such as `if true; then git -c user.name=bot commit -m x; fi` in an interactive session asks for confirmation again, as #6509's decision Q30 specifies.

- **A manifest-only merge conflict no longer fails an orchestrator project.** On `orchestrator/project-*` integration branches, `scripts/review_conflict_prepare.sh` now resolves `.ai/.workspace_source_manifest.txt` deterministically when it is the only unmerged path, instead of refusing with `unhandled reason=integration_sync`.

Tracking issue #6664 failed at final PR #6667 after `main` untracked the generated manifest (#6515): the integration sync hit a modify/delete conflict on that one file, preparation refused to touch it on an integration-sync branch, the resolver sandbox cannot see `.ai/` anyway, and the judge escalation ended the project with "Manual intervention required". The exclusion exists so that resolving the manifest cannot change the working set a following resolver run sees. When the manifest is the only conflicted path there is no resolver run, so preparation now applies the same set-merge or gitignored-deletion handling it already uses on `ai/issue-*` branches. It commits the two-parent `[ai-merge-resolve]` merge only after the integration fingerprint check has run on the merged tree and found no violation, so an auto-merged file that reverts a merged sub-issue still stops the sync. With other unmerged paths present, a fingerprint violation, or a check that cannot run, integration-sync branches still refuse with `reason=integration_sync`.

| The numbers that matter | Value |
| --- | --- |
| Conflicted paths for the new behaviour to apply | exactly 1 (`.ai/.workspace_source_manifest.txt`) |
| Project that hit it | #6664 (final PR #6667, 18:29 UTC 2026-10-07) |
| Tests | `test_manifest_only_conflict_on_integration_sync_branch_is_resolved`, `test_manifest_only_integration_sync_refuses_fingerprint_violation`, `test_manifest_only_integration_sync_refuses_without_fingerprint_check`, updated `test_manifest_union_integration_sync_fails_before_resolver` |

What this means for operators: an orchestrator project whose only integration conflict is the generated manifest heals itself on the next sync tick; no `/judge_resume` or manual merge is needed for that case.

### For contributors

`review_conflict_prepare.sh` reclassifies the branch as `integration-sync-manifest-only` before the existing `case`, so the original `orchestrator/project-*)` arm and the set-algebra pipeline are unchanged. That path sets `_mu_defer_commit`, skips the early commit and the empty-allowlist abort, and commits after the fingerprint-violation expansion only when the verifier ran (`_fp_check_ok`) and listed no file; `tests/test_conflict_manifest_union_contract.py` drives both shapes through the live union block.

- **Check-failure triage no longer crashes on ordinary pipeline PRs.** A failing CI run on an `ai/issue-<N>` PR whose source issue is not a triage issue now starts a new lineage at generation 1 instead of aborting with `parent_generation_missing_or_malformed`.

Since #6273 routed every failed `CI` run on a pull request through `scripts/check_failure_triage.sh`, the lineage step assumed the PR's source issue was itself a triage issue carrying `<!-- check-failure-triage:gen=N -->`. Every implement, activation-gap, heal and orchestrator-wave PR (all on `ai/issue-<N>` branches) hit that assumption: the triage run exited 1 before collecting any context, the `Internal: AI Check Failure Triage` workflow showed a failed run on `main`, a Telegram CRITICAL went out, and no triage issue was ever filed for the PR. A triage-born source issue still increments the generation and inherits the root; a marker that is present but not numeric still fails the run.

| The numbers that matter | Value |
| --- | --- |
| Lineage generation for a non-triage source issue | 1 (new root) |
| Lineage generation for a triage source issue at `gen=N` | N + 1 |
| Tests added | 7 (`CheckFailureTriageLineageTests` in `tests/test_check_failure_triage_workflow_security.py`) |

What this means for operators: CI failures on pipeline PRs reach the diagnosis and posting steps again, so the check-failure auto-fix loop works for every PR, not only for fix PRs of earlier triage issues. The posting step still needs the `CHECK_TRIAGE_ISSUES_TOKEN` repository secret (README secrets table); without it the run fails at `gh issue create` after diagnosis.

### For contributors

The new log line is `CHECK_TRIAGE lineage parent_issue=<N> parent_gen=none gen=1 root=<fp> reason=source_issue_not_triage`; the error key `parent_generation_missing_or_malformed` is unchanged and now means a marker was found but is not numeric. The tests drive the script's `collect` stage end to end with a fake `gh` (PR payload, source issue, empty open-triage list) and `CHECK_RUNS_AUTOFIX_ENABLED=false` so no check-run context is collected.

- **The merged-PR guard no longer prompts on pipes and heredoc text.** Since #6133 and #6135, interactive sessions in Auto mode were stopped by `merged-PR guard (CLAUDE.md §21)` prompts on everyday commands. Two cases are fixed in `.claude/hooks/pr_merge_status_guard.py` and its `workflow-templates/` copy.

A pipe anywhere before a `git push` (`git log ... | head -3; git push origin <branch>`) made the push directory "unknown" and asked for confirmation. Pipeline elements run in subshells and cannot change the directory, so a pipe now leaves it known and a `cd` inside a pipeline is ignored. A `||` branch makes the directory unknown only after a `cd` in the same `&&`/`||` list. A heredoc body with an odd quote, such as `it's` in a commit message or `'''` in a `python3 - <<'EOF'` script, made the whole command unparseable and asked "Cannot parse the Bash command" even with no git in it. Bodies that Bash passes on as data are now removed before parsing.

| Case | Before | After |
| --- | --- | --- |
| `git log \| head -3; git push origin <branch>` | asks | checked normally |
| `cat <<'EOF'` with `it's` in the body | asks | allowed |
| `git commit -F - <<'EOF'` with `main's` in the body, on a merged branch | asks | blocked, as for any commit there |
| `bash <<'EOF'` body, or an unquoted body with `$(...)` | parsed as shell | parsed as shell (unchanged) |
| `cat <<'EOF' \| bash` body, or a delimiter such as `EOF-1` | parsed as shell | parsed as shell (unchanged) |
| `env cat <<'EOF'` with `it's` in the body | asks | allowed |
| `cd "$VAR" && git commit`, `sleep 1 & git push` | asks | asks (unchanged) |

CLAUDE.md §23.D now leads with the rule to type IDs literally into `gh api` endpoint paths, since an unquoted `$VAR` or `$(...)` there prompts in every permission mode.

What this means for interactive sessions: Auto mode stops asking for routine push and heredoc commands, while a push that really could land on a merged branch is still blocked or confirmed.

- **The release smoke gate no longer fails a healthy release because it cannot find its own runs.** `test-and-mark-stable.yml` now finds the smoke issue's own Clarify, Plan and Implement runs, even past the first 100 workflow runs, and Phase 7 survives a single transient GitHub error.

Three of the four release-gate runs on 2026-09-28 and 2026-09-29 failed in the E2E smoke test while every pipeline phase passed. In runs 36374918973 and 36504041362, deep verify reported "Plan: run ID not found" and "Implement: run ID not found": the lookup read one page of 100 runs from the repository's whole run list, and the release window created 166 runs in 11 minutes, so the real run fell off the page behind newer `skipped` runs for the same issue. In run 36389883126, Phase 7 exited `lookup_failed` on one HTTP 502 while the cancel-on-close run it was polling was already in progress. The run lookup now reads further pages until it finds a match, the Clarify and Implement lookups match the smoke issue's title the way the Plan lookup already did (run 36374918973 had recorded another issue's Clarify run, 36375238437, and checked its steps instead), and each Phase 7 run listing retries up to 3 times, 5 seconds apart.

| The numbers that matter | Value |
| --- | --- |
| Run lookup pages read | 1 when page 1 matches, at most 10 (1,000 runs) per run-ID capture |
| Phase 7 listing attempts before `lookup_failed` | 3, 5 s apart |
| Failed gate runs traced to these bugs | 36374918973, 36389883126, 36504041362 |

What this means for operators: a `@stable` promotion or the daily promote cycle should no longer need a manual re-dispatch of `test-and-mark-stable.yml` after a busy release window or a single GitHub 5xx. A real phase failure still blocks the release as before.

### For contributors

The paged lookup is `find_latest_scoped_run_field` in `scripts/comprehensive_test_and_release_gh_api.sh`, used by the three `capture_run_id` definitions and the Plan wait's `latest_scoped_run_field`; all three phases now pass the smoke issue's `ISSUE_TITLE`, and each wait step stops with `run_id_missing` (Clarify, Implement) or `plan_failed` (Plan) when that title is empty instead of dropping the filter. The release gate's captures pass a 10-page cap and get exit 2 when every page up to it was full, so that walk is not retried; the default cap stays 5. The Phase 7 retry is the step-local `phase7_list_cancel_runs`. `tests/test_release_smoke_run_lookup.py` covers both and runs in its own `ci.yml` step.

- **The reviewer consensus summariser no longer fails a review run by trying to open reviewer files it cannot read.** Its prompt now says every reviewer output is inlined, and that it must not call tools.

`scripts/summarize_reviewer_consensus.sh` used to tell `openai/gpt-6-luna` that the full reviewer outputs "remain on disk" under `/tmp/codex-pr-…/previous_reviews/`. When an inlined input looked incomplete (one line of narration, or a failure notice such as `Reviewer minimax/minimax-m3 failed after non-retryable error on attempt 1.`), the model tried to `Read` those files. OpenCode's reviewer config has no `external_directory` rule, so each read was rejected and the session ended with no text. The script then used all 10 attempts and failed the `Run reviewer models` step. The prompt no longer names the on-disk path. The pass-2 cross-pollination header in `scripts/review_run_reviewers.sh` sent pass-2 reviewers, which use the same config, to the same `/tmp` files; it now says the pass-1 ledger is their only pass-1 input. The same script's reviewer prompt also told every reviewer to read files under `previous_reviews/` and `runtime_context/` in `/tmp` (for example `git_status.txt` and `environment_sorted.txt`), and those reads were rejected too. The prompt now says those files cannot be read and to review from the prompt and the repository files. In the no-PR `claude-branch-review` mode, the `Telegram failure` alert from `review_autofix.yml` now shows `Branch: <head ref> (no PR)` instead of `PR: …/pull/` with no number.

| The numbers that matter | Value |
| --- | --- |
| Runs that failed this way | 6 (36518137244, 36545663590, 36573586774, 36598991686, 36656409877, 36666750539) |
| Rejected reads per failed attempt | 3 (one per reviewer file) |
| Reviewer prompt spots that pointed at unreadable `/tmp` files | 4 (`scripts/review_run_reviewers.sh`) |
| Time spent per failed run | 10 attempts, about 43 minutes of backoff |

What this means for operators: `claude/*` branch pushes without a PR should stop producing "PR autofix failed" alerts caused by an empty summariser. When one of those alerts does fire, it names the branch.

### For contributors

The retry loop and the reviewer-role OpenCode permissions are unchanged. The `failed after retries` input filter still misses today's reviewer failure notices. It was left alone because the consensus ledger feeds the Claude-fixer hand-off, and dropping failed reviewers would change which `FINDINGS FROM` blocks it sees. Test: `tests/test_summarize_reviewer_consensus_prompt.py`, run in the `Summariser sandbox-mode pin contract test` step of `ci.yml`.

### For contributors

The new source runs after the `REISSUE_FILES_TOUCHED_UNION` step and only on the spot-fix path. A rejected entry is skipped and never forces `redo`, which matches the closed-PR union. A judge that omits the field produces a byte-identical issue body. Change `prompts/_templates/mode-judge-review-blocked.txt` together with the runtime prompt.

- **The release smoke test no longer reports Clarify, Plan, or Implement success without that phase's run ID.** `test-and-mark-stable.yml` now finds each phase's run past the first 100 runs, only for the smoke issue's own title, and when no run can be found the phase fails at capture with `status=run_id_missing`.

Run 36374918973 blocked the stable release at `Internals .. FAILED` even though its totals showed 0 failed steps. The Plan wait step read one 100-run page of `actions/runs`. As skipped `issue_comment` runs piled up, the smoke issue's Plan run slid to index 96 of that page and then off it, so the step wrote `status=success` with an empty `run_id`. That skipped `Phase 2b: Soft-error analyser (plan)` and left deep verification to fail the release as `Plan: run ID not found`. Run 36504041362 failed the same way at Implement: the smoke issue's Implement run sat at index 137 of the window when the PR appeared. The Clarify and Implement captures also had no issue-title filter, so a newer run from the parallel alt-model smoke job could be taken as ours. All three captures now walk later pages of the same query, still matching only non-skipped runs of that phase whose `display_title` is the smoke issue's title. A missing ID now stops the gate at that phase's line. The check that keeps the Plan step waiting while another Plan run for the issue is still active pages the same way, so an older active run past page 1 no longer ends the step as `plan_failed`. When that check cannot read a page, it retries only until the Plan phase's inactivity limit and then fails with `status=timeout`, instead of polling until the job's 300-minute limit.

| The numbers that matter | Value |
| --- | --- |
| Pages read by each run-ID capture (Clarify, Plan, Implement) | 1 normally, at most 10 per attempt (GitHub's 1,000-result ceiling); a walk that reads all 10 full pages without a match is not retried |
| Pages read by each 10-second Plan status poll | 1 (unchanged) |
| Pages read by the "other active Plan runs" check (Plan completed without its label) | 1 when page 1 holds an active run, otherwise up to 10, stopping at a short page |
| New status value for `wait-clarify`, `wait-plan`, and `wait-implement` | `run_id_missing` |
| Retries of an unreadable runs page in the "other active Plan runs" check | until `PLAN_PHASE_TIMEOUT` (default 60 minutes) without activity, then `status=timeout` |
| Runs created in the first failed window (03:46–03:57 UTC) | 166 |

What this means for release operators: a busy repository no longer turns a successful Clarify, Plan, or Implement phase into a release block with no failed steps, and deep verification checks the smoke issue's own runs rather than the alt-model job's. If a phase's run really cannot be found, the gate prints `FAILED (run_id_missing)` on that phase's line and the wait step names the issue and title it searched for.

### For contributors

The Plan change is in the `wait-plan` step (`fetch_plan_runs_page_json`, `latest_scoped_run_field`, `require_plan_run_id`, `fail_plan_confirm_retry_if_idle`). `wait-clarify` and `wait-implement` keep their own `capture_run_id`, now paged and title-scoped, plus `require_scoped_run_id`, and take `ISSUE_TITLE` from `steps.create-issue.outputs.title`; like `wait-plan`, they fail before polling when that title is empty, because an empty title would match a run with an empty `display_title`. `tests/test_test_and_mark_stable_plan_polling_guard.py` runs the real step scripts against a stubbed `gh` to cover the missing-ID, later-page, other-issue, page-cap, and unreadable-page cases for all three captures, and the empty-title case for `wait-clarify` and `wait-implement`.

### For contributors

Issue #4305's heal traced this failure to the reviewer-majority shortcut that PR #4306 removed; on run 35802596362 Phase 4 had in fact waited for the review run to complete, so that change did not cover it. The retry still has the same 25-minute budget (`EDITOR_RETRY_BUDGET_MINUTES`), and a full review pipeline can take longer, because the job's serial budget is already 295 of its 300 minutes.

- **Workflow failure heal now fixes a failing coding-workflows pull request on that pull request's own branch, instead of opening a `stable` hotfix.** A repeated review/autofix failure on a PR in this repo files its heal issue with `Target branch:` set to the PR's head branch.

In coding-workflows, `review_autofix.yml` resolves `SCRIPT_REF` to `github.sha`, so a review run executes the pull request's own workflow code. When that code breaks the run, the defect may not exist on `stable`. Heal issue #4329 hit this: PR #4323 added an unguarded workspace-guard sandbox call that exited with systemd status 226 on all five review runs. The heal was still filed with `Target branch: stable`, and its fix PR #4332 opened against `stable` carried 100 files of unreleased `orchestrator/project-4139` work. `scripts/workflow_failure_heal_intake.sh` now targets the PR's head branch for a self-repo `autofix_failure` report, falls back to `stable` (`warn source_pr_branch_missing`) when that branch is gone, and adds `target_branch_source=default|failed_run_branch|source_pr_head` to the `WORKFLOW_HEAL created` log line.

| The numbers that matter | Value |
| --- | --- |
| Review runs lost on PR #4323 before the heal fix could land | 5 (exit code 226) |
| Files in heal PR #4332 against `stable` vs against the PR branch | 100 vs 11 |
| Extra GitHub API calls | none when the PR branch exists; one more branch lookup when it is gone |

What this means for operators: a heal fix for a coding-workflows PR now opens against that PR's branch, so it unblocks the PR directly and reaches `main` and `stable` through the PR's normal merge path. Consumer reports and failed release runs keep their current targets, `WORKFLOW_HEAL_TARGET_BRANCH` and the failed run's branch.

- **A conflicted PR whose same-head review state is terminal now gets its merge conflict resolved, and reviewer slots added on main no longer fail on older PR branches.** Before this, PR #4332 was re-dispatched every poller tick for six hours without the conflict ever being touched, and PR #4323 lost two of its six reviewers before launch.

When a PR's cached same-head resume state is terminal (`resume_state=no_progress` or `round_budget_exhausted`), `review_autofix.yml` skipped `Detect merge conflicts` and both resolver steps, even though the gate keeps a conflicted PR on `codex-agent` so those steps can run. The poller's standalone conflict sweep dispatched PR #4332 again and again, and every run restored the terminal state, skipped the resolver, and exited green. Those steps now run on terminal resumes, and `Push all pending commits` pushes on that path only when the conflict was resolved. The editor and `Commit changes` still skip, so a terminal run never pushes autofix edits. Separately, `REVIEWER_MODELS` comes from the workflow ref while `scripts/codex_model_catalog.json` is staged from the PR branch, so a branch forked before the September reviewer roster refresh had no row for `z-ai/glm-5.2` or `google/gemini-3.1-flash-lite`. `Stage workflow support files` now appends the missing rows from the main snapshot and leaves the branch's own rows alone.

| The numbers that matter | Value |
| --- | --- |
| No-op review dispatches on PR #4332 (`ai/issue-4329`) | 40+ between 2026-09-23T18:00Z and 2026-09-24T00:03Z |
| Reviewer slots lost per run on PR #4323 (run 35933627432) | 2 of 6 |
| New log key | `MODEL_CATALOG_BACKFILL added=<n> slugs=<list> source=main_snapshot` |
| Extra GitHub API calls | 0 |

What this means for operators: a conflicted PR stuck in a terminal review state now gets its merge resolved on the next dispatch instead of looping silently, and a PR branch that predates a reviewer roster change keeps its full reviewer panel. A catalog read or parse failure only warns and leaves the branch catalog as it was.

### For contributors

`tests/test_review_autofix_review_pipeline_contract.py` evaluates the real `if:` predicates for a terminal, conflicted run, and runs the extracted backfill block followed by `scripts/write_opencode_config.sh` against a stale catalog. All four new tests fail against the previous workflow. The re-trigger dispatch step keeps its terminal skip, because the resolved push's `synchronize` event reviews the new head.

- **Workflow failure heal now sees the runs that failed, names reviewer failures correctly, and cleans up heal PRs when their source PR closes.** The identical-failure cap's heal report links the failed review runs, and a failed reviewer step is reported as `reviewers_failed` rather than `editor_empty_noop`. When a pull request in coding-workflows closes, its heal PRs are closed or brought up to its base and re-pointed.

On PR #4323, review/autofix stopped after three identical runs reported as `editor_empty_noop`. The heal issue filed from the cap (#4342) said "no failed run could be linked" and blamed the editor. In fact every reviewer slot and the summariser had exited with systemd status 226, and the editor never ran. The fix PR (#4349) then stayed open and merge-queued on #4323's branch after #4323 closed unmerged. Three changes address this. The `fingerprint-cap-block` job passes the PR comments it already fetched to `scripts/workflow_failure_heal_autofix_report.sh`, which lists the runs named by the head's `review-autofix-failure:v1` markers in `run_refs`. A failed `Run reviewer models` step is reported as `reviewers_failed`, with per-slot and summariser exit codes as evidence. And the new `heal-pr-reconcile` job in `internal-cancel-on-pr-close.yml` runs `scripts/workflow_failure_heal_pr_reconcile.sh` when a pull request closes.

| The numbers that matter | Value |
| --- | --- |
| Failed runs listed in a cap report's `run_refs` | up to 3, newest first |
| New API calls in the cap path | 0 |
| Self-named script error lines kept as reviewer-failure evidence | up to 10 |
| Heal PR outcome when the source PR closes unmerged | closed; heal issue closed as not planned |
| Heal PR outcome when the source PR merges | source head and base merged in (fast-forward push), PR re-pointed |

What this means for operators: a cap-triggered heal issue now carries the failed runs' logs, and an error line such as `untrusted_process_sandbox: …` counts as the crash file for ownership routing when that script exists. Review retries are unchanged: a reviewer failure still posts the "AI review/autofix produced no output — will retry" comment and applies no immediate `ai:review-blocked`. A heal PR no longer outlives its source PR on a successful reconciliation: it is closed, or re-pointed at the base with a diff of only its own changes. An explicit remote rejection against an unchanged heal branch or a failed base update closes the heal PR; ambiguous push failures emit workflow warnings without claiming success. No force push is used, so the repository's non-fast-forward rule is respected. Set `WORKFLOW_HEAL_PR_RECONCILE_ENABLED=false` to skip the reconcile job before checkout.

### For contributors

- New env vars, both with defaults: `AUTOFIX_FAILURE_MARKER_AUTHOR` (reporter, default empty) and `WORKFLOW_HEAL_PR_RECONCILE_ENABLED` (repository variable, default `true`). New run flag: `AUTOFIX_REVIEWERS_FAILED`. New `workflow_failure_heal.py` subcommand `reviewer-failure-evidence` and flag `build-autofix-payload --failure-marker-author`; the reporter passes the flag only when the staged helper supports it.
- `reviewers_failed` ranks above `editor_empty_noop` in `derive_autofix_failure_reason`, in the reporter, and in the run summary's `finalize_reason`. Every fingerprint call site reads `reviewers_failure_evidence.txt` first, and missing files are skipped, so other failures keep their fingerprints.
- `extract_crash_file` now maps `::error::resolve_integration_ref.sh: …`-style lines to `scripts/<name>` when the script sits next to the helper; unknown names still give nothing. Ownership is unchanged: when the PR and its base both changed the file, the PR owns it.

- **The workflow failure heal no longer re-files failures that are already fixed, and its diagnosis now reads what the failing step printed rather than the step's source.** Promote-cycle gate failures now join their existing heal lineage, and a failure fixed after the commit that ran is closed out with a note instead of an issue.

Issue #4368 showed three gaps in the heal intake (`scripts/workflow_failure_heal_intake.sh`). First, each promote cycle's Test & Mark Stable Release run carries a unique `[cycle:<id>]` suffix in its run name, and that suffix went into the dedup fingerprint, so every cycle's failure looked new. Second, the error signature and the log tail the model reads came mostly from the step script GitHub echoes at the top of each `run:` step, not from the step's output. Third, the model was never told that the branch had moved past the failing commit. #4368 was filed for a Phase 4b failure whose fix (#4351) had reached `main` 13 minutes after the gate started. The pipeline then planned it and ended with the implementer answering BLOCKED.

| The numbers that matter | Value |
| --- | --- |
| Echoed script lines in the #4368 gate job log (now dropped) | 2,340 of 3,726 |
| Extra GitHub API calls per heal intake | 1 (`compare/<failing sha>...<branch>`) |
| Commits / changed files shown to the model | newest 60 / first 200 |
| Earlier heal issues shown to the model | up to 5, same fingerprint or lineage root |

What this means for operators: expect fewer `ai:workflow-heal` issues that end in `ai:blocked` with "fix already exists". A failure that is already fixed now logs `WORKFLOW_HEAL no_issue classification=already-fixed` and sends a DEBUG Telegram note. A consumer report also gets a comment saying the next wrapper sync picks up the fix. The one-time cost: fingerprints computed from Actions job logs change with this release, so a recurrence of a failure that already has an open heal issue may open one new issue instead of adding an occurrence comment.

### For contributors

- `fingerprint()` in `scripts/workflow_failure_heal.py` drops a trailing `[cycle:<digits>]` from the workflow name. `filter_log()` removes the ANSI-cyan script lines inside `##[group]Run` headers. The first signature bucket also matches `##[error]`.
- `already-fixed` is honoured only when `check-already-fixed` finds a commit from the compare list cited under the diagnosis's `## Fixed by` section. Otherwise the report is filed as `inconclusive` and the intake logs `warn already_fixed_unverified reason=…`.
- The heal-issue list query also returns `title`, `closed_at` and `state_reason` in the same call. The branch-tip checkout (`HEAL_BRANCH_TIP_DIR`) uses `git fetch`, not the API.

- **Review/autofix no longer runs PR-head support scripts with workflow credentials.** Same-repository PR runs resolve a protected `main` commit independently of their PR event SHA, while consumer release refs and immutable pins select the corresponding release commit. Every review job pins executable support to that SHA and keeps the PR checkout for review and edits. Missing trusted identity or required support fails closed; missing optional helpers and failure reporters skip without executing PR worktree fallbacks, even if staging failed before the failure-reporting step.

- **Clarification agents no longer run with access to runner credentials.** Clarify and orchestrator clarify-respond model calls now execute inside a read-only container with a sanitized source snapshot, no Git metadata or host tokens, and no external network. A narrow host-side model broker keeps the provider key outside the container. Issue fetching, memory, comments, retries, and clarification output parsing remain on the runner.

### For contributors

Both clarification workflows stage `clarify_isolated_run.sh`, `clarify_openrouter_broker.py`, and the pinned sandbox Dockerfile with their support files. Missing Docker, image build failures, or broker errors never fall back to host Codex execution.

- **Workflow failure heal diagnoses retain the full ownership safeguards when the prompt is assembled.** Review/autofix failures are no longer attributed to PR or base-branch changes solely because a path matches a trusted runtime helper, and fileless pipeline failures require corroborating evidence before being classified as self-inflicted. This restores parity with the legacy prompt and the release gate's byte-for-byte assembly check.

- **PR build code and review editor commands no longer run in the credential-bearing checkout.** Review dependency installation and the OpenCode writer run in a disposable, restricted container. A fixed-path model relay retains the provider key on the host; only validated edits return to the checkout. Missing isolation prerequisites stop the editor instead of falling back to host execution.

- **Review/autofix retries a conflict resolution that edits outside its captured conflict set only after restoring the pre-attempt worktree.** An unsafe or unverifiable restore still stops the run without committing.

In the workflow source repository, `scripts/review_conflict_resolve.sh` now checks the entire resolver attempt before accepting its result. An out-of-scope edit discards that attempt, verifies the worktree and merge index match the pre-attempt snapshot, and gives the resolver scope-specific feedback on the next bounded attempt. The existing final `check_resolver_diff.sh` commit gate remains in place. Consumer-repository resolver runs are unchanged because the per-attempt snapshot is source-repository-only.

| The numbers that matter | Value |
| --- | --- |
| Resolver attempts | Up to 3 (`INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS`) |
| Snapshot scope | Source-repository resolver attempts only |

What this means for operators: an out-of-scope resolver edit no longer immediately ends an otherwise recoverable source-repository review run. If the snapshot, worktree, or merge index cannot be verified or restored, the run fails closed and leaves the final commit gate intact.

- **Validation targets now require a trusted project PR, and validation hooks run without credentials.** Explicit project branches are authorized against an open, same-repository PR and checked out at its verified head SHA without storing the PAT in the checkout. All four optional validation workspace hooks run in a network-disabled container over a screened workspace copy, preventing branch-provided hooks from reading the runner's credentials or Git metadata.

- **Claude-fixer verdicts no longer enable auto-merge without a fresh independent review.**

Verdict convergence now requires a dedicated bot login configured with `CLAUDE_FIXER_VERDICT_BOT_LOGIN` (empty by default), a verdict bound to the latest workflow-owned hand-off's head, round and SHA-256 consensus-ledger digest, and a new reviewer-panel run. Only a clean review with a fresh `ready` check-run snapshot for the same head can enable auto-merge. Repeated findings block the PR for intervention instead of repeating same-head verdict cycles. Operators must privately provision the bot's issue-comment write credentials and confirm that its comment is posted as the bot; a collaborator comment or a Claude Code Web proxy-authored comment is not accepted. Existing v1 markers remain readable but cannot authorize convergence alone.

- **Validation hooks can no longer replace code in the credentialed workspace.** Container output replay accepts only non-executable, hook-specific plain-text data files; any source, harness, configuration, or other mutation fails validation before replay.

- **Security audits no longer accept exclusion rules from the branch being audited.** The workflow resolves the exclusion catalog against its verified support checkout so branch-authored catch-all rules cannot suppress findings.

The weekly, manual and consumer security-audit workflow now passes an absolute path to unchanged tracked support content, and fails closed for missing, malformed or out-of-checkout catalogs. Overrides that relied on an audited-repository copy must be moved into the trusted workflow support checkout; other callers of `scripts/security_audit.sh` retain their existing resolution behavior.

- **The conflict resolver no longer rejects correct resolutions when `CLAUDE.md` is in conflict.** Symlinks are now compared by their link text when the resolver and the autofix editor work out which files they touched.

`workflow-templates/CLAUDE.md` is a symlink to `../CLAUDE.md`. The resolver hashed it with `git hash-object`, which follows the link, so the symlink looked edited every time `CLAUDE.md` changed. `check_resolver_diff.sh` then aborted the resolution as "edited files outside the conflicted set". Because the check fails the same way on every retry, PR #4443 hit the identical-failure cap and stayed `ai:review-blocked`. A symlink that is actually retargeted or deleted is still reported as touched.

| The numbers that matter | Value |
| --- | --- |
| Sites fixed | 4 (`review_conflict_prepare.sh`, `review_conflict_resolve.sh`, `review_commit_changes.sh`, the pre-editor snapshot in `review_autofix.yml`) |
| Identical failures on PR #4443 before the cap | 6 |

What this means for operators: PRs that conflict on `CLAUDE.md` now resolve through `review_autofix.yml` like any other conflict. No setting changes.

### For contributors

`tests/test_touched_set_symlink_aware.py` runs the extracted snapshot and compare loops against a repo that contains a symlink, and pins the two snapshot loops as identical.

- **Isolated validation renderer imports from pull-request code.**

Validation now installs renderer dependencies in a runner-side virtual environment and checks their resolved origins before running the renderer. Its Python probes and renderer run in isolated mode from a trusted directory, preventing a pull request's shadow modules or Python path settings from executing with validation credentials. Missing or externally resolved dependencies retain the existing harness-error reporting path.

- **Claude-routed issues now get their full issue-mode project again: an hourly pickup session starts the implementation session instead of the routine.** A session that cannot run the chain now stops on the issue instead of shipping a direct edit.

The smoke test on issue #4525 showed that a claude.ai routine run has no claude-code-remote tools (`create_session`, `send_later`, `get_session`, `add_repo`). So the "Claude issue dispatcher" could not start the implementation session. Its fallback implemented the issue in-session as auto-decision AD-1: no plan, no project branch, and no conformance, security or validation pass. `claude-issue-intake.yml` now queues each routed issue as an `ai:claude-issue-queue` issue in coding-workflows. The new `/claude-issue-pickup` session, woken hourly by a trigger bound to itself, starts the Opus `/implement-issue-claude` session for each item and closes it. `claude-issue-queue-watchdog.yml` alerts when an item waits too long. `claude-issue-dispatch.md`, `/implement-issue-claude` and CLAUDE.md §28.C now fail closed with `ai:claude-blocked` when the session tools are missing. The routed comment also reports reason `default` when `AI_ISSUE_IMPLEMENTER` is unset.

| The numbers that matter | Value |
| --- | --- |
| Longest wait before the implementation session starts | about 60 minutes (one pickup wake) |
| Sessions started per pickup wake | at most 10 (the rest wait for the next wake) |
| Watchdog cadence / stale threshold | hourly at :17 / `CLAUDE_ISSUE_QUEUE_STALE_HOURS`, default 3 |
| New labels | `ai:claude-issue-queue`, `ai:claude-issue-queue-stale` |
| Deprecated, unused | `CLAUDE_ISSUE_ROUTINE_ID`, `CLAUDE_ISSUE_ROUTINE_TOKEN`, `CLAUDE_ISSUE_ROUTINE_BETA` |

What this means for operators: start the pickup once by running `/claude-issue-pickup start` in a claude.ai cloud session on coding-workflows that you open from the app, in Auto mode. It refuses to start more than 3 session links deep, because the implementation chain needs 4 more links below it and the session tools stop at 8. If Telegram reports stale queue items, run `/claude-issue-pickup start — restart`. The "Claude issue dispatcher" routine and its variable and secret can be deleted. Consumer repos need no change.

### For contributors

`scripts/claude_issue_route.py` gains `queue-issue`, `queue-pending` (with `--fetch-repo`, one REST read) and `queue-stale`. Only queue issues opened by `github-actions[bot]` for registered repos are acted on. The queue issue is created with `GITHUB_TOKEN`, so neither it nor the pickup's close starts a workflow. `clarify.yml` passes `vars.AI_ISSUE_IMPLEMENTER || ''`. Tests are in `tests/test_claude_issue_route.py` and `tests/test_implement_issue_claude_command.py`; the removal-registry entries are in `docs/scripts-pending-removal.md`.

- **The `/implement-plan-claude` checker now stops waiting on a security follow-up whose pipeline gave up.** An issue-list wait ends with `state: blocked` when a waited-on issue is still open and carries `ai:review-blocked`, `ai:review-autofix-failed`, or `ai:needs-human`.

Until now `.claude/scripts/check_in_status.py --issues` only finished once every issue was closed or labelled `ai:merged`. A follow-up whose fix PR was review-blocked never gets there on its own, so the checker kept re-arming every hour and the 24-hour safety net re-ran the same check, with nobody told. This happened with security follow-up #4512 of `heal-deterministic-autofix-failures`, whose fix PR #4516 stayed conflicted and blocked. The checker now starts the `security-pass <k>/5 — blocked` stage, which stops at `Status: BLOCKED` and asks, as step 9 of `.claude/commands/implement-plan-claude.md` now spells out. Closed and `ai:merged` issues still count as resolved first, whatever other labels they carry.

| The numbers that matter | Value |
| --- | --- |
| Labels that end an issue wait as blocked | `ai:review-blocked`, `ai:review-autofix-failed`, `ai:needs-human` |
| Extra GitHub API calls | 0 (the label is read from the existing one-call-per-issue read) |

What this means for operators: a blocked security follow-up now reaches you within about an hour instead of sitting silently until someone notices.

### For contributors

The script and the command file are mirrored to `workflow-templates/.claude/`; `tests/test_check_in_status.py` pins the new verdict, the closed/merged precedence, template parity, and the command-doc wording.

- **The §26 status check-in and the hourly `claude/*` catch-all sweep now pick up a review hand-off whose review run was triggered by an earlier push than the head it reviewed.**

When two pushes land close together on a `claude/*` pull request, GitHub records the first push as the review run's `head_sha`, but the review workflow reviews the pull request's head when it runs and posts its hand-off for the second push. `.claude/scripts/check_in_status.py` required the run's `head_sha` to equal the pull request head. As a result, `--hand-back` and `scripts/claude_pr_sweep.py` reported `waiting for verified completed review run` indefinitely and no Claude fixer was started (PR #4594, run 36290049170: run head `a59fc87`, reviewed head `d1c6f92`). The check now accepts a run whose triggering commit is the reviewed head or an ancestor of it, confirmed with one compare read. Every other check on the run and on the hand-off comment is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Extra REST reads per PR check | at most 1 (`compare/<run head>...<reviewed head>`), only when the two commits differ |
| Compare results accepted | `ahead` only; `diverged`, `behind` and malformed SHAs keep waiting |

What this means for operators and consumer repos: review findings on a `claude/*` pull request reach the pushing session, or a fresh `/fix-claude-pr` session from the sweep, even after back-to-back pushes. Consumer repos get the updated `workflow-templates/.claude/scripts/check_in_status.py` on the next `@stable` sync.

- **A pull request whose review keeps failing now gets one workflow heal issue, not one per failed run.** Heal fix PRs whose own review fails now count toward the lineage cap instead of starting a new chain.

The workflow failure heal intake (`scripts/workflow_failure_heal_intake.sh`) de-duplicated review/autofix failure reports only by fingerprint, and that fingerprint hashes each run's own failure evidence, so it changed from run to run. PR #4323 opened five `ai:workflow-heal` issues and PR #4348 opened three, two of them (#4409 and #4416) open at the same time. Reports also never carried a lineage generation, so a heal fix PR (branch `ai/issue-<N>`) whose review failed opened a fresh generation-1 heal issue, which got its own fix PR, and the loop went on (#4390 → PR #4394 → #4407 → PR #4408 → #4411 → PR #4413 → #4417). An `autofix_failure` report is now also keyed on its pull request through the heal issue's `<!-- workflow-failure-heal:source=owner/repo#N -->` marker. An open heal issue from the same PR gets an occurrence comment (log `duplicate … match=source`) and no diagnosis model run. A closed one continues its lineage, and so does the heal issue that an `ai/issue-<N>` head branch fixes. `WORKFLOW_HEAL_MAX_LINEAGE_DEPTH` therefore stops the loop.

| The numbers that matter | Value |
| --- | --- |
| Heal issues from review/autofix failures, 2026-09-23 to 2026-09-25 | 18 |
| Same reports replayed through the new decision | 10 opened, 6 occurrence comments, 2 escalations |
| Extra GitHub API calls | 0 (the intake already lists every `ai:workflow-heal` issue) |
| Caps (unchanged) | lineage depth 3, 10 open, 20 per UTC day |

What this means for operators: a failing PR produces one heal issue plus occurrence comments, and a heal-fix chain escalates to a human (`ai:workflow-heal-escalated` + Telegram CRITICAL) at generation 4 instead of looping. Release-run and escalation-label reports are unchanged.

### For contributors

`budget_decision` in `scripts/workflow_failure_heal.py` takes two optional inputs, `source_key` and `linked_heal_issue`. The `budget` CLI takes them as `--source-key` and `--source-head-branch`, and `heal_fix_branch_issue` parses `ai/issue-<N>`. The intake passes both only for `autofix_failure` payloads. A fingerprint duplicate still wins over a source duplicate, and an inherited `source_gen` still wins over both lineage sources.

- **Workflow failure heal now recognises a pull request that breaks its own review run even when the failure names no file.** Such failures get a diagnosis comment on the PR instead of a new `ai:workflow-heal` issue against the shared workflow.

Self-inflicted routing used to need a crash file taken from the evidence (a `scripts/<name>: line N:` shell error or an `::error::` line naming a path). On PRs #4323, #4332, #4376 and #4379, the branch's own sandbox made every reviewer slot, the summariser and the editor exit with systemd status 226 and no output. With no file named, the intake built no `## Ownership facts` block, and the diagnosis had to call it a `workflow-defect`. That filed #4329, #4353 and #4377, whose fix PRs sat on the same broken branches and failed the same way. `scripts/workflow_failure_heal_intake.sh` now adds a second basis when there is no crash file: the run staged the PR head's own scripts (`script_ref` equals the head SHA) and the PR changes pipeline files. The intake then reports ownership `pr`, lists those files in the facts block, and logs `WORKFLOW_HEAL crash_ownership=pr crash_file=none basis=pipeline_files files=<n>`. `prompts/mode-workflow-failure-heal.txt` allows `pr-self-inflicted` on that basis only when the whole pipeline fails the same way.

| The numbers that matter | Value |
| --- | --- |
| Pipeline paths | `scripts/`, `.github/actions/`, `.github/workflows/review_autofix.yml` |
| Files listed in the facts block | up to 20 |
| Extra API or git calls | none (uses the report's `changed_files`, `script_ref` and `head_sha`) |
| Switch | `WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED` (unchanged, default `true`) |

What this means for operators: a PR whose own pipeline changes stop every model from running now gets the diagnosis on the PR for the review-blocked judge, instead of a chain of heal issues that retarget the same broken branch. Base-branch ownership still needs a crash file, and consumer reports are unaffected.

- **`/implement-plan-claude` fixes from its first end-to-end run: validation verdicts, completion PRs, checker status, stall handling, and prompt-free check-ins.** Five problems the dummy two-phase project hit are fixed in the command text, and every check-in now runs on Sonnet in Auto mode so it stops asking for approval.

The validation stage looked for a `VALIDATION_FAILURE_SUMMARY` log line that a passing `validate.yml` run never prints; it now reads `validation_status.json` from the `ai-validation-<run-id>-<attempt>` artifact first, then the record step's `STATUS_VALUE`, and treats the summary line as failure-only. The completion PR now always carries `Refs #N` lines (the tracking issue, or the project's merged phase PRs), because `lint-plan-archival.yml` rejects an archival PR that references nothing. The checker no longer archives itself after starting the next stage, which cut its turn off and left it shown as FAILED; the next stage archives it. A `— source revision` test run now reads the plan and progress log from `origin/<default>`, where they actually live. And the command, `agents.md`, and the safety net now say that only the chain archives its own sessions: archiving a waiting checker by hand kills the project's only pending check-in.

Every check-in, for `/implement-plan-claude` waits and CLAUDE.md §26 PR check-ins alike, now runs on Sonnet in Auto mode instead of Haiku. The claude-code-remote write tools a checker calls (`send_later`, `create_session`, `archive_session`, the trigger tools) ask for approval on every call outside Auto mode, with only *Deny* / *Allow once* and whatever `permissions.allow` says, and Haiku 4.5 cannot run in Auto mode, so every Haiku re-arm stopped at a prompt. A Sonnet trial checker for PR #4366 ran a first check and a woken re-check with no prompt. The interval stays at 3 hours to keep the Sonnet cost down.

| The numbers that matter | Value |
| --- | --- |
| Dummy project used to find these | plan PR #4307, phase PRs #4308 and #4310, completion PR #4355 |
| Validation verdict sources, in order | `validation_status.json`, record-step env, `VALIDATION_FAILURE_SUMMARY` |
| Checker model | `claude-sonnet-5` in Auto mode (was `claude-haiku-4-5-20251001`) |
| Check-in interval | 180 minutes (unchanged) |
| Sonnet trial cost | about $0.32 for the first check, about $0.13 for a woken re-check |

What this means for operators: `/implement-plan-claude` runs no longer need a hand restart after a passing validation or a stale checker, the session list stops showing healthy checkers as FAILED, check-ins run without you approving each re-arm, and to nudge a stalled project you start its next stage session with a `— resume.` block rather than archiving anything.

### For contributors

`tests/test_implement_plan_claude_command.py` (own `ci.yml` step) pins these rules and the byte-identical `workflow-templates/.claude/commands/implement-plan-claude.md` copy.

- **`/implement-plan-claude` no longer treats a failed security-audit or validation run as a clean pass.** A run that ends in anything but `success` now stops the project at `Status: BLOCKED` and asks, instead of moving on to the next stage.

A failed `security-audit.yml` run posts no section to the `AI Security Audit Tracker` issue and opens no `ai:security` follow-ups, which looks exactly like a clean audit unless the run's conclusion is read first. The end-to-end test of the command hit this: run 35821734999 failed on a codex `ENOENT` error and the next stage went straight on to validation. Steps 8 and 9 now read the run conclusion before anything else, record the failing step from `gh run view --log-failed`, and ask whether to re-dispatch after a fix or skip the pass. `.claude/scripts/check_in_status.py --run` now reports `state: failed` for a non-`success` conclusion, so the Haiku checker starts the stage as `security-pass <k>/5 — run failed` / `validation <k>/3 — run failed`.

| The numbers that matter | Value |
| --- | --- |
| Conclusions treated as success | `success` |
| Run that exposed the bug | 35821734999 (`security-audit.yml`, `failure`) |

What this means for operators: a broken audit or validation workflow now shows up as a blocked `/implement-plan-claude` session with the failing step named, rather than a project that reaches `docs/completed/` without a real security pass.

- **`/implement-plan-claude` now runs one checker session per project instead of one per stage, so long projects no longer stall at the session depth limit.**

The claude-code-remote tools refuse `create_session`, `send_later`, and `create_trigger` from a session 8 parent links below its root. Each stage used to create its own Sonnet checker, and each checker created the next stage, so every hand-off added two links. The `heal-deterministic-autofix-failures` project hit the limit at its fourth security cycle on 2026-09-25. The only fallback left was a session-local cron job, which died with the container, so the project stalled for about 14 hours with nobody notified. The command now keeps a single checker, `implement-plan <slug> — checker`, for the whole project. That checker starts every stage session, and each stage hands it the next wait through a one-shot trigger. A depth-limit refusal now stops the project as `BLOCKED`, with a push notification and a ready-to-paste resume prompt.

| The numbers that matter | Value |
| --- | --- |
| Session lineage limit | 8 parent links |
| Depth per hand-off, before / after | +2 per stage / constant (checker at *d+1*, stages at *d+2*) |
| Hand-offs before a stall, before / after | about 4 / unlimited |
| Checkers per project, before / after | one per wait / one |

Every stage also cleans up stray checkers. In step 0 it archives any `implement-plan <slug> — checker` or older `implement-plan <slug> — waiting: …` session that is not the recorded checker. Before handing over a wait, it deletes the reused checker's pending check-ins. Each check-in names its wait, so a superseded wait cannot start a second copy of a stage. The checker is archived when the project reaches LIVE or hands off to `/deploy-activate`.

What this means for operators: a project now runs from its first phase to activation without needing to be restarted from a new top-level session. The session list shows one checker plus the current stage per project.

### For contributors

Projects already in flight keep working. Their next stage finds no reusable `— checker` session (older checkers are titled `— waiting: …`), creates the project checker once, and archives the old one. The new rules are pinned in `tests/test_implement_plan_claude_command.py`, and the command and its `workflow-templates/` copy stay byte-identical.

- **The poller no longer re-dispatches review/autofix on every tick for a PR the identical-failure cap has already stopped.** The "Noop-suspicious recovery: re-dispatched review_autofix for PR #N (retry 3/3)" Telegram WARNING no longer repeats on every cycle.

The noop-suspicious recovery sweep in `scripts/orchestrate_poll_process.sh` counts `⚠️ **Editor no-op suspicious**` comments and re-dispatches `review_autofix.yml` until the count reaches 3. When the identical-failure fingerprint cap has already stopped a PR's head, the `gate` job ends every such dispatch before any job that could post a new warning. The count therefore stayed below 3, and the sweep dispatched a run and sent a WARNING on every poll cycle. PR #4332 got about ten of these on 2026-09-24 alone. The sweep now checks the comments and commits it already fetches: if the current head has a `review-autofix-failure-cap:v1` comment from the `GH_PAT` account, it logs `NOOP_RECOVERY_SKIP_FINGERPRINT_CAP` and skips both the dispatch and the alert.

| The numbers that matter | Value |
| --- | --- |
| Extra GitHub API calls | at most 1 `GET /user` per poll cycle, only when a cap marker is present |
| Reset | any push (new head SHA) |
| Fallback | an unresolvable head SHA or token identity keeps the old re-dispatch |

What this means for operators: a PR stopped by the fingerprint cap stays quiet until someone pushes to it or the review-blocked judge acts, instead of costing a workflow run and a Telegram WARNING on every tick. The cycle summary line `Noop-suspicious recovery skipped (fingerprint cap already applied on head): N.` shows how many PRs were held back.

- **`review_autofix.yml` is back under GitHub's 512,000-byte workflow file limit, so pushes stop creating phantom failed runs and the stable release gate can pass again.**

#4327 grew `.github/workflows/review_autofix.yml` to 540,537 bytes. GitHub does not start runs for a workflow file over 512,000 bytes, and it reports no error: every push to any branch created a zero-job run named `.github/workflows/review_autofix.yml` that concluded `failure` with "workflow file issue". Phase 4 of `test-and-mark-stable.yml` matched that run on the pinned head SHA, accepted it as a finished review before any editor had run, and Phase 4b then failed with `retry_timeout` (stable gate run 35903885958). The five largest step bodies now live in `scripts/review_autofix_step_*.sh` and the steps source them unchanged. Phase 4 also ignores runs named after their own file path. A new CI guard fails any workflow file that reaches 480,000 bytes.

| The numbers that matter | Value |
| --- | --- |
| GitHub workflow file limit (measured with padded probe workflows) | 512,000 bytes run, 512,001 bytes do not |
| `review_autofix.yml` before / after | 540,752 / 425,368 bytes |
| CI guard in `tests/test_workflow_file_size_limit.py` | fails at 480,000 bytes |
| Steps moved to `scripts/` | 5 (merge-topology gate, editor-uncommitted check, merge-conflict detection, partial finalize comment, iteration summary) |

What this means for operators and consumer repos: the phantom `review_autofix.yml` runs stop with this change, and the promote-cycle smoke gate on `main` no longer latches onto them. The moved steps keep their names, ids, conditions, env and log lines. Consumer runs pick up the five new scripts through the existing support staging on the next `@stable` sync, with no wrapper change. The split rule is now `CLAUDE.md` §27, which reaches consumer repos on that sync, and `unattended_system_instructions.md` §24: once a change leaves a workflow file at 480,000 bytes or more, the same PR moves its largest inline `run:` bodies into `scripts/`.

### For contributors

A moved step's `run:` block resolves its script from `${SUPPORT_SCRIPTS_DIR}`, then `.codex-workflow-src/scripts`, then `.codex-workflow-src-main/scripts`, and sources it in the step shell. The main-snapshot fallback covers self-repo PR branches forked before the move, because those reviews run `main`'s YAML against the branch's own scripts. A missing script fails the step, except in the two `always()` steps, which skip with a warning. Contract tests read the moved bodies through `tests/review_autofix_step_scripts.py`, which inlines them again. The expanded text matches the pre-move workflow exactly. When a workflow reaches the 480,000-byte guard, follow the split rule in `agents.md` under "Workflow file size limit".

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

- **Automatic issue triage and approval now require verified original-author provenance.** Newly opened issues from outside contributors no longer start the clarification pipeline; an issue started manually by a maintainer still requires explicit `/approved` if its original author is untrusted. Clarification rechecks the author before posting an automatic `/answer`, and planning refreshes state and author in one issue read before posting `/approved`. Missing or unreadable provenance defers approval instead of assuming the issue is open.

- **Review skips now require complete, unprotected PR file evidence.** Both small-diff and documentation-only candidates run the existing paginated file lookup and verify it against the PR's changed-file count. Agent instructions, automation files, and root build configuration cannot bypass review even when materiality advice is disabled; unavailable or truncated file lists also run review.

- Pin security-audit executables to verified protected or release support commits and audit requested branches only through separate, credential-free SHA checkouts.

- **The Claude issue intake now checks who sent a dispatch and which issue it names before it queues a Claude session.** A dispatch for an issue its sender cannot write to, a closed issue, a pull request, or an issue from an untrusted author is refused.

`claude-issue-intake.yml` used to check only that the payload's repository was registered, so anyone able to send a `claude-issue` dispatch or run the workflow could start a write-capable Claude session on any issue number in any registered repository, skipping clarify's author gate (#4620). The intake now reads live GitHub data first. Every dispatcher login GitHub reports for the run (`github.actor`, and `github.triggering_actor` when it differs) needs `admin` or `write` on the target repository. The target must be an open issue in that repository, not a pull request or a transferred issue. Its author must pass clarify's rule (`OWNER`, `MEMBER` or `COLLABORATOR`, or `github-actions[bot]`), or a trusted user must have commented `/reclarify` on it. A payload that fails validation (an unregistered repository included) is refused the same way, since the issue it names is unverified. A refused dispatch queues nothing and writes nothing to the target issue. It logs `CLAUDE_ISSUE_INTAKE rejected reason=…`, fails the run, and sends a Telegram ERROR.

| The numbers that matter | Value |
| --- | --- |
| New GitHub reads per dispatch | 1 permission read per distinct dispatcher login, 1 issue read, comments only for an untrusted author |
| Refusal reasons | `dispatcher_unknown`, `dispatcher_not_authorized`, `target_not_issue`, `target_repo_mismatch`, `issue_closed`, `untrusted_issue_author`, `authorization_read_failed` |
| New workflow env (from GitHub context) | `CLAUDE_ISSUE_DISPATCHER`, `CLAUDE_ISSUE_TRIGGERING_ACTOR` |

What this means for operators: routed issues from trusted authors work exactly as before, and consumer repositories need no change. If Telegram reports a refused intake for a legitimate issue, fix the cause the reason names (for example, a `GH_PAT` that cannot read the repository's collaborators) and comment `/reclarify` on the issue.

- **The review autofix sweep now runs `internal-review.yml` from the default branch only, never from a pull request's unmerged head branch.** Security finding #4618 (`review-dispatches-unmerged-workflow`, critical) is closed.

Every 30 minutes, `review_autofix_sweep.yml` dispatches `internal-review.yml` for each open non-draft PR. For same-repository PRs it passed `--ref <PR head branch>`, so the scheduled sweep ran that branch's own copy of `internal-review.yml`, with `secrets: inherit` and write permissions, before any review had approved it. The sweep now dispatches without `--ref` and passes only the PR number, which it checks is a positive integer (`AUTOFIX_SWEEP_SKIP … reason=invalid_pr_number` otherwise). `review_autofix.yml` still checks out the PR head from the PR's metadata, and both concurrency groups are keyed by PR number, so reviewers still read the PR head. One difference: a sweep-dispatched review of a same-repository PR builds its optional Semble index and README static context from the default-branch tree, as fork PRs always have.

The head-ref dispatch existed so the duplicate-run guards could see sweep runs (the PR #3895 incident). They now find them by name: `internal-review.yml` names every `workflow_dispatch` run `Internal: AI Review & Autofix [pr:<N>]`, the sweep's active-run snapshot keys those runs by PR, and the poller's `_has_active_autofix_run` looks the name up when its head-branch lookups find nothing. The §26 checker (`.claude/scripts/check_in_status.py --hand-back`) accepts a Claude-fixer hand-off whose review run is such a default-branch dispatch for the same PR, and counts an active one as a running review, so a `claude/*` PR whose hand-off came from a sweep run no longer waits forever. The `/implement-plan-claude` project checker (`check_in_status.py --pr N`) counts an active one too, so a PR with a failed check past the 6-hour stuck window is not handed back as `stuck` while a sweep review of it is still running.

| The numbers that matter | Value |
| --- | --- |
| Sweep dispatches that run an unmerged branch's workflow file | 0 (was: every same-repo PR, every 30 minutes) |
| New GitHub API calls in the sweep | 0 |
| New calls in `_has_active_autofix_run` | 1 `gh run list`, only when no head-branch run was found |
| New calls in `check_in_status.py` (`--hand-back`, and `--pr N`'s stuck check) | 1 REST read of `internal-review.yml` dispatch runs, only when no head-branch run was found |

What this means for operators: sweep-dispatched review runs now show `main` as their branch in the Actions list, with the PR in the run name (`Internal: AI Review & Autofix [pr:<N>]`). Pull request and push runs keep their usual names. Nothing changes for consumer repos.

### For contributors

The orchestrator's `_dispatch_review_for_conflicts`, `scripts/review_merge_train.sh`, and the `forward-merge-stable-to-main.yml` fallback still dispatch at a head ref. The forward-merge branch is cut from `stable` by the workflow itself. The other two are the same pattern as this finding and are out of this fix's scope.

- **The `gh api` permission guard now prompts for every file-backed `-F` value and every `--input`, so a routine comment or a read can no longer publish a local file without a prompt.**

`.claude/hooks/gh_api_write_guard.py` (CLAUDE.md §23.H) checked only the field names of a routine write, never the values. `gh api repos/<repo>/issues/1/comments -F body=@/path/to/credential` was approved with no prompt, and `gh` read the file and posted its contents as a comment. The same file read also reached GitHub through calls the guard treated as reads: a GET or HEAD with `-F q=@<file>` in the query string, a GraphQL variable `-F v=@<file>`, and a GET with `--input <file>`. The guard now classifies any call with an `-F`/`--field` value that starts with `@` (a file, or `@-` for stdin), or with `--input`, as a write, whatever the method, endpoint, or repository. The prompt names every file-backed field. `-f`/`--raw-field` values are sent literally and read no file, so `-f body=@octocat` is still allowed.

Bash can also turn a word into `@<file>` after the guard has read it. An `-F` word that holds `$`, a backtick, `~`, or a glob character (`-F body=$'@f'`, `-F body=$F`, `-F body=~`) now prompts too. So does any command that uses ANSI-C quoting (`$'...'`), an unquoted `#` comment, or brace expansion (`{a,b}`, `{a..b}`), because Bash parses these differently from the guard's tokenizer. Before, `-F{'q=1','x=@/tmp/a b'}` became a second, file-backed `-F` flag the guard never saw, and a quote inside a comment could hide a whole `gh api -X DELETE` on the next line, both with no prompt.

| The numbers that matter | Value |
| --- | --- |
| Finding | `api-guard-allows-file-backed-comment`, high, issue #4619 |
| New guard test cases | 57 (25 new file-backed, shell-expanded, or `--input` asks, 9 classification reasons, 11 shell-construct asks, 1 ask reason, 1 hook process, 3 raw-field allows, 7 quoted-construct allows) |
| Existing fixtures now expected to ask | 5 observed `-F body=@...` commands |
| GitHub API calls added | 0 |

What this means for operators: a session that edits a PR body or a progress comment with `-F body=@<file>` or `--input <file>` now stops at a permission prompt. So does a `gh api` command with `-F key=$VAR`, a `#` comment, or `{a,b}` outside quotes. Post and edit bodies with the GitHub MCP tools (`mcp__github__update_pull_request`, `mcp__github__add_issue_comment`, `mcp__github__update_issue_comment`) or pass the text inline with `-f body=...`. The fix reaches consumer repos on the next `@stable` sync of `.claude/`.

### For contributors

`classify()` checks field values and `--input` before the GraphQL and read-method branches, so the old `--input` check after the read-method branch is gone. The GraphQL branch's `query=@` check stays as defence in depth. The shell-construct check (`_shell_rewrite_hazard`) runs in `evaluate()` on the command with heredoc bodies removed, after the malformed-`--jq` deny. Unquoted `$VAR` word splitting in other words of a call (`gh api repos/.../comments$X`) is still left to the allow list, as documented in CLAUDE.md §23.H, and is tracked in #5558. No identifier changed.

- **Review dispatches from the poller, merge train, and forward-merge fallback now use the default-branch workflow.** PR numbers are validated before dispatch; no PR head ref selects executable workflow code.

The poller, merge train, and forward-merge fallback pass validated PR numbers to review workflows on the default branch instead of executing workflow files from a PR head. Consumer dispatches show `AI Review [pr:<N>]` in the Actions list, while internal dispatches retain `Internal: AI Review & Autofix [pr:<N>]`. The poller and merge train associate those runs with their PRs and continue to recognize legacy head-branch runs. Active reviews no longer invite redundant dispatches or empty-commit pushes simply because their workflow ran on the default branch.

| The numbers that matter | Value |
| --- | --- |
| Dispatch sites using the default-branch workflow | 3 |
| PR-named lookup on a branch-lookup miss | 2 wrapper listings, paginated up to 10 pages each |
| New API calls for the cached stall-judge and empty-commit scans | 0 |

What this means for operators: default-branch review runs remain visible under their PR-numbered Actions names, and the poller can wait for a pending review without pushing over its work. Incomplete PR-named run listings defer dispatch and empty-commit recovery until the next poll tick.

### For contributors

`_pr_named_review_dispatch_runs <pr> [lookback_minutes]` is the shared paginated lookup on head-branch misses. `_direct_inflight_review_run_on_branch <branch> [pr]` accepts an optional PR number and returns `listing-incomplete` when a lookup cannot rule out an active run; its one-argument branch-only behavior remains unchanged.

- **`review_autofix.yml` now starts its follow-up review runs from the default branch, never from the pull request's head branch.** This closes the last two sites of security finding `review-dispatches-unmerged-workflow` (#4618, #4701).

Two steps in `review_autofix.yml` re-dispatch the review workflow: "Re-trigger review via workflow_dispatch", right after the editor pushes its own `[ai-autofix]` commit, and "Re-dispatch review on editor-changes-lost". Both passed `--ref <PR head branch>`, so the next run executed the branch's own copy of the workflow file with `secrets: inherit` and write permissions. With `allow_workflow_edits` on, that copy could carry workflow edits the editor had just made. Both now dispatch without `--ref`, pass only a PR number they have checked is a positive integer, and try the PR-named wrappers (`internal-review.yml`, `ai-review.yml`) before `review_autofix.yml`. The review still checks out the PR head from the PR's metadata.

A run started from the default branch shows the default branch as its head, so the two run probes these steps use now also find runs by name. The duplicate-run check (`autofix_retrigger_has_inflight_peer`) counts queued or running dispatch runs named for the PR. The per-head retry budget of the changes-lost re-dispatch (`autofix_changes_lost_head_retry_consumed`) counts completed runs named for the PR since the head was pushed, so the retry stays bounded now that it runs from the default branch. A dispatch run that is not named for the PR (a renamed caller wrapper, or an `ai-review.yml` that predates the name) never dispatches a changes-lost retry, because that retry could not count it and would loop.

| The numbers that matter | Value |
| --- | --- |
| `review_autofix.yml` dispatches that run an unmerged branch's workflow file | 0 (was 2 steps, 6 `gh workflow run --ref` calls) |
| New GitHub API calls per probe | on a branch-lookup miss, one default-branch metadata read and paged GETs for each review wrapper; incomplete results are never accepted |
| `review_autofix.yml` size | reduced by moving both step bodies to `scripts/` |

What this means for operators: after an autofix push or an editor-changes-lost run, the next review run shows the default branch in the Actions list, with the PR in its run name (`Internal: AI Review & Autofix [pr:<N>]` here, `AI Review [pr:<N>]` in consumer repos). A consumer repo whose `ai-review.yml` predates the PR run name (#4701) still gets the default-branch dispatch, but its probes cannot see those runs until the next workflow sync.

### For contributors

The step bodies live in `scripts/review_autofix_step_post_commit_retrigger.sh` and `scripts/review_autofix_step_changes_lost_redispatch.sh`, registered in `REQUIRED_BOOTSTRAP_SCRIPTS` and `tests/review_autofix_step_scripts.py`. The shared lookup is `_autofix_pr_named_review_runs <pr> [status]` in `scripts/gh_helpers.sh`; it accepts only runs from the repository's authoritative default branch, with the matching workflow path, event and exact PR run name. `autofix_changes_lost_head_retry_consumed` takes an optional fifth argument, the head commit's epoch time, and an optional sixth, the run's event; it fails closed without a push-time bound and on a dispatch run not named for the PR (`reason=unnamed_dispatch_run`). Its `AUTOFIX_CHANGES_LOST_BUDGET` line gains `pr_named_completed=` and `event=` (`-` when no event was passed); `AUTOFIX_PEER_CHECK` is unchanged. The E2E bait-branch dispatches in `test-and-mark-stable.yml` are documented as exposed pending their own default-branch migration.

- **The orchestrator poller no longer misses a live review run because unrelated dispatches crowded it off one page.** Before a review dispatch or a stall-recovery empty-commit push, the poller now reads every review-wrapper dispatch run in the review window, and skips the action when it cannot read them all.

`_pr_named_review_dispatch_runs` in `scripts/orchestrate_poll_process.sh` finds the review runs dispatched from the default branch for a PR (`Internal: AI Review & Autofix [pr:<N>]`, `AI Review [pr:<N>]`). It used to read one page of the newest 100 `workflow_dispatch` runs of every workflow. More than 100 newer dispatches could push a still-active review off that page, and the stall recovery then pushed an empty commit onto the PR head, discarding the in-flight review (security finding `review-run-global-window-exhaustion`, issue #4927). The lookup now lists only `internal-review.yml` and `ai-review.yml` dispatch runs created within `REVIEW_RUN_MAX_RUNTIME_MINUTES`, page by page until it has read every run the listing reports. An incomplete listing (a failed or malformed page, a listing that shifted while being read, or more runs than 10 pages hold) skips the conflict dispatch, the failed-autofix redispatch, and the empty-commit push, and the next poll cycle retries. The failed-autofix redispatch reads back 120 minutes further (`REVIEW_RUN_MAX_RUNTIME_MINUTES + STALL_THRESHOLD_MINUTES`), so a review run that failed at its 240-minute job timeout is still redispatched rather than answered with an empty commit.

| The numbers that matter | Value |
| --- | --- |
| `internal-review.yml` dispatches in coding-workflows, 2026-09-29 | 153 in 5 hours, 701 in 24 hours |
| Window the old single page covered here | under 3.5 hours |
| Lookback window | `REVIEW_RUN_MAX_RUNTIME_MINUTES` (default 250 minutes); 370 minutes for the failed-autofix redispatch |
| Page cap per wrapper | 10 pages of 100 (GitHub's 1,000-result limit) |
| API calls per lookup in coding-workflows | 3 (two `internal-review.yml` pages, one `ai-review.yml` 404), up from 1; 4 for the redispatch lookup |

What this means for operators: a stall recovery that cannot see every recent review dispatch now waits a cycle instead of pushing. Search the poller log for `PR_NAMED_REVIEW_RUNS pr=<N> outcome=incomplete` and `STALL_INFLIGHT_DIRECT_CHECK … outcome=pr_named_listing_incomplete` to see why a push or dispatch was held back.

### For contributors

The helper keeps its stdout contract (a JSON array of matching runs, newest first) and now returns 1 when the listing is incomplete. `_direct_inflight_review_run_on_branch` prints the sentinel `listing-incomplete` in that case, and both empty-commit push sites treat it as a skip under the existing `retrigger_review_skipped_inflight` action. A wrapper the repo does not have answers 404 and counts as complete and empty. The merge train's `_mt_inflight_review_branches` and the sweep's snapshot are unchanged. The poller now strips leading zeros from `STALL_THRESHOLD_MINUTES` and `REVIEW_RUN_MAX_RUNTIME_MINUTES` at startup. Its `^[0-9]+$` check accepted them, and bash arithmetic then read `0250` as octal or failed on `08`.

- **The review autofix sweep now sees a dispatched review run named for its PR even when GitHub reports no head branch for it, so it no longer dispatches that PR a second time.** This closes security finding #4928 (`sweep-discards-null-head-dispatch`, medium, STRIDE: Denial of Service).

`review_autofix_sweep.yml` skips a PR that already has a queued, running, or pending review run. It finds a run started from the default branch by its name, `Internal: AI Review & Autofix [pr:<N>]`, and counts it under the key `pr:<N>`. GitHub can report `head_branch` as null on a `workflow_dispatch` run, and the sweep's active-run snapshot dropped every run without a head branch before it read the name. The PR then looked idle, so the next tick dispatched it again and the new run replaced the pending review in the `review_autofix` concurrency group. The snapshot now keeps such a run under its `pr:<N>` key. A run that has neither a head branch nor a valid PR name is still dropped.

| The numbers that matter | Value |
| --- | --- |
| New GitHub API calls per sweep tick | 0 (the fix reads the run listing the sweep already fetches) |
| Runs newly counted | `workflow_dispatch` runs titled exactly `Internal: AI Review & Autofix [pr:<N>]` with a null, missing, or empty `head_branch` |
| Queued-run cutoff | unchanged, `SWEEP_STALE_QUEUED_MINUTES` (default 120) |

What this means for operators: a pending review run for a PR is no longer replaced by a duplicate sweep dispatch when GitHub omits the run's head branch. A wedged queued run is still logged as `AUTOFIX_SWEEP_STALE_QUEUED` under its `pr:<N>` key and stops suppressing dispatch after the usual cutoff.

- **The merged-PR guard now checks the repository and destination branch of each guarded git command.** A detached worktree can push to an open PR branch without being blocked by an unrelated merged PR in the session checkout.

The Bash PreToolUse hook follows resolvable `cd`, `git -C`, and git-directory overrides when checking commits and pushes. Explicit push refspecs are checked against their destination branch and source commit, so a push to a merged branch cannot inherit a safe verdict from the checkout's branch. When the directory or refspec cannot be resolved, the hook warns and uses the session checkout check instead. Repeated destinations share one PR lookup per repository and branch within the command.

What this means for contributors: work from scratch worktrees without detaching the main checkout to bypass a false merged-PR block.

- **The merge train no longer releases a queued PR beside a review run it could not see.** Before it re-dispatches review for an `ai:merge-queued` PR, the `release` step now reads every active run, recognises review runs whose path carries an `@<ref>` suffix, and leaves the PR queued when it cannot read them all.

`_mt_inflight_review_branches` in `scripts/review_merge_train.sh` tells `release` which queued PRs already have a review running. It used to read one page of the newest 100 workflow runs of every workflow, matched the review workflows with a regex that a path such as `.github/workflows/ai-review.yml@refs/heads/main` fails, and released without the check when the lookup failed. Any of the three let the train dispatch a second review beside a pending one (security finding `merge-train-drops-ref-suffixed-run-paths`, issue #5443). The listing now reads runs in each non-terminal status (`requested`, `pending`, `queued`, `waiting`, `in_progress`, the same five the repo's other active-run guards count), then reads all five once more, so a run that moves between statuses mid-read (for example from `in_progress` back to `waiting` when a later job reaches a deployment environment, and on to `in_progress` again) is still seen. Each status is read 100 at a time until every run the listing reports has been read, and an `@<ref>` suffix is stripped before matching `review_autofix`, `internal-review`, or `ai-review`. Each call after the first asks for runs created at or before the oldest run already read (`created=<=<timestamp>`) instead of the next offset page, so a run that finishes or starts mid-read cannot push a still-active review run past the pages read. When the listing is incomplete (a failed or malformed page, such as a `total_count` that is not a whole number, a page with no `workflow_runs` list, a run with no id, workflow path, or status that could itself be a review run, or a review run with no head branch and no PR-named title, or a `workflow_dispatch` review run with no PR-named title whatever its head branch (such as a `review_autofix.yml` dispatch, which has no PR run name), which could be running for any queued PR; a listing that shifted while being read; or more runs than 10 calls read), every queued PR stays queued and the next `cancel_on_pr_close.yml` event or `orchestrate_poll.yml` tick retries.

| The numbers that matter | Value |
| --- | --- |
| `internal-review.yml` dispatches in coding-workflows, 2026-09-29 | 153 in 5 hours |
| Call cap per status | 10 calls of up to 100 runs (at most 991 distinct runs, since each bounded call re-reads at least the oldest run before it) |
| API calls per release with a queued PR | 10 (one page per status query: the five statuses, twice), up from 1 |
| API calls per release with nothing queued | 0, down from 1 |

What this means for operators: a queued PR may now wait one more close event or poll tick when the Actions API is slow or failing, instead of getting a duplicate review run. Search the release logs for `MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE pr=<N>` and `MERGE_TRAIN_RUNS_LISTING outcome=incomplete reason=<…>` to see why a PR was held back. `MERGE_TRAIN_ENABLED=false` still turns the whole train off.

### For contributors

`_mt_inflight_review_branches` keeps its name and output (one sorted key per line: the head branch of a run that is not a `workflow_dispatch` run, or `pr:<N>` for a PR-named dispatch run) and returns 1 on an incomplete listing. Every run an active-status query returns counts as active, whatever its own `status` field says. `_mt_release` reads it once per invocation, lazily, the first time a queued PR passes the base filter. The same `.path` regex in `scripts/gh_helpers.sh` (`autofix_retrigger_has_inflight_peer`, `autofix_changes_lost_head_retry_consumed`) is unchanged.

- **The `gh api` permission guard approves literal-ID read loops again, and asks again on shell-rewrite hazards inside loops.** Since #6127, a loop such as `for r in 1 2; do gh api repos/o/r/actions/runs/$r/jobs; done` prompted for permission, because the unquoted-expansion check also caught the loop counter.

#6176 and #6175 fixed the counter in the consumer copy, `workflow-templates/.claude/hooks/gh_api_write_guard.py`. That copy also stopped asking on ANSI-C quoting, `#` comments and brace expansion in any command that starts with a loop. As a result, `for r in 1; do gh api -X GET repos/o/r/issues/1 -F{'q=1','x=@/etc/passwd'}; done` got no decision from the hook, though Bash expands it into a file-backed field. This change restores the hazard ask for every command, and makes `.claude/hooks/gh_api_write_guard.py`, the hook this repository's own sessions run, identical to that copy. An unquoted `gh api` expansion still asks unless the whole command passes the read-loop validator. Counter rebinding, other expansions, and hazards inside a loop prompt.

What this means for operators: sessions in coding-workflows stop pausing on the read loops that CLAUDE.md §23.H allows. Consumer repos never receive the loop-hazard gap on the next `@stable` sync.

- **Dispatched PR reviews now read the PR head instead of the dispatching branch.** The review gate validates the head repository and SHA before checkout. Fork or unknown-head PRs skip review and deterministic auto-merge; mismatched workspaces skip agents, and project OpenCode configuration or plugins are refused before agent setup. No-PR branch reviews remain unchanged.

Reviewers and other file-reading agents now see files added on a PR head during `workflow_dispatch` reviews. The gate uses authenticated PR metadata to select the checkout commit instead of the dispatching branch's commit. If the source tree, split workspace, or PR metadata does not agree on that commit, the review does not start. A PR-controlled OpenCode configuration also stops agent setup in the credential-bearing checkout.

What this means for operators: dispatched reviews no longer report PR-added files as missing from the default branch, and unsafe or unverifiable PR checkouts are left unreviewed rather than silently falling back.

- **Every Codex agent that reads untrusted text, and every Claude engine run, now happens in a credential-free, network-isolated container.** A prompt injection in an issue, comment, PR diff, CI log or workflow log can no longer read `GH_PAT`, the OpenRouter key, the Telegram secrets or the checkout's `.git`, and has no network to send anything out.

Until now the plan and implement agents, and over twenty other agent launches, ran on the runner with `--sandbox danger-full-access`, `GH_TOKEN` (`GH_PAT`, with `repo` scope on this repo and every consumer, plus `workflow`) and `OPENROUTER_API_KEY` in their environment, and the PAT stored in `.git/config` by `actions/checkout` and the `git remote set-url` step. They now go through `scripts/codex_isolated_exec.sh`, the pattern clarify already used: a Docker container with `--network none`, a read-only root, no capabilities, no runner environment and no host checkout, that reaches the model only through `scripts/clarify_openrouter_broker.py` on the host. Read-only agents see a copy of the tracked files; the implement agent edits a disposable copy, and only changed regular files come back. Scripts a job runs after an agent wrote files come from a trusted copy the agent cannot reach, so its output never becomes host code. The Claude engine's `claude_run` (`scripts/ai_engine.sh`), which ran the Claude Code CLI on the runner with the job's environment, now uses the same container: the account token stays in `scripts/claude_anthropic_relay.py` on the host and the CLI sees only a placeholder.

| The numbers that matter | Value |
| --- | --- |
| Agent launches moved into a container | 25 (24 Codex, 1 OpenCode fix writer), across 11 workflows and 13 scripts |
| Credentials left in the agent's environment | 0 (was `GH_TOKEN`, `OPENROUTER_API_KEY`, `TG_BOT_SECRET`) |
| Container network | `none` (model calls via the host broker only) |
| Claude engine attempts on the runner | 0 (each `claude_run` account attempt runs in the container; returns `75` and runs codex when Docker or the image is unavailable) |
| Extra time per job | about 70 s for the first image build; implement also installs dependencies once |

What this means for operators: runners need Docker, which GitHub-hosted `ubuntu-latest` provides. A missing Docker or a failed image build fails the step with `::error::CODEX_ISOLATION …`; nothing falls back to running Codex on the host, and there is no variable that turns isolation off. The implement agent can no longer `pip install` or `npm install` during its run: declared dependencies are preinstalled and anything else is reported as UNVERIFIED. Serena is not available inside the container; Semble is unaffected.

### For contributors

- New helpers: `scripts/codex_isolated_exec.sh` (`prepare` / `run --mode read-only|workspace` / `cleanup`) and `scripts/codex_isolated_workspace.py` (credential-filtered snapshot, including `.ssh`, `.npmrc`, `.netrc` and credential-store files such as `credentials.conf`, `credentials.xml`, and `oauth.secret.properties` while preserving ordinary source modules such as `secret_manager.py`; dependency-prep finalisation, rollback-capable write-back, filtered synthetic `.git`). `codex_thread_reuse.sh` launches through the helper when `CODEX_ISOLATED_EXEC` is set; implement removes its persistent sandbox in final cleanup.
- `IMPLEMENT_STAGED_SUPPORT_RUN_DIR` is now staged in every repository; implement's Codex, repair and later steps execute their helpers from it, and `after_run` workspace hooks run from a copy taken before the editor.
- The orchestrator's review-blocked fix and integration-conflict judge work in separate git worktrees. For the integration judge the poller now fetches, merges, checks conflict markers and the merged sub-issue fingerprints, commits and pushes; the agent only resolves files. Protected conflicts cannot drop lines shared by both merge sides or delete a file present on both sides; one-sided delete/modify conflicts can still resolve to deletion. Both judges push using one-shot credential helpers rather than saving `GH_TOKEN` in shared Git config. The review-blocked judge's OpenCode fix writer runs in `scripts/review_untrusted_sandbox.sh`, selecting the validated per-PR worktree without replacing the checkout's `GITHUB_WORKSPACE`. With `ALLOW_WORKFLOW_EDITS=false`, review-blocked fixes also reject changes to `.github/`, `workflow-templates/`, and `.claude/` alongside scripts and prompts.
- The merged-PR guard requests confirmation for pushes targeting a different or unverified repository via `--repo` or a positional remote, or with per-command Git configuration (`git -c`, `--config-env`, inline `GIT_CONFIG_*` / `GIT_CONFIG` assignments, or an `env` wrapper) that may affect `origin`; it does not use the checkout's origin PR history to authorize those writes. Explicit push URLs ask even when their slug matches origin because Git can rewrite them via `url.*.insteadOf` or `pushInsteadOf`, including deletion-only and tag-only pushes. Wrapped commits retain the merged-PR check in their selected worktree (`env -C` / `GIT_DIR`); unparseable `env -S` commands and commits with an unresolved directory selector ask (an absolute `core.worktree` cannot identify an unknown Git directory), and so does a commit with any per-command Git configuration or `env` wrapper when its directory is uncertain, as on `main`; a plain commit under shell control only warns, and a push from an unresolved location is checked against the session checkout (which can block) and otherwise asks. Leading descriptor redirections after environment assignments still receive the merged-PR check, and only digits glued to a redirection (`2>&1`) are treated as a file descriptor; unresolved push sources ask rather than checking the session checkout's HEAD. The Claude relay drains bounded rejected POST bodies before closing the connection, so callers can receive the HTTP 400 response instead of an intermittent broken pipe.
- `claude_run` launches through `codex_isolated_exec.sh run --engine claude` (`--claude-token-file`, `--claude-models`, `--claude-cli-version`, `--claude-settings`, `--claude-guard-hook`, `--claude-instructions`, `--claude-home`, `--hide-claude-md`). A `read` profile gets the read-only snapshot and write profiles the workspace copy; `$RUNNER_TEMP/claude-isolated-home` keeps the CLI's sessions for `--resume`; `hide_claude_md` keeps the top-level `CLAUDE.md` out of the copy and the write-back (`CODEX_ISOLATED_HIDE`) instead of moving the host file. Helper exit `75` means isolation is unavailable, `73` that the relay did not start for that account.
- `tests/test_codex_agent_isolation_contract.py` fails on any direct `codex` launch outside the two container entrypoints, and on any `claude -p` outside the container entrypoints (the token step's fixed-prompt usage probe is the listed exception); `tests/test_codex_isolated_exec.py` and `tests/test_codex_isolated_workspace.py` cover the behaviour, and `tests/test_codex_isolated_exec_docker_e2e.py` (opt-in, `CODEX_ISOLATION_DOCKER_E2E=1`) runs the real image with the Codex CLI and with the Claude Code CLI against fake model endpoints. The isolation test files now run in `ci.yml`.

- **Review editor transfers keep operator-facing Claude commands outside the isolated workspace.** The `audit-plans` command and its consumer template are synchronized so review does not need to repair their parity inside that workspace.

The `Internal: AI Review & Autofix` editor does not snapshot or transfer files under `.claude/commands/` back into the host checkout. Attempts to introduce that directory in an isolated result continue to fail with the path-free `unsafe_directory` reason rather than overwriting an operator command. The matching `audit-plans.md` copies remove the parity mismatch that triggered the repeated review failures on PR #6135.

What this means for operators: command parity can be checked without opening the isolated editor's trust boundary.

- **Bulk-delete overrides no longer cover workflow and automation files.** The unblock judge refuses destructive overrides for paths under `.github/`, `.claude/` or `workflow-templates/` in any repository.

Both implementation deletion guards ignore even previously issued bulk-delete overrides when the staged deletion set contains a protected automation path. Other approved deletions can still use a one-shot override, while protected deletions remain subject to the normal threshold. Scope overrides for consumer-owned workflow edits are unchanged.

- **Unblock judge no longer reads a failed run's logs solely because an item comment links to it.** It now checks up to three pipeline-cited run IDs against repository and item metadata before including a log in the model prompt; a matching issue title alone is insufficient, and unmatched or unreadable runs are omitted.

For maintainers: `UNBLOCK_JUDGE op=run_log` reports whether a run was attached or omitted and why. The judge still proceeds without logs when no run can be verified.

- **The unblock judge no longer runs Claude Code on the credential-bearing runner when judging blocked work.** It runs Claude inside the read-only, network-isolated container, with its OAuth token held by a host-side relay. If Claude is unavailable, it falls back to the isolated Codex path; other isolation failures leave the item blocked without a verdict.

The judge reads issue comments, PR diffs, and run logs when deciding how to unblock an item. Previously those inputs could reach host-side Claude Code with access to the runner filesystem and the Claude credential. The existing clarify isolation runner now accepts the `UNBLOCK_JUDGE` role for Claude and enforces the judge's timeout inside the container. No new workflow trigger or credential is needed.

- **Scope overrides cannot authorize protected automation paths.** The unblock judge now refuses the entire verdict when any requested override reaches protected automation code.

Paths under `.github/`, `.claude/` or `workflow-templates/` are rejected in every repository for both scope-blocked and destructive-blocked issues. In this repository, `scripts/` remains forbidden, while destructive overrides continue to refuse canonical workflow sources. Consumer repositories can still approve other scope paths, but mixing an allowed path with a protected path cannot partially authorize the verdict.

- **Project unblock fix-ups now require verified PR membership.** The unblock judge accepts project-base PRs only from same-repository `ai/issue-<n>` branches listed in a project-state comment posted by the pipeline's authenticated login; the poller uses that state, not an untrusted working snapshot, to distinguish project issues from PRs before filing even a pre-existing fix-up request. Missing trusted state or PR data, or a mismatch between the working and trusted project branches, defers the request; definitively rejected PRs remain blocked.

- **Claude read-only roles no longer have shell access.** The review-blocked judge's verdict pass now uses the read profile rather than the write profile.

The shared read profile allows only `Read`, `Grep` and `Glob`; command-prefix permissions for `git` and `gh` could still write runner files. `AI_ENGINE_READ_ONLY=true` now narrows any Claude role to that profile without affecting the judge's write-capable fix pass. The security-pass exhaustion judge can verify cited files without shell access.

What this means for operators: Claude verdict passes cannot run shell commands, while the separate fix pass retains its write tools.

- **Static prompt assembly skips symlinked checkout files.** Review, clarification and planning omit symlinked README content; clarification, planning and implementation omit symlinked local agents files, and targeted context excludes symlinks and Git metadata.

Review now omits a symlinked `README.md` from its static prompt, and clarification and planning omit symlinked local `agents.md` and `README.md` inputs with a warning. Required instruction and pipeline files that are symlinks cause static prompt assembly to fail before reading their targets. Targeted file context reports symlinked files and `.git` paths without inlining their contents. Regular files continue to be included as before; Git credential persistence in the review checkout is unchanged.

The optional overflow-runbook reference is also omitted, with a warning, when `probably_unnecessary_but_read_if_stuck.md` is a symlink. A regular runbook still receives the reference.

The orchestrator's decomposition and clarification-response static prompts apply the same rules: required instruction symlinks stop assembly, and symlinked local agents files, README files, and overflow-runbook pointers are omitted with warnings. Regular files and checkout authentication continue to work as before.

What this means for operators: unsafe links cannot supply prompt text, while a required symlink causes an explicit failure that must be corrected before the phase runs.

- **The merged-PR guard now checks numeric push refspecs before `&>` and `&>>` redirects.** A redirected `git push` to a branch whose PR already merged is no longer overlooked.

When a numeric refspec touched an ampersand redirect, the guard mistook it for a file descriptor and skipped the destination branch check, even though Bash passes the number to git. The interactive-session hook now retains that refspec and checks the target branch's PR history as it does for other pushes. Numeric file descriptors on regular redirects, such as `2>&1` and `123>|file`, are excluded from git's arguments; the clobber form now checks the current branch instead of a branch named `123`. If a later malformed line makes any Bash command unparsable, the hook requests confirmation instead of relying on a literal spelling of `git` that quoted or escaped commands can evade. The same protection reaches consumer repos through the mirrored hook on the next stable sync.

What this means for operators: redirecting a push's output cannot bypass the merged-PR branch guard for numeric branch names.

- **Unblock guard overrides are bound to the actual rejection.** The unblock judge now accepts scope and bulk-deletion overrides only for the exact paths rejected by the latest trusted guard run; extra, missing, stale, or incomplete paths cannot clear the block.

Guard comments record their rejected paths and run ID in a bounded marker. Scope-lock and non-bulk destructive rejections cannot use this override. Existing consumer blocks without a marker remain blocked from override until a new guarded run records one; other judge verdicts remain available.

- **The unblock judge no longer opens issues from untrusted pull requests.** Issue-creating verdicts on fork PRs or PRs without a verified trusted author instead explain the block and close the PR; a maintainer can reopen it or file an issue manually.

For maintainers: issues derived from trusted same-repository PRs now carry an audit-only provenance marker with the source PR, author, head repository and head SHA. Fork PRs targeting project branches cannot write to project state. If posting the rejection explanation fails, the judge still tries to close the PR and sends a warning if closure fails. No new API calls or issue-open gate changes are required.

- **Unblock judge verifies project PR ownership before posting project actions.** Project-targeting PRs must have a same-repository `ai/issue-<n>` head and a matching child issue in pipeline-authored project state; unverifiable bindings are skipped without writes.

- **Review prompt assembly no longer follows symlinked pull-request READMEs.** Non-regular README files are omitted from static review context instead of being read with job credentials in the environment.

The review workflow reads only a regular, size-bounded `README.md` through a no-follow reader. Its content stays outside the trusted static prompt and reaches the review models as fenced untrusted data stored in an owner-only runtime directory. A rejected README emits a path-free warning and review continues without that section; a reader failure stops the step. The same step refuses to write `pre_assembled_static.txt` if the checkout contains a symlink or non-regular file at that path.

What this means for operators: symlinked READMEs no longer expose runner environment data to reviewer prompts; replace the link with a regular file to restore the README section.

The same no-follow, size-bounded handling now covers clarify, plan, orchestrate, clarify-respond, orchestrator judge, and validation prompt assembly. Accepted README text is explicitly framed as untrusted repository data in every covered phase.

READMEs that would exceed 200,000 bytes after per-line untrusted-data framing are omitted with a warning, preventing short-line expansion from overrunning review prompts. Orchestrate and clarify-respond also reject non-regular static-context output paths before writing them.

- **Unresolved security findings stay visible.** The unblock judge keeps `ai:security` issues open when its verdicts are exhausted, with a terminal label and a CRITICAL alert. On a safe standalone reissue, it carries the finding marker and security label to the replacement; otherwise the original stays open until a linked fix is verified merged.

- **Keep security finding metadata on unblock-judge reissues.** Standalone replacements retain their dependency and integration branch; unsafe metadata or missing labels leave the original finding open.

- **Bulk-delete overrides require verified rejection evidence.** The unblock judge and implement spend step now accept only deletions in the failed implement run's rejection artifact; missing or unrelated evidence leaves the guard in force.

- **The unblock judge no longer abandons an unlabeled project that has resumed.** It refuses to treat an older complete `failed` snapshot as current when a newer V2 state write is incomplete or malformed. It checks again before recording a verdict and immediately before adding the terminal label, after label-catalog preparation. A late resume or unreadable state withholds the terminal label; a resume after the verdict was recorded can leave an unacted-on verdict comment.

- **The merged-PR guard asks for confirmation when a push's effective repository cannot be resolved.** With `GIT_DIR+=<path> git push`, Bash may push from a different repository than the session checkout. The guard already discarded the appended value, but could allow that push based on the checkout's branch instead.

Both guard copies still check the session checkout and block if its branch has a merged pull request. Otherwise an unresolved push directory prompts for confirmation, rather than treating the checkout's open or default branch as proof that the pushed branch is safe. `tests/test_pr_merge_status_guard.py` covers both appended variables.

An unresolvable explicit directory override on `git commit` now asks for confirmation without checking another checkout's pull requests. Commits whose directory is uncertain only because of shell control flow retain the existing checkout check and warning behavior.

What this means for consumer repos: the fix reaches them on the next `@stable` sync, and nothing needs configuring.

- **Review and validation now load workflow prompt overrides from the repository's default branch, not the PR checkout.** Merge-decision judge prompts cannot be replaced by an overlay.

`review_autofix.yml` and `validate.yml` pin one default-branch commit and copy its `.github/ai/WORKFLOW.md` and fragments into private runtime storage. PR changes to those files take effect only after merge; an unavailable trusted source disables the overlay and retains stock prompts. Judge-mode `replace_path` overrides are ignored with a warning, while trusted `append_path` overrides remain available.

What this means for operators: a PR cannot supply its own judge instructions through the workflow overlay.

- **The merged-PR guard checks numeric push branches even before output redirects.** A spaced or quoted number in `git push origin 2 > /dev/null` remains a branch argument rather than being discarded as a file descriptor; adjacent unquoted `2>/dev/null` remains a redirect. The live hook and consumer template stay in sync.

- **The orchestrator no longer closes a project based solely on `ai:unblock-closed`.** It requires the unblock judge's latest trusted close verdict for a project still in `failed` state, with no later V1 or V2 state write superseding it; a spoofed label cannot hide a still-failed project from the unblock scan, and failed close requests can still be retried.

- **Blocked standalone issues and pull requests reach the unblock judge even without an open tracking project.** The scheduled orchestrator poll now scans blocked items on idle project ticks as well as after active projects are processed.

Previously, the unblock scan was skipped when no orchestrator tracking issue was open, leaving standalone blocked work waiting indefinitely. The idle-tick scan uses the existing cooldown, trusted-marker checks, and one-dispatch-per-tick limit. No new scheduler or credentials are required.

- **Check-failure triage now diagnoses PR failures inside a credential-free, read-only container.**

The check-failure triage wrappers pass only their four declared secrets, and the PR checkout uses a read-only job token instead of `GH_PAT`. The diagnosis runs through the existing clarify isolation helper with trusted support, a network-disabled container, and a host-side model broker. Issue posting checks the triage fingerprint marker and caps bodies at 60,000 characters. If Docker or isolation support is unavailable, triage files a raw-context issue rather than running host Codex.

The Claude Anthropic relay also rejects incomplete POST bodies after a 60-second total read deadline, so one stalled client cannot block the single-threaded relay indefinitely.

What this means for consumer maintainers: the workflow requires Docker for model diagnosis; without it, the normal issue pipeline still receives the raw failure context.

- **Review-blocked poller fixes cannot publish unrelated or newly protected files.** The poller checks staged paths against the blocked PR's complete changed-file list and validated judge citations before committing. Missing file listings or out-of-scope edits reject the entire fix, notify operators, and consume a bounded retry without pushing.

- **Review-blocked fixes now verify their PR target before writing.** The poller rejects fork or unrelated PR heads, checks the fetched branch tip against the PR head SHA, and rechecks the head before pushing a fix. Failed verification leaves the branch untouched for the next poll.

- Full security audits now inspect every tracked text file over 2 MiB that passes the existing credential filter, using bounded read-only chunks, instead of listing them in a coverage note. Binary files (a NUL byte in the first 8 KiB) and files that would pass the per-file cap (16 MiB by default) or the total cap (64 MiB by default) are listed in the coverage note as not inspected, so one large asset does not fail the audit. Explicitly scoped changed, prior-finding and fix-cycle files take the cap budget first and still fail the audit when they cannot be inspected; incremental audits still report out-of-scope files as coverage notes.

- **The poller's review-blocked judge no longer falls back to host Codex when sandbox preparation fails.** A PR can cause preparation to fail with its checkout contents; the judge now refuses the privileged fallback, leaves the issue review-blocked, and retries on the next poll tick. Missing verified sandbox support also defers the judge; an operator-selected Codex engine uses isolated OpenCode.

- Integration-conflict judge resolutions cannot introduce new lines in conflicted protected files.

The scheduled orchestrator poller rejects a resolution when a conflicted protected file contains lines absent from both merge sides. The protected set includes automation directories such as `scripts/` and `.github/`, agent-instruction files, and build, dependency, config and script files. Unprotected application code can still combine or synthesize lines, and rejected resolutions are not pushed.

What this means for operators: an integration-conflict judge cannot publish invented executable lines through a protected file during merge recovery.

- **Check-failure triage no longer lets PR-controlled logs redirect automated fixes.**

The triage workflow neutralises routing metadata and issue markers in both raw-log fallbacks and model diagnoses before posting an issue. Fallback evidence uses a fence longer than any backtick run in the captured logs, preserving readable failure context without allowing a log line to break out of the fence. If neutralisation fails, triage stops instead of posting the issue.

What this means for consumer maintainers: triage issues still follow the existing default-branch routing, while failure logs remain available as evidence.

- **Fix PRs for check-failure triage issues now receive a head-bound security pass.** The triage issue's owner-authored label and fingerprint no longer exempt its fix PR from the single-issue audit: triage findings can originate from contributor-controlled PR failure logs. Other automation follow-up exemptions remain unchanged.

- **Check-failure triage no longer follows PR-head agent-file symlinks when building diagnosis prompts.**

Agent files are now read as bounded regular files by a credential-free process. Symlinks and other non-regular files are omitted, and oversized files are truncated, preventing a PR from pulling runner credentials into the model prompt.

- **Check-failure triage rejects forged routing metadata in generated issues.** Check names and other header metadata are flattened before display, and the complete issue body is validated after redaction. Unsafe routing directives or markers stop issue creation; raw check names still determine de-duplication.

- Review-blocked poller fixes now allow edits only to files in the PR's verified, complete changed-file list. Judge citations can no longer expand the writable scope; out-of-PR edits are rejected without a push.

- Review-blocked poller fixes now honor `ALLOW_WORKFLOW_EDITS=false` for workflow templates, composite actions, and Claude Code hooks and settings, including files already in the PR.

- **Live Claude copy sync now verifies its PR target.** The post-push sync no longer mistakes a PR into another branch or from a fork for its PR into `main`. When no matching PR exists, it opens the intended PR instead of leaving the live copies out of date.

- **Review-blocked judge OpenCode fallback now stays isolated.** Verdict and fix passes run in fresh credential-free sandboxes when Claude is disabled or unavailable; verdicts use a read-only source snapshot and reviewer tools, while fixes retain isolated writer tools with validated transfer. Sandbox failures defer the judge instead of executing PR-controlled prompts on the host.

- **Orchestrator judge isolation latches remain in place when label reads fail.** An escalated judge resumes only after a live issue read confirms `ai:needs-human` was removed; a failed or malformed read defers the judge to the next poll tick.

- **The review-blocked judge no longer merges single-issue PRs with unresolved high, critical, or unrated security findings after audit exhaustion.** It fixes and re-audits while retries remain, then holds the PR for a clean audit or a human decision.

When the single-issue security pass exhausts its cycles, the judge's open security-finding list now distinguishes blocking severities from medium and low. A merge verdict with blocking findings becomes a fix attempt; a fix that makes no changes holds rather than merging. For blocking findings, an earlier auto-merge enrollment is withdrawn before the judge runs, even if the head has moved; the judge refuses to act on the mismatched head. At the final retry even a `close_and_reissue` verdict leaves the PR open with `ai:security-pass-failed` and `ai:review-blocked`, and the existing review alert reports the hold once per head. Medium and low findings continue through the existing follow-up flow.

What this means for operators: a blocked head cannot merge through the exhaustion judge until a clean audit or a human decision.

- **Single-issue security passes no longer permit merges without a completed audit of the current PR head.** Audit dispatch failures hold the merge, exhausted heads receive bounded additional retries before remaining held, and security-mode judge merges re-check the audited commit. Completed findings remain valid for the same head if a later attempt fails.

- **Check-failure triage now isolates host Python imports from PR-head files.**

The diagnosis helper runs from trusted workflow support and reads the PR checkout only as snapshot data. Host Python uses isolated imports so PR-added modules cannot run with the model provider credential. Existing clarify callers keep their current snapshot root by default.

- **Check-failure triage no longer lets check names inject issue routing metadata.** Workflow and check-run names are flattened and neutralized before appearing in the triage issue title, body, prompt, logs, and failure alerts; raw names still identify duplicate failures.

- **Stable releases now stop when shipped Claude templates differ from this repo's committed live copies.** Both `mark-stable.yml` and `test-and-mark-stable.yml` run the existing template/live parity test in `validate-scripts` before tagging.

Template-only PRs can pass CI with live copies prepared in a disposable checkout, but the release gates previously did not re-check the committed tree. The new gate makes no additional GitHub API calls and rejects unallowlisted drift before those templates reach consumer repositories. A pending or held `ai/sync-claude-live-copies*` PR must be merged, or both copies edited by hand, before the next release can pass; if automatic promotion has exhausted its retries, promote manually after parity is restored.

- Integration-conflict judge instructions require retaining shared protected-file lines.

The poller rejects a protected-file resolution that removes any occurrence present on both merge sides, including lines both sides added independently. The judge prompt now states that requirement. Rejected resolutions are not pushed; one-sided delete/modify conflicts can still resolve to deletion, but no automated override permits removing shared lines.

What this means for operators: resolve any protected-file merge that genuinely needs to remove shared lines manually.

- **Review consolidation no longer runs OpenCode on the host.** Both the default path and Claude-unavailable fallback use a fresh credential-free, read-only sandbox. If isolation fails, consolidation is skipped and the reviewer bundle remains authoritative.

- **Workflow heal now verifies phase-failure reports against GitHub evidence.** Before acting on a clarify, plan or implement failure report, the intake checks the run's repository, wrapper and outcome, its failed phase job, and a failure comment by a trusted source-issue author linking the run to the issue. Consumer reporting accounts can differ from the intake account; self-reports require an exact account match. Unverifiable reports are dropped with a warning rather than creating or escalating heal issues.

- **Workflow heal no longer trusts lineage markers on ordinary issues.** The intake checks reported generation and root against an authenticated, labelled heal issue before inheriting them. Unverified claims fall back to recorded fingerprint or source lineage instead of prematurely exhausting the heal budget.

- **`/deploy-activate` no longer runs Cloudflare preflight checks on unmerged project code with session credentials.** Worker deployment steps now require a verified, protected default-branch commit.

The command and its consumer template require an unmerged project PR to be merged before a Worker deploy step. They prefer CI check-runs and allow local checks only in a credential-free, no-egress sandbox. A confirmed deploy uses the verified commit and strips unrelated session credentials. This tightens the command's deployment path without loosening the existing approval or secret-handling rules.

What this means for operators: merge the project before approving a Worker deployment; if commit protection cannot be verified, the command will guide you through the step rather than deploying itself. Account-scoped Cloudflare tokens must be narrowed through external credential provisioning where possible.

- Full security audits now leave the last-audited-commit marker unchanged when a tracked text file exceeds the export caps. Findings still post, but the tracker records partial coverage and a warning is sent; subsequent default-branch audits repeat the full scan until a complete run clears the partial state. Binary skips do not hold the marker.

- Integration-conflict judge resolutions now verify line provenance for every conflicted file.

The scheduled poller rejects invented lines, deleted files, mode changes, and unverified line reordering or duplication in all conflicted paths, including application source files. A resolution composed of lines from the merge sides can still be pushed; symlink and gitlink targets may be chosen intact from either side. Consumer repositories receive the stricter check with the next stable release.

What this means for operators: source conflicts requiring genuinely new glue lines are rejected without a push and follow the existing bounded retry and escalation path.

- **The orchestrator's review-blocked judge can no longer push edits to workflows, scripts, prompts, Claude hooks or consumer templates.** Security finding `review-blocked-protected-file-write` (high) is closed. Refs #3576.

When a PR is stuck in `ai:review-blocked`, the poller asks a judge to unblock it, and the judge may choose `fix` and edit files on the PR branch. The judge reads untrusted PR comments. Before this change, with `ALLOW_WORKFLOW_EDITS` at its default of `true`, a fix could change any file already in the PR, including `.github/`, `scripts/`, `prompts/`, `.claude/` and `workflow-templates/`. The check looked at file names, not content, and the poller then pushed the change with its token. Now any fix that stages a path under those directories is rejected, whatever `ALLOW_WORKFLOW_EDITS` is set to. The rejection works like other scope rejections: no commit or push, one review-blocked retry used, and a Telegram WARNING.

The judge prompt now says protected-path fixes are unavailable. For findings in those directories, the judge chooses `merge_with_followup` when the PR is shippable, otherwise `close_and_reissue`. The repair then goes through the normal implement and review pipeline.

| The numbers that matter | Value |
| --- | --- |
| Protected-path fixes the poller judge can push | 0 (was: any protected file in the PR, by default) |
| New environment variables or GitHub API calls | 0 (a rejected fix now skips one PR file listing) |

What this means for operators: `REVIEW_BLOCKED_FIX_SCOPE_REJECTED` lines can now carry `reason=protected_path_forbidden`. Repos with `ALLOW_WORKFLOW_EDITS=false` still see `reason=workflow_edits_disabled`.

### For contributors

The standalone review-blocked judge (`scripts/review_rb_judge.sh`) fix path is unchanged and will be handled separately.

- **Alert on missing or stalled single-issue security follow-ups.** A findings hold only suppresses the review-blocked judge's CRITICAL alert when every finding in the current audit has an open pipeline-created follow-up for the PR branch and the findings marker is less than 24 hours old. The audit carries finding IDs into its trusted PR result comment; older results without IDs, missing current follow-ups, and unverifiable or stalled follow-ups keep auto-merge held and page a human. The source review sweep and consumer `ai-review.yml` schedule now dispatch one judge-only recheck per stale head/cycle, so the alert does not depend on an unrelated PR event. `SECURITY_PASS_FOLLOWUP_STALE_HOURS` adjusts the 24-hour limit.

- **Workflow-heal fix PRs no longer skip the single-issue security pass on the strength of their issue label and fingerprint marker.** Heal issues can originate from logs contributors influence, so their fix PRs into the default branch now require a current-head audit.

| Labels checked | Before | After |
| --- | --- | --- |
| Skip labels | `ai:security`, `ai:workflow-heal` | `ai:security` |

What this means for operators: heal fix PRs into the default branch now wait for a clean single-issue audit before auto-merge.

- **Claude write roles can no longer start with web tools, and the host provider relay rejects server-side web, code-execution and MCP connectors.** Claude roles now require the isolated execution helper and fall back to Codex rather than reading pool credentials from a host-side CLI.

Author-controlled issue context can reach the implement model. Previously the host Claude process could read pool tokens and Git credentials, and a container with `Bash` could still request provider-side egress via the relay. The CLI tool lists no longer include `WebFetch` or `WebSearch`; the relay accepts only untyped and `custom` client tools, rejecting unknown typed tools before forwarding either Messages or token-count requests. If the isolation helper is not staged, the Claude run fails closed with `AI_ENGINE_FALLBACK reason=support_missing` and Codex runs instead.

- **Fork PRs can no longer hold same-repository review PRs in the merge train.** Closes the high-severity STRIDE denial-of-service finding `fork-pr-blocks-merge-train` (issue #6500).

The merge-train gate and scheduled release now count only older PRs whose head repository matches the base repository, ignoring case. A fork named `ai/issue-*` and a deleted fork with no head repository cannot block a same-repository PR, consume its older-PR examination budget, or trigger a file-list fetch. Authorized same-repository PRs retain lowest-number-first ordering and the existing overlap check. The release backstop dispatches review when only fork blockers remain.

| The numbers that matter | Value |
| --- | --- |
| New GitHub API calls | 0 (head repository comes from the existing open-PR listing) |
| Fork PRs examined as blockers | 0 |

### For contributors

The `MERGE_TRAIN_ENABLED` switch, `MERGE_TRAIN_MAX_OLDER_PRS` cap, and one-shot manual bypass continue to work as before. The train logs `MERGE_TRAIN_FOREIGN_HEAD_SKIPPED` with PR numbers only; it does not print the fork repository name.

- **Forged queue comments can no longer bypass the merge train.** Resolves the `forged-queue-comment-bypasses-train` finding (issue #6501).

The train now recognizes queue markers only from the account authenticated for the run. A one-shot manual bypass proceeds only after its verified marker is consumed successfully; a failed update re-queues the review instead. Release also leaves a queued PR alone when the marker author cannot be verified.

| The numbers that matter | Value |
| --- | --- |
| New GitHub API calls | Up to 1 identity read per run, only when inspecting queue markers |

### For contributors

Removing `ai:merge-queued` and re-running review still provides a one-shot bypass when the train's own queued marker can be retired.

- **Issue text can no longer override implementation commit guards.** The implement workflow keeps issue bodies in a data file, frames title and scope labels with checked random delimiters, and pins guard settings to trusted repository variables at each guard step. The merged-PR guard now asks for confirmation when an appended Git directory override or unresolved directory change could send a commit to another checkout.

- **Merge-train bypasses now require a verified queue history.** A queued comment only counts when the authenticated automation account wrote it, and a one-shot bypass requires a later queue-label removal by a collaborator with triage-or-higher access. Forged comments cannot skip the queue or be retired by automation.

- **Workflow failure heal issues reject untrusted branch-routing directives.** Failure evidence and model diagnoses can no longer override the intake-selected target branch or forge routing and lineage markers; unsafe issue bodies stop creation and alert the operator.

- **Auto-merge no longer remains enrolled across reviewed-head changes without a new review.** A synchronize event withdraws stale enrollment, and review and security approval publish an `ai-review/head-gate` status on the evaluated commit. Requiring the status in default-branch protection also blocks pushes that do not trigger a synchronize event. Refs #3576.

- **The merged-PR guard no longer checks the session checkout in place of a push's unresolvable explicit Git override.** A push such as `GIT_DIR+=/path/to/other/.git git push origin HEAD`, or one with an unresolvable `-C`, `env -C`, `GIT_DIR` or `--git-dir` path, now asks for confirmation without querying the session checkout's PR history.

Security finding `appended-git-dir-checks-wrong-checkout` (#6305, re-issued as #6638) showed the gap: Git applies an appended `GIT_DIR+=` value on top of the shell's, which the hook cannot read, yet the hook fell back to the session checkout and, from a clean default-branch checkout, let the push to the other repository's merged branch pass. Commits with an unresolvable override already asked without that fallback; pushes now follow the same rule. A push whose directory is unknown only because of shell control flow or an unresolved `cd` keeps today's behaviour: the session checkout is checked and can block, and otherwise the push asks.

What this means for operators: in an interactive session, a push that carries an explicit Git directory override the hook cannot resolve prompts once; the prompt names the override, not the session checkout. Nothing changes for ordinary pushes.

### For contributors

`_GitInvocation` gains `explicit_directory_unresolved`; `_guarded_git_invocations` sets it for appended selectors and unresolvable `-C` / `env -C` / `GIT_DIR` paths, and `evaluate` routes such pushes to the confirmation reasons before any PR lookup. Both hook copies are byte-identical. Tests: `test_appended_git_override_falls_back_without_using_rhs` (rewritten), `test_appended_git_override_exploit_asks_from_default_branch_checkout`, `test_unresolved_explicit_push_override_asks_without_checking_checkout` in `tests/test_pr_merge_status_guard.py`.

- **Claude-selected conflict resolution now fails closed when isolation is unavailable.** Unsupported conflict paths, cleanup failures and failed transfers cannot route untrusted PR content to host OpenCode with runner credentials; Claude unavailability retries OpenCode in a fresh credential-free sandbox. Repeated same-head isolation failures count toward resolver escalation without relaxing fingerprint verification, and a symlinked workflow-support directory cannot supply the engine SHA for security-pass auto-reset.

### For contributors

The decision is `authorize_target` in `scripts/claude_issue_route.py` (CLI `authorize-target`), a pure function. `scripts/claude_issue_intake.sh` performs the reads and refuses through `reject()`, which unlike `fail()` never labels or comments on the target; `fail()` is left for failures after authorization (`queue_not_configured`, `queue_failed`). Tests are in `tests/test_claude_issue_route.py`.

- **The Claude issue pickup now starts only queue items that match what the run that queued them recorded.** An `ai:claude-issue-queue` issue edited after it was opened is refused instead of starting a session for the edited target.

Before this fix, the pickup trusted a queue issue because `github-actions[bot]` had opened it. That says nothing about the title and payload, which anyone who can edit the issue can change afterwards (security finding #4621). Now `claude-issue-intake.yml` and the `claude-pr-catch-all` job of `review_autofix_sweep.yml` record every queue item they open, with its exact title and payload, in a `claude-issue-queue-binding` artifact of their own run. `claude_issue_route.py queue-pending --fetch-repo` follows the item's `Intake run:` / `Sweep run:` line to that run. It checks that the run is a completed default-branch run of the right workflow and event, and that the run's head commit is on the default branch itself (one compare read against `refs/heads/<default branch>`, so a tag carrying the branch's name does not pass). It also requires the artifact to list the item unchanged, and the body to be exactly what the producer wrote, so no added text reaches the pickup. Anything else is listed under `ignored` (`unbound`, `binding_mismatch`, `binding_untrusted`, `binding_pending`, `binding_unavailable`) and left open for the watchdog.

| The numbers that matter | Value |
| --- | --- |
| Producer workflows and events trusted | `claude-issue-intake.yml` (`repository_dispatch`, `workflow_dispatch`), `review_autofix_sweep.yml` (`schedule`, `workflow_dispatch`), default branch only |
| Binding artifact | `claude-issue-queue-binding`, uploaded with `if: always()`, kept 30 days |
| Pickup API reads per wake with items open | 1 queue read + 4 shared reads + 1 compare read and 1 artifact download per completed producer run (per-run fallbacks when a listing misses) |
| New env vars (with defaults) | `CLAUDE_ISSUE_QUEUE_BINDING_FILE`, `CLAUDE_PR_SWEEP_QUEUE_BINDING_FILE` |

What this means for operators: nothing to configure. Queue items opened before this change carry no binding, so the pickup refuses them and the watchdog flags them after `CLAUDE_ISSUE_QUEUE_STALE_HOURS` (default 3). For an issue item, comment `/reclarify` on the target issue: the intake rewrites and re-binds the queue item. For a PR-fix item, close the stale queue issue, and the next hourly sweep queues a fresh, bound one. Consumer repos need no change.

### For contributors

`scripts/claude_issue_route.py` gains `append_queue_binding` / `add-queue-binding`, `load_queue_binding`, `evaluate_producer_run`, `fetch_queue_bindings`, `queue_binding_run_ids` and `queue_binding_verdict`. `queue_pending` takes an optional `bindings` argument and reports `deferred`. `queue-pending` gains `--bindings-json` and `--default-branch`. `queue_pending(..., bindings=None)` skips the check and is kept only for the sweep's "already queued" dedupe, which must count every open trusted item. Tests are in `tests/test_claude_issue_route.py` and `tests/test_claude_pr_sweep.py`.

- **Only the PR's author or the workflow account can claim or hold a `claude/*` pull request.** Before this change any collaborator could post an `ai:claude-fix-claim` hold marker and keep the §26 checker and the catch-all sweep away from a PR indefinitely.

`read_fix_claims` in `.claude/scripts/check_in_status.py` used to count every claim comment with an `OWNER`, `MEMBER`, or `COLLABORATOR` author association. A collaborator could therefore comment a well-formed `kind=hold` marker for a PR's current head, which parks it until someone pushes. The same collaborator could also post `conflict` / `ci` / `blocked` claims on made-up heads to push `hand_backs` to the cap, which forces the real fixer into a hold. Now a claim counts only when its comment is posted as the PR's own author (the account whose Claude sessions push and fix it) or as `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` (the `GH_PAT` account the `claude-pr-catch-all` sweep posts its reservations with). Logins are compared case-insensitively. When neither is known, no claim counts. The check reuses the PR read the verdict already makes, so it adds no GitHub API call (issue #4622).

| The numbers that matter | Value |
| --- | --- |
| Accounts whose claims count | 2: the PR's author and `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` |
| Extra GitHub API calls | 0 |
| Claim marker format, lease, cap | unchanged (`CLAUDE_FIX_CLAIM_LEASE_HOURS` 3, `CLAUDE_FIX_HAND_BACK_CAP` 3) |

What this means for operators: a hold or claim posted from any other account is now ignored, including one from a teammate's Claude session fixing a PR someone else opened, so the checker or the sweep may start a second fixer on that head. If the `GH_PAT` account differs from the users whose Claude sessions open `claude/*` PRs, set the `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` repository variable so the sweep's reservations keep counting. Consumer repos receive the updated `.claude/scripts/check_in_status.py` on the next `@stable` sync.

### For contributors

`_fix_claim_trusted_logins(pr)` builds the trusted set from `pr["user"]["login"]` and the environment variable, and `check_pr_hand_back` passes it to `read_fix_claims` through the new keyword parameter `trusted_logins` (default `()`, which counts nothing). New cases in `tests/test_check_in_status_hand_back.py` cover a forged hold, forged counted claims, the workflow account's reservation, a PR with no readable author, and case-insensitive matching.

- **An `ai:security`, `ai:check-triage`, or `ai:workflow-heal` label no longer switches off the security pass by itself.** `/implement-issue-claude` now skips an issue-mode project's security audit only for issues that the issue automation created and labelled.

Before this change, anyone who could label an issue could make the project built from it skip `security-audit.yml` (finding `mutable-label-skips-security-pass`, #4623). Step 6 of `.claude/commands/implement-issue-claude.md` now runs `.claude/scripts/security_pass_skip.py --repo <owner>/<repo> --issue <N>` and writes `Security pass: skip` only when the script prints `"skip": true`. That requires four things:

- the author is `github-actions[bot]`, or the repository `OWNER` account that the audit, triage and heal workflows post as through `GH_PAT`;
- that account applied the label within 120 seconds of creation, and no other account ever applied it;
- the body carries the producer's marker line;
- for `ai:security`, `Refs #<tracker>` names the `ai:security-audit` tracker created by the same account.

Any other result, and any failed read, keeps `Security pass: run`.

| The numbers that matter | Value |
| --- | --- |
| REST reads per check | 1 with no skip label, at most 3 otherwise |
| Label-at-creation window | 120 seconds |
| Skip labels checked | `ai:security`, `ai:check-triage`, `ai:workflow-heal` |

What this means for operators: security follow-ups, check-failure triage issues and workflow-heal issues filed by automation still skip their own audit, so a fix cannot spawn follow-ups of follow-ups. An ordinary issue that someone labels by hand now runs the full security pass. In a repository owned by an organisation, the `GH_PAT` account is a `MEMBER`, not the `OWNER`, so the skip never verifies there and the audit always runs.

### For contributors

The script is mirrored to `workflow-templates/.claude/scripts/` and allowlisted in both `.claude/settings.json` files. `route_issue`'s `skip_security_pass` field in `scripts/claude_issue_route.py` is unchanged and remains advisory: it is logged and carried in the dispatch payload, but it decides nothing. The contract lives in `tests/test_security_pass_skip.py`, which runs in the `Claude issue implementer tests` step of `ci.yml`.

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
