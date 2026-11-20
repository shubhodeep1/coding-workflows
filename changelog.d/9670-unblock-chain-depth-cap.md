<!-- changelog: changed -->
- **The unblock judge stops splitting an item that is already two re-issues or fix-ups away from the original, and hands it to a person instead.**

`Re-issue of`, `Follow-up to`, `Unblock descope for` and `Unblock operator step for` issues each restart clarify, plan and implement, and such an issue, or its PR, could block and be split again without limit: #9190 led to #9261, its PR #9283, then #9398 and #9670, and 24 issues of these kinds were opened in one week. The judge (`scripts/unblock_judge.sh`, `scripts/unblock_ledger.py`) now counts how many of those prefixes an item's title starts with. At two, the verdicts `reissue`, `accept_with_followup`, `descope` and `operator_step` are no longer offered: the judge can only keep working on the item itself (`retry_budget`, `auto_answer`, `override_guard`, `wait`), and when nothing is left the item gets `ai:needs-human` and an entry in the "Items waiting for a person" digest, as any exhausted item does. For a PR on an `ai/issue-<n>` branch the judge reads the title of issue `<n>`, and an issue filed from that PR carries that title, so the count is not lost when the chain passes through a PR.

| The numbers that matter | Value |
| --- | --- |
| Chain depth at which no further issue is opened | 2 (`MAX_CHAIN_DEPTH` in `scripts/unblock_ledger.py`) |
| Extra API calls | 1 issue read per judged trusted PR on an `ai/issue-<n>` branch |
| Alert when the item is finally parked | CRITICAL, once (the existing needs-human park, reason `menu_exhausted`) |

What this means for operators: a block that two rounds of re-issues or fix-ups did not clear now reaches you as one `ai:needs-human` item instead of more issues. Consumer repositories get this on their next `@stable` sync. Issues filed from a PR whose issue is itself a re-issue or fix-up get a longer title that keeps the earlier prefixes.

### For contributors

The depth comes from the title only, so it works for issues that already exist and needs no new marker. Titles are editable, which makes the cap a brake on the pipeline's own chains and not a trust boundary. The cap is a Python constant next to the other judge limits, not a repository variable.
