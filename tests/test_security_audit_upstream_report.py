"""Tests for scripts/security_audit_upstream_report.sh (issue #7623, project #7604 Phase 2).

The sender reads the security audit's upstream-managed export and dispatches one
``synced_security_finding`` heal report to coding-workflows. A fake ``gh`` on
PATH records the dispatch body; a stub ``workflow_failure_heal.py`` wraps the
real module and answers ``request-report-identity`` without the Actions OIDC
endpoint.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "security_audit_upstream_report.sh"
REAL_HEAL_PY = REPO_ROOT / "scripts" / "workflow_failure_heal.py"
CONSUMER = "acme/widgets"
SHA = "0123456789abcdef0123456789abcdef01234567"
RUN_ID = "4242"
PAYLOAD_KEYS = {"schema_version", "source_repo", "source_kind", "wrapper_sha", "reporter_run_url", "reporter_run_id", "run_refs", "findings"}
FINDING_KEYS = {"file", "line", "category", "severity", "confidence", "finding_id"}

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import workflow_failure_heal as heal  # noqa: E402

STUB_HEAL = f'''
# Test stub for workflow_failure_heal.py: knows synced_security_finding and
# request-report-identity, delegates everything else to the real module.
import importlib.util
import os
import sys

_spec = importlib.util.spec_from_file_location("_real_heal", {str(REAL_HEAL_PY)!r})
_real = importlib.util.module_from_spec(_spec)
sys.modules["_real_heal"] = _real
_spec.loader.exec_module(_real)
globals().update({{k: v for k, v in vars(_real).items() if not k.startswith("__")}})

if __name__ == "__main__":
	if len(sys.argv) > 1 and sys.argv[1] == "request-report-identity":
		if os.environ.get("STUB_IDENTITY", "1") != "1":
			print("identity=absent reason=oidc_unavailable")
			sys.exit(0)
		out = sys.argv[sys.argv.index("--out") + 1]
		with open(out, "w") as handle:
			handle.write("aaa.bbb.ccc")
		print("identity=attached")
		sys.exit(0)
	sys.exit(_real.main())
'''

FAKE_GH = '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_GH_LOG}"
prev=""
for arg in "$@"; do
	if [ "${prev}" = "--input" ]; then
		cp "${arg}" "${FAKE_GH_BODY}"
	fi
	prev="${arg}"
done
if [ "${FAKE_GH_FAIL:-0}" = "1" ]; then
	echo "HTTP 403: Resource not accessible" >&2
	exit 1
fi
exit 0
'''


def _finding(index: int = 0, **overrides) -> dict:
	finding = {
		"finding_id": f"guard-bypass-{index}",
		"owasp_or_stride_category": "A01:2021-Broken Access Control",
		"severity": "high",
		"confidence": 9,
		"file": ".claude/hooks/pr_merge_status_guard.py",
		"line": 30 + index,
		"exploit_scenario": "SECRET-SCENARIO-TEXT",
		"recommendation": "SECRET-RECOMMENDATION-TEXT",
	}
	finding.update(overrides)
	return finding


@pytest.fixture
def env_setup(tmp_path: Path):
	heal_dir = tmp_path / "heal"
	heal_dir.mkdir()
	(heal_dir / "workflow_failure_heal.py").write_text(STUB_HEAL, encoding="utf-8")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
	runner_temp = tmp_path / "runner"
	runner_temp.mkdir()
	env = {
		"PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
		"HOME": str(tmp_path),
		"GITHUB_REPOSITORY": CONSUMER,
		"GITHUB_RUN_ID": RUN_ID,
		"GITHUB_SERVER_URL": "https://github.com",
		"GH_TOKEN": "fake-token",
		"RUNNER_TEMP": str(runner_temp),
		"REPORT_WRAPPER_SHA": SHA,
		"WORKFLOW_JOB_JSON": json.dumps({"workflow_ref": f"shubhodeep1/coding-workflows/.github/workflows/security-audit.yml@{SHA}"}),
		"WORKFLOW_HEAL_PY": str(heal_dir / "workflow_failure_heal.py"),
		"FAKE_GH_LOG": str(tmp_path / "gh.log"),
		"FAKE_GH_BODY": str(tmp_path / "body.json"),
		"GH_RETRY_MAX_ATTEMPTS": "1",
		"GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
	}
	return tmp_path, env


def _write_export(env: dict, findings: list) -> Path:
	path = Path(env["RUNNER_TEMP"]) / "security-audit-upstream-managed.json"
	path.write_text(json.dumps({"upstream_managed": findings}), encoding="utf-8")
	return path


def _run(env: dict, **extra) -> subprocess.CompletedProcess:
	merged = dict(env)
	merged.update(extra)
	return subprocess.run(["bash", str(SCRIPT)], env=merged, capture_output=True, text=True, timeout=120)


def _dispatched(tmp_path: Path) -> bool:
	log = tmp_path / "gh.log"
	return log.exists() and "dispatches" in log.read_text(encoding="utf-8")


def _body(tmp_path: Path) -> dict:
	return json.loads((tmp_path / "body.json").read_text(encoding="utf-8"))


def test_switch_false_skips_without_any_gh_call(env_setup) -> None:
	tmp_path, env = env_setup
	_write_export(env, [_finding()])
	result = _run(env, SECURITY_AUDIT_UPSTREAM_REPORT_ENABLED="FALSE")
	assert result.returncode == 0
	assert "SECURITY_AUDIT_UPSTREAM_REPORT skip reason=disabled" in result.stdout
	assert not (tmp_path / "gh.log").exists()


def test_invalid_switch_is_off_with_a_warning_and_no_value_echo(env_setup) -> None:
	tmp_path, env = env_setup
	_write_export(env, [_finding()])
	result = _run(env, SECURITY_AUDIT_UPSTREAM_REPORT_ENABLED="maybe-XYZZY")
	assert result.returncode == 0
	assert "skip reason=invalid_switch" in result.stdout
	assert "::warning::" in result.stdout
	assert "XYZZY" not in result.stdout + result.stderr
	assert not _dispatched(tmp_path)


@pytest.mark.parametrize(
	("extra", "reason"),
	[
		({"WORKFLOW_HEAL_ENABLED": "false"}, "workflow_heal_disabled"),
		({"CLAUDE_CAPACITY_WAIT": "true"}, "capacity_wait"),
		({"GITHUB_REPOSITORY": "shubhodeep1/coding-workflows"}, "self_repo"),
		({"GH_TOKEN": ""}, "token_missing"),
		({"WORKFLOW_JOB_JSON": json.dumps({"workflow_ref": "shubhodeep1/coding-workflows/.github/workflows/security-audit.yml@refs/tags/stable"})}, "wrapper_not_pinned"),
		({"WORKFLOW_JOB_JSON": json.dumps({"workflow_ref": "shubhodeep1/coding-workflows/.github/workflows/security-audit.yml@" + "f" * 40})}, "wrapper_not_pinned"),
		({"REPORT_WRAPPER_SHA": "stable"}, "wrapper_not_pinned"),
	],
)
def test_gates_skip_without_dispatch(env_setup, extra, reason) -> None:
	tmp_path, env = env_setup
	_write_export(env, [_finding()])
	result = _run(env, **extra)
	assert result.returncode == 0
	assert f"skip reason={reason}" in result.stdout
	assert not _dispatched(tmp_path)


def test_missing_export_is_no_summary(env_setup) -> None:
	tmp_path, env = env_setup
	result = _run(env)
	assert result.returncode == 0
	assert "skip reason=no_summary" in result.stdout
	assert not _dispatched(tmp_path)


def test_symlinked_export_is_refused(env_setup) -> None:
	tmp_path, env = env_setup
	real = tmp_path / "real.json"
	real.write_text(json.dumps({"upstream_managed": [_finding()]}), encoding="utf-8")
	(Path(env["RUNNER_TEMP"]) / "security-audit-upstream-managed.json").symlink_to(real)
	result = _run(env)
	assert "skip reason=export_symlink" in result.stdout
	assert not _dispatched(tmp_path)


def test_missing_helper_skips(env_setup) -> None:
	tmp_path, env = env_setup
	_write_export(env, [_finding()])
	result = _run(env, WORKFLOW_HEAL_PY=str(tmp_path / "absent.py"))
	assert "skip reason=helper_missing" in result.stdout
	assert not _dispatched(tmp_path)


def test_medium_and_low_findings_report_nothing(env_setup) -> None:
	tmp_path, env = env_setup
	_write_export(env, [_finding(0, severity="medium"), _finding(1, severity="low")])
	result = _run(env)
	assert result.returncode == 0
	assert "SECURITY_AUDIT_UPSTREAM_REPORT outcome=none findings=0" in result.stdout
	assert not _dispatched(tmp_path)


def test_qualifying_findings_send_one_dispatch_with_six_fields(env_setup) -> None:
	tmp_path, env = env_setup
	_write_export(env, [_finding(0), _finding(1, severity="CRITICAL"), _finding(2)])
	result = _run(env)
	assert result.returncode == 0, result.stderr
	assert "SECURITY_AUDIT_UPSTREAM_REPORT outcome=dispatched findings=3" in result.stdout
	calls = (tmp_path / "gh.log").read_text(encoding="utf-8").splitlines()
	assert len([c for c in calls if "dispatches" in c]) == 1
	assert "repos/shubhodeep1/coding-workflows/dispatches" in calls[0]
	body = _body(tmp_path)
	assert body["event_type"] == heal.DISPATCH_EVENT_TYPE
	client = body["client_payload"]
	assert client["report_identity"] == "aaa.bbb.ccc"
	report = client["report"]
	assert set(report) == PAYLOAD_KEYS
	assert set(report) <= heal.SYNCED_SECURITY_PAYLOAD_KEYS
	assert report["source_kind"] == "synced_security_finding"
	assert report["source_repo"] == CONSUMER
	assert report["wrapper_sha"] == SHA
	assert report["run_refs"] == []
	assert report["reporter_run_id"] == RUN_ID
	assert report["reporter_run_url"] == f"https://github.com/{CONSUMER}/actions/runs/{RUN_ID}"
	assert report["findings"][0]["severity"] == "critical"
	for finding in report["findings"]:
		assert set(finding) == FINDING_KEYS
	raw = (tmp_path / "body.json").read_text(encoding="utf-8")
	for forbidden in ("exploit_scenario", "recommendation", "SECRET-SCENARIO-TEXT", "SECRET-RECOMMENDATION-TEXT"):
		assert forbidden not in raw
	# The report is accepted by the intake's own validator.
	heal.validate_payload(report)


def test_cap_at_fifty(env_setup) -> None:
	tmp_path, env = env_setup
	_write_export(env, [_finding(i) for i in range(60)])
	result = _run(env)
	assert "outcome=dispatched findings=50" in result.stdout
	assert "capped=10" in result.stdout
	assert len(_body(tmp_path)["client_payload"]["report"]["findings"]) == 50


def test_dedupe_by_location_and_by_finding_id(env_setup) -> None:
	tmp_path, env = env_setup
	_write_export(env, [
		_finding(0),
		_finding(0, finding_id="other-id"),
		_finding(5, finding_id="guard-bypass-0"),
		_finding(7),
	])
	result = _run(env)
	assert "findings=2" in result.stdout and "deduped=2" in result.stdout
	ids = [f["finding_id"] for f in _body(tmp_path)["client_payload"]["report"]["findings"]]
	assert sorted(ids) == ["guard-bypass-0", "guard-bypass-7"]


def test_hostile_text_is_dropped_and_never_printed(env_setup) -> None:
	tmp_path, env = env_setup
	hostile = [
		_finding(1, owasp_or_stride_category="::error::pwned $(id)"),
		_finding(2, owasp_or_stride_category="A01:2021\n::error::x"),
		_finding(3, owasp_or_stride_category="`touch /tmp/x`"),
		_finding(4, file="../../etc/passwd"),
		_finding(5, file="a/$(id).py"),
		_finding(6, finding_id="ghp_FAKETOKEN0123456789 ::error::"),
		_finding(7, exploit_scenario="::error::HOSTILE-SCENARIO ghp_FAKETOKEN0123456789\n$(rm -rf /)"),
	]
	_write_export(env, hostile)
	result = _run(env)
	assert result.returncode == 0
	output = result.stdout + result.stderr
	for needle in ("pwned", "::error::", "touch /tmp", "etc/passwd", "$(id)", "ghp_FAKETOKEN", "HOSTILE-SCENARIO", "rm -rf"):
		assert needle not in output
	assert "outcome=dispatched findings=1" in result.stdout
	report = _body(tmp_path)["client_payload"]["report"]
	assert [f["finding_id"] for f in report["findings"]] == ["guard-bypass-7"]
	raw = (tmp_path / "body.json").read_text(encoding="utf-8")
	assert "HOSTILE-SCENARIO" not in raw and "ghp_FAKETOKEN" not in raw


def test_trailing_newline_in_file_or_finding_id_is_dropped(env_setup) -> None:
	tmp_path, env = env_setup
	_write_export(env, [
		_finding(0, file=".claude/hooks/pr_merge_status_guard.py\n"),
		_finding(1, finding_id="guard-bypass-1\n"),
		_finding(2),
	])
	result = _run(env)
	assert "outcome=dispatched findings=1" in result.stdout and "dropped=2" in result.stdout
	report = _body(tmp_path)["client_payload"]["report"]
	assert [f["finding_id"] for f in report["findings"]] == ["guard-bypass-2"]


def test_unmappable_category_is_dropped_not_the_whole_report(env_setup) -> None:
	tmp_path, env = env_setup
	_write_export(env, [_finding(0, owasp_or_stride_category="Made Up Category"), _finding(1)])
	result = _run(env)
	assert "outcome=dispatched findings=1" in result.stdout and "dropped=1" in result.stdout


def test_no_identity_sends_nothing(env_setup) -> None:
	tmp_path, env = env_setup
	_write_export(env, [_finding()])
	result = _run(env, STUB_IDENTITY="0")
	assert "skip reason=identity_unavailable" in result.stdout
	assert not _dispatched(tmp_path)


def test_dispatch_failure_is_logged_and_exits_zero(env_setup) -> None:
	tmp_path, env = env_setup
	_write_export(env, [_finding()])
	result = _run(env, FAKE_GH_FAIL="1")
	assert result.returncode == 0
	assert "outcome=failed reason=dispatch_denied findings=1" in result.stdout
	assert "::warning::" in result.stdout
	# The report is one shot: incremental audits do not re-report an
	# unchanged synced file, so the warning must not promise a retry.
	assert "The report is not retried" in result.stdout
	assert "next audit retries" not in result.stdout


def test_script_is_executable_and_logs_only_its_prefix(env_setup) -> None:
	assert os.access(SCRIPT, os.X_OK)
	tmp_path, env = env_setup
	_write_export(env, [_finding()])
	result = _run(env)
	for line in result.stdout.splitlines():
		if line.startswith("::"):
			continue
		assert line.startswith("SECURITY_AUDIT_UPSTREAM_REPORT "), line
