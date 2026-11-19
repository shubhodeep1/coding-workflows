# awesome-cc Future Improvements

This doc enumerates items observed in `hesreallyhim/awesome-claude-code`
that the implementation plan
`docs/plans/awesome-claude-code-learnings-plan.md` explicitly **deferred
or excluded**. Each item lists what it is, why it was excluded, and what
conditions would justify revisit.

This is a backlog, not a roadmap. No phase ordering, no commit
messages. Items here are reviewed:

- When a new plan touches one of the adopted themes — cross-check
  whether deferral conditions still hold.
- When a real-world incident (e.g. an informal-issue cluster, a
  model-catalog drift, a contributor-facing UX gap) makes a deferred
  item concretely valuable.

For context, the parent plan (`docs/plans/awesome-claude-code-learnings-plan.md`)
adopts nine themes from upstream (A–I) with concrete edits in nine
commits. The items below are everything else surfaced by the survey.

## Explicitly excluded per Q4 of the clarification batch

### EX1 — SVG badges, animated tickers, README-style alternatives

**Source:**

- `awesome-claude-code/assets/badge-*.svg`
- `awesome-claude-code/scripts/ticker/` (animated SVG ticker driven
  by `data/repo-ticker.csv`)
- `awesome-claude-code/README_ALTERNATIVES/` — 41 generated `.md`
  files (3 style variants × 9 flat category cuts × 4 sort orders)
- `awesome-claude-code/scripts/readme/generators/{awesome,flat,minimal,visual}.py`

**Why excluded.** Presentation tooling for a curated-list / awesome
repo. Our `README.md` is a long-form operator runbook, not a
discoverability page; we have no audience for badges, multi-style
alternative views, or animated activity tickers.

**Revisit trigger.** If we ever ship a public catalog of workflows-as-a-product
or a consumer-facing dashboard, the multi-style README generator and
SVG ticker patterns become relevant.

### EX2 — Cloning the curated CSV catalog itself

**Source.** `awesome-claude-code/THE_RESOURCES_TABLE.csv` (226 rows ×
20 columns).

**Why excluded.** We are not an awesome-list. The Phase 2
model-catalog drift check borrows the CSV-as-source *pattern*; it
does not clone the *catalog*. There is no curated-resource maintenance
burden to inherit.

**Revisit trigger.** If we add a curated index of consumer repos with
per-repo metadata (currently `.github/ai/consumer_repos.json` is a
flat array of `owner/repo` strings), the structured CSV-with-validation
pattern becomes appropriate. A future plan would extend
`consumer_repos.json` into a richer YAML/CSV with per-row metadata
(intake date, last-dispatch SHA, opt-out flags) and add a validator
running in CI.

### EX3 — Adopting the generic `pr-review.md` slash command

**Source.** `awesome-claude-code/resources/slash-commands/pr-review/pr-review.md`
— 4-role rubric (PM / Dev / QA / Security review, fix everything
now).

**Why excluded.** Weaker than our CLAUDE.md §12 PR-review-mode policy
(multi-tier, model-multiplexed, ledger-tracked, with a separate
review-blocked judge). Adopting the upstream's 4-role rubric would
dilute, not augment, our existing policy.

**Revisit trigger.** None foreseen. Our policy supersedes.

### EX4 — Consumer-repo propagation in this PR

**Source.** awesome-claude-code does not propagate anything to
consumer repos (it is a destination, not a hub). The "exclude" here
is about not extending this plan's adoptions into
`workflow-templates/` or `.github/ai/consumer_repos.json` dispatch in
the same PR.

**Why excluded.** Consumer repos pick up changes via the next
`@stable` tag and their own workflow-update path. Bundling consumer
propagation into the same PR multiplies blast radius for a plan that
is otherwise local-only.

**Revisit trigger.** If a specific adopted theme turns out to be
relevant for consumers, a follow-up plan can promote that one item to
`workflow-templates/`. Most likely candidates:

- Phase 6's overrides-with-auto-locking pattern, if consumer-side
  workflow templates start accumulating intentional non-default
  values that contributors keep "fixing".
