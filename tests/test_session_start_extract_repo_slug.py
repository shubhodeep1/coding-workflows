#!/usr/bin/env python3
"""Contract: extract_repo_slug() in .claude/hooks/session-start.sh produces the
right <owner>/<repo> for every URL shape the hook is expected to handle.

The slug is the only thing deciding whether the SessionStart hook reports
actions:read availability accurately. A silent regression in this awk-based
extraction would re-introduce the misleading "token likely lacks actions:read"
message that PR #2012 was opened to fix.

Both the in-repo hook and the workflow-templates copy are exercised — they
must stay byte-identical because the consumer-sync step in
`.github/workflows/update_workflows.yml` mirrors the template into every
consumer repo.
"""

from __future__ import annotations

import subprocess
import sys
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

    if failures:
        for line in failures:
            print(f"FAIL: {line}", file=sys.stderr)
        return 1

    print(
        f"PASS: extract_repo_slug across {len(CASES)} URL shapes in "
        f"{HOOK.relative_to(REPO_ROOT)} and {TEMPLATE_HOOK.relative_to(REPO_ROOT)}; "
        f"hook and settings.json parity checks passed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
