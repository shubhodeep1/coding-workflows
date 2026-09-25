<!-- changelog: fixed -->
- **Review/autofix model processes now start reliably with private temporary namespaces.** Sandbox control state and forwarded output files live outside private `/tmp`, reviewer and summariser model configurations are validated before fan-out, and upstream review failures are no longer reported as empty editor no-ops.

The runtime keeps `PrivateTmp`, read-only root, provider-only networking, credential masking, cgroup limits, and process-group cleanup. Provider credentials are copied into a canonical runner-owned control directory and hidden from the model unit; model output files and validator/guard writable trees are mirrored through that directory and checked before being copied back. The model catalog is staged from the same complete main-primary snapshot as the OpenCode runtime helpers, including entries for `google/gemini-3.1-flash-lite` and `z-ai/glm-5.2`.

What this means for operators: a reviewer or summariser setup failure now stops before fan-out and appears as `review_pipeline_failure`. `editor_empty_noop` remains reserved for a real editor attempt that returned neither a summary nor changes.
