<!-- changelog: security -->
- **Check-failure triage no longer lets check names inject issue routing metadata.** Workflow and check-run names are flattened and neutralized before appearing in the triage issue title, body, prompt, and logs; raw names still identify duplicate failures.
