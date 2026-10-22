<!-- changelog: changed -->
- **Scheduled close cleanup now lists the active runs it keeps because they have no pull-request link.** Before, `cancel_on_pr_close.yml` logged only how many such runs it preserved, and finding them took a separate API search.

The existing warning line is unchanged. After it, the step prints up to 10 plain log lines, one per unlinked run, with the run id, workflow path (without any `@<ref>` suffix), event, status, head branch and creation time, plus one line with the number not shown when there are more. The lines come from the `actions/runs` snapshot the step already reads, so no API call is added. Run titles are never printed; every value keeps only `[A-Za-z0-9._/@:+-]`, has `::` broken up so it cannot form a workflow command, and is capped at 200 characters. If the listing fails, the step warns and cleanup carries on unchanged.

What this means for operators: the run ids of preserved unlinked runs are in the cleanup log, ready to inspect or cancel by hand.
