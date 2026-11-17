#!/usr/bin/env python3
"""Contract test for the LAST_RUN_DIFF generation in `review_autofix.yml`.

The downstream OSCILLATION GUARD in the editor prompt
(`scripts/review_apply_fixes.sh:441-462`) tells the model to leave any hunk
visible in `last_run_diff.patch` alone unless it can attach a "regression
fingerprint" + "runtime failure path". This guardrail is correct ONLY when
the diff actually reflects prior AI autofix work; feeding it the diff of
non-autofix commits (impl commits, smoke-test bait commits, manual pushes)
falsely trips the guard and causes the editor to silently bail out.

That regression was observed on review_autofix run 25254574828 (smoke gate
of release v1.1.0): the bait commit was at HEAD, the unconditional
`git diff HEAD~1...HEAD` fed the bait line into `last_run_diff.patch`,
the editor at `reasoning=low` produced empty output across 6 attempts
(3 + 3 retry), and Phase 4b's `bait_remained` verification correctly
failed the smoke gate. The fix narrows the diff source to the most recent
`[ai-autofix]`/`[judge-fix]` commit on the PR head's first-parent chain,
matching the existing prefix convention used by the
`Count autofix iterations` step (`.github/workflows/review_autofix.yml`
~line 2096-2105) and the in-step retry gate
(`_prior_autofix_count` in the `Apply fixes with editor model` step).

Four invariants must hold for the OSCILLATION GUARD not to false-trip:

1. The diff source must be a walk-back to the most recent
   `[ai-autofix]`/`[judge-fix]` commit (awk regex on `git log`), NOT an
   unconditional `git diff HEAD~1...HEAD`.
2. The walk must use `--first-parent` so merged-in autofix commits from
   the base branch don't pollute LAST_RUN_DIFF on a first-iteration PR.
3. The fall-through placeholder must start with "No previous AI autofix"
   so `scripts/review_run_reviewers.sh:249`'s sentinel-detection regex
   correctly classifies the run as the first iteration.
4. The diff range must use `${SHA}^...${SHA}` so the autofix commit's own
   diff is shown (not whatever HEAD~1 happens to be).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_autofix_step_scripts import expanded_review_autofix_text  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"


def _review_autofix_text() -> str:
	# Moved step bodies (scripts/review_autofix_step_*.sh) are inlined again.
	return expanded_review_autofix_text()


def _generate_diff_block(text: str) -> str:
	"""Return the body of the `Generate diff context` step."""
	marker = "- name: Generate diff context"
	start = text.find(marker)
	assert start != -1, "Missing `Generate diff context` step in review_autofix.yml"
	next_step = text.find("\n      - name:", start + len(marker))
	assert next_step != -1, "Could not bound the `Generate diff context` step"
	return text[start:next_step]


def test_last_run_diff_walks_back_to_autofix_commit() -> None:
	"""The diff source must be the most recent `[ai-autofix]`/`[judge-fix]`
	commit, not an unconditional `HEAD~1...HEAD` diff. Without this walk,
	the smoke gate's bait commit (or any non-autofix commit at HEAD)
	leaks into `last_run_diff.patch` and false-trips the OSCILLATION
	GUARD downstream."""
	block = _generate_diff_block(_review_autofix_text())
	assert "LAST_AUTOFIX_SHA=" in block, (
		"`Generate diff context` step is missing the LAST_AUTOFIX_SHA "
		"walk-back. Reverting to unconditional `git diff HEAD~1...HEAD` "
		"re-introduces the run-25254574828 OSCILLATION GUARD false-trip."
	)
	assert r"$2 ~ /^\[(ai-autofix|judge-fix)\]/" in block, (
		"LAST_AUTOFIX_SHA walk must filter on the "
		"`[ai-autofix]`/`[judge-fix]` commit-subject prefix. The same "
		"regex is used by the `_prior_autofix_count` block in the "
		"`Apply fixes with editor model` step of this workflow — keep "
		"them in lockstep so the OSCILLATION GUARD's diff source and "
		"the in-step retry gate agree on what counts as a prior run."
	)
	assert "git log --first-parent" in block, (
		"LAST_AUTOFIX_SHA walk is missing `--first-parent`. Without it, "
		"`git log` walks merged-in history and could pick up an autofix "
		"commit from the base branch (e.g. a `[ai-autofix]` on `main` "
		"pulled in via merge), polluting LAST_RUN_DIFF on a "
		"first-iteration PR."
	)


def test_last_run_diff_uses_caret_dotdotdot_range() -> None:
	"""The diff range must be `${SHA}^...${SHA}` so it shows the autofix
	commit's own changes against its parent. A range against HEAD would
	leak any post-autofix commits (manual pushes, bait) back into the
	diff and re-introduce the OSCILLATION GUARD false-trip."""
	block = _generate_diff_block(_review_autofix_text())
	assert '"${LAST_AUTOFIX_SHA}^...${LAST_AUTOFIX_SHA}"' in block, (
		"LAST_RUN_DIFF must diff `${SHA}^...${SHA}` to scope the diff to "
		"the autofix commit alone. Any other range can leak unrelated "
		"commits into the editor's OSCILLATION GUARD context."
	)


def test_last_run_diff_no_unconditional_head1_diff() -> None:
	"""The unconditional `git diff HEAD~1...HEAD` must not be present.
	That was the regression-source: it fed the smoke gate's bait commit
	(authored as `[E2E Smoke Test] inject editor bait line ...`, NOT
	`[ai-autofix]`) into the editor's OSCILLATION GUARD context."""
	block = _generate_diff_block(_review_autofix_text())
	assert 'git diff "HEAD~1...HEAD"' not in block, (
		"`Generate diff context` step still has the unconditional "
		'`git diff "HEAD~1...HEAD"` for LAST_RUN_DIFF. This is exactly '
		"the regression that caused review_autofix run 25254574828 to "
		"silently no-op against the smoke-test bait commit."
	)


