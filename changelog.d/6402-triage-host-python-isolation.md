<!-- changelog: security -->
- **Check-failure triage now isolates host Python imports from PR-head files.**

The diagnosis helper runs from trusted workflow support and reads the PR checkout only as snapshot data. Host Python uses isolated imports so PR-added modules cannot run with the model provider credential. Existing clarify callers keep their current snapshot root by default.
