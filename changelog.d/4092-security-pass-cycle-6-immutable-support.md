<!-- changelog: security -->
- **Issue-status, Semble, and review bootstrap code now stays bound to reviewed immutable commits.** The PR-close workflow verifies its reusable-workflow SHA before staging memory and Telegram helpers, while setup-uv and free-disk-space actions use full commit pins.
