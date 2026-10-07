<!-- changelog: security -->
- **The orchestrator no longer closes a project based solely on `ai:unblock-closed`.** It requires the unblock judge's latest trusted close verdict for a project still in `failed` state, with no later V1 or V2 state write superseding it; a spoofed label cannot hide a still-failed project from the unblock scan, and failed close requests can still be retried.
