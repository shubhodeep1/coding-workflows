<!-- changelog: fixed -->
- **Review editor transfer failures now retain a sanitized reason in workflow failure evidence.** Rejected transfers still stop the editor instead of falling back after an incomplete result.

When the isolated review editor cannot transfer its result, the workflow reports a fixed, path-free reason code, or `unknown` when no approved reason is available. The reason is also preserved in the editor attempt's stderr artifact for diagnosing repeated `Internal: AI Review & Autofix` failures. Transfer validation and the fail-closed exit remain unchanged; the new evidence does not imply that a rejected result was safe to accept.

What this means for operators: a failed review run can identify which transfer check rejected the result without exposing raw sandbox paths or untrusted error text.
