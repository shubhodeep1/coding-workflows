<!-- changelog: fixed -->
- **A heal issue the unblock judge re-issued now keeps its lineage, and a heal issue whose failure was already fixed elsewhere closes itself.** #7082 sat waiting for a person after two unblock rounds, although the same failure on drhyg_ecommerce_automation#66 had already been fixed through the re-issue #7059 (PR #7065).

The workflow failure heal intake now records a new report on the open re-issue of a closed heal issue in the same lineage, instead of filing another generation (`HEAL_REISSUE_DEDUPE_ENABLED`, on by default). Before judging a blocked `ai:workflow-heal` issue, the unblock judge looks for a sibling with the same source and failure reason that closed with a merged `ai/issue-<N>` pull request. If it finds one, it closes the issue as completed with a link, without using a round (`UNBLOCK_HEAL_ALREADY_FIXED_ENABLED`, on by default). The hourly heal-scope recovery scan can now rebuild a heal issue whose body names no failed run, using the run from the pipeline's own newest matching occurrence comment. Before, every such issue ended in `runs_missing`.

| The numbers that matter | Value |
| --- | --- |
| Extra reads per intake report with a closed lineage issue | 1 search per repository (at most 2) |
| Extra reads per blocked heal issue the judge sees | heal issue list, 1 search, at most 3 pull-request reads |
| Re-issue chain followed | up to 5 steps |

What this means for operators: a heal failure that the pipeline already fixed through a re-issue no longer reaches the needs-human digest. Set either variable to `false` to restore the old behaviour.
