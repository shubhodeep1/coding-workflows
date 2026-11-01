<!-- changelog: added -->
- **Heals approved with `[repro-required]` now open a PR only when their tests really fail at the failing commit and pass after the fix.** The heal editor runs the check behind `WORKFLOW_HEAL_REPRODUCE_ENABLED` (off by default); with the switch on, the heal pre-push gate also gets a dependency venv, so pytest files are checked instead of coming back `unverified`.

When the approving comment carries `[repro-required]`, the heal editor reads the test ids from the approved plan's `HEAL_COUNTERFACTUAL: REPRODUCIBLE:` line and the failing commit from the heal issue's `Head SHA` line. It runs `scripts/heal_reproduce.sh` on the edited tree and, when every test passes there, once more at the failing commit. Any test that still fails, passes at the failing commit or cannot be checked holds the heal: nothing reaches the checkout, the issue gets a comment with the per-test result and the `ai:needs-human` label. A `[repro-required]` approval with the switch off is held too, never skipped.

| The numbers that matter | Value |
| --- | --- |
| Default | off (`WORKFLOW_HEAL_REPRODUCE_ENABLED=false`) |
| New exit code | 43, mapped to the refusal reason `repro_not_fixed` |
| Reproduction runs | at most 2, each capped at half the remaining gate budget (max 1 hour) |
| Extra GitHub API calls | 0 |
| Log line | `HEAL_ISOLATED_EDITOR phase=repro_check outcome=<fixed|not_fixed|unverified> reason=<reason> tests=<n>` |

What this means for operators: nothing changes until the switch is on and a plan asks for reproduction. Test code only ever runs in the network-less, credential-free gate container.
