#!/usr/bin/env python3
"""Unit tests for scripts/sync_contract_list_union.py (stuck-states P12).

The helper merges the three index stages of an unmerged
``db/contracts/*.yml`` path when every conflict hunk only appends to a
``read_entrypoints`` / ``write_entrypoints`` list, and declares the path
ineligible (exit 3, one ``SYNC_LIST_UNION_INELIGIBLE_V1`` line) otherwise.
Only local ``git merge-file`` runs; no network.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "sync_contract_list_union.py"


def _load_module():
	spec = importlib.util.spec_from_file_location("sync_contract_list_union_under_test", HELPER)
	module = importlib.util.module_from_spec(spec)
	assert spec.loader is not None
	spec.loader.exec_module(module)
	return module


# Shaped after the consumer conflict in #3862 (fantasy_leaderboards.yml):
# both sides appended a different entrypoint to the same list.
BASE = (
	"collection: fantasy_leaderboards\n"
	"purpose: Weekly leaderboard snapshots per league.\n"
	"indexes:\n"
	"  - name: league_week_unique\n"
	"    keys:\n"
	"      league_id: 1\n"
	"      week: 1\n"
	"    unique: true\n"
	"read_entrypoints:\n"
	"  - backend/app/leaderboards/service.py:get_leaderboard\n"
	"write_entrypoints:\n"
	"  - backend/app/leaderboards/jobs.py:snapshot_week\n"
	"invariants:\n"
	"  - One snapshot per league and week.\n"
)
READ_BASE = "  - backend/app/leaderboards/service.py:get_leaderboard\n"
WRITE_BASE = "  - backend/app/leaderboards/jobs.py:snapshot_week\n"


def _append_after(text: str, anchor: str, *entries: str) -> str:
	assert anchor in text
	return text.replace(anchor, anchor + "".join(entries), 1)


def _run(tmp_path: Path, base: str, ours: str, theirs: str, path: str = "db/contracts/fantasy_leaderboards.yml"):
	files = {}
	for name, content in (("base", base), ("ours", ours), ("theirs", theirs)):
		target = tmp_path / f"{name}.yml"
		target.write_text(content, encoding="utf-8")
		files[name] = target
	out = tmp_path / "merged.yml"
	proc = subprocess.run(
		[sys.executable, str(HELPER), f"--path={path}", "--base", str(files["base"]),
			"--ours", str(files["ours"]), "--theirs", str(files["theirs"]), "--out", str(out)],
		capture_output=True, text=True, check=False,
	)
	return proc, out


def _assert_ineligible(proc, out: Path, reason: str) -> None:
	assert proc.returncode == 3, (proc.returncode, proc.stderr)
	assert f"reason={reason}" in proc.stderr, proc.stderr
	lines = [line for line in proc.stderr.splitlines() if line.startswith("SYNC_LIST_UNION_INELIGIBLE_V1: ")]
	assert len(lines) == 1, proc.stderr
	assert not out.exists()


def test_issue_3862_shape_merges_union(tmp_path: Path) -> None:
	yaml = pytest.importorskip("yaml")
	ours = _append_after(BASE, READ_BASE, "  - backend/app/leaderboards/api.py:list_leaderboards\n")
	theirs = _append_after(BASE, READ_BASE, "  - backend/app/fantasy/rollup.py:weekly_rollup\n")
	proc, out = _run(tmp_path, BASE, ours, theirs)
	assert proc.returncode == 0, proc.stderr
	merged_text = out.read_text(encoding="utf-8")
	assert "<<<<<<<" not in merged_text and ">>>>>>>" not in merged_text
	merged = yaml.safe_load(merged_text)
	assert merged["read_entrypoints"] == [
		"backend/app/leaderboards/service.py:get_leaderboard",
		"backend/app/leaderboards/api.py:list_leaderboards",
		"backend/app/fantasy/rollup.py:weekly_rollup",
	]
	base = yaml.safe_load(BASE)
	for key in ("collection", "purpose", "indexes", "write_entrypoints", "invariants"):
		assert merged[key] == base[key]


def test_two_hunks_one_per_list(tmp_path: Path) -> None:
	yaml = pytest.importorskip("yaml")
	# git merge-file folds conflicts only a few lines apart into one hunk
	# (which would include the write_entrypoints: key and be ineligible), so
	# the write list carries enough entries to keep the two hunks apart.
	write_entries = "".join(f"  - backend/app/leaderboards/jobs.py:job_{n}\n" for n in range(4))
	base = BASE.replace(WRITE_BASE, write_entries + WRITE_BASE)
	ours = _append_after(_append_after(base, READ_BASE, "  - api.py:read_ours\n"), WRITE_BASE, "  - jobs.py:write_ours\n")
	theirs = _append_after(_append_after(base, READ_BASE, "  - api.py:read_theirs\n"), WRITE_BASE, "  - jobs.py:write_theirs\n")
	proc, out = _run(tmp_path, base, ours, theirs)
	assert proc.returncode == 0, proc.stderr
	merged = yaml.safe_load(out.read_text(encoding="utf-8"))
	assert merged["read_entrypoints"][1:] == ["api.py:read_ours", "api.py:read_theirs"]
	assert merged["write_entrypoints"][-2:] == ["jobs.py:write_ours", "jobs.py:write_theirs"]
	assert len(merged["write_entrypoints"]) == 7


def test_shared_duplicate_entry_kept_once(tmp_path: Path) -> None:
	yaml = pytest.importorskip("yaml")
	ours = _append_after(BASE, READ_BASE, "  - shared.py:both\n", "  - api.py:read_ours\n")
	theirs = _append_after(BASE, READ_BASE, "  - shared.py:both\n", "  - api.py:read_theirs\n")
	proc, out = _run(tmp_path, BASE, ours, theirs)
	assert proc.returncode == 0, proc.stderr
	merged = yaml.safe_load(out.read_text(encoding="utf-8"))
	entries = merged["read_entrypoints"]
	assert entries.count("shared.py:both") == 1
	assert set(entries) == {
		"backend/app/leaderboards/service.py:get_leaderboard", "shared.py:both", "api.py:read_ours", "api.py:read_theirs",
	}


def test_hunk_outside_entrypoint_lists_is_ineligible(tmp_path: Path) -> None:
	pytest.importorskip("yaml")
	anchor = "  - One snapshot per league and week.\n"
	ours = _append_after(BASE, anchor, "  - Ours invariant.\n")
	theirs = _append_after(BASE, anchor, "  - Theirs invariant.\n")
	proc, out = _run(tmp_path, BASE, ours, theirs)
	_assert_ineligible(proc, out, "hunk_outside_entrypoints")


def test_hunk_inside_indexes_is_ineligible(tmp_path: Path) -> None:
	pytest.importorskip("yaml")
	anchor = "    unique: true\n"
	ours = _append_after(BASE, anchor, "  - name: by_ours\n")
	theirs = _append_after(BASE, anchor, "  - name: by_theirs\n")
	proc, out = _run(tmp_path, BASE, ours, theirs)
	_assert_ineligible(proc, out, "hunk_outside_entrypoints")


def test_non_list_line_is_ineligible(tmp_path: Path) -> None:
	pytest.importorskip("yaml")
	ours = BASE.replace("purpose: Weekly leaderboard snapshots per league.", "purpose: Ours purpose.")
	theirs = BASE.replace("purpose: Weekly leaderboard snapshots per league.", "purpose: Theirs purpose.")
	proc, out = _run(tmp_path, BASE, ours, theirs)
	_assert_ineligible(proc, out, "hunk_non_list_line")


def test_one_sided_hunk_is_ineligible(tmp_path: Path) -> None:
	pytest.importorskip("yaml")
	ours = BASE.replace(READ_BASE, "")
	theirs = BASE.replace(READ_BASE, "  - backend/app/leaderboards/service.py:get_leaderboard_v2\n")
	proc, out = _run(tmp_path, BASE, ours, theirs)
	_assert_ineligible(proc, out, "hunk_one_sided")


def test_non_contract_path_is_ineligible(tmp_path: Path) -> None:
	ours = _append_after(BASE, READ_BASE, "  - a.py:x\n")
	theirs = _append_after(BASE, READ_BASE, "  - b.py:y\n")
	proc, out = _run(tmp_path, BASE, ours, theirs, path="backend/app.yml")
	_assert_ineligible(proc, out, "path_not_contract")


@pytest.mark.parametrize("path", [
	"db/contracts/../../etc/passwd.yml",
	"db/contracts/nested/x.yml",
	"-db/contracts/x.yml",
	"db/contracts/x.yml\nreason=forged",
])
def test_path_traversal_and_odd_names_are_rejected(tmp_path: Path, path: str) -> None:
	proc, out = _run(tmp_path, BASE, BASE, BASE, path=path)
	_assert_ineligible(proc, out, "path_not_contract")
	assert "\nreason=forged" not in proc.stderr


def test_empty_base_is_ineligible(tmp_path: Path) -> None:
	ours = _append_after(BASE, READ_BASE, "  - a.py:x\n")
	theirs = _append_after(BASE, READ_BASE, "  - b.py:y\n")
	proc, out = _run(tmp_path, "", ours, theirs)
	_assert_ineligible(proc, out, "base_missing")


def test_pyyaml_missing_is_ineligible_and_writes_nothing(tmp_path: Path, capsys) -> None:
	module = _load_module()
	module.yaml = None
	for name, content in (("base", BASE), ("ours", _append_after(BASE, READ_BASE, "  - a.py:x\n")),
			("theirs", _append_after(BASE, READ_BASE, "  - b.py:y\n"))):
		(tmp_path / f"{name}.yml").write_text(content, encoding="utf-8")
	out = tmp_path / "merged.yml"
	rc = module.main([
		"--path", "db/contracts/fantasy_leaderboards.yml",
		"--base", str(tmp_path / "base.yml"), "--ours", str(tmp_path / "ours.yml"),
		"--theirs", str(tmp_path / "theirs.yml"), "--out", str(out),
	])
	assert rc == 3
	assert "SYNC_LIST_UNION_INELIGIBLE_V1: path=db/contracts/fantasy_leaderboards.yml reason=pyyaml_missing" in capsys.readouterr().err
	assert not out.exists()


def test_mismatched_indentation_fails_yaml_parse(tmp_path: Path) -> None:
	pytest.importorskip("yaml")
	base = "collection: x\nread_entrypoints:\n    - a.py:a\nwrite_entrypoints:\n    - a.py:w\n"
	ours = base.replace("    - a.py:a\n", "    - a.py:a\n  - b.py:b\n")
	theirs = base.replace("    - a.py:a\n", "    - a.py:a\n  - c.py:c\n")
	proc, out = _run(tmp_path, base, ours, theirs, path="db/contracts/x.yaml")
	_assert_ineligible(proc, out, "yaml_parse_failed")


def test_mixed_indentation_inside_hunk_is_ineligible(tmp_path: Path) -> None:
	pytest.importorskip("yaml")
	ours = _append_after(BASE, READ_BASE, "  - a.py:x\n")
	theirs = _append_after(BASE, READ_BASE, "    - b.py:y\n")
	proc, out = _run(tmp_path, BASE, ours, theirs)
	_assert_ineligible(proc, out, "hunk_non_list_line")


def test_both_sides_replacing_a_base_entry_is_ineligible(tmp_path: Path) -> None:
	# Both sides replaced the same base entry: not an append, so the union
	# (which would drop the base entry) must not be written.
	pytest.importorskip("yaml")
	ours = BASE.replace(READ_BASE, "  - backend/app/leaderboards/api.py:replaced_ours\n")
	theirs = BASE.replace(READ_BASE, "  - backend/app/leaderboards/api.py:replaced_theirs\n")
	proc, out = _run(tmp_path, BASE, ours, theirs)
	_assert_ineligible(proc, out, "list_not_union")


def test_delete_plus_append_on_one_side_is_ineligible(tmp_path: Path) -> None:
	# Ours removed the base entry and appended another; theirs only appended.
	# A union would bring the deleted entry back, so the path is ineligible.
	pytest.importorskip("yaml")
	ours = BASE.replace(READ_BASE, "  - backend/app/leaderboards/api.py:read_ours\n")
	theirs = _append_after(BASE, READ_BASE, "  - backend/app/fantasy/rollup.py:read_theirs\n")
	proc, out = _run(tmp_path, BASE, ours, theirs)
	assert proc.returncode == 3, (proc.returncode, proc.stderr)
	assert "reason=list_not_union" in proc.stderr or "reason=hunk_one_sided" in proc.stderr, proc.stderr
	assert not out.exists()


def test_bare_base_list_first_entry_appends_merge(tmp_path: Path) -> None:
	# A bare ``read_entrypoints:`` key in the merge base loads as None; both
	# sides adding their first entry is still a pure append and must merge.
	yaml = pytest.importorskip("yaml")
	base = BASE.replace(READ_BASE, "")
	anchor = "read_entrypoints:\n"
	ours = _append_after(base, anchor, "  - backend/app/leaderboards/api.py:read_ours\n")
	theirs = _append_after(base, anchor, "  - backend/app/fantasy/rollup.py:read_theirs\n")
	proc, out = _run(tmp_path, base, ours, theirs)
	assert proc.returncode == 0, proc.stderr
	merged = yaml.safe_load(out.read_text(encoding="utf-8"))
	assert merged["read_entrypoints"] == [
		"backend/app/leaderboards/api.py:read_ours",
		"backend/app/fantasy/rollup.py:read_theirs",
	]
	assert merged["write_entrypoints"] == yaml.safe_load(base)["write_entrypoints"]


def test_reorder_plus_append_is_ineligible(tmp_path: Path) -> None:
	# Base [a, b]; ours reorders to [b, a] and appends c, theirs appends d.
	# Every base entry survives as a set, but a reorder is not an append, so
	# the path must go to the LLM resolver instead of taking ours' order.
	pytest.importorskip("yaml")
	base = "collection: x\nread_entrypoints:\n  - a.py:a\n  - b.py:b\nwrite_entrypoints:\n  - w.py:w\n"
	ours = base.replace("  - a.py:a\n  - b.py:b\n", "  - b.py:b\n  - a.py:a\n  - c.py:c\n")
	theirs = base.replace("  - a.py:a\n  - b.py:b\n", "  - a.py:a\n  - b.py:b\n  - d.py:d\n")
	proc, out = _run(tmp_path, base, ours, theirs, path="db/contracts/x.yml")
	_assert_ineligible(proc, out, "list_not_union")


def test_interior_insertion_on_both_sides_merges_union(tmp_path: Path) -> None:
	# Base [a, b, c]; ours inserts x and theirs inserts y between a and b.
	# Every base entry keeps its relative order on both sides and nothing is
	# removed, so the documented policy (add-only, base order kept) merges it
	# as [a, x, y, b, c] instead of dispatching the LLM resolver.
	yaml = pytest.importorskip("yaml")
	base = "collection: x\nread_entrypoints:\n  - a.py:a\n  - b.py:b\n  - c.py:c\nwrite_entrypoints:\n  - w.py:w\n"
	ours = base.replace("  - a.py:a\n", "  - a.py:a\n  - x.py:x\n")
	theirs = base.replace("  - a.py:a\n", "  - a.py:a\n  - y.py:y\n")
	proc, out = _run(tmp_path, base, ours, theirs, path="db/contracts/x.yml")
	assert proc.returncode == 0, proc.stderr
	merged = yaml.safe_load(out.read_text(encoding="utf-8"))
	assert merged["read_entrypoints"] == ["a.py:a", "x.py:x", "y.py:y", "b.py:b", "c.py:c"]
	assert merged["write_entrypoints"] == ["w.py:w"]