def test_last_run_diff_placeholder_matches_first_iteration_sentinel() -> None:
	"""The fall-through placeholder must begin with "No previous AI autofix"
	so `scripts/review_run_reviewers.sh:249`'s
	`^(No previous AI autofix|Initial run — no previous commit)` regex
	correctly flips IS_FIRST_ITERATION=true. Without this, reviewers get
	the iteration-2 prompt on a fresh smoke PR, which is also wrong."""
	block = _generate_diff_block(_review_autofix_text())
	assert '"No previous AI autofix run on PR head" > "${LAST_RUN_DIFF_FILE}"' in block, (
		"LAST_RUN_DIFF_FILE placeholder must start with `No previous AI autofix` "
		"so review_run_reviewers.sh's first-iteration sentinel regex matches."
	)
	# The stat-file sentinel must remain intact for downstream consumers.
	assert '"FIRST_RUN_NO_PREVIOUS_AI_CHANGES" > "${LAST_RUN_DIFF_STAT_FILE}"' in block, (
		"LAST_RUN_DIFF_STAT_FILE sentinel `FIRST_RUN_NO_PREVIOUS_AI_CHANGES` "
		"is part of the cross-script contract and must be preserved."
	)



# --- Final-CI repair delta range (final-ci-failure-ci-fix plan, Phase 3) ------

