<!-- changelog: fixed -->
- **Project validation runs again in the rootless sandbox.** Provisioning now gives the sandbox's Docker daemon a cgroup setup that can start containers, and proves it with a test container before validation begins.

Every validation on this repository stopped before any check ran: Docker refused to start the test container because `/sys/fs/cgroup/user.slice/user-1002.slice/cgroup.controllers` did not exist. Rootless dockerd uses systemd's cgroup driver on a systemd host, and the `ai-validation` user had no systemd user session. `scripts/validation_harness_sandbox.sh provision` now starts that session, or falls back to the `cgroupfs` driver, and fails closed with `container_start_failed` only when a container starts under neither. The test container comes from a local one-binary image, so it needs no registry pull.

| The numbers that matter | Value |
| --- | --- |
| Projects blocked on this today | #6902, #6664 |
| New setting | `VALIDATION_HARNESS_SANDBOX_CGROUP_MODE` (`auto` default, or `cgroupfs`) |
| User-session wait | 15 s |
| Exit code treated as a daemon refusal | `125` (126/127 are inconclusive) |

What this means for operators: re-run validation (`/revalidate`) on projects stuck with `ai:harness-broken` from this error. Under the cgroupfs fallback, container resource limits in a validation compose file are ignored.
