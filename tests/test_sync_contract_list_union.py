import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "sync_contract_list_union.py"


def _contract(read_entries: list[str], write_entries: list[str] | None = None, *, prefix: str = "") -> str:
	write_entries = write_entries or ["persist_leaderboard"]
	return (
		prefix
		+ "collection: fantasy_leaderboards\n"
		+ "purpose: Store fantasy leaderboard rankings.\n"
		+ "read_entrypoints:\n"
		+ "".join(f"  - {entry}\n" for entry in read_entries)
		+ "write_entrypoints:\n"
		+ "".join(f"  - {entry}\n" for entry in write_entries)
		+ "invariants:\n"
		+ "  - Rankings remain stable.\n"
	)


def _run_helper(
	tmp_path: Path,
	base_text: str,
	ours_text: str,
	theirs_text: str,
	*,
	contract_path: str = "db/contracts/fantasy_leaderboards.yml",
	env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
	base_path = tmp_path / "base.yml"
	ours_path = tmp_path / "ours.yml"
	theirs_path = tmp_path / "theirs.yml"
	output_path = tmp_path / "merged.yml"
	base_path.write_text(base_text, encoding="utf-8")
	ours_path.write_text(ours_text, encoding="utf-8")
	theirs_path.write_text(theirs_text, encoding="utf-8")
	result = subprocess.run(
		[
			sys.executable,
			str(HELPER),
			"--path",
			contract_path,
			"--base",
			str(base_path),
			"--ours",
			str(ours_path),
			"--theirs",
			str(theirs_path),
			"--out",
			str(output_path),
		],
		cwd=REPO_ROOT,
		capture_output=True,
		text=True,
		env=env,
		check=False,
	)
	return result, output_path


def _assert_ineligible(result: subprocess.CompletedProcess[str], output_path: Path, reason: str) -> None:
	assert result.returncode == 3
	assert result.stderr.strip().endswith(f"reason={reason}")
	assert result.stderr.count("SYNC_LIST_UNION_INELIGIBLE_V1:") == 1
	assert not output_path.exists()


def test_3862_adjacent_read_entrypoint_appends_are_unioned(tmp_path: Path) -> None:
	base_entries = ["api_fantasy_leaderboard", "_load_group_ranking"]
	ours_entries = ["api_fantasy_leaderboard", "_ranking_documents_for_pot", "_load_group_ranking"]
	theirs_entries = ["api_fantasy_leaderboard", "cosmodea_fantasy", "_load_group_ranking"]
	result, output_path = _run_helper(
		tmp_path,
		_contract(base_entries),
		_contract(ours_entries),
		_contract(theirs_entries),
	)
	assert result.returncode == 0, result.stderr
	merged = output_path.read_text(encoding="utf-8")
	assert merged.index("_ranking_documents_for_pot") < merged.index("cosmodea_fantasy") < merged.index("_load_group_ranking")
	assert "write_entrypoints:\n  - persist_leaderboard\n" in merged


def test_two_entrypoint_hunks_are_unioned(tmp_path: Path) -> None:
	base = _contract(
		["read_old", "read_2", "read_3", "read_4", "read_5"],
		["write_old", "write_2", "write_3", "write_4", "write_5"],
	)
	ours = _contract(
		["read_old", "read_ours", "read_2", "read_3", "read_4", "read_5"],
		["write_old", "write_ours", "write_2", "write_3", "write_4", "write_5"],
	)
	theirs = _contract(
		["read_old", "read_theirs", "read_2", "read_3", "read_4", "read_5"],
		["write_old", "write_theirs", "write_2", "write_3", "write_4", "write_5"],
	)
	result, output_path = _run_helper(tmp_path, base, ours, theirs)
	assert result.returncode == 0, result.stderr
	merged = output_path.read_text(encoding="utf-8")
	for expected in ("read_ours", "read_theirs", "write_ours", "write_theirs"):
		assert expected in merged


def test_same_entry_added_by_both_sides_appears_once(tmp_path: Path) -> None:
	base = _contract(["read_old"])
	ours = _contract(["read_old", "read_shared"])
	result, output_path = _run_helper(tmp_path, base, ours, ours)
	assert result.returncode == 0, result.stderr
	assert output_path.read_text(encoding="utf-8").count("  - read_shared\n") == 1


def test_hunk_outside_entrypoints_is_rejected(tmp_path: Path) -> None:
	base = "collection: x\nindexes:\n  - old\npurpose: safe\n"
	ours = "collection: x\nindexes:\n  - old\n  - ours\npurpose: safe\n"
	theirs = "collection: x\nindexes:\n  - old\n  - theirs\npurpose: safe\n"
	result, output_path = _run_helper(tmp_path, base, ours, theirs)
	_assert_ineligible(result, output_path, "hunk_outside_entrypoints")


def test_hunk_with_non_list_line_is_rejected(tmp_path: Path) -> None:
	base = _contract(["old"])
	ours = base.replace("  - old\n", "  - old\n  # ours\n", 1)
	theirs = base.replace("  - old\n", "  - old\n  # theirs\n", 1)
	result, output_path = _run_helper(tmp_path, base, ours, theirs)
	_assert_ineligible(result, output_path, "hunk_non_list_line")


def test_one_sided_hunk_is_rejected(tmp_path: Path) -> None:
	base = _contract(["old"])
	ours = _contract([])
	theirs = _contract(["replacement"])
	result, output_path = _run_helper(tmp_path, base, ours, theirs)
	_assert_ineligible(result, output_path, "hunk_one_sided")


def test_non_contract_paths_are_rejected(tmp_path: Path) -> None:
	base = _contract(["old"])
	ours = _contract(["old", "ours"])
	theirs = _contract(["old", "theirs"])
	for index, contract_path in enumerate(("backend/foo.py", "db/other.yml")):
		case_dir = tmp_path / str(index)
		case_dir.mkdir()
		result, output_path = _run_helper(case_dir, base, ours, theirs, contract_path=contract_path)
		_assert_ineligible(result, output_path, "path_not_contract")


def test_empty_base_is_rejected(tmp_path: Path) -> None:
	ours = _contract(["ours"])
	theirs = _contract(["theirs"])
	result, output_path = _run_helper(tmp_path, "", ours, theirs)
	_assert_ineligible(result, output_path, "base_missing")


def test_missing_pyyaml_is_rejected_without_output(tmp_path: Path) -> None:
	shadow_dir = tmp_path / "shadow"
	shadow_dir.mkdir()
	(shadow_dir / "yaml.py").write_text("raise ImportError('blocked for test')\n", encoding="utf-8")
	base = _contract(["old"])
	ours = _contract(["old", "ours"])
	theirs = _contract(["old", "theirs"])
	environment = os.environ.copy()
	environment["PYTHONPATH"] = str(shadow_dir)
	result, output_path = _run_helper(tmp_path, base, ours, theirs, env=environment)
	_assert_ineligible(result, output_path, "pyyaml_missing")


def test_invalid_merged_yaml_is_rejected(tmp_path: Path) -> None:
	prefix = "broken: [unterminated\n"
	base = _contract(["old"], prefix=prefix)
	ours = _contract(["old", "ours"], prefix=prefix)
	theirs = _contract(["old", "theirs"], prefix=prefix)
	result, output_path = _run_helper(tmp_path, base, ours, theirs)
	_assert_ineligible(result, output_path, "yaml_parse_failed")


def test_divergent_replacements_are_not_pure_appends(tmp_path: Path) -> None:
	base = _contract(["old"])
	ours = _contract(["ours"])
	theirs = _contract(["theirs"])
	result, output_path = _run_helper(tmp_path, base, ours, theirs)
	_assert_ineligible(result, output_path, "list_not_union")


def test_residual_marker_is_rejected(tmp_path: Path) -> None:
	prefix = "<<<<<<< literal\n"
	base = _contract(["old"], prefix=prefix)
	ours = _contract(["old", "ours"], prefix=prefix)
	theirs = _contract(["old", "theirs"], prefix=prefix)
	result, output_path = _run_helper(tmp_path, base, ours, theirs)
	_assert_ineligible(result, output_path, "markers_remain")
