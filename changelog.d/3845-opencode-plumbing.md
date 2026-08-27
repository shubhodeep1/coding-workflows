<!-- changelog: added -->
- **OpenCode now has an inert, pinned Phase 1 foundation and a dispatchable all-model rollout gate.** A role-scoped configuration writer, shared command/output/bootstrap helpers, and the exact `opencode-ai@1.18.23` install action support a live smoke across all reviewer and editor slots without changing any production runtime path.

After this change reaches `main`, operators must dispatch `opencode-live-smoke.yml` without a model filter and record the all-green run URL on tracking issue `#3845` before the read-side or write-side cutover begins. Existing production workflows continue to use the independently pinned `CODEX_VERSION`; no current review/autofix invocation is routed through OpenCode by this phase.
