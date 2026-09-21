<!-- changelog: fixed -->
- **The release step that tags a version now retries each tag push up to five times, so a transient GitHub rejection no longer fails a fully green release gate.**

`Test & Mark Stable Release` run 35570966035 (stable, v1.29.7) passed every gate job and then lost the release in "Tag version and update stable pointer": GitHub rejected the first push of `refs/tags/v1.29.7` with "Unable to determine if workflow can be created or updated due to timeout; `workflows` scope may be required.", the step ran each push once, and the job failed without cutting the tag. Because `scripts/auto_release_stable.sh` treats a failed gate on the current tip as `last_gate_failed`, auto-release then stopped re-dispatching that tip until a human re-ran the gate. Both `.github/workflows/test-and-mark-stable.yml` and `.github/workflows/mark-stable.yml` now publish the version, `stable`, and major-version tags through the bounded `publish_tag_with_remote_verification` helper. After a failed push, the helper verifies the exact remote tag and accepts the publication only when its object ID matches the local tag; genuine rejections still fail after the last attempt.

| The numbers that matter | Value |
| --- | --- |
| Attempts per tag push | 5 |
| Backoff between attempts | 2s, 4s, 8s, 16s |
| Tag pushes covered per workflow | 3 (`refs/tags/<version>`, `refs/tags/stable`, `refs/tags/<major>`) |
| Failing run | 35570966035 (v1.29.7) |

What this means for operators: a release gate that turns green no longer depends on a single push succeeding on the first try, and the auto-release tick is not left parked on a tip whose only failure was a server-side hiccup. Manual releases via `scripts/mark-stable.sh` are unchanged.

### For contributors

`tests/test_mark_stable_release_tag_refspec_contract.py` pins `publish_tag_with_remote_verification`, its five-attempt bound, remote-object verification, and the fully qualified `refs/tags/` publication calls in both workflows.
