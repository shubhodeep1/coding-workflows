#!/usr/bin/env python3
"""issue_pr_status.yml keeps a partially delivered issue open (issue #7657).

Runs the shipped "Update linked issue labels when PR closes" step body with a
fake `gh` and the real support scripts. A merged PR bound to its issue by the
#7160 rules whose body carries the partial-delivery marker must not close the
issue or finalize its lineage; it posts the remaining items, returns the issue
to ai:clarification and restarts clarify. Every other PR closes as before.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "issue_pr_status.yml"
STEP_NAME = "Update linked issue labels when PR closes"
SUPPORT_SCRIPTS = ("gh_helpers.sh", "label_helpers.sh", "partial_delivery.py", "orchestrate_lib.py")
HEAD_SHA = "a" * 40
ISSUE = 4639
PR = 4646

sys.path.insert(0, str(REPO_ROOT / "scripts"))

FAKE_GH = r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["GH_LOG"], "a", encoding="utf-8") as log:
    entry = {"args": args}
    for i, arg in enumerate(args):
        if arg in ("-F", "-f") and i + 1 < len(args) and args[i + 1].startswith("body="):
            value = args[i + 1][len("body="):]
            if value.startswith("@"):
                value = open(value[1:], encoding="utf-8").read()
            entry["body"] = value
        # set_issue_phase_label_resilient sends the label set on stdin.
        if arg == "--input" and i + 1 < len(args) and args[i + 1] == "-":
            entry["stdin"] = sys.stdin.read()
    log.write(json.dumps(entry) + "\n")

joined = " ".join(args)
if args[:2] == ["api", "user"]:
    if os.environ.get("FAKE_USER_FAIL") == "1":
        sys.exit(1)
    print(os.environ.get("FAKE_LOGIN", "pipeline-bot"))
elif args[:2] == ["api", "graphql"]:
    if "closingIssuesReferences" in joined:
        print(json.dumps({"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": []}}}}}))
    else:
        print(os.environ["FAKE_ISSUES_JSON"])
elif args[:1] == ["issue"]:
    pass
elif "/git/ref/heads/" in joined:
    sha = os.environ.get("FAKE_REF_SHA", "")
    if not sha:
        print("gh: Not Found (HTTP 404)", file=sys.stderr)
        sys.exit(1)
    print(sha)
elif "-X" in args and "DELETE" in args:
    pass
elif "/comments" in joined and "POST" in args:
    print("900" if "--jq" in args else "{}")
elif "/comments" in joined:
    print(os.environ.get("FAKE_COMMENTS_JSONL", ""), end="")
elif "/labels" in joined and "--paginate" in args:
    print("" if "--jq" in args else "[]")
else:
    print("{}")
'''


def _run_script() -> str:
	lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
	start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {STEP_NAME}")
	run_idx = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
	indent = len(lines[run_idx]) - len(lines[run_idx].lstrip()) + 2
	body = []
	for line in lines[run_idx + 1:]:
		if line.strip() and len(line) - len(line.lstrip()) < indent:
			break
		body.append(line[indent:])
	return "\n".join(body) + "\n"


def _issues_json(managed: bool = False) -> str:
	labels = [{"name": "ai:orchestrator-managed"}] if managed else [{"name": "ai:ready-to-merge"}]
	body = "<!-- ai:activation-fix:v1 source=pr-4637 -->\nImplement the guest homepage and the member homepage.\n"
	return json.dumps({"data": {"repository": {"i0": {"number": ISSUE, "labels": {"nodes": labels}, "body": body}}}})


def _partial_body() -> str:
	import partial_delivery

	return "Automated implementation. Refs #4639\n" + partial_delivery.pr_section(["Implement the member homepage (Phase 2)."], ISSUE)


def _run(*, body: str, env_overrides: dict | None = None, managed: bool = False) -> dict:
	assert shutil.which("jq") is not None, "jq is required to run the issue_pr_status step"
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		(tmp / "scripts").mkdir()
		for name in SUPPORT_SCRIPTS:
			shutil.copyfile(REPO_ROOT / "scripts" / name, tmp / "scripts" / name)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		(bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
		(bin_dir / "gh").chmod(0o755)
		gh_log = tmp / "gh.log"
		gh_log.write_text("", encoding="utf-8")
		github_env = tmp / "github_env"
		github_env.write_text("", encoding="utf-8")
		(tmp / "step.sh").write_text(_run_script(), encoding="utf-8")
		env = {
			"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
			"HOME": str(tmp),
			"GH_LOG": str(gh_log),
			"GITHUB_ENV": str(github_env),
			"GH_TOKEN": "t",
			"GH_RETRY_MAX_ATTEMPTS": "1",
			"REPOSITORY": "o/r",
			"GITHUB_REPOSITORY": "o/r",
			"PR_NUMBER": str(PR),
			"PR_HEAD_REF": f"ai/issue-{ISSUE}",
			"PR_BASE_REF": "main",
			"PR_DEFAULT_BRANCH": "main",
			"PR_MERGED": "true",
			"PR_TITLE": f"AI implementation for issue #{ISSUE}",
			"PR_BODY": body,
			"PR_HEAD_REPO": "o/r",
			"PR_AUTHOR_LOGIN": "pipeline-bot",
			"PR_HEAD_SHA": HEAD_SHA,
			"FINAL_LABEL": "ai:merged",
			"FAKE_ISSUES_JSON": _issues_json(managed),
			"FAKE_REF_SHA": HEAD_SHA,
			"PYTHONDONTWRITEBYTECODE": "1",
		}
		env.update(env_overrides or {})
		result = subprocess.run(["bash", str(tmp / "step.sh")], cwd=str(tmp), env=env, capture_output=True, text=True, timeout=120)
		assert result.returncode == 0, result.stdout + result.stderr
		calls = [json.loads(line) for line in gh_log.read_text(encoding="utf-8").splitlines() if line.strip()]
		return {"stdout": result.stdout, "calls": calls, "github_env": github_env.read_text(encoding="utf-8")}


def _closed(run: dict) -> bool:
	return any(call["args"][:2] == ["issue", "close"] for call in run["calls"])


def _posted_bodies(run: dict) -> list[str]:
	return [call["body"] for call in run["calls"] if "body" in call and "/comments" in " ".join(call["args"])]


def _set_clarification(run: dict) -> bool:
	return any(
		"labels" in " ".join(c["args"]) and "ai:clarification" in (" ".join(c["args"]) + c.get("stdin", ""))
		for c in run["calls"]
	)


def _lineage(run: dict) -> str:
	text = run["github_env"]
	start = text.index("LINEAGE_FINALIZE_ISSUE_NUMBERS<<EOF\n") + len("LINEAGE_FINALIZE_ISSUE_NUMBERS<<EOF\n")
	return text[start:text.index("EOF", start)].strip()


def test_partial_activation_fix_delivery_keeps_the_issue_open() -> None:
	# The #4639 regression: Phase 1 merged, Phase 2 must stay tracked.
	run = _run(body=_partial_body())
	assert not _closed(run), run["stdout"]
	assert _lineage(run) == ""
	assert f"LINKED_ISSUE_NUMBERS<<EOF\n{ISSUE}\n" in run["github_env"]
	bodies = _posted_bodies(run)
	assert any("> - Implement the member homepage (Phase 2)." in b and b.rstrip().endswith(f"issue={ISSUE} pr={PR} -->") for b in bodies)
	assert f"/reclarify\n<!-- ai:partial-delivery-restart:{ISSUE} -->" in bodies
	assert any(
		"labels" in " ".join(c["args"]) and "ai:clarification" in (" ".join(c["args"]) + c.get("stdin", ""))
		for c in run["calls"]
	)
	assert not any("ai:merged" in " ".join(c["args"]) for c in run["calls"])
	assert any("DELETE" in c["args"] and f"git/refs/heads/ai/issue-{ISSUE}" in " ".join(c["args"]) for c in run["calls"])
	assert "PARTIAL_DELIVERY mode=merge pr=4646 issue=4639 outcome=kept_open reason=deferred_scope comment=posted restart=posted branch=deleted" in run["stdout"]


def test_full_delivery_still_closes_the_issue() -> None:
	run = _run(body=f"Automated implementation. Closes https://github.com/o/r/issues/{ISSUE}\n")
	assert _closed(run)
	assert _lineage(run) == str(ISSUE)
	assert not any("partial-delivery" in b for b in _posted_bodies(run))
	# A full delivery never restarts clarification.
	assert not any(b.startswith("/reclarify") for b in _posted_bodies(run))
	assert not _set_clarification(run)


def test_unbound_or_disabled_partial_marker_still_closes() -> None:
	for overrides in (
		{"PR_HEAD_REPO": "fork/r"},
		{"PR_AUTHOR_LOGIN": "maintainer"},
		{"PR_HEAD_REF": f"ai/issue-{ISSUE}-x"},
		{"PR_HEAD_REF": "feature/x", "PR_BODY": _partial_body() + f"\nCloses https://github.com/o/r/issues/{ISSUE}\n"},
		{"PARTIAL_DELIVERY_TRACKING_ENABLED": "false"},
	):
		run = _run(body=overrides.pop("PR_BODY", _partial_body()), env_overrides=overrides)
		assert _closed(run), (overrides, run["stdout"])
		assert not any(b.startswith("/reclarify") for b in _posted_bodies(run)), overrides
	managed = _run(body=_partial_body(), managed=True)
	assert _closed(managed)


def test_unavailable_identity_keeps_the_issue_open() -> None:
	# A failed /user read cannot verify the binding; the issue stays open
	# (like reason=helper_missing and the close sweep's defer) and nothing is posted.
	run = _run(body=_partial_body(), env_overrides={"FAKE_USER_FAIL": "1"})
	assert not _closed(run), run["stdout"]
	assert _lineage(run) == ""
	assert _posted_bodies(run) == []
	assert _set_clarification(run)
	assert not any("ai:merged" in " ".join(c["args"]) for c in run["calls"])
	assert f"PARTIAL_DELIVERY mode=merge pr={PR} issue={ISSUE} outcome=kept_open reason=identity_unavailable" in run["stdout"]


def test_rerun_with_markers_present_posts_nothing_new() -> None:
	comments = "\n".join(json.dumps(c) for c in (
		{"id": 100, "login": "pipeline-bot", "body": f"x\n<!-- ai:partial-delivery-remaining:v1 issue={ISSUE} pr={PR} -->"},
		{"id": 101, "login": "pipeline-bot", "body": f"/reclarify\n<!-- ai:partial-delivery-restart:{ISSUE} -->"},
	)) + "\n"
	run = _run(body=_partial_body(), env_overrides={"FAKE_COMMENTS_JSONL": comments})
	assert not _closed(run)
	assert _posted_bodies(run) == []
	assert "comment=present restart=present" in run["stdout"]


def test_moved_branch_is_kept() -> None:
	run = _run(body=_partial_body(), env_overrides={"FAKE_REF_SHA": "b" * 40})
	assert not any("DELETE" in c["args"] for c in run["calls"])
	assert "branch=kept" in run["stdout"]


def test_activation_fix_merge_skip_is_unchanged() -> None:
	# The recursion guard keeps skipping a merge that closes or refers to an
	# activation-fix issue, whether it delivered all or part of it.
	script = (REPO_ROOT / "scripts" / "activation_verify.sh").read_text(encoding="utf-8")
	assert "reason=activation_fix_merge" in script
	workflow = WORKFLOW.read_text(encoding="utf-8")
	assert 'fetch_from_ref_or_local "scripts/partial_delivery.py"' in workflow
	assert "PR_HEAD_SHA: ${{ github.event.pull_request.head.sha || '' }}" in workflow


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
	for test in tests:
		test()
	print(f"OK: {len(tests)} issue_pr_status partial-delivery tests passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
