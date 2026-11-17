<!-- changelog: fixed -->
- **The review slop scan runs again on Python 3.14.** `scripts/slop_scan_local.py` used `ast.Str`, which Python 3.14 removed. Any expression statement such as `print(1)` raised `AttributeError`, and review_autofix recorded the `scan_error` sentinel, so the scan was skipped on every review after the runner image moved to 3.14. It now checks `ast.Constant` only, and a regression test hides the removed `ast` names to cover the 3.14 behaviour on every interpreter.

What this means for consumer repos: nothing changes in the synced workflows. After the next `@stable` sync, reviews get slop-scan findings again.
