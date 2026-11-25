# Deploy-Activation Log — CI re-issue base-branch routing + opt-in sandbox solc

- Reference: #9169   (+ PR #9178, merge commit b9d9db22ac9d58226d53555e7ffe8d7347363377; plan docs/plans/ci-reissue-branch-routing-and-sandbox-solc-plan.md)
- Deploy target: shubhodeep1/coding-workflows (consumer variable on shubhodeep1/fun-token-multi-chain)
- How it runs: implement workflow (`.github/workflows/implement.yml`) in consumers via the `@stable` sync; solc is installed only when `vars.SANDBOX_SOLC_VERSIONS` is non-empty
- Status: IN_PROGRESS
- Last updated: 2026-11-25
- Last note: Step 1 sent to the owner. The change is not in @stable yet: v1.47.6 (2026-11-20) does not contain b9d9db2, and test-and-mark-stable runs have failed from 2026-11-22 to 2026-11-25.
- Human-steps issue: https://github.com/shubhodeep1/coding-workflows/issues/9647 (closed tracker) — keys project-9169

## Runbook
1. [ ] Set repo variable SANDBOX_SOLC_VERSIONS=0.8.34 on shubhodeep1/fun-token-multi-chain (read at .github/workflows/implement.yml:62, default empty) — covers project-9169 «Set SANDBOX_SOLC_VERSIONS on the Solidity consumer to switch on the sandbox compiler»
2. [ ] Wait for a @stable release that contains b9d9db2 (latest is v1.47.6, which does not)
3. [ ] Confirm shubhodeep1/fun-token-multi-chain synced that release (update_workflows)
4. [ ] Verify LIVE: the next implement run in fun-token-multi-chain logs `CODEX_ISOLATION solc_versions count=1` and `CODEX_ISOLATION solc_install version=0.8.34 outcome=ok`

## Notes
- The human-steps entry says to expect `CODEX_ISOLATION solc version=0.8.34 outcome=ready`. The code prints `CODEX_ISOLATION solc_install version=<v> outcome=ok` (scripts/codex_isolated_exec.sh:300, :411), so step 4 checks for the code's line.
- Setting the variable before the release is harmless: the implement.yml in v1.47.6 does not read it.
- The variable could not be set from the session: the cloud proxy refuses repository-variable endpoints, and the attached Mac's Claude Code sandbox blocks api.github.com. The owner runs step 1 in their own terminal.
