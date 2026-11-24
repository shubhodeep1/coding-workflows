#!/usr/bin/env python3
"""Static contract of the droplet boot script and the scaler's deployment files (plan Phase 2)."""

from __future__ import annotations

import re

try:
	import yaml
except ImportError:  # pragma: no cover - CI installs PyYAML; the offline pre-push gate container has none
	yaml = None

from runner_scaler_fakes import BOOT_SCRIPT, SCALER_ROOT
from runner_scaler.config import Config, from_env, repo_slug
from runner_scaler.reconcile import JIT_PLACEHOLDER

TEXT = BOOT_SCRIPT.read_text(encoding="utf-8")


def _fallback_scalar(raw: str) -> object:
	raw = raw.strip()
	if raw in ("true", "false"):
		return raw == "true"
	if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
		return raw[1:-1]
	return raw


def _load_app_spec_service(text: str) -> dict:
	"""Return services[0] of app.yaml, parsed by PyYAML when it is installed.

	Without PyYAML, a minimal parser reads the same block-style layout so the
	assertions below still check the parsed contract instead of erroring.
	"""
	if yaml is not None:
		return yaml.safe_load(text)["services"][0]
	service: dict = {}
	section: str | None = None
	item: dict | None = None
	for line in text.splitlines():
		if not line.strip() or line.lstrip().startswith("#"):
			continue
		indent = len(line) - len(line.lstrip(" "))
		body = line.strip()
		if indent < 4:
			if service and indent <= 2 and not body.startswith("- "):
				break
			if body.startswith("- "):
				body = body[2:]
				indent = 4
			else:
				continue
		key, _, value = body.partition(":")
		if indent == 4:
			section, item = None, None
			if value.strip():
				service[key] = _fallback_scalar(value)
			else:
				section = key
				service[key] = [] if key == "envs" else {}
		elif section == "envs":
			if key.startswith("- "):
				item = {}
				service["envs"].append(item)
				key = key[2:]
			assert item is not None
			item[key] = _fallback_scalar(value)
		elif section is not None:
			service[section][key] = _fallback_scalar(value)
	return service


def test_runner_release_is_pinned_and_checksummed() -> None:
	version = re.search(r'^RUNNER_VERSION="(\d+\.\d+\.\d+)"$', TEXT, re.MULTILINE)
	digest = re.search(r'^RUNNER_SHA256="([0-9a-f]{64})"$', TEXT, re.MULTILINE)
	assert version and digest
	assert "sha256sum -c" in TEXT
	assert "actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" in TEXT


def test_placeholder_appears_exactly_once_and_is_validated_before_use() -> None:
	assert TEXT.count(JIT_PLACEHOLDER) == 1
	assert "fail_boot jit_config_invalid" in TEXT


def test_self_check_runs_before_the_runner_and_powers_off_on_failure() -> None:
	self_check = TEXT.index("\tself_check\n")
	run = TEXT.index("./run.sh --jitconfig")
	assert self_check < run
	fail_boot = TEXT[TEXT.index("fail_boot()"):TEXT.index("retry()")]
	assert "shutdown -h now" in fail_boot
	for command in ("docker", "gh", "git", "jq", "shellcheck", "python3"):
		assert re.search(rf"SELF_CHECK_COMMANDS=\([^)]*\b{command}\b", TEXT), command
	assert 'docker info' in TEXT and "python3 -m venv" in TEXT
	assert "hello-world" not in TEXT  # no Docker Hub pull in the self-check
	assert TEXT.count("DPkg::Lock::Timeout=300") == 4


def test_runner_runs_unprivileged_and_the_droplet_holds_no_scaler_credential() -> None:
	assert 'sudo -u "${RUNNER_USER}" -H bash -c' in TEXT
	assert 'RUNNER_USER="runner"' in TEXT
	for secret in ("RUNNER_SCALER_DO_TOKEN", "RUNNER_SCALER_GH_APP_PRIVATE_KEY", "RUNNER_SCALER_WEBHOOK_SECRET", "DIGITALOCEAN_ACCESS_TOKEN", "GH_TOKEN", "GH_PAT"):
		assert secret not in TEXT


def test_script_uses_tabs_for_indentation() -> None:
	assert not re.search(r"^ {2,}\S", TEXT, re.MULTILINE)


