"""Review gate skip for consumer sync PRs on ai-sync/stable (#7128)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_build_sync_manifest import make_upstream_repo  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "review_workflow_sync_skip.sh"
BUILDER = REPO_ROOT / "scripts" / "build_sync_manifest.py"
REVIEW_AUTOFIX = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"


@pytest.fixture()
def support_root(tmp_path):
	repo, _sha = make_upstream_repo(tmp_path)
	(repo / "scripts").mkdir(exist_ok=True)
	shutil.copy2(BUILDER, repo / "scripts" / "build_sync_manifest.py")
	return repo


def run_helper(head_ref: str, paths_file: Path, support: Path) -> subprocess.CompletedProcess:
	return subprocess.run(["bash", str(HELPER), "check", head_ref, str(paths_file), str(support)], capture_output=True, text=True)


def test_managed_paths_only_skip(tmp_path, support_root):
	paths = tmp_path / "paths.txt"
	paths.write_text(".github/workflows/ai-review.yml\nCLAUDE.md\nchangelog.d/1-x.md\n", encoding="utf-8")
	result = run_helper("ai-sync/stable", paths, support_root)
	assert result.returncode == 0, result.stdout + result.stderr
	assert result.stdout.strip() == "skip"


def test_unmanaged_path_reviews(tmp_path, support_root):
	paths = tmp_path / "paths.txt"
	paths.write_text(".github/workflows/ai-review.yml\nsrc/app.py\n", encoding="utf-8")
	result = run_helper("ai-sync/stable", paths, support_root)
	assert result.returncode == 1
	assert result.stdout.strip() == "review reason=unmanaged_path"


@pytest.mark.parametrize("head_ref", ["ai-sync/other", "ai/issue-1", "main", ""])
def test_other_heads_review(tmp_path, support_root, head_ref):
	paths = tmp_path / "paths.txt"
	paths.write_text("CLAUDE.md\n", encoding="utf-8")
	result = run_helper(head_ref, paths, support_root)
	assert result.returncode == 1
	assert "reason=head_ref_not_sync" in result.stdout


def test_missing_inputs_review(tmp_path, support_root):
	paths = tmp_path / "paths.txt"
	paths.write_text("CLAUDE.md\n", encoding="utf-8")
	empty = tmp_path / "empty.txt"
	empty.write_text("", encoding="utf-8")
	assert "reason=paths_unavailable" in run_helper("ai-sync/stable", empty, support_root).stdout
	assert "reason=paths_unavailable" in run_helper("ai-sync/stable", tmp_path / "absent.txt", support_root).stdout
	bare = tmp_path / "bare"
	bare.mkdir()
	assert "reason=builder_missing" in run_helper("ai-sync/stable", paths, bare).stdout


def _gate_text() -> str:
	return REVIEW_AUTOFIX.read_text(encoding="utf-8")


def _sync_block(text: str) -> str:
	start = text.index("          # Consumer sync PRs (#7128)")
	return text[start:text.index("          # Deterministic pre-review skip (last gate check):", start)]


def test_gate_block_contract():
	text = _gate_text()
	block = _sync_block(text)
	assert text.index("          # Consumer sync PRs (#7128)") < text.index('          DETERMINISTIC_SKIP="false"\n')
	assert '[ "${pr_head_ref}" = "ai-sync/stable" ]' in block
	assert '[ "${pr_head_repo}" = "${REPOSITORY}" ]' in block
	assert '[ "${HEAD_GATE_HELPER_VERIFIED:-false}" = "true" ]' in block
	assert '[force-review]' in block and "*,force-review,*" in block
	assert "length == $n and length < 3000" in block
	assert ".previous_filename // empty" in block
	assert 'SKIP_REASON="workflow_sync_managed"' in block
	assert 'GATE_SKIP_RECORDED="true"' in block
	assert "DETERMINISTIC_SKIP" not in block.replace("DETERMINISTIC_SKIP stays false", "")
	for forbidden in ("gh pr merge", "--add-label", "statuses/", "issues/${PR_NUMBER}/comments"):
		assert forbidden not in block
	assert "AUTOFIX_GATE_WORKFLOW_SYNC_SKIP" in block


def test_head_gate_checkout_lists_the_helper_paths():
	text = _gate_text()
	start = text.index("      - name: Checkout head-gate helper\n")
	block = text[start:text.index("      - name: Verify head-gate helper identity\n", start)]
	for path in ("scripts/review_workflow_sync_skip.sh", "scripts/build_sync_manifest.py", "workflow-templates/"):
		assert f"            {path}\n" in block


if __name__ == "__main__":
	sys.exit(pytest.main([__file__, "-q"]))
