<!-- changelog: added -->
- **Orchestrator state readers accept zlib-compressed state chains, and a new `ORCH_STATE_COMPRESS` variable lets the writer compress them.** The writer stays off by default in this release.

A large project's state is split across many tracking-issue comments: tracking issue #8291 writes a 472 KB state as 12 `ORCHESTRATOR_STATE_V2` parts, 442 KB of it the regex lists in `merged_issue_fingerprints`. `scripts/orchestrate_state_v2.py` can now compress the state before base64, which packs the same state into 2 parts. `extract` and `prune-plan` read compressed and uncompressed chains, with an 8 MiB limit on inflated output. The comment header, closer and manifest hash are unchanged.

| The numbers that matter | Value |
| --- | --- |
| #8291 state, uncompressed | 472 KB, 12 comment parts per write |
| #8291 state, compressed | 84 KB zlib, 112 KB base64, 2 comment parts per write |
| New repository variable | `ORCH_STATE_COMPRESS`, default `false` |
| Inflated-output limit | 8 MiB |

What this means for operators: nothing changes until `ORCH_STATE_COMPRESS` is `true`. Do not set it before every consumer repository has synced a `stable` release with this reader, because an older reader that meets a compressed chain finds no valid state and rebuilds the tracking state. A later release turns it on by default once that has happened.
