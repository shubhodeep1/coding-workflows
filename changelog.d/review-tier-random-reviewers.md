<!-- changelog: changed -->
- **Review tiers are now on by default: small PRs get one or three reviewers instead of the full six-model panel, drawn at random per PR.** A diff of up to 50 lines that touches no protected path runs one reviewer, any diff of up to 200 lines runs three, and larger diffs keep the full panel.

`review_autofix.yml` now defaults `REVIEW_TIER_RESOLVER_ENABLED` to `true`, so every repo on `@stable` picks this up on the next sync. The tiers also no longer depend on folder names. Until now the resolver was off, and turning it on gave three reviewers only when every change sat in one of `scripts/`, `prompts/`, `.github/workflows/` or `tests/`, and one reviewer only for docs-only diffs, so application code in a consumer repo still got all six. A small diff that touches a protected path (the same list the deterministic skip gate uses: `agents.md`, `CLAUDE.md`, `.github/`, `.claude/`, `scripts/`, `prompts/`, `workflow-templates/`, `db/contracts/`, build, dependency and config files, both sides of a rename) never drops below three reviewers. The reviewers for the one- and three-reviewer tiers are picked from `REVIEWER_MODELS` by `sha256("<PR number>:<model>")`, so a PR keeps the same reviewers on every fix round and rerun while different PRs spread evenly across the panel.

| The numbers that matter | Value |
| --- | --- |
| `lite` tier | 1 reviewer, diff of at most `REVIEW_TIER_LITE_MAX_LOC` (50) lines, no protected path |
| `standard` tier | 3 reviewers, diff of at most `REVIEW_TIER_STANDARD_MAX_LOC` (200) lines, any folder |
| `full` tier | all 6 `REVIEWER_MODELS`, larger diffs, `[force-review]` / `force-review`, fail-open cases |
| `REVIEW_TIER_LITE_REVIEWER_SLUG` default | empty (was `qwen/qwen3.7-plus`): random pick |
| `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` default | empty (was `minimax/minimax-m3,deepseek/deepseek-v4-pro,openai/gpt-6-luna`): random pick |

What this means for operators: most small PRs now cost one or three reviewer calls per pass instead of six. Set `vars.REVIEW_TIER_RESOLVER_ENABLED=false` to keep the full panel on every PR, or set `vars.REVIEW_TIER_LITE_REVIEWER_SLUG` / `vars.REVIEW_TIER_STANDARD_REVIEWER_SLUGS` to pin specific reviewers instead of the random pick. Docs-only PRs and PRs of at most 10 added and 10 removed lines that touch no protected path still skip review entirely through `AUTOFIX_SKIP_DOC_ONLY` and `AUTOFIX_SKIP_MAX_ADDITIONS` / `AUTOFIX_SKIP_MAX_DELETIONS`, and the release-gate smoke PRs still get the full panel through their `force-review` label.

### For contributors

The `REVIEW_TIER:` log line gains `protected=<bool>` and, when set, `protected_path=<path>`; `models_source` gains `random_lite` and `random_standard`, and `reason` gains `code_<=50_loc_unprotected`, `protected_path_<=50_loc` and `code_<=200_loc`. Existing reason values are kept where they still describe the decision. A full panel already forced by the risk-tier resolver (`REVIEWER_RISK_TIER_ENABLED`, for example its `REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX`) is never shrunk by the size tiers (`reason=risk_tier_forced_full`), and a random pick that returns fewer reviewers than the tier asks for (for example without `sha256sum`) fails open to the full panel (`reason=random_reviewer_pick_failed`, `models_source=fallback_full_random_pick_failed`). The protected-path lists in `scripts/review_run_reviewers.sh` mirror the gate's `PROTECTED_SKIP_SUPPRESSED` patterns, and `tests/test_review_autofix_review_pipeline_contract.py` fails if the two drift apart.
