"""update_workflows.yml delivers verified updates through a PR (#7128)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_WORKFLOWS = REPO_ROOT / ".github" / "workflows" / "update_workflows.yml"
TEMPLATE = REPO_ROOT / "workflow-templates" / "ai-update-workflows.yml"
ORIGINAL_COMMIT = "git commit -m \"$(printf 'chore: update AI workflow wrappers from coding-workflows@stable\\n\\n%b\\nAuto-applied by ai-update-workflows. Managed wrappers always follow\\nthe pinned release; ALLOW_WORKFLOW_EDITS='\\''false'\\'' only skips other assets.' \"$COMMIT_BODY\")\""


def text() -> str:
	return UPDATE_WORKFLOWS.read_text(encoding="utf-8")


def commit_step() -> str:
	body = text()
	start = body.index("      - name: Commit and push updates\n")
	return body[start:body.index("      - name: Send Telegram notification\n", start)]


def test_input_defaults_to_pull_request_on_both_triggers():
	on_block = text().split("\njobs:\n", 1)[0]
	call, dispatch = on_block.split("  workflow_dispatch:\n", 1)
	for section in (call, dispatch):
		match = re.search(r"      update_via_pull_request:\n(?:        .+\n)+", section)
		assert match, section
		assert "        default: true\n" in match.group(0)
		assert "        type: boolean\n" in match.group(0)


def test_direct_push_path_is_unchanged():
	block = commit_step()
	assert "id: commit_push" in block
	assert "UPDATE_VIA_PR: ${{ inputs.update_via_pull_request != false }}" in block
	assert ORIGINAL_COMMIT in block
	gate = 'if [ "${PR_DELIVERY}" != "true" ] && [ "${UPDATE_VIA_PR:-true}" != "true" ]; then\n'
	direct = block.split(gate, 1)[1].split("\n          fi\n", 1)[0]
	direct_lines = [line.strip() for line in direct.splitlines()]
	assert direct_lines[:2] == ['echo "delivery=direct" >> "${PR_OUT}"', "git push"]
	assert 'echo "sync_pr_state=pushed" >> "$GITHUB_OUTPUT"' in direct
	assert block.index(ORIGINAL_COMMIT) < block.index(gate)


def test_pull_request_path_contract():
	block = commit_step()
	assert 'SYNC_BRANCH="ai-sync/stable"' in block
	assert block.count('gh api "repos/${REPO}")') == 1
	assert block.count("pulls?head=${OWNER}:${SYNC_BRANCH}&base=${DEFAULT_BRANCH}&state=open") == 1
	assert block.count("-X PATCH") == 1
	assert block.count('-X POST "repos/${REPO}/pulls"') == 1
	assert 'git push --force-with-lease="refs/heads/${SYNC_BRANCH}:${SYNC_REMOTE_SHA}" origin "HEAD:refs/heads/${SYNC_BRANCH}"' in block
	assert "git push --force origin" not in block
	assert 'TG_LEVEL="WARNING"' in text().split('if [ "${SYNC_PR_STATE:-}" = "awaiting_merge" ]; then', 1)[1].split("fi", 1)[0]
	assert "FETCH_HEAD^{tree}" in block and "HEAD^{tree}" in block
	assert "<!-- ai-sync:v1 version=%s commit=%s -->" in block
	auto = block.index('--auto "--${MERGE_METHOD}" --match-head-commit "${MERGE_HEAD_SHA}"')
	direct = block.index('"--${MERGE_METHOD}" --match-head-commit "${MERGE_HEAD_SHA}" >/dev/null 2>&1')
	assert auto < direct
	assert block.count("--match-head-commit") == 2
	for state in ("awaiting_merge", "auto_merge_enabled", "merged"):
		assert f'"{state}"' in block
	assert 'echo "sync_pr_state=${SYNC_PR_STATE}" >> "$GITHUB_OUTPUT"' in block
	assert 'echo "sync_pr_url=${SYNC_PR_URL}" >> "$GITHUB_OUTPUT"' in block
	assert "SYNC_DELIVERY outcome=${SYNC_PR_STATE}" in block


def test_consumer_template_gate_and_permissions():
	template = TEMPLATE.read_text(encoding="utf-8")
	assert "permissions:\n  contents: write\n  pull-requests: write\n" in template
	assert "    if: ${{ vars.AI_WORKFLOW_AUTO_UPDATE_ENABLED != 'false' }}\n" in template
	assert "uses: shubhodeep1/coding-workflows/.github/workflows/update_workflows.yml@stable\n" in template
	assert "allow_workflow_edits: ${{ vars.ALLOW_WORKFLOW_EDITS != 'false' }}" in template
	assert "AI_WORKFLOW_AUTO_UPDATE_ENABLED" in template.split("name: AI Update Workflows", 1)[0]


if __name__ == "__main__":
	sys.exit(pytest.main([__file__, "-q"]))
