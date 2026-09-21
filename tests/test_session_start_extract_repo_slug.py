#!/usr/bin/env python3
"""Contracts for the GitHub token probes and repository-slug extraction in
.claude/hooks/session-start.sh.

The slug is the only thing deciding whether the SessionStart hook reports
actions:read availability accurately. A silent regression in this URL parser
(currently bash parameter expansion + a `grep -E` character-set check) would
re-introduce the misleading "token likely lacks actions:read" message that
PR #2012 was opened to fix.

Both the in-repo hook and the workflow-templates copy are exercised — they
must stay byte-identical because the consumer-sync step in
`.github/workflows/update_workflows.yml` mirrors the template into every
consumer repo.

The verify_token cases pin the bounded REST identity probe and its no-timeout
fallback, confirmed proxy substitution, absence of an always-on proxy
credential, and inconclusive outcomes so none can be misreported as direct
session-token authentication.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / ".claude" / "hooks" / "session-start.sh"
TEMPLATE_HOOK = REPO_ROOT / "workflow-templates" / ".claude" / "hooks" / "session-start.sh"
SETTINGS = REPO_ROOT / ".claude" / "settings.json"
TEMPLATE_SETTINGS = REPO_ROOT / "workflow-templates" / ".claude" / "settings.json"

# (url, expected_slug). Empty expected = parser should produce no slug
# (caller treats that as "couldn't derive — skip the actions:read probe").
CASES: list[tuple[str, str]] = [
    # Claude Code Web local proxy form — the case PR #2012 was opened for.
    ("http://local_proxy@127.0.0.1:46539/git/shubhodeep1/coding-workflows", "shubhodeep1/coding-workflows"),
    ("http://127.0.0.1:46539/git/shubhodeep1/coding-workflows.git", "shubhodeep1/coding-workflows"),
    # github.com HTTPS, with and without `.git` and trailing slash.
    ("https://github.com/foo/bar.git", "foo/bar"),
    ("https://github.com/foo/bar", "foo/bar"),
    ("https://github.com/foo/bar/", "foo/bar"),
    ("https://github.com/foo/bar.git/", "foo/bar"),
    # Repo names containing dots must survive `.git`-stripping.
    ("https://github.com/foo/code.weave.git", "foo/code.weave"),
    ("https://github.com/foo/code.weave.git/", "foo/code.weave"),
    # SSH form.
    ("git@github.com:owner/repo.git", "owner/repo"),
    # HTTPS with embedded credentials (e.g. x-access-token).
    ("https://x-access-token:abc@github.com/o/r.git", "o/r"),
    # URLs with extra path segments after owner/repo must NOT yield a
    # slug. A greedy suffix regex would otherwise return the wrong
    # tail (e.g. "bar/pulls" or "tree/main") and send `gh -R` to a
    # bogus repo.
    ("https://github.com/foo/bar/pulls", ""),
    ("https://github.com/foo/bar/issues/123", ""),
    ("https://github.com/foo/bar/tree/main", ""),
    ("https://github.com/foo/bar/actions/runs/123", ""),
    ("https://github.com/foo/bar/pull/42", ""),
    ("https://github.com/foo/bar/blob/main/README.md", ""),
    # Proxy-shaped URLs on non-localhost hosts must NOT yield a slug —
    # only the Claude Code Web local proxy (127.0.0.1 / localhost) is
    # whitelisted. Otherwise an arbitrary forge with a `/git/owner/repo`
    # path would feed `gh -R` and probe the wrong github.com repo.
    ("https://gitlab.com/git/foo/bar", ""),
    ("http://attacker.com/git/foo/bar", ""),
    # Localhost proxy form (alternate to 127.0.0.1) should still work.
    ("http://localhost:46539/git/owner/repo", "owner/repo"),
    # Lookalike hostnames must NOT match the github.com / 127.0.0.1 /
    # localhost whitelist. The previous substring-glob implementation
    # (`*github.com/*`, `*localhost*/git/*`, `*127.0.0.1*/git/*`) would
    # happily yield a slug for any of these — the parser now splits the
    # URL into host/path and matches the host exactly.
    ("https://evilgithub.com/foo/bar", ""),
    ("https://github.com.attacker.com/foo/bar", ""),
    ("https://notgithub.com/foo/bar", ""),
    ("http://localhost.evil.com:8080/git/foo/bar", ""),
    ("http://127.0.0.1.evil.com/git/foo/bar", ""),
    ("http://attacker127.0.0.1/git/foo/bar", ""),
    ("http://my127.0.0.1host:8080/git/foo/bar", ""),
    # 127.0.0.1 / localhost without the `/git/` prefix is rejected too —
    # it could be any local service, not the Claude Code Web proxy.
    ("http://127.0.0.1:8080/foo/bar", ""),
    ("http://localhost:3000/foo/bar", ""),
    # Edge cases — should produce empty output.
    ("https://github.com/", ""),
    ("https://github.com/foo", ""),  # one path segment — must NOT capture host as owner
    ("", ""),
    # Non-GitHub remotes must not yield a slug, otherwise `gh -R` would
    # probe the wrong github.com repo and revive the misleading
    # actions:read NOTE this PR was opened to eliminate.
    ("https://gitlab.com/foo/bar", ""),
    ("https://gitlab.com/foo/bar.git", ""),
    ("git@bitbucket.org:foo/bar.git", ""),
    ("https://example.com/some/repo", ""),
    ("ssh://git@codeberg.org/owner/repo.git", ""),
]


def extract(hook_path: Path, url: str) -> str:
    """Source the hook and call `extract_repo_slug` on `url`."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{hook_path}"; extract_repo_slug "$1"',
            "_",
            url,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def run_verify_token_probe(
    hook_path: Path,
    *,
    gh_exit: int = 0,
    gh_login: str = "proxy-user",
    anon_status: str | None = "200",
    curl_exit: int = 0,
    timeout_available: bool = True,
    github_token_only: bool = False,
) -> str:
    """Run verify_token() with isolated gh/curl/timeout/git shims."""
    with tempfile.TemporaryDirectory() as temporary_directory:
        shim_directory = Path(temporary_directory)
        shim_bodies = {
            "gh": """#!/bin/bash
if [ "${SESSION_START_TEST_EXPECT_TIMEOUT_WRAPPER:-1}" = "1" ] && [ "${SESSION_START_TEST_TIMEOUT_WRAPPED:-}" != "1" ]; then
  exit 97
fi
if [ "${1:-}" = "api" ] && [ "${2:-}" = "user" ]; then
  [ "${SESSION_START_TEST_GH_EXIT:-0}" -eq 0 ] || exit "${SESSION_START_TEST_GH_EXIT}"
  printf '%s\\n' "${SESSION_START_TEST_GH_LOGIN:-}"
  exit 0
fi
exit 1
""",
            "git": """#!/bin/bash
exit 1
""",
        }
        if timeout_available:
            shim_bodies["timeout"] = """#!/bin/bash
[ "${1:-}" = "15" ] || exit 96
shift
SESSION_START_TEST_TIMEOUT_WRAPPED=1 exec "$@"
"""
        if anon_status is not None:
            shim_bodies["curl"] = """#!/bin/bash
printf '%s' "${SESSION_START_TEST_ANON_STATUS:-000}"
[ "${SESSION_START_TEST_CURL_EXIT:-0}" -eq 0 ] || exit "${SESSION_START_TEST_CURL_EXIT}"
"""

        for shim_name, shim_body in shim_bodies.items():
            shim_path = shim_directory / shim_name
            shim_path.write_text(shim_body)
            shim_path.chmod(0o755)

        probe_environment = {
            "PATH": str(shim_directory),
            "GH_TOKEN": "" if github_token_only else "test-token",
            "GITHUB_TOKEN": "test-token" if github_token_only else "",
            "SESSION_START_TEST_GH_EXIT": str(gh_exit),
            "SESSION_START_TEST_GH_LOGIN": gh_login,
            "SESSION_START_TEST_ANON_STATUS": anon_status or "",
            "SESSION_START_TEST_CURL_EXIT": str(curl_exit),
            "SESSION_START_TEST_EXPECT_TIMEOUT_WRAPPER": "1" if timeout_available else "0",
        }
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                f'source "{hook_path}"; verify_token',
            ],
            capture_output=True,
            text=True,
            check=True,
            env=probe_environment,
        )
        return result.stdout


