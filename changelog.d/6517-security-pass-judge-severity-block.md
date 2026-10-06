<!-- changelog: security -->
- **The project security-pass exhaustion judge no longer automatically waives high, critical or unrated findings.** Attempts to accept them open another fix cycle before the keep-fixing cap; after the cap they fail the project for an explicit operator decision. Medium/low advisories still converge as before. Previously recorded waivers are not revisited.
