<!-- changelog: fixed -->
- **Orchestrator poller judges no longer run on the credentialed host.** Wave, stall, integration and security-pass judges use read-only, credential-free review sandboxes on both engines. Isolation failures defer and escalate instead of invoking a host agent; integration conflicts are diagnosed by a judge and re-dispatched to the existing resolver.
