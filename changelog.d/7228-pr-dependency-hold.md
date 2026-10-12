<!-- changelog: added -->
- **A standalone issue can wait for a pull request to merge before the pipeline works on it.** A trusted issue that declares `- Depends on PR: #N` and `- Requires on main: <path> contains <text>` is held until PR #N is merged into the default branch and that file on the default branch contains the text; the poller then restarts it once with `/reclarify`.

Re-issues of #7186 kept reaching planning and blocking again while PR #6667 was still a draft, because the only automatic dependency hold was the one for security follow-ups. The new helper `scripts/pr_dependency.py` is called by clarify, plan, both implement gates and the standalone stall poller. While an issue is held, clarify posts no questions, keeps only `ai:clarification` and removes `ai:blocked`; plan and implement skip it; and stall recovery does nothing for it, so the unblock scan never spends rounds on it. The issue author must be OWNER, MEMBER, COLLABORATOR or the pipeline account. Anything malformed, repeated, untrusted or unreadable stays held. Orchestrator-managed issues are not affected. The security follow-up hold (`- Depends on: #N`) is unchanged.

| The numbers that matter | Value |
| --- | --- |
| API reads per held issue per poll tick | 1 (`pulls/N`), plus 1 `contents` read once the PR has merged |
| Release marker | `<!-- ai:pr-dependency-released:<issue> -->`, counted only from the pipeline account |
| Log lines | `AI_PHASE_GATE_V1 ... gate=pr_dependency`, `STALL_SKIP ... reason=pr_dependency_held\|pr_dependency_released` |

What this means for operators: to park follow-up work behind an open PR, put both lines in the issue body. The four feature tracks planned after #7186 (capacity tiers, MEMORY_LEARNINGS, SOFT_ERROR_ANALYZER, JUDGE_INTERIM) are not filed by this change; file them with `- Depends on PR: #6667` and `- Requires on main: scripts/ai_engine.sh contains _AI_ENGINE_EXIT_REFUSED`.
