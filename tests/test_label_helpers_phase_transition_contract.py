#!/usr/bin/env python3
"""Contract tests for resilient phase-label transitions in label helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LABEL_HELPERS = REPO_ROOT / "scripts" / "label_helpers.sh"


def _script_text() -> str:
	return LABEL_HELPERS.read_text(encoding="utf-8")


def _function_body(text: str, function_name: str) -> str:
	match = re.search(rf"(?ms)^{function_name}\(\) \{{\n(.*?)^\}}", text)
	assert match is not None, f"Missing function: {function_name}"
	return match.group(1)


def test_set_issue_phase_label_resilient_contract() -> None:
	text = _script_text()
	body = _function_body(text, "set_issue_phase_label_resilient")

	assert "_AI_PHASE_LABELS='[\"ai:done\"" in text
	assert 'ensure_label_exists "${target_label}" "${repo}" || true' in body
	assert '--argjson p "${_AI_PHASE_LABELS}" --arg t "${target_label}"' in body
	assert "'[.[] | select(. as $l | $p | index($l) != null) | select(. != $t)] | unique'" in body
	assert 'gh_retry gh api --paginate "repos/${repo}/issues/${issue_number}/labels"' in body
	# BUG-001: the full-set PUT raced with concurrent label writers.
	assert "-X PUT" not in body
	assert "--input -" not in body
	assert 'GH_RETRY_IDEMPOTENT=true gh_retry gh api -X POST "repos/${repo}/issues/${issue_number}/labels"' in body
	assert "gh api -X DELETE" in body
	assert '"repos/${repo}/issues/${issue_number}/labels/${_stale_enc}"' in body
	assert "'$l|@uri'" in body
	assert "HTTP 404" in body
	assert body.count("return 0") >= 4


def test_ensure_label_exists_backward_compatible_contract() -> None:
	text = _script_text()
	body = _function_body(text, "ensure_label_exists")

	assert "gh_retry gh label create" in body
	assert "already[ _-]*exists|already_exists" in body
	assert "return 1" in body


def test_security_pass_labels_are_in_the_resilient_phase_set() -> None:
	text = _script_text()
	for label in ("ai:security-pass", "ai:security-pass-fixing", "ai:security-pass-failed"):
		assert f'"{label}"' in text
		assert f'["{label}"]=' in text


# --- Behavioural harness ----------------------------------------------------

_FAKE_GH = r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
log = os.environ["FAKE_GH_LOG"]
with open(log, "a", encoding="utf-8") as fh:
	fh.write(json.dumps(args) + "\n")

if args[:2] == ["label", "create"]:
	sys.exit(0)
if args and args[0] == "api":
	method = "GET"
	if "-X" in args:
		method = args[args.index("-X") + 1]
	if method == "GET":
		if os.environ.get("FAKE_GET") == "fail":
			sys.stderr.write("gh: Server Error (HTTP 500)\n")
			sys.exit(1)
		# Snapshot the GET sees. A concurrent writer adds force-review right
		# after it, so a later full-set write would drop that label.
		print(json.dumps(json.loads(os.environ["FAKE_LABELS"])))
		state = os.environ["FAKE_GH_STATE"]
		with open(state, "w", encoding="utf-8") as fh:
			fh.write("force-review added after GET\n")
		sys.exit(0)
	if method == "POST":
		if os.environ.get("FAKE_POST") == "fail":
			sys.stderr.write("gh: Server Error (HTTP 500)\n")
			sys.exit(1)
		print("[]")
		sys.exit(0)
	if method == "DELETE":
		mode = os.environ.get("FAKE_DELETE", "ok")
		if mode == "404":
			sys.stderr.write("gh: Label does not exist (HTTP 404)\n")
			sys.exit(1)
		if mode == "500":
			sys.stderr.write("gh: Server Error (HTTP 500)\n")
			sys.exit(1)
		sys.exit(0)
	if method == "PUT":
		sys.exit(0)
sys.exit(0)
'''


def _run_swap(
	labels: list[str],
	target: str,
	env_extra: dict[str, str] | None = None,
) -> tuple[int, str, str, list[list[str]]]:
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		scripts_dir = tmp_path / "scripts"
		scripts_dir.mkdir()
		# Copy only label_helpers.sh so the fallback gh_retry is used (no
		# retry sleeps in tests).
		shutil.copy(LABEL_HELPERS, scripts_dir / "label_helpers.sh")
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir()
		fake = bin_dir / "gh"
		fake.write_text(_FAKE_GH, encoding="utf-8")
		fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
		log = tmp_path / "gh_calls.jsonl"
		log.write_text("", encoding="utf-8")
		env = dict(os.environ)
		env.update(
			{
				"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
				"FAKE_GH_LOG": str(log),
				"FAKE_GH_STATE": str(tmp_path / "state.txt"),
				"FAKE_LABELS": json.dumps(labels),
				"GITHUB_REPOSITORY": "o/r",
			}
		)
		env.pop("GH_TOKEN", None)
		env.update(env_extra or {})
		# pipefail mirrors the `set -euo pipefail` callers run under, so a
		# failed GET reaches the helper's GET-failure fallback.
		script = (
			'set -o pipefail\n'
			f'source "{scripts_dir / "label_helpers.sh"}"\n'
			f'set_issue_phase_label_resilient 5 "{target}" o/r\n'
			'rc=$?\n'
			'echo "OUTCOME=${SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME}"\n'
			'exit "${rc}"\n'
		)
		proc = subprocess.run(
			["bash", "-c", script],
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
			check=False,
		)
		calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
	outcome = ""
	for line in proc.stdout.splitlines():
		if line.startswith("OUTCOME="):
			outcome = line[len("OUTCOME="):]
	return proc.returncode, outcome, proc.stderr, calls


def _jq_available() -> bool:
	# The helper needs the jq binary (present on CI runners). Skip the
	# behavioural cases, with a notice, where it is missing.
	if shutil.which("jq") is None:
		print("SKIP: jq binary not available; behavioural phase-swap test skipped")
		return False
	return True


def _api_calls(calls: list[list[str]], method: str) -> list[list[str]]:
	out = []
	for args in calls:
		if not args or args[0] != "api":
			continue
		m = args[args.index("-X") + 1] if "-X" in args else "GET"
		if m == method:
			out.append(args)
	return out


def _delete_paths(calls: list[list[str]]) -> list[str]:
	paths = []
	for args in _api_calls(calls, "DELETE"):
		paths.extend(a for a in args if a.startswith("repos/"))
	return paths


def test_phase_swap_keeps_label_added_after_get() -> None:
	if not _jq_available():
		return
	rc, outcome, _stderr, calls = _run_swap(
		["ai:review-blocked", "ai:orchestrator-managed"], "ai:ready-to-merge"
	)
	assert rc == 0
	assert outcome == "applied"
	assert _api_calls(calls, "PUT") == []
	posts = _api_calls(calls, "POST")
	assert len(posts) == 1
	assert "labels[]=ai:ready-to-merge" in posts[0]
	deletes = _delete_paths(calls)
	assert deletes == ["repos/o/r/issues/5/labels/ai%3Areview-blocked"]
	assert not any("force-review" in p for p in deletes)
	assert not any("ai%3Aorchestrator-managed" in p for p in deletes)
	post_index = calls.index(posts[0])
	first_delete = next(i for i, c in enumerate(calls) if c in _api_calls(calls, "DELETE"))
	assert post_index < first_delete


def test_phase_swap_delete_404_is_already_removed() -> None:
	if not _jq_available():
		return
	rc, outcome, stderr, calls = _run_swap(
		["ai:review-blocked"], "ai:ready-to-merge", {"FAKE_DELETE": "404"}
	)
	assert rc == 0
	assert outcome == "applied"
	assert len(_delete_paths(calls)) == 1
	assert "could not remove stale phase label" not in stderr


def test_phase_swap_delete_error_warns_without_changing_outcome() -> None:
	if not _jq_available():
		return
	rc, outcome, stderr, _calls = _run_swap(
		["ai:review-blocked"], "ai:ready-to-merge", {"FAKE_DELETE": "500"}
	)
	assert rc == 0
	assert outcome == "applied"
	assert "::warning::" in stderr
	assert "'ai:review-blocked'" in stderr
	assert "#5" in stderr


def test_phase_swap_post_failure_skips_deletes() -> None:
	if not _jq_available():
		return
	rc, outcome, _stderr, calls = _run_swap(
		["ai:review-blocked"], "ai:ready-to-merge", {"FAKE_POST": "fail"}
	)
	assert rc == 0
	assert outcome == "failed"
	assert _api_calls(calls, "DELETE") == []
	assert _api_calls(calls, "PUT") == []


def test_phase_swap_get_failure_falls_back_to_single_post() -> None:
	if not _jq_available():
		return
	rc, outcome, stderr, calls = _run_swap(
		["ai:review-blocked"], "ai:ready-to-merge", {"FAKE_GET": "fail"}
	)
	assert rc == 0
	assert outcome == "applied"
	assert "GET labels failed for #5" in stderr
	assert len(_api_calls(calls, "POST")) == 1
	assert _api_calls(calls, "DELETE") == []
	assert _api_calls(calls, "PUT") == []


def test_phase_swap_never_deletes_target_already_present() -> None:
	if not _jq_available():
		return
	rc, outcome, _stderr, calls = _run_swap(
		["ai:ready-to-merge", "ai:done"], "ai:ready-to-merge"
	)
	assert rc == 0
	assert outcome == "applied"
	deletes = _delete_paths(calls)
	assert deletes == ["repos/o/r/issues/5/labels/ai%3Adone"]


if __name__ == "__main__":
	test_set_issue_phase_label_resilient_contract()
	test_ensure_label_exists_backward_compatible_contract()
	test_security_pass_labels_are_in_the_resilient_phase_set()
	test_phase_swap_keeps_label_added_after_get()
	test_phase_swap_delete_404_is_already_removed()
	test_phase_swap_delete_error_warns_without_changing_outcome()
	test_phase_swap_post_failure_skips_deletes()
	test_phase_swap_get_failure_falls_back_to_single_post()
	test_phase_swap_never_deletes_target_already_present()
	print("PASS")
