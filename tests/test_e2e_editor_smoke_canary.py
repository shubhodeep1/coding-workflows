"""E2E smoke-test canary verification (run from test-and-mark-stable.yml Phase 4b).

Asserts that the canary file produced by the autofix editor on the
post-review PR head matches the issue body's exact 3-line target. Phase 4b
fetches the canary from the PR branch via the GitHub Contents API, writes
it to a tmp path, and runs this test with three env vars:

- E2E_CANARY_FILE        absolute path to the fetched canary content
- E2E_EXPECTED_RUN_ID    the GITHUB_RUN_ID of the test-and-mark-stable run
                         (same value the issue body templated into the spec)
- E2E_PHASE_4B_INVOKED=1 sentinel that this is the production invocation;
                         missing canary vars must fail loudly here, not skip

When E2E_PHASE_4B_INVOKED is not set and either of the canary vars is
missing, every test skips so the file stays runnable from a normal
`pytest tests/` sweep without polluting CI signal.

The expected canary spec is read from the sibling `e2e_smoke_canary_spec.txt`
template file (with `__RUN_ID__` substituted) so it stays in sync with
whatever the create-issue step in test-and-mark-stable.yml renders into
the issue body — single source of truth lives in the spec file.
"""

from __future__ import annotations

import os

import pytest


SPEC_PATH = os.path.join(os.path.dirname(__file__), "e2e_smoke_canary_spec.txt")
RUN_ID_PLACEHOLDER = b"__RUN_ID__"


def _expected_content(run_id: str) -> bytes:
    """Read the spec template + substitute __RUN_ID__ at byte level.

    Reading in binary mode and substituting bytes (rather than text mode +
    encoding) preserves the trailing newline and any other byte-level
    properties of the template, matching the byte-for-byte contract
    Phase 4b enforces.
    """
    with open(SPEC_PATH, "rb") as fh:
        template = fh.read()
    if RUN_ID_PLACEHOLDER not in template:
        raise AssertionError(
            f"Spec template at {SPEC_PATH!r} is missing the {RUN_ID_PLACEHOLDER!r} "
            "placeholder — workflow harness change broke the test contract"
        )
    return template.replace(RUN_ID_PLACEHOLDER, run_id.encode("ascii"))


@pytest.fixture(scope="module")
def canary_invocation() -> tuple[str, str, bytes]:
    """Resolve canary path + expected run id, then read the file as bytes.

    Reading the file in the fixture (not in individual tests) makes
    file-existence failures deterministic regardless of pytest's test-
    collection / execution order. Each test gets the (path, run_id,
    content_bytes) tuple and works on the already-loaded content.

    Read mode is binary ("rb") so universal-newlines translation
    (CRLF→LF on text-mode read) does not mask line-ending drift in
    the byte-for-byte assertion downstream.
    """
    canary_file = os.environ.get("E2E_CANARY_FILE")
    expected_run_id = os.environ.get("E2E_EXPECTED_RUN_ID")
    invoked_from_phase_4b = os.environ.get("E2E_PHASE_4B_INVOKED") == "1"
    if invoked_from_phase_4b:
        # Production invocation: missing config is a workflow bug, not
        # a reason to skip silently. Fail loudly — Phase 4b's
        # classify_pytest_failure maps any non-FAILED pytest exit to
        # status=harness_misconfigured (operators look at the workflow
        # YAML / runner setup, not the editor).
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
    assert canary_file and expected_run_id  # narrowed by the branches above
    if not os.path.isfile(canary_file):
        raise AssertionError(
            f"canary file not found at {canary_file!r} — "
            "Phase 4b should have written the fetched contents here "
            "before invoking pytest"
        )
    with open(canary_file, "rb") as fh:
        content = fh.read()
    return canary_file, expected_run_id, content


def test_canary_does_not_contain_bait_marker(
    canary_invocation: tuple[str, str, bytes],
) -> None:
    _, _, content = canary_invocation
    # Phase 3c always injects a marker line of the form
    # "# E2E_EDITOR_BAIT_<run_id>: ...". The editor MUST strip this
    # marker; if it survives, the canary is still corrupted.
    assert b"E2E_EDITOR_BAIT_" not in content, (
        "canary still contains the bait marker — editor failed to remove it"
    )


def test_canary_matches_issue_spec_byte_for_byte(
    canary_invocation: tuple[str, str, bytes],
) -> None:
    _, expected_run_id, actual = canary_invocation
    expected = _expected_content(expected_run_id)
    assert actual == expected, (
        "canary content does not match the issue's required 3-line spec.\n"
        f"expected: {expected!r}\n"
        f"actual:   {actual!r}"
    )
