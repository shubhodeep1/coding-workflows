<!-- changelog: added -->
- **The AI review pipeline now resolves the PR review threads it has addressed.** `review_autofix.yml` gains a `Resolve addressed PR review threads` step that marks a reviewer's thread resolved once the editor's `PR comment audit:` section has given that comment a validated disposition; an `applied` disposition additionally requires a productive commit.

The pipeline has always read PR review comments — `scripts/review_collect_pr_metadata.sh` fetches them into `PR_ALL_COMMENTS_CONTEXT_FILE`, the reviewer and editor prompts both inline it, and the editor must audit each one — but nothing ever closed the thread. A comment the editor fixed on iteration 2 looked exactly like one it had never read, so a PR whose findings were all addressed still read as a PR whose review feedback had been ignored. On `shubhodeep1/fun-token-multi-chain#404`, 11 of 12 Copilot findings were fixed in code across four autofix rounds and all 12 threads still showed unresolved. Resolution is keyed on the audited comment's id rather than its file and line, so two comments at one location cannot close each other. An `ignored` disposition resolves the thread too, but only after the editor's stated reason is posted as a reply, so a reviewer can see the rejection and reopen.

| The numbers that matter | Value |
| --- | --- |
| New repo vars | `REVIEW_RESOLVE_THREADS_ENABLED` (`true`), `REVIEW_RESOLVE_THREADS_MAX` (`50`) |
| GitHub API calls added per run | 1 paginated thread query + 1 mutation per resolved thread + 1 reply per `ignored` thread |
| Threads resolved per run before the cap warns | 50 |
| New tests | 31 in `tests/test_review_resolve_review_threads.py` |

What this means for operators: a PR that has been through the autofix loop now shows its review threads in the state the pipeline actually left them, so an open thread is a real signal again rather than the default. Set `REVIEW_RESOLVE_THREADS_ENABLED=false` in a consumer repo to keep the previous behaviour and leave every thread untouched.

### For contributors

Only entries the editor explicitly listed are eligible, and an entry whose audited path disagrees with the real comment's path is skipped — that pair of rules is what keeps a mis-keyed audit line from burying a live finding, which is a case observed in production rather than a hypothetical. Context-file parsing is first-wins per field because comment bodies are dumped into the same `entry[N].<field>` stream after the structured fields, so last-wins parsing would let attacker-controlled comment prose containing `entry[0].id: 999` redirect a resolve at an unrelated thread. Thread lookup uses GraphQL because REST has no resolve-review-thread endpoint; the §21.D/§23.D preference for REST addresses the Claude Code Web agent proxy in interactive sessions and does not apply to this Actions-side caller. Every failure path warns and exits 0, and the step carries `continue-on-error: true`.
