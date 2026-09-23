<!-- changelog: security -->
- **Harden model-writer publication and resource isolation.** Conflict and review-blocked writers now publish only current-head, span-authorized edits; causal waivers fail closed on truncated caller graphs, while provider connections and sandbox processes have deterministic worker, timeout, cgroup, I/O, and runtime ceilings.