def main() -> int:
    failures: list[str] = []

    # Both hooks must be byte-identical so consumer auto-sync produces the
    # same probe behaviour everywhere.
    if HOOK.read_bytes() != TEMPLATE_HOOK.read_bytes():
        failures.append(
            f"{TEMPLATE_HOOK.relative_to(REPO_ROOT)} must be byte-identical to "
            f"{HOOK.relative_to(REPO_ROOT)}; consumer auto-sync would otherwise "
            f"propagate stale behaviour."
        )

    # settings.json must also be byte-identical — it registers the SessionStart
    # hook in consumer repos and is mirrored by the same claude_sync step.
    if SETTINGS.read_bytes() != TEMPLATE_SETTINGS.read_bytes():
        failures.append(
            f"{TEMPLATE_SETTINGS.relative_to(REPO_ROOT)} must be byte-identical to "
            f"{SETTINGS.relative_to(REPO_ROOT)}; consumer auto-sync would otherwise "
            f"propagate stale SessionStart registration."
        )

    for hook in (HOOK, TEMPLATE_HOOK):
        for url, expected in CASES:
            got = extract(hook, url)
            if got != expected:
                failures.append(
                    f"{hook.relative_to(REPO_ROOT)}: extract_repo_slug({url!r}) "
                    f"returned {got!r}, expected {expected!r}"
                )

    probe_cases = [
        (
            "identity-failure",
            {"gh_exit": 1},
            ("REST identity probe 'gh api user' failed", "does not prove the configured token is invalid"),
            ("via GH_TOKEN",),
        ),
        (
            "proxy-substitution",
            {"anon_status": "200", "github_token_only": True},
            (
                "the agent proxy authenticates api.github.com calls itself",
                "does NOT forward the configured session credential (GH_TOKEN or GITHUB_TOKEN)",
            ),
            ("probe was inconclusive", "via GH_TOKEN"),
        ),
        (
            "anonymous-401",
            {"anon_status": "401"},
            (
                "no always-on proxy credential was detected",
                "does not prove the configured session credential (GH_TOKEN or GITHUB_TOKEN) was forwarded",
            ),
            ("via GH_TOKEN",),
        ),
        (
            "timeout-unavailable",
            {"timeout_available": False},
            ("the agent proxy authenticates api.github.com calls itself",),
            ("REST identity probe 'gh api user' failed",),
        ),
        (
            "probe-timeout",
            {"anon_status": "000", "curl_exit": 28},
            (
                "proxy-substitution probe was inconclusive (result: 000)",
                "configured session credential (GH_TOKEN or GITHUB_TOKEN)",
            ),
            ("via GH_TOKEN",),
        ),
        (
            "curl-unavailable",
            {"anon_status": None},
            (
                "proxy-substitution probe was inconclusive (result: unavailable)",
                "configured session credential (GH_TOKEN or GITHUB_TOKEN)",
            ),
            ("via GH_TOKEN",),
        ),
    ]
    for probe_case_name, probe_arguments, required_fragments, forbidden_fragments in probe_cases:
        probe_output = run_verify_token_probe(HOOK, **probe_arguments)
        for required_fragment in required_fragments:
            if required_fragment not in probe_output:
                failures.append(
                    f"verify_token {probe_case_name}: missing expected output {required_fragment!r}"
                )
        for forbidden_fragment in forbidden_fragments:
            if forbidden_fragment in probe_output:
                failures.append(
                    f"verify_token {probe_case_name}: unexpected output {forbidden_fragment!r}"
                )

    if failures:
        for line in failures:
            print(f"FAIL: {line}", file=sys.stderr)
        return 1

    print(
        f"PASS: extract_repo_slug across {len(CASES)} URL shapes in "
        f"{HOOK.relative_to(REPO_ROOT)} and {TEMPLATE_HOOK.relative_to(REPO_ROOT)}; "
        f"verify_token across {len(probe_cases)} auth outcomes; "
        f"hook and settings.json parity checks passed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
