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
import urllib.parse

args = sys.argv[1:]
log = os.environ["FAKE_GH_LOG"]
state_path = os.environ["FAKE_GH_STATE"]


def _load():
	with open(state_path, encoding="utf-8") as fh:
		return json.load(fh)


def _save(labels):
	with open(state_path, "w", encoding="utf-8") as fh:
		json.dump(labels, fh)

with open(log, "a", encoding="utf-8") as fh:
	fh.write(json.dumps(args) + "\n")

if args[:2] == ["label", "create"]:
	mode = os.environ.get("FAKE_LABEL_CREATE", "ok")
	if mode == "exists":
		sys.stderr.write('label with name "x" already exists; use --force to update its color and description\n')
		sys.exit(1)
	if mode == "fail":
		sys.stderr.write("gh: Server Error (HTTP 500)\n")
		sys.exit(1)
	if mode == "fail_once":
		counter = state_path + ".create_count"
		try:
			with open(counter, encoding="utf-8") as fh:
				seen = int(fh.read().strip() or "0")
		except FileNotFoundError:
			seen = 0
		with open(counter, "w", encoding="utf-8") as fh:
			fh.write(str(seen + 1))
		if seen == 0:
			sys.stderr.write("gh: Server Error (HTTP 500)\n")
			sys.exit(1)
	sys.exit(0)
if args and args[0] == "api":
	method = "GET"
	if "-X" in args:
		method = args[args.index("-X") + 1]
	if method == "GET":
		if os.environ.get("FAKE_GET") == "fail":
			sys.stderr.write("gh: Server Error (HTTP 500)\n")
			sys.exit(1)
		# Serve the current label state, then let a concurrent writer add
		# force-review right after the read. A later full-set write built
		# from this snapshot would drop that label from the state file.
		print(json.dumps(_load()))
		labels = _load()
		if "force-review" not in labels:
			labels.append("force-review")
		_save(labels)
		sys.exit(0)
	if method == "POST":
		if os.environ.get("FAKE_POST") == "fail":
			sys.stderr.write("gh: Server Error (HTTP 500)\n")
			sys.exit(1)
		labels = _load()
		for i, arg in enumerate(args):
			if arg == "-f" and i + 1 < len(args) and args[i + 1].startswith("labels[]="):
				name = args[i + 1][len("labels[]="):]
				if name not in labels:
					labels.append(name)
		_save(labels)
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
		path = next(a for a in args if a.startswith("repos/"))
		name = urllib.parse.unquote(path.rsplit("/", 1)[1])
		_save([label for label in _load() if label != name])
		sys.exit(0)
	if method == "PUT":
		# Full-set replacement (the removed BUG-001 shape).
		_save(json.load(sys.stdin).get("labels", []))
		sys.exit(0)
