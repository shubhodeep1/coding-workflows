#!/usr/bin/env python3
"""Tests for validation owner routing in scripts/validate_process.sh (project #7913, p1c).

The `# BEGIN/END validation owner routing` block is extracted from the script
and run under bash with stubbed GitHub calls and a stub (or the real)
`workflow_failure_heal.py` in a fake trusted support checkout. Structural
checks pin where the block is called from.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"
REAL_HELPER = REPO_ROOT / "scripts" / "workflow_failure_heal.py"
DIAGNOSE_PROMPTS = (
	REPO_ROOT / "prompts" / "mode-validate-diagnose.txt",
	REPO_ROOT / "prompts" / "_templates" / "mode-validate-diagnose.txt",
)
CONTRACT = REPO_ROOT / "prompts" / "contracts" / "mode-validate-diagnose.yml"

FP_A = "a" * 64
FP_B = "b" * 64
SUPPORT_SHA = "c" * 40
MARKER_RE = re.compile(
	r"^<!-- validation-harness-owner:v1 fp=[0-9a-f]{64} stage=[a-z_]+ reason=[a-z_]+ support=[0-9a-f]{40} run=[0-9]+ -->$"
)

STUB_HELPER = textwrap.dedent(
	"""\
	#!/usr/bin/env python3
	import json, os, sys
	log = os.environ.get("STUB_ARGV_LOG")
	if log:
		with open(log, "a") as fh:
			fh.write(json.dumps(sys.argv[1:]) + "\\n")
	cmd = sys.argv[1] if len(sys.argv) > 1 else ""
	if cmd == "classify-validation-owner":
		rc = int(os.environ.get("STUB_CLASSIFY_RC", "0"))
		if rc:
			sys.exit(rc)
		sys.stdout.write(open(os.environ["STUB_CLASSIFY_FILE"]).read())
	elif cmd == "validation-failure-fingerprint":
		print(os.environ.get("STUB_FP", ""))
	elif cmd == "redact-stream":
		sys.stdout.write(sys.stdin.read())
	elif cmd == "error-signature":
		print(os.environ.get("STUB_SIGNATURE", "stub signature"))
	"""
)

STUBS = textwrap.dedent(
	"""\
	gh_retry() { "$@"; }
	gh() {
		printf '%s\\n' "$*" >> "${GH_LOG}"
		case "$*" in
			"api user"*) printf '%s\\n' "${STUB_LOGIN:-}" ;;
			*comments*) cat "${STUB_COMMENTS_FILE:-/dev/null}" ;;
		esac
	}
	is_tracking_run() { [ "${TRACKING_ISSUE_NUM}" -gt 0 ]; }
	compact_validation_summary_value() { printf '%s' "$1" | tr '\\r\\n' '  '; }
	"""
)


class SkipTest(Exception):
	pass


def _need_jq() -> None:
	# validate_process.sh needs jq; CI runners have it. Same convention as
	# tests/test_activation_verify.py.
	if not shutil.which("jq"):
		raise SkipTest("jq is not installed")


def _script_text() -> str:
	return VALIDATE_PROCESS.read_text(encoding="utf-8")


def _block() -> str:
	text = _script_text()
	match = re.search(
		r"^# BEGIN validation owner routing \(project #7913 p1c\)\n.*?^# END validation owner routing\n",
		text,
		re.DOTALL | re.MULTILINE,
	)
	if not match:
		raise AssertionError("owner routing block not found in validate_process.sh")
	return match.group(0)


class Sandbox:
	def __init__(self, helper: str = "stub") -> None:
		self.root = Path(tempfile.mkdtemp(prefix="owner-routing-"))
		self.runtime = self.root / "runtime"
		self.runtime.mkdir()
		self.stage = self.root / "stage"
		(self.stage / "scripts").mkdir(parents=True)
		if helper == "stub":
			(self.stage / "scripts" / "workflow_failure_heal.py").write_text(STUB_HELPER, encoding="utf-8")
		elif helper == "real":
			shutil.copy(REAL_HELPER, self.stage / "scripts" / "workflow_failure_heal.py")
		self.consumer = self.root / "consumer"
		self.consumer.mkdir()
		self.argv_log = self.root / "argv.log"
		self.gh_log = self.root / "gh.log"
		self.classify_file = self.root / "classify.json"
		self.diagnosis = self.runtime / "validation_diagnosis.json"
		self.env = {
			"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
			"HOME": str(self.root),
			"RUNTIME_DIR": str(self.runtime),
			"VALIDATE_HELPER_STAGE_DIR": str(self.stage),
			"STUB_ARGV_LOG": str(self.argv_log),
			"GH_LOG": str(self.gh_log),
			"STUB_CLASSIFY_FILE": str(self.classify_file),
			"DIAGNOSE_RESULT_FILE": str(self.diagnosis),
			"GITHUB_REPOSITORY": "acme/widgets",
			"GITHUB_RUN_ID": "4242",
			"TRACKING_ISSUE_NUM": "7",
			"STATUS_FILE": str(self.runtime / "validation_status.json"),
			"METADATA_FILE": str(self.runtime / "validation_metadata.json"),
			"VALIDATION_LOG_FILE": str(self.runtime / "validation.log"),
			"GENERATE_LOG_FILE": str(self.runtime / "validate_generate.log"),
			"DIAGNOSE_LOG_FILE": str(self.runtime / "validate_diagnose.log"),
			"PYTHONDONTWRITEBYTECODE": "1",
		}

	def classify_result(self, **fields) -> None:
		base = {"owner": "none", "reason": "", "stage": "", "fingerprint": "", "harness_paths": [], "project_paths": [], "project_fix_ids": [], "truncated": False}
		base.update(fields)
		self.classify_file.write_text(json.dumps(base), encoding="utf-8")

	def run(self, body: str, **env: str) -> subprocess.CompletedProcess:
		script = self.root / "run.sh"
		script.write_text("set -u\n" + STUBS + _block() + "\n" + textwrap.dedent(body), encoding="utf-8")
		full_env = dict(self.env)
		full_env.update(env)
		return subprocess.run(["bash", str(script)], cwd=str(self.consumer), env=full_env, capture_output=True, text=True, timeout=120)

	def argv(self) -> list[list[str]]:
		if not self.argv_log.exists():
			return []
		return [json.loads(line) for line in self.argv_log.read_text(encoding="utf-8").splitlines() if line.strip()]

	def cleanup(self) -> None:
		shutil.rmtree(self.root, ignore_errors=True)


def _sandbox(helper: str = "stub") -> Sandbox:
	return Sandbox(helper)


def test_harness_owner_reclassifies_needs_fixes_to_harness_error() -> None:
	_need_jq()
	sb = _sandbox()
	try:
		sb.diagnosis.write_text(json.dumps({"status": "needs_fixes", "fix_issues": [{"id": "a", "files": ["validation/tests/10_x.sh"]}]}), encoding="utf-8")
		sb.classify_result(owner="harness", reason="owner_paths", stage="diagnose", fingerprint=FP_A, harness_paths=["validation/tests/10_x.sh"])
		result = sb.run(
			"""
			DIAG_STATUS=needs_fixes
			ESCALATED_FROM_NEEDS_FIXES=false
			validation_owner_route_diagnosis
			echo "DIAG=${DIAG_STATUS} REPORT=${VALIDATION_OWNER_REPORT}"
			"""
		)
		assert "DIAG=harness_error REPORT=true" in result.stdout, result.stdout + result.stderr
		assert "VALIDATION_OWNER owner=harness reason=owner_paths stage=diagnose fp=" + FP_A in result.stdout
		classify = [a for a in sb.argv() if a and a[0] == "classify-validation-owner"]
		assert classify, sb.argv()
		args = classify[0]
		assert args[args.index("--diagnosis-json") + 1] == str(sb.diagnosis)
		assert args[args.index("--raw-status") + 1] == "needs_fixes"
		assert args[args.index("--support-root") + 1] == str(sb.stage)
		assert args[args.index("--support-commit") + 1] == "HEAD"
		assert "--fixed-reason" not in args
	finally:
		sb.cleanup()


def test_mixed_split_keeps_only_project_fix_issues() -> None:
	_need_jq()
	sb = _sandbox()
	try:
		issues = [{"id": "a", "title": "A"}, {"id": "b", "title": "B", "files": ["validation/x.sh"]}, {"title": "no id"}]
		sb.diagnosis.write_text(json.dumps({"status": "needs_fixes", "fix_issues": issues}), encoding="utf-8")
		sb.classify_result(owner="mixed", reason="mixed_harness_part", stage="diagnose", fingerprint=FP_A, project_fix_ids=["a", "index-2"])
		result = sb.run(
			"""
			DIAG_STATUS=needs_fixes
			validation_owner_route_diagnosis
			echo "DIAG=${DIAG_STATUS} REPORT=${VALIDATION_OWNER_REPORT}"
			"""
		)
		assert "DIAG=needs_fixes REPORT=true" in result.stdout, result.stdout + result.stderr
		kept = json.loads(sb.diagnosis.read_text(encoding="utf-8"))["fix_issues"]
		assert [i.get("title") for i in kept] == ["A", "no id"], kept
		unsplit = json.loads((sb.runtime / "validation_diagnosis_unsplit.json").read_text(encoding="utf-8"))
		assert len(unsplit["fix_issues"]) == 3
	finally:
		sb.cleanup()


def test_mixed_split_keeps_original_when_filter_would_drop_everything() -> None:
	_need_jq()
	for ids in ([], ["zz"]):
		sb = _sandbox()
		try:
			issues = [{"id": "a"}, {"id": "b"}]
			sb.diagnosis.write_text(json.dumps({"status": "needs_fixes", "fix_issues": issues}), encoding="utf-8")
			sb.classify_result(owner="mixed", reason="mixed_harness_part", stage="diagnose", fingerprint=FP_A, project_fix_ids=ids)
			result = sb.run("DIAG_STATUS=needs_fixes\nvalidation_owner_route_diagnosis\n")
			assert len(json.loads(sb.diagnosis.read_text(encoding="utf-8"))["fix_issues"]) == 2
			assert "split_skipped" in result.stderr, result.stderr
		finally:
			sb.cleanup()


def test_bounce_markers_trust_only_the_pipeline_login() -> None:
	_need_jq()
	sb = _sandbox()
	try:
		comments = sb.root / "comments.jsonl"
		rows = [
			{"login": "pipeline-bot", "body": f"Returned.\n\n<!-- validation-harness-bounce:v1 fp={FP_A} by=owner -->\n"},
			{"login": "mallory", "body": f"<!-- validation-harness-bounce:v1 fp={FP_B} -->"},
			{"login": "pipeline-bot", "body": f"<!-- validation-harness-bounce:v1 fp={FP_B} -->\nnot the last line"},
		]
		comments.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
		result = sb.run("validation_owner_load_bounced\n", STUB_LOGIN="pipeline-bot", STUB_COMMENTS_FILE=str(comments))
		bounced = (sb.runtime / "validation_harness_bounced_fps.txt").read_text(encoding="utf-8").split()
		assert bounced == [FP_A], (bounced, result.stderr)
		gh_calls = sb.gh_log.read_text(encoding="utf-8")
		assert "api user" in gh_calls and "repos/acme/widgets/issues/7/comments --paginate" in gh_calls

		# Memoized: a second load (self-heal re-exec) makes no API call.
		sb.gh_log.unlink()
		sb.run("validation_owner_load_bounced\n", STUB_LOGIN="pipeline-bot", STUB_COMMENTS_FILE=str(comments))
		assert not sb.gh_log.exists()

		# The bounced file reaches the classifier; a bounced result is not reported.
		sb.classify_result(owner="none", reason="bounced_repeat", stage="sandbox", fingerprint=FP_A)
		result = sb.run(
			"""
			validation_owner_fixed sandbox_unavailable "sandbox_failed"
			echo "OWNER=${VALIDATION_OWNER_OWNER} REPORT=${VALIDATION_OWNER_REPORT} MARKER=[$(validation_owner_comment_marker)]"
			""",
			VALIDATE_SUPPORT_SHA=SUPPORT_SHA,
		)
		assert "OWNER=none REPORT=false MARKER=[]" in result.stdout, result.stdout + result.stderr
		args = [a for a in sb.argv() if a[0] == "classify-validation-owner"][-1]
		assert args[args.index("--bounced-fingerprints-file") + 1] == str(sb.runtime / "validation_harness_bounced_fps.txt")
		assert args[args.index("--fixed-reason") + 1] == "sandbox_unavailable"
		assert args[args.index("--fixed-stage") + 1] == "sandbox"
	finally:
		sb.cleanup()


def test_ruled_out_block_only_for_a_matching_candidate() -> None:
	sb = _sandbox()
	try:
		(sb.runtime / "validation_harness_bounced_fps.txt").write_text(FP_A + "\n", encoding="utf-8")
		hit = sb.run("validation_owner_ruled_out_block\n", STUB_FP=FP_A)
		assert "=== HARNESS RULED OUT ===" in hit.stdout and FP_A in hit.stdout, hit.stdout
		miss = sb.run("validation_owner_ruled_out_block\n", STUB_FP=FP_B)
		assert "HARNESS RULED OUT" not in miss.stdout
		idle = sb.run("VALIDATION_IDLE_KILLED=1\nvalidation_owner_ruled_out_block\necho \"REASON=[${VALIDATION_HARNESS_REASON}]\"\n", STUB_FP=FP_B)
		assert "=== VALIDATION RUN FACTS ===" in idle.stdout and "idle_watchdog_killed=true" in idle.stdout
		assert "REASON=[]" in idle.stdout, "an idle kill must not set a fixed harness reason"
	finally:
		sb.cleanup()


def test_owner_marker_format_and_support_source() -> None:
	sb = _sandbox()
	try:
		subprocess.run(["git", "init", "-q", str(sb.stage)], check=True)
		subprocess.run(["git", "-C", str(sb.stage), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
		stage_head = subprocess.run(["git", "-C", str(sb.stage), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
		harness = f"VALIDATION_OWNER_OWNER=harness VALIDATION_OWNER_REASON=canary_failed VALIDATION_OWNER_STAGE=canary VALIDATION_OWNER_FP={FP_A}\n"
		body = harness + 'printf "%s" "## body$(validation_owner_comment_marker)"\n'
		with_sha = sb.run(body, VALIDATE_SUPPORT_SHA=SUPPORT_SHA)
		last = with_sha.stdout.splitlines()[-1]
		assert MARKER_RE.match(last), with_sha.stdout
		assert f"support={SUPPORT_SHA}" in last and "run=4242" in last and "stage=canary reason=canary_failed" in last
		from_git = sb.run(body)
		assert f"support={stage_head}" in from_git.stdout.splitlines()[-1], from_git.stdout
		bad_run = sb.run(body, VALIDATE_SUPPORT_SHA=SUPPORT_SHA, GITHUB_RUN_ID="12a")
		assert "validation-harness-owner" not in bad_run.stdout
		shutil.rmtree(sb.stage / ".git")
		no_sha = sb.run(body)
		assert "validation-harness-owner" not in no_sha.stdout
		for override in ("VALIDATION_OWNER_OWNER=project", "VALIDATION_OWNER_REASON=bounced_repeat", "VALIDATION_OWNER_FP=xyz"):
			other = sb.run(harness + override + "\nprintf '[%s]' \"$(validation_owner_comment_marker)\"\n", VALIDATE_SUPPORT_SHA=SUPPORT_SHA)
			assert other.stdout.endswith("[]"), (override, other.stdout)
	finally:
		sb.cleanup()


def test_status_and_metadata_fields() -> None:
	_need_jq()
	sb = _sandbox()
	try:
		base_status = {"status": "fail", "raw_status": "harness_error", "summary": "s", "failure_summary": "f", "tracking_issue": "7"}
		Path(sb.env["STATUS_FILE"]).write_text(json.dumps(base_status), encoding="utf-8")
		Path(sb.env["METADATA_FILE"]).write_text(json.dumps({"status": "fail", "raw_status": "harness_error"}), encoding="utf-8")
		sb.classify_result(owner="harness", reason="diagnosis_harness", stage="diagnose", fingerprint=FP_A, harness_paths=["validation/a.sh", "validation/b.sh"], project_paths=["src/app.py"])
		sb.run("DIAG_STATUS=harness_error\nvalidation_owner_route_diagnosis\nvalidation_owner_merge_fields\n")
		status = json.loads(Path(sb.env["STATUS_FILE"]).read_text(encoding="utf-8"))
		assert status["raw_status"] == "harness_error" and status["status"] == "fail"
		assert status["failure_owner"] == "harness"
		assert status["harness_report"] is True
		assert status["harness_stage"] == "diagnose" and status["harness_reason"] == "diagnosis_harness" and status["harness_fp"] == FP_A
		meta = json.loads(Path(sb.env["METADATA_FILE"]).read_text(encoding="utf-8"))
		assert meta["harness_paths"] == ["validation/a.sh", "validation/b.sh"] and meta["project_paths"] == ["src/app.py"]
		assert meta["failure_owner"] == "harness"

		Path(sb.env["STATUS_FILE"]).write_text(json.dumps(base_status), encoding="utf-8")
		sb.run(
			"""
			VALIDATION_OWNER_OWNER=weird VALIDATION_OWNER_FP=xyz VALIDATION_OWNER_STAGE=nope VALIDATION_OWNER_REASON=nope VALIDATION_OWNER_REPORT=yes
			validation_owner_merge_fields
			"""
		)
		status = json.loads(Path(sb.env["STATUS_FILE"]).read_text(encoding="utf-8"))
		assert status["failure_owner"] == "none" and status["harness_fp"] == "" and status["harness_stage"] == ""
		assert status["harness_reason"] == "" and status["harness_report"] is False
	finally:
		sb.cleanup()


def test_non_tracking_run_is_never_reported_or_read() -> None:
	sb = _sandbox()
	try:
		sb.classify_result(owner="harness", reason="sandbox_unavailable", stage="sandbox", fingerprint=FP_A)
		result = sb.run('validation_owner_fixed sandbox_unavailable "x"\necho "REPORT=${VALIDATION_OWNER_REPORT}"\n', TRACKING_ISSUE_NUM="0")
		assert "REPORT=false" in result.stdout
		assert not sb.gh_log.exists()
	finally:
		sb.cleanup()


def test_switch_off_leaves_behaviour_unchanged() -> None:
	for value, warns in (("false", False), ("FALSE", False), ("maybe", True)):
		sb = _sandbox()
		try:
			base_status = {"status": "fail", "raw_status": "harness_error", "summary": "s", "failure_summary": None, "tracking_issue": "7"}
			Path(sb.env["STATUS_FILE"]).write_text(json.dumps(base_status), encoding="utf-8")
			sb.diagnosis.write_text(json.dumps({"status": "needs_fixes", "fix_issues": [{"id": "a"}]}), encoding="utf-8")
			sb.classify_result(owner="harness", reason="owner_paths", stage="diagnose", fingerprint=FP_A)
			(sb.runtime / "validation_harness_bounced_fps.txt").write_text(FP_A + "\n", encoding="utf-8")
			result = sb.run(
				"""
				DIAG_STATUS=needs_fixes
				validation_owner_signature diagnose "x"
				validation_owner_load_bounced
				validation_owner_fixed sandbox_unavailable "x"
				validation_owner_route_diagnosis
				validation_owner_merge_fields
				printf 'DIAG=%s MARKER=[%s] BLOCK=[%s]\\n' "${DIAG_STATUS}" "$(validation_owner_comment_marker)" "$(validation_owner_ruled_out_block)"
				""",
				VALIDATION_HARNESS_HEAL_ENABLED=value,
				VALIDATE_SUPPORT_SHA=SUPPORT_SHA,
				STUB_FP=FP_A,
			)
			assert "DIAG=needs_fixes MARKER=[] BLOCK=[]" in result.stdout, result.stdout + result.stderr
			assert sb.argv() == [], sb.argv()
			assert not sb.gh_log.exists()
			assert set(json.loads(Path(sb.env["STATUS_FILE"]).read_text(encoding="utf-8"))) == set(base_status)
			assert ("reason=invalid_switch" in result.stderr) is warns, result.stderr
			assert "VALIDATION_OWNER owner=" not in result.stdout
		finally:
			sb.cleanup()


def test_fail_open_when_classifier_is_missing_or_broken() -> None:
	cases = ("missing", "rc", "garbage", "bad_owner")
	for case in cases:
		sb = _sandbox("none" if case == "missing" else "stub")
		try:
			env = {}
			if case == "rc":
				env["STUB_CLASSIFY_RC"] = "2"
			elif case == "garbage":
				sb.classify_file.write_text("not json", encoding="utf-8")
			elif case == "bad_owner":
				sb.classify_result(owner="everyone", reason="owner_paths", stage="diagnose", fingerprint=FP_A)
			if case in ("missing", "rc"):
				sb.classify_result(owner="harness", reason="owner_paths", stage="diagnose", fingerprint=FP_A)
			result = sb.run("DIAG_STATUS=needs_fixes\nvalidation_owner_route_diagnosis\necho \"DIAG=${DIAG_STATUS} OWNER=${VALIDATION_OWNER_OWNER}\"\n", **env)
			assert "DIAG=needs_fixes OWNER=none" in result.stdout, (case, result.stdout, result.stderr)
			assert "outcome=fail_open" in result.stderr, (case, result.stderr)
		finally:
			sb.cleanup()


def test_missing_fingerprint_is_computed_with_the_fingerprint_subcommand() -> None:
	_need_jq()
	sb = _sandbox()
	try:
		sb.classify_result(owner="harness", reason="canary_failed", stage="canary", fingerprint="")
		result = sb.run('validation_owner_fixed canary_failed "x"\necho "FP=${VALIDATION_OWNER_FP}"\n', STUB_FP=FP_B)
		assert f"FP={FP_B}" in result.stdout, result.stdout + result.stderr
		assert any(a[0] == "validation-failure-fingerprint" and a[a.index("--stage") + 1] == "canary" for a in sb.argv())
	finally:
		sb.cleanup()


def test_signature_uses_the_stage_log_and_is_capped() -> None:
	sb = _sandbox()
	try:
		Path(sb.env["VALIDATION_LOG_FILE"]).write_text("line one\nERROR boom\n", encoding="utf-8")
		sb.run("validation_owner_signature diagnose fallback\n", STUB_SIGNATURE="E" * 300)
		signature = (sb.runtime / "validation_harness_signature.txt").read_text(encoding="utf-8").strip()
		assert signature == "E" * 200, signature
		tail = (sb.runtime / "validation_harness_log_tail.txt").read_text(encoding="utf-8")
		assert "ERROR boom" in tail
		calls = [a[0] for a in sb.argv()]
		assert calls == ["redact-stream", "error-signature"], calls
	finally:
		sb.cleanup()


def test_engine_refusal_record_detection() -> None:
	sb = _sandbox()
	try:
		records = sb.root / "records.tsv"
		records.write_text("v1\t1\tVALIDATE\tall_gated\tcapacity\twait\tcapacity\n", encoding="utf-8")
		waited = sb.run("validation_owner_engine_refused && echo YES || echo NO\n", AI_ENGINE_FALLBACK_RECORDS_FILE=str(records))
		assert waited.stdout.strip() == "NO"
		records.write_text("v1\t1\tVALIDATE\tisolation_unavailable\tother\trefused\tcapacity\n", encoding="utf-8")
		refused = sb.run("validation_owner_engine_refused && echo YES || echo NO\n", AI_ENGINE_FALLBACK_RECORDS_FILE=str(records))
		assert refused.stdout.strip() == "YES"
		sb.classify_result(owner="harness", reason="engine_unavailable", stage="engine", fingerprint=FP_A)
		result = sb.run('validation_owner_fixed engine_unavailable "x"\necho "R=${VALIDATION_HARNESS_REASON} S=${VALIDATION_HARNESS_STAGE}"\n')
		assert "R=engine_unavailable S=engine" in result.stdout
		args = [a for a in sb.argv() if a[0] == "classify-validation-owner"][-1]
		assert args[args.index("--raw-status") + 1] == "codex_failure"
	finally:
		sb.cleanup()


def test_real_classifier_accepts_the_argv_contract() -> None:
	_need_jq()
	sb = _sandbox("real")
	try:
		result = sb.run('validation_owner_fixed sandbox_unavailable "sandbox_failed"\necho "OWNER=${VALIDATION_OWNER_OWNER} FP=${VALIDATION_OWNER_FP}"\n')
		match = re.search(r"OWNER=harness FP=([0-9a-f]{64})$", result.stdout, re.MULTILINE)
		assert match, result.stdout + result.stderr
		sb.diagnosis.write_text(json.dumps({"status": "needs_fixes", "fix_issues": [{"id": "a", "title": "t"}]}), encoding="utf-8")
		project = sb.run("DIAG_STATUS=needs_fixes\nvalidation_owner_route_diagnosis\necho \"DIAG=${DIAG_STATUS} OWNER=${VALIDATION_OWNER_OWNER}\"\n")
		assert "DIAG=needs_fixes OWNER=project" in project.stdout, project.stdout + project.stderr
	finally:
		sb.cleanup()


def test_call_sites_are_wired() -> None:
	text = _script_text()
	for reason in ("sandbox_unavailable", "preflight_failed", "canary_failed", "generate_failed"):
		assert f"validation_owner_fixed {reason} " in text, reason
	assert "validation_owner_fixed engine_unavailable" in text and "validation_owner_engine_refused" in text
	assert re.search(r"\nvalidation_owner_route_diagnosis\n\ncase \"\$\{DIAG_STATUS\}\" in\n", text)
	assert re.search(r"  validation_owner_ruled_out_block\n\} > \"\$\{DIAGNOSE_PROMPT_FILE\}\"", text)
	write_result = re.search(r"write_result_files\(\)\n\{.*?\n\}\n", text, re.DOTALL).group(0)
	assert "validation_owner_merge_fields" in write_result
	assert text.count("${HARNESS_FIXES}${HARNESS_RUN_REFERENCE}$(validation_owner_comment_marker)\"") == 2
	assert 'comment_body+="$(validation_owner_comment_marker)"' in text
	assert "VALIDATION_HARNESS_HEAL_ENABLED-true" in _block()


def test_diagnose_prompts_document_the_optional_fields() -> None:
	for path in DIAGNOSE_PROMPTS:
		text = path.read_text(encoding="utf-8")
		assert '"files": ["<consumer-relative path this fix edits>"]' in text, path
		assert '"harness_files": ["<path the harness fix touches>"]' in text, path
		assert '"harness_fixes": "string",' in text, path
		assert "fallback hint" in text, path
	contract = CONTRACT.read_text(encoding="utf-8")
	assert "harness_files" in contract
	keys = {line.split(":", 1)[0] for line in contract.splitlines() if line and not line.startswith((" ", "#", "-"))}
	assert keys <= {"required_vars", "optional_vars", "forbidden_vars"}, keys
	rendered = subprocess.run(
		["bash", "scripts/render_prompt.sh", "prompts/mode-validate-diagnose.txt"],
		cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
	)
	assert rendered.returncode == 0, rendered.stderr
	assert '"harness_files"' in rendered.stdout


def main() -> int:
	failed = 0
	for name in sorted(n for n in globals() if n.startswith("test_")):
		try:
			globals()[name]()
			print(f"  PASS  {name}")
		except SkipTest as exc:
			print(f"  SKIP  {name}: {exc}")
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
