#!/usr/bin/env python3
"""Regression guard: clarify's sandbox base image does not come from Docker Hub.

Reported by workflow failure heal #6985 (clarify runs 37990077414,
37990177613, 37990757342, 37990879005 and 37992097499):
scripts/clarify_isolated_run.sh builds scripts/clarify_sandbox/Dockerfile,
whose ``FROM node:22.16.0-bookworm-slim`` is an anonymous Docker Hub pull on
a fresh runner. Docker Hub answered

  failed to resolve source metadata for docker.io/library/node:...:
  unexpected status from HEAD request ...: 429 Too Many Requests

on the Claude attempt and both Codex attempts, so the clarify run failed and
retrying could never help. The fix is the "Pre-seed clarify sandbox base
image" step in .github/workflows/clarify.yml: it loads the same tag from a
Docker Official Images mirror and tags it under its Docker Hub name, so the
unchanged ``docker build`` (no --pull) finds the image locally.

The behavioural tests run the step's real ``run:`` body against a fake
``docker`` whose Docker Hub access always fails with 429, as it did in the
reported runs.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

try:
	import yaml
except ImportError:  # pragma: no cover - CI installs PyYAML; offline fallback below
	yaml = None


REPO_ROOT = Path(__file__).resolve().parent.parent
CLARIFY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "clarify.yml"
SANDBOX_DOCKERFILE = REPO_ROOT / "scripts" / "clarify_sandbox" / "Dockerfile"
STEP_NAME = "Pre-seed clarify sandbox base image"
SUPPORTED_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*:[A-Za-z0-9._-]{1,128}$")
DEFAULT_MIRRORS = "public.ecr.aws/docker/library mirror.gcr.io/library"

# A fake docker CLI with a file-backed image store. Pulls succeed only for
# references under FAKE_DOCKER_GOOD_MIRRORS; everything else, and a build
# whose FROM image is not stored locally, fails with Docker Hub's 429.
FAKE_DOCKER = r"""#!/usr/bin/env bash
store="${FAKE_DOCKER_STORE:?}"
log="${FAKE_DOCKER_LOG:?}"
printf '%s\n' "$*" >> "${log}"
key() { printf '%s' "$1" | sha256sum | cut -c1-40; }
hub_429() {
	echo "ERROR: failed to resolve source metadata for docker.io/library/$1: unexpected status from HEAD request: 429 Too Many Requests" >&2
	exit 1
}
case "$1" in
	image)
		[ "$2" = inspect ] || exit 2
		[ -f "${store}/$(key "$3")" ]
		exit $?
		;;
	pull)
		shift
		[ "$1" = --quiet ] && shift
		ref="$1"
		for mirror in ${FAKE_DOCKER_GOOD_MIRRORS:-}; do
			case "${ref}" in
				"${mirror}"/*) touch "${store}/$(key "${ref}")"; echo "sha256:pulled"; exit 0 ;;
			esac
		done
		echo "HUB_OR_BAD_PULL ${ref}" >> "${log}"
		hub_429 "${ref}"
		;;
	tag)
		[ -f "${store}/$(key "$2")" ] || exit 1
		touch "${store}/$(key "$3")"
		exit 0
		;;
	build)
		dockerfile=""
		while [ "$#" -gt 0 ]; do
			if [ "$1" = -f ]; then dockerfile="$2"; shift; fi
			shift
		done
		base="$(awk 'toupper($1) == "FROM" { print $2; exit }' "${dockerfile}")"
		if [ -f "${store}/$(key "${base}")" ]; then
			echo "sha256:built"
			exit 0
		fi
		echo "HUB_HEAD ${base}" >> "${log}"
		hub_429 "${base}"
		;;
esac
exit 2
"""


def _fallback_steps(text: str) -> list[dict]:
	"""Minimal reader for the clarify job's steps when PyYAML is absent.

	Understands only the shapes this test reads: `- name:`, scalar keys,
	`env:` maps and `run: |` literal blocks at the workflow's indentation.
	"""
	lines = text.splitlines()
	start = lines.index("  clarify:")
	steps: list[dict] = []
	step: dict | None = None
	i = start + 1
	while i < len(lines):
		line = lines[i]
		if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
			break
		m = re.match(r"^      - name: (.*)$", line)
		if m:
			step = {"name": m.group(1)}
			steps.append(step)
			i += 1
			continue
		m = re.match(r"^        ([A-Za-z][A-Za-z0-9_-]*):\s?(.*)$", line)
		if step is not None and m:
			key, value = m.group(1), m.group(2)
			if value == "|":
				block = []
				i += 1
				while i < len(lines) and (lines[i].startswith(" " * 10) or not lines[i].strip()):
					block.append(lines[i][10:])
					i += 1
				while block and not block[-1].strip():
					block.pop()
				step[key] = "\n".join(block) + "\n"
				continue
			if value == "":
				mapping = {}
				i += 1
				while i < len(lines):
					sub = re.match(r"^          ([A-Za-z_][A-Za-z0-9_]*): (.*)$", lines[i])
					if not sub:
						break
					mapping[sub.group(1)] = sub.group(2)
					i += 1
				step[key] = mapping
				continue
			step[key] = value
		i += 1
	return steps


def _steps() -> list[dict]:
	text = CLARIFY_WORKFLOW.read_text(encoding="utf-8")
	if yaml is None:
		return _fallback_steps(text)
	data = yaml.safe_load(text)
	return data["jobs"]["clarify"]["steps"]


def _step(name: str) -> dict:
	for step in _steps():
		if step.get("name") == name:
			return step
	raise AssertionError(f"clarify.yml has no step named {name!r}")


class Sandbox:
	"""A temp working tree with the staged Dockerfile and a fake docker."""

	def __init__(self, root: Path, dockerfile_text: str) -> None:
		self.root = root
		(root / "scripts" / "clarify_sandbox").mkdir(parents=True)
		self.dockerfile = root / "scripts" / "clarify_sandbox" / "Dockerfile"
		self.dockerfile.write_text(dockerfile_text, encoding="utf-8")
		self.bin = root / "fakebin"
		self.bin.mkdir()
		docker = self.bin / "docker"
		docker.write_text(FAKE_DOCKER, encoding="utf-8")
		docker.chmod(docker.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
		self.store = root / "store"
		self.store.mkdir()
		self.log = root / "docker.log"
		self.log.write_text("", encoding="utf-8")

	def env(self, good_mirrors: str, mirrors: str = DEFAULT_MIRRORS) -> dict[str, str]:
		env = dict(os.environ)
		env.update(
			{
				"PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
				"FAKE_DOCKER_STORE": str(self.store),
				"FAKE_DOCKER_LOG": str(self.log),
				"FAKE_DOCKER_GOOD_MIRRORS": good_mirrors,
				"CLARIFY_BASE_IMAGE_MIRRORS": mirrors,
				"CLARIFY_BASE_IMAGE_PULL_TIMEOUT_SECS": "60",
				"PYTHONDONTWRITEBYTECODE": "1",
			}
		)
		return env

	def run_step(self, good_mirrors: str, mirrors: str = DEFAULT_MIRRORS) -> subprocess.CompletedProcess:
		body = self.root / "step.sh"
		body.write_text(_step(STEP_NAME)["run"], encoding="utf-8")
		# GitHub Actions runs a `run:` body without `shell:` as `bash -e {0}`;
		# use the same errexit so a failed mirror pull cannot pass here and
		# abort the step in CI.
		return subprocess.run(
			["bash", "--noprofile", "--norc", "-e", str(body)],
			cwd=self.root,
			env=self.env(good_mirrors, mirrors),
			capture_output=True,
			text=True,
			timeout=120,
		)

	def build(self, good_mirrors: str = "") -> subprocess.CompletedProcess:
		"""The build clarify_isolated_run.sh runs (no --pull)."""
		return subprocess.run(
			[
				"docker", "build", "-q",
				"-f", "scripts/clarify_sandbox/Dockerfile", "scripts/clarify_sandbox",
			],
			cwd=self.root,
			env=self.env(good_mirrors),
			capture_output=True,
			text=True,
			timeout=60,
		)

	def log_lines(self) -> list[str]:
		return self.log.read_text(encoding="utf-8").splitlines()


def _with_sandbox(dockerfile_text: str, fn) -> None:
	with tempfile.TemporaryDirectory() as tmp:
		fn(Sandbox(Path(tmp), dockerfile_text))


def _real_dockerfile() -> str:
	return SANDBOX_DOCKERFILE.read_text(encoding="utf-8")


def _real_base_ref() -> str:
	for line in _real_dockerfile().splitlines():
		parts = line.split()
		if parts and parts[0].upper() == "FROM":
			return parts[1]
	raise AssertionError("clarify sandbox Dockerfile has no FROM line")


# --- static contract ---------------------------------------------------------


def test_step_runs_immediately_before_run_codex_with_same_condition() -> None:
	names = [step.get("name") for step in _steps()]
	assert STEP_NAME in names, f"missing step {STEP_NAME!r}"
	index = names.index(STEP_NAME)
	assert names[index + 1] == "Run Codex", "the pre-seed step must run right before Run Codex"
	assert _step(STEP_NAME)["if"] == _step("Run Codex")["if"]


def test_step_env_has_no_secrets_and_defaults_to_mirrors() -> None:
	step = _step(STEP_NAME)
	env = step.get("env") or {}
	for key, value in env.items():
		assert "secrets." not in str(value), f"{key} must not carry a secret"
	assert "public.ecr.aws/docker/library" in env["CLARIFY_BASE_IMAGE_MIRRORS"]
	assert "mirror.gcr.io/library" in env["CLARIFY_BASE_IMAGE_MIRRORS"]
	assert "'300'" in env["CLARIFY_BASE_IMAGE_PULL_TIMEOUT_SECS"]
	body = step["run"]
	assert "${{" not in body, "expressions must reach the body through env:"
	assert "exit 1" not in body, "the step must fail open"
	assert "docker login" not in body
	assert step.get("continue-on-error") in (True, "true"), "the best-effort step must not block Run Codex"
	assert not re.search(r"^\s*preseed_pull_rc=\$\?\s*$", body, re.M), (
		"a standalone `$?` read after the pull is unreachable under bash -e"
	)


def test_real_dockerfile_base_is_a_supported_official_image() -> None:
	assert SUPPORTED_REF_RE.match(_real_base_ref()), (
		"scripts/clarify_sandbox/Dockerfile FROM is no longer a `name:tag` Docker "
		"Hub official image; the pre-seed step would skip it"
	)


# --- behaviour ---------------------------------------------------------------


def test_reported_failure_reproduces_without_the_step() -> None:
	"""Negative control: the reported head builds straight from Docker Hub."""

	def check(sb: Sandbox) -> None:
		result = sb.build(good_mirrors="public.ecr.aws/docker/library")
		assert result.returncode != 0
		assert "429 Too Many Requests" in result.stderr

	_with_sandbox(_real_dockerfile(), check)


def test_step_seeds_from_mirror_and_build_avoids_docker_hub() -> None:
	def check(sb: Sandbox) -> None:
		result = sb.run_step(good_mirrors="public.ecr.aws/docker/library mirror.gcr.io/library")
		assert result.returncode == 0, result.stderr
		ref = _real_base_ref()
		assert (
			f"CLARIFY_BASE_IMAGE_PRESEED image={ref} outcome=seeded "
			"source=public.ecr.aws/docker/library"
		) in result.stdout
		build = sb.build()
		assert build.returncode == 0, build.stderr
		lines = sb.log_lines()
		assert not any(line.startswith(("HUB_HEAD", "HUB_OR_BAD_PULL")) for line in lines), lines
		assert f"pull --quiet {ref}" not in lines

	_with_sandbox(_real_dockerfile(), check)


def test_second_mirror_used_when_first_fails() -> None:
	def check(sb: Sandbox) -> None:
		result = sb.run_step(good_mirrors="mirror.gcr.io/library")
		assert result.returncode == 0, result.stderr
		assert "outcome=mirror_failed source=public.ecr.aws/docker/library" in result.stdout
		assert "outcome=seeded source=mirror.gcr.io/library" in result.stdout
		assert f"tag mirror.gcr.io/library/{_real_base_ref()} {_real_base_ref()}" in sb.log_lines()
		assert sb.build().returncode == 0

	_with_sandbox(_real_dockerfile(), check)


def test_all_mirrors_failing_fails_open_without_tagging() -> None:
	def check(sb: Sandbox) -> None:
		result = sb.run_step(good_mirrors="")
		assert result.returncode == 0, result.stderr
		assert "::warning::CLARIFY_BASE_IMAGE_PRESEED" in result.stdout
		assert "outcome=unavailable mirrors=2" in result.stdout
		assert not any(line.startswith("tag ") for line in sb.log_lines())

	_with_sandbox(_real_dockerfile(), check)


def test_present_image_is_not_pulled_again() -> None:
	def check(sb: Sandbox) -> None:
		ref = _real_base_ref()
		subprocess.run(
			["docker", "pull", "--quiet", f"mirror.gcr.io/library/{ref}"],
			env=sb.env("mirror.gcr.io/library"), capture_output=True, check=True,
		)
		subprocess.run(
			["docker", "tag", f"mirror.gcr.io/library/{ref}", ref],
			env=sb.env("mirror.gcr.io/library"), capture_output=True, check=True,
		)
		sb.log.write_text("", encoding="utf-8")
		result = sb.run_step(good_mirrors="public.ecr.aws/docker/library")
		assert result.returncode == 0, result.stderr
		assert f"image={ref} outcome=present" in result.stdout
		assert not any(line.startswith("pull") for line in sb.log_lines())

	_with_sandbox(_real_dockerfile(), check)


def test_invalid_mirror_entries_are_skipped() -> None:
	def check(sb: Sandbox) -> None:
		result = sb.run_step(
			good_mirrors="mirror.gcr.io/library",
			mirrors="evil$(touch_pwned) HOST:5000/lib -x/lib mirror.gcr.io/library",
		)
		assert result.returncode == 0, result.stderr
		assert result.stdout.count("reason=invalid_mirror") == 3
		assert "outcome=seeded source=mirror.gcr.io/library" in result.stdout
		pulls = [line for line in sb.log_lines() if line.startswith("pull")]
		assert pulls == [f"pull --quiet mirror.gcr.io/library/{_real_base_ref()}"], pulls

	_with_sandbox(_real_dockerfile(), check)


def test_unsupported_or_malicious_refs_are_skipped() -> None:
	for ref in (
		"ghcr.io/x/y:1",
		"org/img:1",
		"node@sha256:" + "a" * 64,
		"node:1$(touch pwned)",
		"node",
	):

		def check(sb: Sandbox, ref: str = ref) -> None:
			result = sb.run_step(good_mirrors="public.ecr.aws/docker/library")
			assert result.returncode == 0, result.stderr
			assert "outcome=skip reason=unsupported_ref" in result.stdout, (ref, result.stdout)
			assert sb.log_lines() == [], (ref, sb.log_lines())
			assert not (sb.root / "pwned").exists()

		_with_sandbox(f"# fixture\nFROM {ref}\n", check)


def test_missing_dockerfile_warns_and_skips() -> None:
	def check(sb: Sandbox) -> None:
		sb.dockerfile.unlink()
		result = sb.run_step(good_mirrors="public.ecr.aws/docker/library")
		assert result.returncode == 0, result.stderr
		assert (
			"::warning::CLARIFY_BASE_IMAGE_PRESEED image=none outcome=skip reason=dockerfile_missing"
		) in result.stdout
		assert sb.log_lines() == [], sb.log_lines()

	_with_sandbox(_real_dockerfile(), check)


def test_isolated_run_build_does_not_force_pull() -> None:
	"""The pre-seeded local image is only used while the build omits --pull."""
	script = (REPO_ROOT / "scripts" / "clarify_isolated_run.sh").read_text(encoding="utf-8")
	builds = [line for line in script.splitlines() if "docker build" in line]
	assert builds, "clarify_isolated_run.sh no longer runs docker build"
	assert not any("--pull" in line for line in builds), builds


def test_invalid_timeout_falls_back_to_default() -> None:
	body = _step(STEP_NAME)["run"]
	assert 'preseed_timeout=300' in body
	assert '-lt 30' in body and '-gt 1800' in body


def main() -> int:
	tests = [
		test_step_runs_immediately_before_run_codex_with_same_condition,
		test_step_env_has_no_secrets_and_defaults_to_mirrors,
		test_real_dockerfile_base_is_a_supported_official_image,
		test_reported_failure_reproduces_without_the_step,
		test_step_seeds_from_mirror_and_build_avoids_docker_hub,
		test_second_mirror_used_when_first_fails,
		test_all_mirrors_failing_fails_open_without_tagging,
		test_present_image_is_not_pulled_again,
		test_invalid_mirror_entries_are_skipped,
		test_unsupported_or_malicious_refs_are_skipped,
		test_missing_dockerfile_warns_and_skips,
		test_isolated_run_build_does_not_force_pull,
		test_invalid_timeout_falls_back_to_default,
	]
	passed = 0
	failed = 0
	for test in tests:
		name = test.__name__
		try:
			test()
			print(f"  PASS  {name}")
			passed += 1
		except AssertionError as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1
		except Exception as exc:
			print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
