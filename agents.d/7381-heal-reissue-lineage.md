<!-- agents: section="Workflow architecture" -->
Heal lineage across unblock-judge re-issues (Q147, Q149, from #7082): the
unblock judge files the re-issue of a blocked heal issue as an ordinary issue
without heal markers, so the intake's `ai:workflow-heal` list stopped seeing
the lineage once the original closed, and a later report for the same failure
opened a new generation next to the open re-issue (#7082 next to #7059).
With `HEAL_REISSUE_DEDUPE_ENABLED` on (default), when the budget finds a
closed heal issue in the report's lineage (same fingerprint, same `source=`,
or the linked heal issue) and no open duplicate, `_heal_reissue_lookup` runs
one `search/issues` read per repository that can own the heal issue (self,
plus the source repository when it differs) and re-runs `budget` with
`--reissues-json`. `workflow_failure_heal.py reissue_origin` reads the
`Re-issued by the unblock judge from #N.` line only as the body's first line
that is not an HTML comment; `resolve_reissue_roots` follows a re-issue of a
re-issue back up to five steps in the same repository and requires every issue
on the chain to be written by the heal issue's author. An open re-issue of a
closed lineage issue becomes the duplicate (`match: reissue`, `reissue_of`,
the heal issue's `gen` / `root`), so the report is an occurrence comment on
the re-issue; heal-scope recovery skips that match. A reconstructed recovery
replay never runs the lookup, and a failed search keeps the first decision.
Log: `WORKFLOW_HEAL reissue_lookup outcome=found|none|skip reason=search_failed|budget_failed|invalid_switch`.

The recovery scan's `reconstruct` (Q149) reads the failed run from the issue
body's `Failed run` lines and, when the body has none (a heal issue composed
without run summaries, such as #7082), from the newest occurrence comment
posted by the pipeline login whose first line is the occurrence marker and
whose `Source pull request` and `Head SHA` lines match the body. The intake
still verifies that run's provenance and the fingerprint before anything is
replaced; `runs_missing` stays a permanent skip when neither has a run.

<!-- agents: section="Integration-sync verifier + bootstrap contract" -->
- **Unblock judge closes heal issues a sibling already fixed (Q148).** After the check-triage gate and before checking a hold or asking the model, the judge checks an open `ai:workflow-heal` issue with no tracking issue (`UNBLOCK_HEAL_ALREADY_FIXED_ENABLED`, default on): it lists the repository's heal issues (paginated), runs one `search/issues` read for unblock-judge re-issues, and `workflow_failure_heal.py heal-already-fixed` returns up to three siblings, newest first. A sibling is written by the pipeline login, has the item's `source=` marker and `Failure reason` line (or its fingerprint for kinds without one), directly or through its re-issue chain, and is closed as completed with `ai:merged`; the item itself must be written by the pipeline login. For each candidate one closed-pull-request read looks for a merged `ai/issue-<N>` pull request; the first found gets a comment ending in `<!-- ai:unblock-heal-already-fixed:v1 item=<n> sibling=<N> pr=<P> -->` and the item is closed as completed, with no round used. A failed comment or close stops the run so the next scan retries; every read failure judges the item as before. Log: `UNBLOCK_JUDGE item= op=heal_already_fixed outcome=closed|skip|judge sibling= pr= status=|reason=comment_failed|close_failed`.