- Phase 8's structured-task issue template, if consumers want the
  same opt-in structured-form path.

### EX5 — Cross-repo cooldown / sanctions enforcement

**Source.**

- `awesome-claude-code/.github/workflows/submission-enforcement-v2.yml`
- `awesome-claude-code/docs/COOLDOWN.md`
- The external `awesome-claude-code-ops` private state-store repo
  (referenced by `submission-enforcement-v2.yml:1-40`).

**Why excluded.** The operational complexity (separate PAT scope,
separate repo, separate state store, per-user concurrency groups) is
justified only when you have abusive contributors. We do not. Our
`/answer` and `/approved` commands are issued only by maintainers and
the orchestrator; the consumer repos that dispatch to our workflows
are listed in `.github/ai/consumer_repos.json` and are trusted by
construction.

**Revisit trigger.** If we ever expose AI workflows for direct
triggering by external contributors (currently they are gated behind
PR review and maintainer-only commands), the cross-repo state-store
pattern becomes the right shape for rate-limiting.

### EX6 — §6-breaking renames

**Source.** N/A — categorical exclusion.

**Why excluded.** CLAUDE.md §6 binds every plan. Any adoption that
requires renaming an existing identifier (function, log prefix, env
var, label, file name, JSON field) gets routed to a paired
alias-shim plan, not bundled into a learnings plan. The parent plan
preserves every existing identifier verbatim.

**Revisit trigger.** N/A — this is a permanent constraint. Specific
rename ideas surfaced by the survey (e.g. renaming `record_id` to a
shorter form, renaming `ai:done` to `ai:reviewing`) would each need
their own §6 alias-shim plan; the cost typically exceeds the
benefit.

## Natural extensions of adopted themes (deferred but tractable)

### EXT1 — Wire `clarify_informal_detect.py` into the clarify workflow

**Status.** Phase 4 of the parent plan ships the helper + tests +
advisory clarify-prompt rule, but does NOT wire the score into
`.github/workflows/clarify.yml` / `internal-clarify.yml`. This was an
intentional scope cut.

**What "wired in" would look like.**

- New step in `clarify.yml` (and `internal-clarify.yml`) before the
  clarifier model runs: invoke
  `scripts/clarify_informal_detect.py --issue-body-file <path>` and
  prepend the resulting `CLARIFY_INFORMAL_SCORE:` line to the model
  prompt's issue-context block.
- Optional behavior gate (new env var
  `CLARIFY_INFORMAL_AUTO_REJECT_SCORE`, default `0` = disabled): when
  the score is at or above the threshold, post an auto-comment asking
  the contributor to use the structured-task form
  (`.github/ISSUE_TEMPLATE/structured-task.yml` from Phase 8) and
  pause the clarify run.
- Telemetry: emit a `CLARIFY_INFORMAL_SCORE_OBSERVED` log line for
  workflow-log-analysis to track score distribution.

**Why deferred.** Requires touching two workflow files and adding a
new env var; would expand the parent PR scope and lengthen review.
The helper is useful standalone (callable from CLI or via a future
hook).

**Revisit trigger.** After Phase 4 lands and we have one or two
real-world informal-issue cases logged.

### EXT2 — Unified overrides registry

**Status.** Phase 6 of the parent plan ships two separate override
files (`scripts/codex_model_catalog_overrides.yaml`,
`prompts/.overrides.yaml`). The awesome-claude-code original
`templates/resource-overrides.yaml` is a single registry that targets
rows by stable ID across the catalog.

**What "consolidated" would look like.** A single `overrides.yaml` at
repo root with `targets:` entries, each scoping by file glob + optional
ID/field selector. The two phase-6 YAMLs become subsections of this
unified file. The generator (and any future prompt-rewriter) reads
the unified file once.

**Why deferred.** Two override files cover the two known surfaces. A
unified registry only earns its keep when we have ≥3 distinct
override surfaces and the lookup-by-target abstraction starts paying
for itself.

