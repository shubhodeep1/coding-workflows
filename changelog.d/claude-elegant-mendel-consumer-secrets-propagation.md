<!-- changelog: added -->
- **Consumer repositories receive the pipeline's secrets automatically.** A new `Propagate consumer secrets` workflow copies `CHECK_TRIAGE_ISSUES_TOKEN`, `GH_PAT`, `OPENROUTER_API_KEY` and `TG_BOT_SECRET` from this repository into every consumer the moment its registration lands in `.github/ai/consumer_repos.json`.

Until now a `/seed-repo` run ended with a manual checklist: an operator had to open each new consumer's Settings → Secrets page and paste four values, and the check-failure triage posting token was not even on that list. `.github/workflows/propagate-consumer-secrets.yml` runs on every push to `main` that changes the registry and targets only the entries that push added; `scripts/propagate_consumer_secrets.sh` writes each secret through `gh secret set` with the value on stdin, then confirms the names with `gh secret list`. A `workflow_dispatch` with an empty `targets` input backfills every registry entry once for the consumers registered before this workflow existed. Targets outside the registry are refused, an empty library secret is skipped with a warning, and any failed or unverified write leaves the run red with a Telegram CRITICAL so the workflow-failure heal intake picks it up.

| The numbers that matter | Value |
| --- | --- |
| Secrets copied per consumer | 4 |
| Trigger path | `.github/ai/consumer_repos.json` on `main` |
| Required `GH_PAT` scope on consumers | `repo` (unchanged; the `@stable` dispatch already needs it) |
| Tests | 16 in `tests/test_propagate_consumer_secrets.py` (own `ci.yml` step) |

What this means for operators: issue the triage posting token once with "All repositories" access, store it in this repository, and never visit a consumer's secrets page again; the seed checklist now says so.

### For contributors

Log keys are `CONSUMER_SECRETS_PROPAGATE repo=… secret=… status=<set|skipped_empty|failed|verify_missing>` plus a `summary` line. The script sources `scripts/gh_helpers.sh` for `gh_retry`; retry diagnostics echo only the command words, never the stdin value.
