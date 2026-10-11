<!-- changelog: security -->
- **A re-issue or fix-up the pipeline files from an untrusted author's issue no longer gets to edit `.github/`, `scripts/` or other automation paths.** The automation-path guard now checks where a pipeline-authored issue came from before granting anything.

An issue from an outside contributor only enters the pipeline after a collaborator posts `/reclarify`, and its own implementation run is still refused automation paths. Until now, the unblock judge (`operator_step`, `descope`, `reissue`) or the review-blocked judge (`close_and_reissue`) could file a new issue as the pipeline account from that request. The new issue then received the open or exact automation-path grant pipeline-authored issues get. The guard now follows the issue's `Re-issued from #N`, unblock fix-up and review-blocked `Replaces: #N` references. It grants automation paths only when every source in the chain was written by a trusted person or by the pipeline from trusted sources.

| The numbers that matter | Value |
| --- | --- |
| Source issues read per grant | at most 8, across at most 4 hops |
| Extra API calls for an ordinary issue | 0 |
| New rejection reasons | `untrusted_origin`, `origin_unverified` |

What this means for operators: re-issues and fix-ups of trusted work behave as before. A derived issue whose source cannot be read, or that traces back to an untrusted author, stops at `ai:scope-blocked` with one of the new reasons. A trusted maintainer can then decide what to do with the requested change.