**Revisit trigger.** When a third override surface lands — likely
candidates are workflow-template freezing (so consumer-side
auto-updates can skip intentionally-customised templates),
`/db/contracts/*.yml` field freezing (so contract regenerators don't
auto-rewrite intentionally-customised invariants), or
`scripts/codex_model_catalog.json` whole-row freezes (rather than
per-field).

### EXT3 — Tree-marker pattern for `README.md` env-var table

**Status.** Phase 3 wires the tree-marker drift check into
`agents.md` for two workflow-file directory listings. `README.md`'s
much larger env-var table (≈ 150 rows in the "Variables" section) is
the highest-drift surface and is currently maintained by hand.

**What "extended" would look like.**

- Generate the env-var table from a structured source. Two
  candidates: (a) a new `docs/env_vars.yaml` with one entry per env
  var (`name`, `default`, `consumers`, `description`); (b) parse the
  existing inline definitions from `agents.md` / workflow `env:`
  blocks. Option (a) is cleaner; option (b) avoids creating a third
  source of truth.
- Render the parsed data into a `<!-- TREE:START id=env_vars -->`
  block in `README.md` via `tools/repo_tree/update_repo_tree.py`
  (extended with a render-from-yaml mode).
- Gate via the existing `make generate-check` CI step.

**Why deferred.** The env-var table is the most-edited section of
`README.md`. A generator that round-trips the existing hand-written
prose without loss is non-trivial — the existing descriptions are
multi-paragraph, contain internal cross-references, and embed
inline code that the table format would need to preserve. Worth a
dedicated plan.

**Revisit trigger.** Next time we add ≥5 env vars in one plan, the
table-maintenance cost will justify the generator. Alternatively, if
a future bug surfaces a table row that contradicted the runtime
behavior (the table claimed a default that the workflow no longer
uses), that's a strong revisit signal.

### EXT4 — XML-tag scaffolding for prompts (cross-reference)

**Status.** This deferred item is already covered by
`docs/ai-tools-future-improvements.md` item S1 from the prior
`apply-ai-tools-learnings-plan` precedent. Recorded here for
grep-ability and so this doc enumerates the full deferred surface.

**Why deferred.** See `docs/ai-tools-future-improvements.md` S1.

**Revisit trigger.** Same as the prior precedent doc.

### EXT5 — Repo-tree drift check extended to consumer repos

**Status.** Phase 3 of the parent plan wires the tree-marker drift
check into `agents.md` for `.github/workflows/*.yml` and
`workflow-templates/*.yml`. Consumer repos that copy
`workflow-templates/` to `.github/workflows/` could benefit from the
same pattern in their own READMEs.

**What "consumer-side" would look like.**

- Ship `tools/repo_tree/update_repo_tree.py` as part of
  `workflow-templates/` propagation so consumer repos inherit it.
- Document the recommended `<!-- TREE:START id=workflows -->`
  convention in consumer-facing docs.

**Why deferred.** Q4-D excludes consumer-repo propagation from this
PR. The tooling is small enough that consumer repos can copy it
manually if they want.

**Revisit trigger.** If a consumer repo reports they would benefit
from the drift check.

## Observed but not categorized

### OBS1 — `make test-regenerate-no-cleanup` debugging variant

**Source.** `awesome-claude-code/Makefile` (debugging variant of the
regenerate cycle that preserves intermediate state when the
generator fails).

**Why noted.** If our Phase 2/3 cycle test ever needs to debug a
generator failure, this variant is the right shape: re-run the
generator with intermediate state preserved (no temp-dir cleanup) so
the diff can be inspected post-mortem.

**Adoption cost.** ≈ 10 lines added to the parent plan's `Makefile`.
Worth adding the first time we hit a generator failure that the
basic `make generate-check` output doesn't explain.

### OBS2 — Animated SVG ticker for repo activity

**Source.** `awesome-claude-code/scripts/ticker/` — builds an
animated SVG ticker from `data/repo-ticker.csv` snapshots.

**Why noted.** Excluded from the parent plan (see EX1). The
technique (animated SVG built from a CSV time series) is reusable if
we ever want a public dashboard.

