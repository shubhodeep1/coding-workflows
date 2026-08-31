<!-- changelog: added -->
- **Orchestrator projects can opt into a mandatory, SHA-bound security pass before validation or final merge.**

When `ENABLE_SECURITY_PASS=true`, the scheduled poller audits the composed integration head, creates one normal-pipeline fix issue for surviving findings, re-audits after merged fixes, and fails closed when the engine is unavailable. The dark launch defaults off; `MAX_SECURITY_PASS_CYCLES=3`, `/re-security-pass`, visible status updates, and CRITICAL alerts bound and expose recovery.

### For contributors

The poller reuses `scripts/security_audit.sh` findings-JSON mode under `codex_heartbeat.sh`. Pass validity is tied to the exact integration SHA, and all completion routes share the same gate.
