<!-- changelog: fixed -->
- **Isolate orchestrator decomposition and authenticate recovery.** Run the decomposer without Git credentials or general network access; sign new state with a separate secret and require a frozen, signed project descriptor before reconstructing missing state.

Legacy signed state remains readable, but a missing signing secret or incomplete descriptor stops unattended writes rather than trusting an editable issue body.
