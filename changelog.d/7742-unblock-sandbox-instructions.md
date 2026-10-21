<!-- changelog: fixed -->
- **The unblock judge no longer sends the implementer instructions it cannot follow.** A verdict whose instructions need network access, a git remote or a package install is refused, and the judge is asked once more with the reason.

In fun-token-multi-chain#639, round 2 of the unblock judge chose `retry_budget` with instructions to fetch `origin/main`, compare against a merge base and reinstall packages. The implementer runs in a container with no network and no git remote, so the retry failed the same way and the item used up its rounds.

- `prompts/mode-judge-unblock.txt` now has an "Implementer environment" paragraph: no network, no remote, no `origin/*` refs, no GitHub token or `gh`, dependencies already installed, and the default branch readable as `git show isolated-base:<path>`.
- `scripts/unblock_ledger.py validate --sandbox-check` refuses the `instructions` of `retry_budget`, `descope`, `reissue`, `accept_with_followup` and `operator_step` verdicts that name a remote git command, a history deepen, an `origin/` ref, a package install, `curl` / `wget` or a `gh` command (`sandbox_incompatible:<id>`). `operator_instructions` and `answer` are not checked. Without the flag the output is unchanged.
- `scripts/unblock_judge.sh` passes the flag and, on the first such refusal, asks the model once more with the reason, reusing the run log and pull request diff it already read. It logs `UNBLOCK_JUDGE ... op=instruction_sandbox_check outcome=reasked|refused_after_reask reason=<id>`. A second refusal ends in the existing `invalid_verdict` wait marker, and the scan tries again later.

What this means for operators: the check is on by default. Set the repository variable `UNBLOCK_INSTRUCTION_SANDBOX_CHECK_ENABLED=false` to turn it off. The patterns are broad (`yarn.lock` and prose such as `origin/destination` match), so a verdict that only mentions such words is re-asked once; if both answers are refused, the item waits for the next scan.
