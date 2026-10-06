<!-- changelog: fixed -->
- **Workflow log analysis can read full run logs inside its isolated container.** The analyzer mounts the downloaded log artifact read-only, with an errors-first bounded subset and omission list when it exceeds 200 MiB. If staging fails or the artifact is unavailable, analysis continues using its summary context.
