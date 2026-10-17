<!-- changelog: changed -->
- **Implementation agents can now read the default branch from inside their sandbox.** The isolated git they work in used to hold one synthetic commit with no remote and no network. An agent fixing an integration branch so it stays compatible with production could not see what production runs, and fun-token-multi-chain#639 stopped as BLOCKED for that reason.

The implement step "Run Codex implementation" now fetches the default branch's tip into a private ref (`refs/codex-isolated/base`, depth 1). `scripts/codex_isolated_workspace.py seed-git` adds it to the sandbox git as branch `isolated-base`, filtered exactly like the checkout: credential-like and oversized files are left out and their objects are never packed. It also becomes the parent of the `isolated` HEAD commit, so `git diff isolated-base` shows the branch's changes, and `prompts/mode-implement.txt` tells the agent to use `git show isolated-base:<path>`. If the fetch fails, the run logs a warning and continues without that branch.

What this means for operators: nothing to set. A project issue that blocks on "no pre-project commit exists" can be re-run (`/approved`) once this reaches `@stable`.

### For contributors

`CODEX_ISOLATED_GIT_BASE_REF` (default empty) is the new input to `scripts/codex_isolated_exec.sh`; only the implement step sets it. Tests are in `tests/test_codex_isolated_workspace.py`.
