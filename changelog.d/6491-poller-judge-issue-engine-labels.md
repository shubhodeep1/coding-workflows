<!-- changelog: fixed -->
- Poller stall and review-blocked judges now honor `ai:codex` on the judged issue. Standalone stalls use only verified issue labels; managed judges combine issue and tracking labels, falling back to codex when the issue's label snapshot is unavailable.
