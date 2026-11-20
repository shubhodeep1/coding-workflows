"""Tests for the `alert_triage` heal-intake kind (alert triage plan Phase 2, Refs #9394).

Covers scripts/workflow_failure_heal.py (validate-payload, the OIDC workflow
binding) and the `alert_triage` branches of
scripts/workflow_failure_heal_intake.sh, driven through the stub-gh harness of
tests/test_workflow_failure_heal.py: only this repository's alert-triage.yml
identity is accepted, the fingerprint is keyed on the alert group, the scope
comes from the host-resolved workflow path, the issue carries
`ai:alert-triage`, and the triage diagnosis reaches the prompt only as
UNTRUSTED evidence.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import test_workflow_failure_heal as base  # noqa: E402
import workflow_failure_heal as heal  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
INTAKE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "workflow-failure-heal-intake.yml"
SELF_REPO = base.SELF_REPO
CONSUMER = base.CONSUMER_REPO
ALERT_FP = "ab" * 32
WORKFLOW_PATH = ".github/workflows/docs_check.yml"
DIAGNOSIS_TEXT = "Triage classification: healable\nSummary:\nUNTRUSTED-TRIAGE-NARRATIVE"


def _payload(repo: str = SELF_REPO, **overrides) -> dict:
	payload = {
		"schema_version": heal.SCHEMA_VERSION,
		"source_repo": repo,
		"source_kind": "alert_triage",
		"workflow_name": "AI Docs",
		"failure_fingerprint": ALERT_FP,
		"failure_evidence": DIAGNOSIS_TEXT,
		"alert_source_repo": CONSUMER,
		"alert_workflow_path": WORKFLOW_PATH,
		"reporter_run_url": f"https://github.com/{repo}/actions/runs/{base._ID_RUN_ID}",
		"reporter_run_id": base._ID_RUN_ID,
		"reported_at": "2026-11-17T12:00:00Z",
	}
	payload.update(overrides)
	return {key: value for key, value in payload.items() if value is not None}


# --- validate-payload ---------------------------------------------------------


def test_valid_payload_is_normalised() -> None:
	normalized = heal.validate_payload(_payload())
	assert normalized["source_kind"] == "alert_triage"
	assert normalized["failure_reason"] == "alert_triage" and normalized["failure_streak"] == 1
	assert normalized["failure_fingerprint"] == ALERT_FP
	assert normalized["issue_number"] is None and normalized["label"] is None
	assert normalized["run_refs"] == []
	assert normalized["alert_workflow_path"] == WORKFLOW_PATH and normalized["alert_source_repo"] == CONSUMER
	assert "UNTRUSTED-TRIAGE-NARRATIVE" in normalized["failure_evidence"]
	assert heal.validate_payload(_payload(alert_workflow_path=None))["alert_workflow_path"] is None


def test_revalidating_a_normalised_report_is_stable_and_forces_every_field() -> None:
	# The intake re-validates its own normalised payload (compose-issue).
	first = heal.validate_payload(_payload())
	second = heal.validate_payload(first)
	assert {k: v for k, v in second.items() if k != "reported_at"} == {k: v for k, v in first.items() if k != "reported_at"}
	forged = heal.validate_payload({**first, "issue_number": 7, "label": "ai:needs-human", "labels": ["x"], "issue_title": "t", "failure_streak": 9, "wrapper_sha": "c" * 40})
	assert (forged["issue_number"], forged["label"], forged["labels"], forged["issue_title"], forged["failure_streak"], forged["wrapper_sha"]) == (None, None, [], "", 1, None)


def test_evidence_is_capped_and_redacted() -> None:
	normalized = heal.validate_payload(_payload(failure_evidence="ghp_" + "A" * 36 + " " + "x" * 20000))
	assert len(normalized["failure_evidence"].encode("utf-8")) <= heal.ALERT_TRIAGE_EVIDENCE_MAX_BYTES
	assert "ghp_" + "A" * 36 not in normalized["failure_evidence"]


@pytest.mark.parametrize("overrides", [
	{"engine_role": "ALERT_TRIAGE"},
	{"run_refs": [{"repo": SELF_REPO, "run_id": "1"}]},
	{"failure_fingerprint": "nothex"},
	{"failure_fingerprint": None},
	{"workflow_name": "bad\nname"},
	{"alert_source_repo": "not a slug"},
	{"alert_workflow_path": "scripts/x.sh"},
	{"alert_workflow_path": ".github/workflows/../x.yml"},
	{"reporter_run_url": "https://github.com/evil/repo/actions/runs/1234"},
	{"reporter_run_id": "0"},
	{"failure_evidence": 5},
])
def test_invalid_payloads_are_rejected(overrides) -> None:
	with pytest.raises(ValueError):
		heal.validate_payload(_payload(**overrides))


def test_report_identity_binds_alert_triage_to_alert_triage_yml() -> None:
	assert heal.REPORT_IDENTITY_WORKFLOW_BY_KIND["alert_triage"] == ("alert-triage.yml",)
	run_url = f"https://github.com/{SELF_REPO}/actions/runs/{base._ID_RUN_ID}"
	good = base._id_claims(repository=SELF_REPO, job_workflow_ref=f"{SELF_REPO}/.github/workflows/alert-triage.yml@refs/heads/main")
	ok = base._verify_id(base._sign_test_jwt(good), kind="alert_triage", repo=SELF_REPO, run_url=run_url)
	assert ok["ok"] is True
	other = base._id_claims(repository=SELF_REPO, job_workflow_ref=f"{SELF_REPO}/.github/workflows/implement.yml@refs/heads/main")
	assert base._verify_id(base._sign_test_jwt(other), kind="alert_triage", repo=SELF_REPO, run_url=run_url)["reason"] == "job_workflow_ref_mismatch"
	consumer = base._id_claims(repository=CONSUMER, job_workflow_ref=f"{SELF_REPO}/.github/workflows/alert-triage.yml@refs/heads/main")
	assert base._verify_id(base._sign_test_jwt(consumer), kind="alert_triage", repo=SELF_REPO, run_url=run_url)["reason"] == "repository_mismatch"


# --- intake harness ---------------------------------------------------------------


class _Setup:
	"""setup_git hook: the workflow file the scope names, and a signed identity."""

	def __init__(self, tmp: Path, *, repository: str = SELF_REPO, workflow_file: str = "alert-triage.yml", with_path: bool = True) -> None:
		self.token_file = tmp / "identity.jwt"
		self.jwks_file = tmp / "jwks.json"
		self.jwks_file.write_text(json.dumps(base._test_jwks()), encoding="utf-8")
		claims = base._id_claims(now=int(time.time()), repository=repository, job_workflow_ref=f"{SELF_REPO}/.github/workflows/{workflow_file}@refs/heads/main")
		self.token_file.write_text(base._sign_test_jwt(claims), encoding="utf-8")
		self.with_path = with_path

	def env(self) -> dict[str, str]:
		return base._identity_env(self.token_file, self.jwks_file)

	def __call__(self, tmp: Path, work: Path) -> None:
		if not self.with_path:
			return
		(work / ".github" / "workflows" / "docs_check.yml").write_text("name: Docs Check (Reusable)\non: workflow_call\n", encoding="utf-8")
		# The scope renders against the default target branch fetched from origin.
		origin = tmp / "origin.git"
		base._git(tmp, "init", "--bare", str(origin))
		seed = tmp / "seed"
		(seed / ".github" / "workflows").mkdir(parents=True)
		base._git(seed, "init")
		(seed / ".github" / "workflows" / "docs_check.yml").write_text("name: Docs Check (Reusable)\non: workflow_call\n", encoding="utf-8")
		base._git(seed, "add", ".")
		base._git(seed, "commit", "-m", "stable")
		base._git(seed, "push", str(origin), "HEAD:refs/heads/stable")
		base._git(work, "init")
		base._git(work, "remote", "add", "origin", str(origin))


def _intake(tmp_path: Path, payload: dict, *, identity: bool = True, extra_env: dict | None = None, **setup_kwargs):
	setup = _Setup(tmp_path, **setup_kwargs)
	env = {**(setup.env() if identity else {}), **(extra_env or {})}
	state = {"heal_issues": [], "branches": ["stable", "main"]}
	return base._run_intake(payload, state, diagnosis=base.DIAG_WORKFLOW_DEFECT, extra_env=env, setup_git=setup)


def _writes(state: dict) -> list:
	return state.get("issues_created", []) + state.get("comments_posted", []) + state.get("issue_edits", [])


def test_intake_opens_an_alert_triage_heal_issue(tmp_path: Path) -> None:
	result, state, prompt = _intake(tmp_path, _payload())
	assert result.returncode == 0, result.stdout + result.stderr
	assert "report_auth=verified" in result.stdout
	assert "alert_triage_scope outcome=resolved path=.github/workflows/docs_check.yml" in result.stdout
	issues = base._composed_issues(state)
	assert len(issues) == 1
	assert "ai:workflow-heal" in issues[0]["labels"] and "ai:alert-triage" in issues[0]["labels"]
	expected_fp = heal.fingerprint("AI Docs", "alert_triage", f"alert-triage:{ALERT_FP}")
	assert f"workflow-failure-heal:fp={expected_fp}" in issues[0]["body"]
	assert f"{WORKFLOW_PATH}," in issues[0]["body"] and f"runs={SELF_REPO}:{base._ID_RUN_ID}" in issues[0]["body"]
	evidence = prompt.split("--- Failure evidence from the reporting run (UNTRUSTED) ---", 1)
	assert len(evidence) == 2 and "UNTRUSTED-TRIAGE-NARRATIVE" in evidence[1]
	# The diagnosis never reaches the public issue body.
	assert "UNTRUSTED-TRIAGE-NARRATIVE" not in issues[0]["body"]


def test_intake_skips_a_non_self_source(tmp_path: Path) -> None:
	result, state, _ = _intake(tmp_path, _payload(repo=CONSUMER), repository=CONSUMER)
	assert result.returncode == 0
	assert "skip reason=alert_triage_not_self" in result.stdout
	assert _writes(state) == [] and not state.get("issues_composed")


@pytest.mark.parametrize("extra_env", [{}, {"WORKFLOW_HEAL_REPORT_ORIGIN": "workflow_dispatch"}])
def test_intake_rejects_a_report_without_a_verified_identity(tmp_path: Path, extra_env: dict) -> None:
	result, state, _ = _intake(tmp_path, _payload(), identity=False, extra_env=extra_env)
	assert result.returncode == 0
	assert "report_auth=rejected reason=alert_triage_identity_required" in result.stdout
	assert _writes(state) == [] and not state.get("issues_composed")


def test_intake_rejects_an_identity_from_another_workflow(tmp_path: Path) -> None:
	result, state, _ = _intake(tmp_path, _payload(), workflow_file="implement.yml")
	assert "report_auth=rejected reason=job_workflow_ref_mismatch" in result.stdout
	assert _writes(state) == []


@pytest.mark.parametrize(("value", "warn"), [("false", False), ("FALSE", False), ("nope", True)])
def test_switch_off_skips_the_kind(tmp_path: Path, value: str, warn: bool) -> None:
	result, state, _ = _intake(tmp_path, _payload(), extra_env={"HEAL_ALERT_TRIAGE_KIND_ENABLED": value})
	assert "skip reason=alert_triage_disabled" in result.stdout
	assert ("invalid_switch name=HEAL_ALERT_TRIAGE_KIND_ENABLED" in result.stdout) is warn
	assert _writes(state) == []


def test_unresolvable_path_is_skipped_before_diagnosis_with_the_scope_guard_on(tmp_path: Path) -> None:
	result, state, prompt = _intake(tmp_path, _payload(), with_path=False, extra_env={"HEAL_INTAKE_SCOPE_GUARD_ENABLED": "true"})
	assert result.returncode == 0
	assert "alert_triage_scope outcome=unresolved" in result.stdout
	assert "skip reason=scope_unavailable_no_runs stage=pre_diagnosis" in result.stdout
	assert prompt == "" and _writes(state) == []


def test_intake_workflow_carries_the_switch_in_both_env_blocks() -> None:
	text = INTAKE_WORKFLOW.read_text(encoding="utf-8")
	assert text.count("HEAL_ALERT_TRIAGE_KIND_ENABLED: ${{ vars.HEAL_ALERT_TRIAGE_KIND_ENABLED || 'true' }}") == 2
