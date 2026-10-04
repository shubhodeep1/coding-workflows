<!-- changelog: security -->
- **Every Codex agent that reads untrusted text now runs in a credential-free, network-isolated container.** A prompt injection in an issue, comment, PR diff, CI log or workflow log can no longer read `GH_PAT`, the OpenRouter key, the Telegram secrets or the checkout's `.git`, and has no network to send anything out.

Until now the plan and implement agents, and over twenty other agent launches, ran on the runner with `--sandbox danger-full-access`, `GH_TOKEN` (`GH_PAT`, with `repo` scope on this repo and every consumer, plus `workflow`) and `OPENROUTER_API_KEY` in their environment, and the PAT stored in `.git/config` by `actions/checkout` and the `git remote set-url` step. They now go through `scripts/codex_isolated_exec.sh`, the pattern clarify already used: a Docker container with `--network none`, a read-only root, no capabilities, no runner environment and no host checkout, that reaches the model only through `scripts/clarify_openrouter_broker.py` on the host. Read-only agents see a copy of the tracked files; the implement agent edits a disposable copy, and only changed regular files come back. Scripts a job runs after an agent wrote files come from a trusted copy the agent cannot reach, so its output never becomes host code.

| The numbers that matter | Value |
| --- | --- |
| Agent launches moved into a container | 24 (23 Codex, 1 OpenCode fix writer), across 10 workflows and 12 scripts |
| Credentials left in the agent's environment | 0 (was `GH_TOKEN`, `OPENROUTER_API_KEY`, `TG_BOT_SECRET`) |
| Container network | `none` (model calls via the host broker only) |
| Extra time per job | about 70 s for the first image build; implement also installs dependencies once |

What this means for operators: runners need Docker, which GitHub-hosted `ubuntu-latest` provides. A missing Docker or a failed image build fails the step with `::error::CODEX_ISOLATION …`; nothing falls back to running Codex on the host, and there is no variable that turns isolation off. The implement agent can no longer `pip install` or `npm install` during its run: declared dependencies are preinstalled and anything else is reported as UNVERIFIED. Serena is not available inside the container; Semble is unaffected.

### For contributors

- New helpers: `scripts/codex_isolated_exec.sh` (`prepare` / `run --mode read-only|workspace` / `cleanup`) and `scripts/codex_isolated_workspace.py` (snapshot, dependency-prep finalisation, validated write-back, synthetic `.git`). `codex_thread_reuse.sh` launches through the helper when `CODEX_ISOLATED_EXEC` is set.
- `IMPLEMENT_STAGED_SUPPORT_RUN_DIR` is now staged in every repository; implement's Codex, repair and later steps execute their helpers from it, and `after_run` workspace hooks run from a copy taken before the editor.
- The orchestrator's review-blocked fix and integration-conflict judge work in separate git worktrees. For the integration judge the poller now fetches, merges, checks conflict markers and the merged sub-issue fingerprints, commits and pushes; the agent only resolves files. The review-blocked judge's OpenCode fix writer runs in `scripts/review_untrusted_sandbox.sh`.
- `tests/test_codex_agent_isolation_contract.py` fails on any direct `codex` launch outside the two container entrypoints; `tests/test_codex_isolated_exec.py` and `tests/test_codex_isolated_workspace.py` cover the behaviour, and `tests/test_codex_isolated_exec_docker_e2e.py` (opt-in, `CODEX_ISOLATION_DOCKER_E2E=1`) runs the real image and Codex CLI against a fake model endpoint.