def _diff_context_run(tmp_path: Path, review_diff_base: str | None) -> tuple[str, dict[str, str]]:
	"""Run the step script in a clone whose PR branch has an earlier
	[ai-autofix] commit (lib.py), the approved head, and a repair commit
	(tests/test_lib.py) on top; returns (stdout, {output name: text})."""
	import os
	import subprocess

	def git(cwd: Path, *args: str) -> str:
		env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
		env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
		return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()

	origin = tmp_path / "origin.git"
	work = tmp_path / "work"
	subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
	subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
	(work / "README").write_text("base\n", encoding="utf-8")
	git(work, "add", "-A")
	git(work, "commit", "-q", "-m", "base")
	git(work, "push", "-q", "origin", "HEAD:main")
	git(work, "checkout", "-q", "-b", "topic")
	(work / "lib.py").write_text("VALUE = 2\n", encoding="utf-8")
	git(work, "add", "-A")
	git(work, "commit", "-q", "-m", "[ai-autofix] earlier review fix")
	approved = git(work, "rev-parse", "HEAD")
	(work / "tests").mkdir()
	(work / "tests" / "test_lib.py").write_text("def test_value():\n\tassert True\n", encoding="utf-8")
	git(work, "add", "-A")
	git(work, "commit", "-q", "-m", "[ai-merge-resolve] repair final CI failures")
	support = tmp_path / "support"
	support.mkdir()
	(support / "git_ref_health_check.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
	out = tmp_path / "out"
	out.mkdir()
	(out / "pr.diff").write_text("FULL PR DIFF FROM gh pr diff\n", encoding="utf-8")
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in ("BASH_ENV", "ENV")}
	env.update({
		"SUPPORT_SCRIPTS_DIR": str(support), "BASE_BRANCH": "main", "TARGET_BRANCH": "topic", "GITHUB_ENV": str(out / "github_env"),
		"PR_DIFF_FILE": str(out / "pr.diff"), "ORIGINAL_PR_DIFF_FILE": str(out / "original.diff"),
		"PR_CHANGED_FILES_FILE": str(out / "changed.txt"), "LAST_COMMIT_STAT_FILE": str(out / "stat.txt"),
		"LAST_RUN_DIFF_FILE": str(out / "lr.diff"), "LAST_RUN_CHANGED_FILES_FILE": str(out / "lr.txt"),
		"LAST_RUN_DIFF_STAT_FILE": str(out / "lr.stat"), "PR_DIFF_SOURCE": "gh_pr_diff",
	})
	if review_diff_base is not None:
		env["REVIEW_DIFF_BASE_SHA"] = approved if review_diff_base == "APPROVED" else review_diff_base
	script = REPO_ROOT / "scripts" / "review_autofix_step_generate_diff_context.sh"
	proc = subprocess.run(["bash", "-c", f'source "{script}"'], cwd=work, env=env, capture_output=True, text=True, check=False)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	names = {"pr": "pr.diff", "changed": "changed.txt", "original": "original.diff", "last": "lr.diff", "last_files": "lr.txt", "last_stat": "lr.stat"}
	return proc.stdout, {key: (out / name).read_text(encoding="utf-8") for key, name in names.items()}


def test_final_ci_repair_head_is_reviewed_on_its_delta(tmp_path: Path) -> None:
	stdout, files = _diff_context_run(tmp_path, "APPROVED")
	assert "REVIEW_DIFF_CONTEXT range=delta base=" in stdout
	# The repair commit is the previous AI run the reviewers focus on ...
	assert "tests/test_lib.py" in files["last"] and "VALUE = 2" not in files["last"]
	assert files["last_files"].split() == ["tests/test_lib.py"]
	assert "tests/test_lib.py" in files["last_stat"]
	# ... while the whole-PR inputs (risk tier, materiality, panel) are unchanged.
	assert files["pr"] == files["original"] == "FULL PR DIFF FROM gh pr diff\n"
	assert sorted(files["changed"].split()) == ["lib.py", "tests/test_lib.py"]


def test_diff_context_keeps_the_last_autofix_diff_otherwise(tmp_path: Path) -> None:
	cases = {None: "REVIEW_DIFF_CONTEXT range=merge_base\n", "not-a-sha": "reason=invalid_base", "f" * 40: "reason=not_an_ancestor"}
	for index, (base, expected) in enumerate(cases.items()):
		sub = tmp_path / str(index)
		sub.mkdir()
		stdout, files = _diff_context_run(sub, base)
		assert expected in stdout, (base, stdout)
		# LAST_RUN_DIFF stays the newest [ai-autofix] commit.
		assert "VALUE = 2" in files["last"] and "tests/test_lib.py" not in files["last"], base
		assert files["pr"] == "FULL PR DIFF FROM gh pr diff\n", base
		assert sorted(files["changed"].split()) == ["lib.py", "tests/test_lib.py"], base


def test_diff_context_step_receives_the_gate_output() -> None:
	text = REVIEW_AUTOFIX.read_text(encoding="utf-8")
	step = text[text.index("      - name: Generate diff context\n"):]
	step = step[:step.index("\n      - name: ", 10)]
	assert "REVIEW_DIFF_BASE_SHA: ${{ needs.gate.outputs.review_diff_base_sha }}" in step
	assert "      review_diff_base_sha: ${{ steps.evaluate.outputs.review_diff_base_sha }}" in text
	assert "FINAL_CI_REPAIR_DELTA_REVIEW_ENABLED: ${{ vars.FINAL_CI_REPAIR_DELTA_REVIEW_ENABLED || 'true' }}" in text

if __name__ == "__main__":
	test_last_run_diff_walks_back_to_autofix_commit()
	test_last_run_diff_uses_caret_dotdotdot_range()
	test_last_run_diff_no_unconditional_head1_diff()
	test_last_run_diff_placeholder_matches_first_iteration_sentinel()
	print("All LAST_RUN_DIFF OSCILLATION GUARD contract tests passed.")
