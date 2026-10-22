<!-- changelog: fixed -->
- **Blocked items now page a person instead of waiting silently.** An implement run that stops with a deliberate `BLOCKED:` verdict sends a WARNING Telegram, and the unblock judge parks an item for a person after two refused answers in a row.

Before this, tele-funtoken-msg-scoring#4770 was blocked with "npm registry unavailable for npm ci" and nobody was told. The `implement.yml` alert for the block went out at DEBUG, which the default `ALERT_MSG_LEVEL=WARNING` drops. The unblock judge's answer was then refused because its instructions quoted the issue's `<!-- changelog: fixed -->` line, and a refused answer is not a round, so none of the conditions that page a person could ever hold; the item would have been retried every 6 hours indefinitely. Now the blocked implement run sends `Implementation blocked on issue #<n>: human input required` with the reason and run link at WARNING, the judge strips `<!--` and `-->` from its answer text instead of refusing the answer, and a second refused answer in a row on the same block parks the item in the needs-human digest with one CRITICAL page.

| The numbers that matter | Value |
| --- | --- |
| Implement `BLOCKED:` alert level | DEBUG → WARNING |
| Refused judge answers in a row before parking (`INVALID_VERDICT_PARK_AFTER`) | 2 |
| Judge retry interval for a refused answer (`--marker-hours`, unchanged) | 6 hours |

What this means for operators: a blocked item reaches Telegram when it blocks, and an item the judge cannot answer reaches the needs-human digest within one retry instead of never. Other implement failures keep their DEBUG message.

### For contributors

Refused answers now post `<!-- ai:unblock-wait:v1 item=<n> reason=invalid_verdict stop=<id> fingerprint=<12 hex> -->`. The older marker without `stop=` and `fingerprint=` is ignored by the streak count, so items already carrying one start counting from their next refusal. With `NEEDS_HUMAN_DIGEST_ENABLED=false` the terminal close closes the item, as for the other terminal reasons.