### OBS3 — `scripts/maintenance/check_repo_health.py`

**Source.** awesome-claude-code's per-resource link checker.

**Why noted.** Calls `gh api repos/<owner>/<repo>` per row with no
batching. Direct adoption would violate our §15 GitHub API hygiene
(per-iteration calls inside a loop is a review-blocker per §15). If
we ever need an equivalent link-check sweep across all consumer
repos, batch via GraphQL — see the existing helpers
`_fetch_candidate_issue_details_graphql` and
`_fetch_linked_pr_status_graphql` in
`scripts/orchestrate_poll_process.sh`.

**Adoption cost (if revisited).** ≈ 100 lines for a GraphQL-batched
consumer-repo health checker. Useful for the periodic
workflow-log-analysis sweep.

### OBS4 — Pre-commit `.pre-commit-config.yaml` patterns

**Source.** `awesome-claude-code/.pre-commit-config.yaml:30-36` —
runs `make generate && git diff --exit-code README.md` as a
pre-commit hook so drift fails the local commit as well as CI.

**Why noted.** This repo has no `.pre-commit-config.yaml` today. The
parent plan's Phase 2 gates drift in CI only, which means a
contributor can push a drifted commit and only find out from a
failed CI run. A `.pre-commit-config.yaml` would catch it earlier.

**Adoption cost.** Small (≈ 15 lines of YAML), but adopting
pre-commit as a project convention has cross-cutting consequences
(every contributor needs `pre-commit install`, the README needs a
contributor-setup section). Deferred until the convention is worth
the friction.

**Revisit trigger.** If contributors regularly push drifted commits
that fail the parent-plan's CI drift check.

### OBS5 — Issue-form-driven submission pipeline (parent: Phase 8)

**Source.** `awesome-claude-code/.github/ISSUE_TEMPLATE/recommend-resource.yml`
+ `.github/workflows/submission-enforcement-v2.yml` +
`handle-resource-submission-commands.yml` +
`validate-new-issue.yml` + `close-resource-pr.yml`.

**Why noted.** Phase 8 of the parent plan adopts the issue-form
*template* and the state-machine *documentation* but does NOT adopt
the upstream's command-handling workflow (`/approve`, `/reject`,
`/request-changes` parsing). Our pipeline has a richer command
vocabulary (`/answer`, `/approved`, `/reclarify`, `/clarify-now`) and
its own dispatch path; cloning the upstream's command-handler would
collide with ours.

**Adoption cost.** Zero — already covered by the parent plan's
Phase 8 in the form appropriate to our pipeline.

## Revisit policy

This doc is a backlog, not a roadmap. The owning maintainer reviews
items here:

- When a new plan touches one of the adopted themes — cross-check
  whether the deferral conditions still hold.
- When a real-world incident (informal-issue cluster, model-catalog
  drift, contributor-facing UX gap) makes one of the deferred items
  concretely valuable.
- Once per quarter as part of the workflow-log-analysis cadence — if
  any deferred item has accumulated enough signal to promote.

When an item is promoted to a future plan, this doc is updated in
the same PR to either move the entry to a `Promoted` section or
delete it with a one-line forward reference to the promoting plan.

## Open Questions

- **OQ-FIX1.** When OBS4 (pre-commit) is eventually adopted, should
  the hook run only `make generate-check` or also lint /
  validation-harness checks? Decide when adopting.
- **OQ-FIX2.** When EXT1 (clarify-wrapper wiring) is eventually
  adopted, what's the default value of
  `CLARIFY_INFORMAL_AUTO_REJECT_SCORE` — `0` (disabled), `0.6`
  (mid), or `0.8` (high-confidence only)? Decide based on observed
  score distribution after Phase 4 ships standalone.

## References

- Parent plan:
  `docs/plans/awesome-claude-code-learnings-plan.md`.
- Sibling precedent: `docs/ai-tools-future-improvements.md`.
- Upstream survey source:
  <https://github.com/hesreallyhim/awesome-claude-code>.
