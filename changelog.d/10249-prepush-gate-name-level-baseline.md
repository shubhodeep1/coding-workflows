<!-- changelog: fixed -->
- **The pre-push test gate no longer lets new failures hide in a test file that already fails on the merge-base.**
  - A test file red on both trees is now compared test by test. It counts as inherited only when every test failing on the change also fails on the merge-base; tests that fail on the change only are reported as caused by it and sent to the repair turn. When the failing test names cannot be read, the file is reported as unverified, never as inherited.
  - Each inherited file now shows the first failure line of its merge-base run in the PR body summary, and the gate wrapper logs it as `PREPUSH_TEST_GATE phase=baseline`.
  - The gate also selects test files that import a selected test module (one level), and runs them last so a spent budget leaves them, not the direct tests, unverified.
  - The implement and review gate images now install `python3-yaml` and `python3-jsonschema`, which CI already installs; without them many contract tests failed to collect on both trees and were reported as inherited.

What this means for consumer repos: after the next `@stable` sync, a pull request that breaks more tests in an already failing file gets a repair turn instead of a `pass`, and the PR body says why each inherited file fails on the merge-base. No switch changes.

Refs #10225, Refs #10149, Refs #10249.
