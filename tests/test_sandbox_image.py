"""Prebuilt sandbox images (scripts/sandbox_image.sh, scripts/sandbox_images_publish.sh).

The helper pulls <registry>:<family>-<input hash> and falls back to a local
`docker build` when the pull fails, times out or returns an image whose input
label does not match. The publish script pushes one tag per build site; these
tests pin that its inputs match what the build sites pass. A fake docker
records every call.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "sandbox_image.sh"
PUBLISH = ROOT / "scripts" / "sandbox_images_publish.sh"
BUILD_SITES = (
	"codex_isolated_exec.sh",
	"clarify_isolated_run.sh",
	"heal_isolated_implement.sh",
	"review_untrusted_sandbox.sh",
)
PULLED_ID = "sha256:" + "a" * 64
BUILT_ID = "sha256:" + "b" * 64

FAKE_DOCKER = r"""#!/bin/bash
printf '%s\n' "$*" >> "${FAKE_DOCKER_LOG}"
case "$1" in
  pull) [ "${FAKE_PULL:-fail}" = ok ] || exit 1 ;;
  image)
    case "$*" in
      *'{{.Id}}'*) echo "${FAKE_PULLED_ID}" ;;
      *Labels*) cat "${FAKE_LABEL_FILE}" 2>/dev/null || true ;;
    esac
    ;;
  build) [ "${FAKE_BUILD:-ok}" = ok ] || exit 1; echo "${FAKE_BUILT_ID}" ;;
  manifest) [ "${FAKE_MANIFEST:-missing}" = present ] || exit 1 ;;
  tag|push) ;;
  *) exit 1 ;;