sys.exit(0)
'''


def _run_swap(
	labels: list[str],
	target: str,
	env_extra: dict[str, str] | None = None,
	final_labels: list[str] | None = None,
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
		state_file = tmp_path / "labels.json"
		state_file.write_text(json.dumps(labels), encoding="utf-8")
		env = dict(os.environ)
		env.update(
			{
				"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
				"FAKE_GH_LOG": str(log),
				"FAKE_GH_STATE": str(state_file),
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
		if final_labels is not None:
			final_labels.extend(json.loads(state_file.read_text(encoding="utf-8")))
	outcome = ""
	for line in proc.stdout.splitlines():
		if line.startswith("OUTCOME="):
			outcome = line[len("OUTCOME="):]
	return proc.returncode, outcome, proc.stderr, calls


def _run_bash(
	script_body: str,
	env_extra: dict[str, str] | None = None,
	labels: list[str] | None = None,
) -> tuple[int, str, str, list[list[str]]]:
	"""Run *script_body* under set -euo pipefail with the fake gh on PATH.

	``HELPERS`` holds the path of the copied label_helpers.sh.
	"""
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		scripts_dir = tmp_path / "scripts"
		scripts_dir.mkdir()
		shutil.copy(LABEL_HELPERS, scripts_dir / "label_helpers.sh")
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir()
		fake = bin_dir / "gh"
		fake.write_text(_FAKE_GH, encoding="utf-8")
		fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
		log = tmp_path / "gh_calls.jsonl"
		log.write_text("", encoding="utf-8")
		state_file = tmp_path / "labels.json"
		state_file.write_text(json.dumps(labels or []), encoding="utf-8")
		env = dict(os.environ)
		env.update(
			{
				"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
				"FAKE_GH_LOG": str(log),
				"FAKE_GH_STATE": str(state_file),
				"GITHUB_REPOSITORY": "o/r",
				"HELPERS": str(scripts_dir / "label_helpers.sh"),
			}
		)
		env.pop("GH_TOKEN", None)
		env.update(env_extra or {})
		proc = subprocess.run(
			["bash", "-c", "set -euo pipefail\n" + script_body],
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
			check=False,
		)
		calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
	return proc.returncode, proc.stdout, proc.stderr, calls


def _label_create_calls(calls: list[list[str]]) -> list[list[str]]:
	return [args for args in calls if args[:2] == ["label", "create"]]


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
	final: list[str] = []
	rc, outcome, _stderr, calls = _run_swap(
		["ai:review-blocked", "ai:orchestrator-managed"], "ai:ready-to-merge",
		final_labels=final,
	)
	assert rc == 0
	assert outcome == "applied"
	# The fake adds force-review after the GET; it must survive the swap.
	assert sorted(final) == ["ai:orchestrator-managed", "ai:ready-to-merge", "force-review"]
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


# --- ensure_label_exists per-process cache ----------------------------------


def test_ensure_label_exists_cache_is_declared_with_resource_guard() -> None:
	text = _script_text()
	assert "declare -gA _AI_LABEL_ENSURED_CACHE=()" in text
	guard = 'if [[ "$(declare -p _AI_LABEL_ENSURED_CACHE 2>/dev/null)" != "declare -A"* ]]; then'
	assert guard in text
	assert text.index(guard) < text.index("declare -gA _AI_LABEL_ENSURED_CACHE=()")
	body = _function_body(text, "ensure_label_exists")
	assert '${_AI_LABEL_ENSURED_CACHE["${_label_cache_key}"]+x}' in body
	assert body.index("repo required") < body.index("_label_cache_key")
	assert body.count('_AI_LABEL_ENSURED_CACHE["${_label_cache_key}"]=1') == 2


def test_ensure_label_exists_cache_same_repo_and_label_creates_once() -> None:
	rc, stdout, stderr, calls = _run_bash(
		'source "${HELPERS}"\n'
		'a=0; ensure_label_exists ai:review-blocked o/r || a=$?\n'
		'b=0; ensure_label_exists ai:review-blocked o/r || b=$?\n'
		'echo "RC=${a},${b}"\n'
	)
	assert rc == 0, stderr
	assert "RC=0,0" in stdout
	assert len(_label_create_calls(calls)) == 1
	assert "already ensured in this process for 'o/r'" in stderr


def test_ensure_label_exists_cache_is_keyed_by_repo_and_label() -> None:
	rc, stdout, stderr, calls = _run_bash(
		'source "${HELPERS}"\n'
		'ensure_label_exists ai:planning o/r\n'
		'ensure_label_exists ai:done o/r\n'
		'ensure_label_exists ai:planning o/x\n'
		'ensure_label_exists ai:planning o/r\n'
		'echo DONE\n'
	)
	assert rc == 0, stderr
	assert "DONE" in stdout
	creates = _label_create_calls(calls)
	assert len(creates) == 3
	pairs = {(c[2], c[c.index("--repo") + 1]) for c in creates}
	assert pairs == {("ai:planning", "o/r"), ("ai:done", "o/r"), ("ai:planning", "o/x")}


def test_ensure_label_exists_failure_is_not_cached() -> None:
	rc, stdout, stderr, calls = _run_bash(
		'source "${HELPERS}"\n'
		'a=0; ensure_label_exists ai:planning o/r || a=$?\n'
		'b=0; ensure_label_exists ai:planning o/r || b=$?\n'
		'echo "RC=${a},${b}"\n',
		{"FAKE_LABEL_CREATE": "fail"},
	)
	assert rc == 0, stderr
	assert "RC=1,1" in stdout
	assert len(_label_create_calls(calls)) == 2
	assert stderr.count("::warning::ensure_label_exists: failed to create label") == 2

	rc, stdout, stderr, calls = _run_bash(
		'source "${HELPERS}"\n'
		'a=0; ensure_label_exists ai:planning o/r || a=$?\n'
		'b=0; ensure_label_exists ai:planning o/r || b=$?\n'
		'c=0; ensure_label_exists ai:planning o/r || c=$?\n'
		'echo "RC=${a},${b},${c}"\n',
		{"FAKE_LABEL_CREATE": "fail_once"},
	)
	assert rc == 0, stderr
	assert "RC=1,0,0" in stdout
	assert len(_label_create_calls(calls)) == 2


def test_ensure_label_exists_already_exists_is_cached() -> None:
	rc, stdout, stderr, calls = _run_bash(
		'source "${HELPERS}"\n'
		'a=0; ensure_label_exists ai:planning o/r || a=$?\n'
		'b=0; ensure_label_exists ai:planning o/r || b=$?\n'
		'echo "RC=${a},${b}"\n',
		{"FAKE_LABEL_CREATE": "exists"},
	)
	assert rc == 0, stderr
	assert "RC=0,0" in stdout
	assert len(_label_create_calls(calls)) == 1


def test_set_issue_phase_label_resilient_second_call_skips_create() -> None:
	if not _jq_available():
		return
	rc, stdout, stderr, calls = _run_bash(
		'source "${HELPERS}"\n'
		'set_issue_phase_label_resilient 5 ai:planning o/r\n'
		'echo "OUT1=${SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME}"\n'
		'set_issue_phase_label_resilient 5 ai:planning o/r\n'
		'echo "OUT2=${SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME}"\n',
		labels=["ai:clarification"],
	)
	assert rc == 0, stderr
	assert "OUT1=applied" in stdout
	assert "OUT2=applied" in stdout
	assert len(_label_create_calls(calls)) == 1


def test_label_helpers_resource_keeps_cache() -> None:
	rc, stdout, stderr, calls = _run_bash(
		'source "${HELPERS}"\n'
		'ensure_label_exists ai:planning o/r\n'
		'source "${HELPERS}"\n'
		'ensure_label_exists ai:planning o/r\n'
		'echo DONE\n'
	)
	assert rc == 0, stderr
	assert "DONE" in stdout
	assert len(_label_create_calls(calls)) == 1


def test_ensure_label_exists_missing_repo_unchanged() -> None:
	rc, stdout, stderr, calls = _run_bash(
		'source "${HELPERS}"\n'
		'a=0; ensure_label_exists ai:planning || a=$?\n'
		'echo "RC=${a}"\n',
		{"GITHUB_REPOSITORY": ""},
	)
	assert rc == 0, stderr
	assert "RC=1" in stdout
	assert "ensure_label_exists: repo required" in stderr
	assert _label_create_calls(calls) == []


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
	test_ensure_label_exists_cache_is_declared_with_resource_guard()
	test_ensure_label_exists_cache_same_repo_and_label_creates_once()
	test_ensure_label_exists_cache_is_keyed_by_repo_and_label()
	test_ensure_label_exists_failure_is_not_cached()
	test_ensure_label_exists_already_exists_is_cached()
	test_set_issue_phase_label_resilient_second_call_skips_create()
	test_label_helpers_resource_keeps_cache()
	test_ensure_label_exists_missing_repo_unchanged()
	print("PASS")