def test_app_spec_builds_from_stable_with_health_check_and_secret_placeholders() -> None:
	service = _load_app_spec_service((SCALER_ROOT / "app.yaml").read_text(encoding="utf-8"))
	assert service["github"] == {"repo": "shubhodeep1/coding-workflows", "branch": "stable", "deploy_on_push": True}
	assert service["source_dir"] == "tools/runner_scaler"
	assert service["dockerfile_path"] == "tools/runner_scaler/Dockerfile"
	assert service["health_check"]["http_path"] == "/healthz"
	envs = {item["key"]: item for item in service["envs"]}
	assert envs["RUNNER_SCALER_DRY_RUN"]["value"] == "true"  # first deploy is a dry run (rollout R3)
	assert envs["RUNNER_SCALER_REPOS"]["value"] == ""
	assert envs["RUNNER_SCALER_VPC_UUID"]["value"] == ""  # set at rollout to the dedicated runner VPC
	for secret in ("RUNNER_SCALER_DO_TOKEN", "RUNNER_SCALER_GH_APP_ID", "RUNNER_SCALER_GH_APP_PRIVATE_KEY", "RUNNER_SCALER_WEBHOOK_SECRET", "TG_BOT_SECRET"):
		assert envs[secret]["type"] == "SECRET" and "value" not in envs[secret]


def test_dockerfile_runs_the_package_as_nobody() -> None:
	text = (SCALER_ROOT / "Dockerfile").read_text(encoding="utf-8")
	assert "USER nobody" in text and 'CMD ["python", "-m", "runner_scaler"]' in text
	assert "COPY bootstrap /app/bootstrap" in text
	pins = (SCALER_ROOT / "requirements.txt").read_text(encoding="utf-8")
	assert all("==" in line for line in pins.splitlines() if line and not line.startswith("#"))


def test_config_defaults_match_the_plan() -> None:
	config = from_env({})
	assert config == Config()
	assert (config.label, config.droplet_size, config.region, config.image) == ("do-runner", "s-4vcpu-8gb", "nyc3", "ubuntu-26-04-x64")
	assert (config.max_droplets, config.reconcile_seconds, config.full_scan_seconds) == (60, 120, 600)
	assert (config.register_timeout_seconds, config.max_lifetime_minutes, config.queue_alert_minutes) == (900, 300, 10)
	assert not config.active and config.rerun_enabled and not config.dry_run


def test_config_rejects_bad_values_without_failing() -> None:
	lines: list[tuple[str, dict]] = []
	config = from_env(
		{
			"RUNNER_SCALER_REPOS": "shubhodeep1/digital_pa, not a repo,shubhodeep1/bitsafe.io shubhodeep1/digital_pa",
			"RUNNER_SCALER_MAX_DROPLETS": "-3",
			"RUNNER_SCALER_DRY_RUN": "maybe",
			"RUNNER_SCALER_DROPLET_SIZE": "s-4vcpu-8gb; rm",
			"RUNNER_SCALER_GH_APP_PRIVATE_KEY": "line1\\nline2",
		},
		log=lambda outcome, **fields: lines.append((outcome, fields)),
	)
	assert config.repos == ("shubhodeep1/digital_pa", "shubhodeep1/bitsafe.io")
	assert config.max_droplets == 60 and config.dry_run is False and config.droplet_size == "s-4vcpu-8gb"
	assert config.gh_app_private_key == "line1\nline2"
	assert {fields["key"] for outcome, fields in lines if outcome == "config_invalid"} >= {"RUNNER_SCALER_REPOS", "RUNNER_SCALER_MAX_DROPLETS", "RUNNER_SCALER_DRY_RUN", "RUNNER_SCALER_DROPLET_SIZE"}
	assert config.missing_credentials() == ["RUNNER_SCALER_DO_TOKEN", "RUNNER_SCALER_GH_APP_ID", "RUNNER_SCALER_WEBHOOK_SECRET"]
	assert config.missing_required()[-1] == "RUNNER_SCALER_VPC_UUID"


def test_config_never_serves_coding_workflows_and_validates_the_vpc() -> None:
	lines: list[tuple[str, dict]] = []
	config = from_env(
		{"RUNNER_SCALER_REPOS": "shubhodeep1/Coding-Workflows shubhodeep1/digital_pa", "RUNNER_SCALER_VPC_UUID": "not-a-uuid"},
		log=lambda outcome, **fields: lines.append((outcome, fields)),
	)
	assert config.repos == ("shubhodeep1/digital_pa",)
	assert config.vpc_uuid == ""
	reasons = {fields.get("reason") for outcome, fields in lines if outcome == "config_invalid"}
	assert "excluded_repo" in reasons
	assert from_env({"RUNNER_SCALER_VPC_UUID": "5a4f0c1e-0000-4000-8000-00000000abcd"}).vpc_uuid == "5a4f0c1e-0000-4000-8000-00000000abcd"


def test_repo_slugs_are_droplet_and_tag_safe() -> None:
	assert repo_slug("shubhodeep1/bitsafe.io") == "bitsafe-io"
	assert repo_slug("shubhodeep1/digital_pa") == "digital-pa"
	assert repo_slug("o/" + "x" * 90) == "x" * 40
	assert re.fullmatch(r"[a-z0-9-]+", repo_slug("o/--Weird__Name.."))