esac
"""


@pytest.fixture()
def fake(tmp_path: Path):
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	docker = bin_dir / "docker"
	docker.write_text(FAKE_DOCKER)
	docker.chmod(0o755)
	context = tmp_path / "ctx"
	context.mkdir()
	(context / "Dockerfile").write_text("FROM scratch\nARG CODEX_VERSION=v1\n")
	log = tmp_path / "docker.log"
	label_file = tmp_path / "label"
	base_env = dict(
		os.environ,
		PATH=f"{bin_dir}:{os.environ['PATH']}",
		FAKE_DOCKER_LOG=str(log),
		FAKE_LABEL_FILE=str(label_file),
		FAKE_PULLED_ID=PULLED_ID,
		FAKE_BUILT_ID=BUILT_ID,
		PYTHONDONTWRITEBYTECODE="1",
	)
	base_env.pop("SANDBOX_IMAGE_REGISTRY", None)

	def run(*args: str, script: Path = HELPER, **env: str) -> dict[str, object]:
		result = subprocess.run(
			["bash", str(script), *args],
			env={**base_env, **env}, capture_output=True, text=True, check=False, cwd=ROOT,
		)
		calls = log.read_text().splitlines() if log.exists() else []
		log.unlink(missing_ok=True)
		return {"rc": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr, "calls": calls}

	def ref(*args: str, **env: str) -> str:
		result = run("ref", "--family", "clarify", *args, str(context), **env)
		assert result["rc"] == 0, result["stderr"]
		return str(result["stdout"])

	return {"run": run, "ref": ref, "context": context, "label_file": label_file, "tmp": tmp_path}


def _hash_of(ref: str) -> str:
	return ref.rsplit("-", 1)[1]


def test_ref_uses_default_registry_family_and_input_hash(fake) -> None:
	ref = fake["ref"]("--build-arg", "CODEX_VERSION=v0.114.0")
	assert re.fullmatch(r"ghcr\.io/shubhodeep1/coding-workflows-sandbox:clarify-[0-9a-f]{32}", ref)


def test_hash_covers_args_and_files_but_not_arg_order(fake) -> None:
	base = fake["ref"]("--build-arg", "A=1", "--build-arg", "B=2")
	assert fake["ref"]("--build-arg", "B=2", "--build-arg", "A=1") == base
	assert fake["ref"]("--build-arg", "A=1", "--build-arg", "B=3") != base
	assert fake["ref"]("--build-arg", "A=1") != base
	(fake["context"] / "Dockerfile").write_text("FROM scratch\n")
	assert fake["ref"]("--build-arg", "A=1", "--build-arg", "B=2") != base


def test_pull_with_matching_label_skips_the_build(fake) -> None:
	ref = fake["ref"]()
	# A pulled image without the input label is rejected and built locally.
	result = fake["run"]("build", "--family", "clarify", str(fake["context"]), FAKE_PULL="ok")
	assert result["stdout"] == BUILT_ID
	assert "outcome=built reason=label_mismatch" in result["stderr"]
	label_arg = next(c for c in result["calls"] if c.startswith("build "))
	# The label holds the full hash; the tag holds its first 32 characters.
	full = re.search(r"coding-workflows\.sandbox-input=([0-9a-f]{64})", label_arg).group(1)
	assert full.startswith(_hash_of(ref))
	fake["label_file"].write_text(full + "\n")
	result = fake["run"]("build", "--family", "clarify", str(fake["context"]), FAKE_PULL="ok")
	assert result["rc"] == 0
	assert result["stdout"] == PULLED_ID
	assert "SANDBOX_IMAGE family=clarify outcome=pulled" in result["stderr"]
	assert not any(c.startswith("build ") for c in result["calls"])
	assert f"pull -q {ref}" in result["calls"]


def test_pull_failure_builds_locally_with_the_same_inputs(fake) -> None:
	result = fake["run"](
		"build", "--family", "clarify", "--build-arg", "CODEX_VERSION=v0.114.0",
		"-f", str(fake["context"] / "Dockerfile"), str(fake["context"]),
	)
	assert result["rc"] == 0
	assert result["stdout"] == BUILT_ID
	assert "outcome=built reason=pull_failed" in result["stderr"]
	build = next(c for c in result["calls"] if c.startswith("build "))
	assert "--build-arg CODEX_VERSION=v0.114.0" in build
	assert "--label coding-workflows.sandbox-input=" in build
	assert build.endswith(str(fake["context"]))


def test_local_build_failure_propagates(fake) -> None:
	result = fake["run"]("build", "--family", "clarify", str(fake["context"]), FAKE_BUILD="fail")
	assert result["rc"] != 0
	assert result["stdout"] == ""


def test_registry_off_never_pulls(fake) -> None:
	result = fake["run"]("build", "--family", "clarify", str(fake["context"]), SANDBOX_IMAGE_REGISTRY="off", FAKE_PULL="ok")
	assert result["stdout"] == BUILT_ID
	assert "reason=disabled" in result["stderr"]
	assert not any(c.startswith("pull ") for c in result["calls"])


def test_invalid_registry_builds_locally(fake) -> None:
	result = fake["run"]("build", "--family", "clarify", str(fake["context"]), SANDBOX_IMAGE_REGISTRY="Bad Registry!")
	assert result["stdout"] == BUILT_ID
	assert "invalid SANDBOX_IMAGE_REGISTRY" in result["stderr"]
	assert not any(c.startswith("pull ") for c in result["calls"])


def test_custom_registry_is_used(fake) -> None:
	ref = fake["ref"](SANDBOX_IMAGE_REGISTRY="registry.example.com:5000/team/sandbox")
	assert ref.startswith("registry.example.com:5000/team/sandbox:clarify-")


@pytest.mark.parametrize(
	"args",
	[
		["--family", "Bad_Family"],
		["--family", "clarify", "--build-arg", "lower=1"],
		["--family", "clarify", "--build-arg", "A=$(id)"],
		["--family", "clarify", "--bogus"],
	],
)
def test_invalid_arguments_are_rejected(fake, args: list[str]) -> None:
	result = fake["run"]("build", *args, str(fake["context"]))
	assert result["rc"] == 2
	assert result["calls"] == []


def test_dockerfile_outside_the_context_is_rejected(fake) -> None:
	outside = fake["tmp"] / "Dockerfile"
	outside.write_text("FROM scratch\n")
	result = fake["run"]("build", "--family", "clarify", "-f", str(outside), str(fake["context"]))
	assert result["rc"] == 2
	assert result["calls"] == []


def test_publish_skips_an_existing_tag_unless_forced(fake) -> None:
	ref = fake["ref"]()
	result = fake["run"]("publish", "--family", "clarify", str(fake["context"]), FAKE_MANIFEST="present")
	assert result["rc"] == 0
	assert "outcome=publish_skipped reason=tag_exists" in result["stderr"]
	assert not any(c.startswith(("build ", "push ")) for c in result["calls"])
	result = fake["run"]("publish", "--family", "clarify", "--force", str(fake["context"]), FAKE_MANIFEST="present")
	assert result["rc"] == 0
	assert f"tag {BUILT_ID} {ref}" in result["calls"]
	assert f"push -q {ref}" in result["calls"]
	assert "outcome=published" in result["stderr"]


def test_publish_build_failure_fails(fake) -> None:
	result = fake["run"]("publish", "--family", "clarify", str(fake["context"]), FAKE_BUILD="fail")
	assert result["rc"] == 1
	assert not any(c.startswith("push ") for c in result["calls"])


def test_codex_isolated_image_context_matches_both_engines(tmp_path: Path) -> None:
	for engine in ("codex", "claude"):
		out = tmp_path / engine
		out.mkdir()
		subprocess.run(["bash", str(ROOT / "scripts/codex_isolated_exec.sh"), "image-context", "--engine", engine, "--out", str(out)], check=True)
		assert sorted(p.name for p in out.iterdir()) == ["Dockerfile"]
		text = (out / "Dockerfile").read_text()
		assert text.startswith("# Fixed build context generated by scripts/codex_isolated_exec.sh.\nFROM node:22.16.0-bookworm-slim\n")
		assert ("@anthropic-ai/claude-code" in text) is (engine == "claude")
	# A non-empty --out is refused.
	result = subprocess.run(["bash", str(ROOT / "scripts/codex_isolated_exec.sh"), "image-context", "--out", str(tmp_path / "codex")], capture_output=True, text=True)
	assert result.returncode == 2


def test_publish_script_publishes_every_build_site_variant(fake) -> None:
	result = fake["run"](script=PUBLISH, FAKE_MANIFEST="missing")
	assert result["rc"] == 0, result["stderr"]
	pushed = [c.split()[-1] for c in result["calls"] if c.startswith("push ")]
	assert len(pushed) == len(set(pushed)) == 7
	families = sorted(re.search(r":([a-z-]+)-[0-9a-f]{32}$", ref).group(1) for ref in pushed)
	assert families == ["clarify"] * 3 + ["codex-isolated-claude", "codex-isolated-codex"] + ["review"] * 2
	cli_version = json.loads((ROOT / ".github/ai/claude_engine.json").read_text())["cli_version"]
	builds = [c for c in result["calls"] if c.startswith("build ")]
	assert sum(f"CLAUDE_CLI_VERSION={cli_version}" in b for b in builds) == 3
	assert sum("CODEX_VERSION=v0.114.0" in b for b in builds) == 4
	assert sum("OPENCODE_VERSION=1.18.23" in b for b in builds) == 2


def test_publish_script_refs_match_the_build_sites_inputs(fake) -> None:
	"""Each published tag equals the tag a build site resolves with default inputs."""
	scripts = ROOT / "scripts"
	cli_version = json.loads((ROOT / ".github/ai/claude_engine.json").read_text())["cli_version"]
	expected = set()

	def site_ref(family: str, context: Path, *args: str) -> str:
		result = fake["run"]("ref", "--family", family, *args, str(context))
		assert result["rc"] == 0, result["stderr"]
		return str(result["stdout"])

	expected.add(site_ref("clarify", scripts / "clarify_sandbox", "--build-arg", "CODEX_VERSION=v0.114.0"))
	expected.add(site_ref("clarify", scripts / "clarify_sandbox", "--build-arg", "CODEX_VERSION=v0.114.0", "--build-arg", f"CLAUDE_CLI_VERSION={cli_version}"))
	expected.add(site_ref("clarify", scripts / "clarify_sandbox"))
	expected.add(site_ref("review", scripts / "review_sandbox", "--build-arg", "OPENCODE_VERSION=1.18.23"))
	expected.add(site_ref("review", scripts / "review_sandbox", "--build-arg", "OPENCODE_VERSION=1.18.23", "--build-arg", f"CLAUDE_CLI_VERSION={cli_version}"))
	for engine in ("codex", "claude"):
		out = fake["tmp"] / f"site-{engine}"
		out.mkdir()
		subprocess.run(["bash", str(scripts / "codex_isolated_exec.sh"), "image-context", "--engine", engine, "--out", str(out)], check=True)
		args = ["--build-arg", "CODEX_VERSION=v0.114.0"]
		if engine == "claude":
			args += ["--build-arg", f"CLAUDE_CLI_VERSION={cli_version}"]
		expected.add(site_ref(f"codex-isolated-{engine}", out, *args))
	result = fake["run"](script=PUBLISH, FAKE_MANIFEST="missing")
	pushed = {c.split()[-1] for c in result["calls"] if c.startswith("push ")}
	assert pushed == expected


def test_build_site_defaults_match_the_publish_script() -> None:
	scripts = ROOT / "scripts"
	assert 'local version="${CODEX_VERSION:-v0.114.0}"' in (scripts / "codex_isolated_exec.sh").read_text()
	assert 'version="${CLARIFY_CODEX_VERSION:-v0.114.0}"' in (scripts / "clarify_isolated_run.sh").read_text()
	assert 'version="${OPENCODE_VERSION:-1.18.23}"' in (scripts / "review_untrusted_sandbox.sh").read_text()
	publish = PUBLISH.read_text()
	assert 'publish_codex_version="${CODEX_VERSION:-v0.114.0}"' in publish
	assert 'publish_opencode_version="${OPENCODE_VERSION:-1.18.23}"' in publish
	workflow = (ROOT / ".github/workflows/publish-sandbox-images.yml").read_text()
	assert "CODEX_VERSION: ${{ vars.CODEX_VERSION || 'v0.114.0' }}" in workflow
	assert "OPENCODE_VERSION: ${{ vars.OPENCODE_VERSION || '1.18.23' }}" in workflow


@pytest.mark.parametrize("site", BUILD_SITES)
def test_every_build_site_resolves_through_the_helper(site: str) -> None:
	text = (ROOT / "scripts" / site).read_text()
	assert 'sandbox_image.sh" build --family' in text


def test_every_support_copy_list_with_a_build_site_ships_the_helper() -> None:
	offenders = []
	for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
		for number, line in enumerate(path.read_text().splitlines(), 1):
			if re.match(r"\s*for \w+ in .*; do\s*$", line) and any(re.search(r"(?<![\w/])" + re.escape(s) + r"(?=\s|;)", line) for s in BUILD_SITES):
				if not re.search(r"(?<![\w/])sandbox_image\.sh(?=\s|;)", line):
					offenders.append(f"{path.name}:{number}")
	assert offenders == []
	staging = (ROOT / "scripts/stage_workflow_support.sh").read_text()
	required = re.search(r'^REQUIRED_BOOTSTRAP_SCRIPTS="([^"]*)"', staging, re.MULTILINE).group(1).split()
	assert "review_untrusted_sandbox.sh" in required and "sandbox_image.sh" in required
	triage = (ROOT / ".github/workflows/check_failure_triage.yml").read_text()
	assert '.codex-workflow-src/scripts/sandbox_image.sh "${trusted_dir}/scripts/sandbox_image.sh"' in triage
	assert '"scripts/sandbox_image.sh",' in (ROOT / ".github/workflows/validate.yml").read_text()


def test_publish_workflow_is_scoped_and_least_privilege() -> None:
	import yaml

	workflow = yaml.safe_load((ROOT / ".github/workflows/publish-sandbox-images.yml").read_text())
	assert workflow["permissions"] == {"contents": "read", "packages": "write"}
	job = workflow["jobs"]["publish"]
	assert job["if"] == (
		"github.repository == 'shubhodeep1/coding-workflows' && "
		"(github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main')"
	)
	triggers = workflow["on"]
	assert triggers["push"]["branches"] == ["main", "stable"]
	assert "schedule" in triggers and "workflow_dispatch" in triggers
	checkout = job["steps"][0]
	assert checkout["with"]["persist-credentials"] is False
	for step in job["steps"]:
		assert "${{" not in step.get("run", ""), step.get("name")
