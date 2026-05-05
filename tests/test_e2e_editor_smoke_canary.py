"""E2E smoke-test canary verification (run from test-and-mark-stable.yml Phase 4b).

Asserts that the canary file produced by the autofix editor on the
post-review PR head matches the issue body's exact 3-line target. Phase 4b
fetches the canary from the PR branch via the GitHub Contents API, writes
it to a tmp path, and runs this test with two env vars:

- E2E_CANARY_FILE       absolute path to the fetched canary content
- E2E_EXPECTED_RUN_ID   the GITHUB_RUN_ID of the test-and-mark-stable run
                        (same value the issue body templated into the spec)

Both vars are required when invoked from Phase 4b. When neither is set,
the tests skip so the file stays runnable from a normal `pytest` sweep
of the tests/ directory without polluting CI signal.
"""

from __future__ import annotations

import os

import pytest


def _expected_content(run_id: str) -> str:
    return f"status: ok\nrun_id: {run_id}\nupdated-by: ai-pipeline\n"


@pytest.fixture(scope="module")
def canary_invocation() -> tuple[str, str]:
    canary_file = os.environ.get("E2E_CANARY_FILE")
    expected_run_id = os.environ.get("E2E_EXPECTED_RUN_ID")
    invoked_from_phase_4b = os.environ.get("E2E_PHASE_4B_INVOKED") == "1"
    if invoked_from_phase_4b:
        # Production invocation: missing config is a workflow bug, not
        # a reason to skip silently. Fail loudly so the run shows
        # status=spec_mismatch in Phase 4b's exit handling.
        if not canary_file or not expected_run_id:
            raise RuntimeError(
                "E2E_PHASE_4B_INVOKED=1 but E2E_CANARY_FILE / "
                "E2E_EXPECTED_RUN_ID not set — Phase 4b harness misconfigured"
            )
    elif not canary_file or not expected_run_id:
        pytest.skip(
            "E2E_CANARY_FILE / E2E_EXPECTED_RUN_ID not set — "
            "test only runs from test-and-mark-stable Phase 4b"
        )
    return canary_file or "", expected_run_id or ""


def test_canary_file_exists(canary_invocation: tuple[str, str]) -> None:
    canary_file, _ = canary_invocation
    assert os.path.isfile(canary_file), (
        f"canary file not found at {canary_file!r} — "
        "Phase 4b should have written the fetched contents here"
    )


def test_canary_does_not_contain_bait_marker(
    canary_invocation: tuple[str, str],
) -> None:
    canary_file, _ = canary_invocation
    with open(canary_file, encoding="utf-8") as fh:
        content = fh.read()
    # Phase 3c always injects a marker line of the form
    # "# E2E_EDITOR_BAIT_<run_id>: ...". The editor MUST strip this
    # marker; if it survives, the canary is still corrupted.
    assert "E2E_EDITOR_BAIT_" not in content, (
        "canary still contains the bait marker — editor failed to remove it"
    )


def test_canary_matches_issue_spec_byte_for_byte(
    canary_invocation: tuple[str, str],
) -> None:
    canary_file, expected_run_id = canary_invocation
    with open(canary_file, encoding="utf-8") as fh:
        actual = fh.read()
    expected = _expected_content(expected_run_id)
    assert actual == expected, (
        "canary content does not match the issue's required 3-line spec.\n"
        f"expected: {expected!r}\n"
        f"actual:   {actual!r}"
    )
