"""Release jobs build, attest and upload the consumer sync manifest (#7128)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOWS = ("test-and-mark-stable.yml", "mark-stable.yml")
# Same gate as the release-manifest steps: nothing is attested or uploaded
# for a version that the release switches leave unpublished.
RELEASE_GATE = "if: ${{ !inputs.dry_run && steps.release_switch.outputs.enabled == 'true' && steps.release_tagging.outputs.enabled == 'true' }}"


def release_job(name: str) -> str:
	text = (REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
	start = text.index("\n  release:\n")
	match = re.compile(r"^  [A-Za-z0-9_-]+:\n", re.M).search(text, start + len("\n  release:\n"))
	return text[start:match.start() if match else len(text)]


def step(job: str, name: str) -> str:
	start = job.index(f"      - name: {name}\n")
	nxt = job.find("\n      - name: ", start + 1)
	return job[start:] if nxt == -1 else job[start:nxt]


def run_body(block: str) -> str:
	return block.split("run: |", 1)[1] if "run: |" in block else ""


@pytest.mark.parametrize("workflow", RELEASE_WORKFLOWS)
def test_release_permissions_allow_attestation(workflow):
	job = release_job(workflow)
	perms = job.split("    permissions:\n", 1)[1].split("\n    concurrency:", 1)[0]
	for line in ("contents: write", "checks: read", "actions: read", "id-token: write", "attestations: write"):
		assert f"      {line}" in perms, (workflow, line)


@pytest.mark.parametrize("workflow", RELEASE_WORKFLOWS)
def test_manifest_is_built_and_attested_before_tagging(workflow):
	job = release_job(workflow)
	names = re.findall(r"^      - name: (.+)$", job, re.M)
	build = names.index("Build sync manifest")
	attest = names.index("Attest sync manifest")
	tag = names.index("Tag version and update stable pointer")
	assert names.index("Extract changelog for this version") < build < attest < tag
	build_block = step(job, "Build sync manifest")
	assert "id: sync_manifest" in build_block
	assert "if:" not in build_block.split("run: |", 1)[0], "the builder also runs on dry runs"
	assert "VERSION: ${{ needs.resolve-version.outputs.version }}" in build_block
	assert 'scripts/build_sync_manifest.py build' in build_block
	assert '--commit "$(git rev-parse HEAD)"' in build_block
	assert 'echo "path=${SYNC_MANIFEST_PATH}" >> "$GITHUB_OUTPUT"' in build_block
	attest_block = step(job, "Attest sync manifest")
	assert RELEASE_GATE in attest_block
	assert "uses: actions/attest-build-provenance@v3" in attest_block
	assert "subject-path: ${{ steps.sync_manifest.outputs.path }}" in attest_block
	assert "continue-on-error" not in attest_block


@pytest.mark.parametrize("workflow", RELEASE_WORKFLOWS)
def test_manifest_is_uploaded_after_the_release(workflow):
	job = release_job(workflow)
	names = re.findall(r"^      - name: (.+)$", job, re.M)
	upload = names.index("Upload sync manifest")
	assert names.index("Create GitHub Release") + 1 == upload
	assert upload < names.index("Notify consumer repos via repository_dispatch")
	block = step(job, "Upload sync manifest")
	assert RELEASE_GATE in block
	assert 'gh release upload "${VERSION}" "${SYNC_MANIFEST_PATH}" --clobber' in block
	assert "SYNC_MANIFEST outcome=upload_failed" in block


@pytest.mark.parametrize("workflow", RELEASE_WORKFLOWS)
def test_new_run_bodies_use_env_not_expressions(workflow):
	job = release_job(workflow)
	for name in ("Build sync manifest", "Upload sync manifest"):
		assert "${{" not in run_body(step(job, name)), (workflow, name)


if __name__ == "__main__":
	sys.exit(pytest.main([__file__, "-q"]))
