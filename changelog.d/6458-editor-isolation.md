<!-- changelog: fixed -->
- **Plan and implementation editors can no longer survive until Git credentials return.** Model processes now run in a network-isolated, tokenless container against a checked source snapshot. The host reaps and verifies containers before transferring edits or restoring credentials, including after syntax-repair attempts.
