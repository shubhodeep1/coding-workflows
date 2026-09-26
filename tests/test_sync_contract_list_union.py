import os
from pathlib import Path
import subprocess
import sys

import pytest

import scripts.sync_contract_list_union as list_union_helper


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


def test_block_scalar_list_shaped_conflict_is_rejected(tmp_path: Path) -> None:
	base = _contract(["old"]) + "notes: |\n  - original\n"
	ours = _contract(["old"]) + "notes: |\n  - ours\n"
	theirs = _contract(["old"]) + "notes: |\n  - theirs\n"
	result, output_path = _run_helper(tmp_path, base, ours, theirs)
	_assert_ineligible(result, output_path, "hunk_outside_entrypoints")


def test_nested_entrypoint_named_sequence_conflict_is_rejected(tmp_path: Path) -> None:
	base = _contract(["old"]) + "metadata:\n  read_entrypoints:\n    - original\n"
	ours = _contract(["old"]) + "metadata:\n  read_entrypoints:\n    - ours\n"
	theirs = _contract(["old"]) + "metadata:\n  read_entrypoints:\n    - theirs\n"
	result, output_path = _run_helper(tmp_path, base, ours, theirs)
	_assert_ineligible(result, output_path, "hunk_outside_entrypoints")


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


def test_loader_constructor_parse_failure_is_ineligible() -> None:
	import yaml

	import scripts.sync_contract_list_union as list_union_helper

	with pytest.raises(list_union_helper.IneligibleError, match="yaml_parse_failed"):
		list_union_helper._safe_load(yaml, "\x00")


@pytest.mark.parametrize("duplicate_source", ["base", "ours", "theirs"])
def test_duplicate_top_level_key_in_each_input_is_rejected(tmp_path: Path, duplicate_source: str) -> None:
	inputs = {
		"base": _contract(["old"]),
		"ours": _contract(["old", "ours"]),
		"theirs": _contract(["old", "theirs"]),
	}
	inputs[duplicate_source] = inputs[duplicate_source].replace(
		"purpose: Store fantasy leaderboard rankings.\n",
		"purpose: Store fantasy leaderboard rankings.\npurpose: Decoy value.\n",
		1,
	)
	result, output_path = _run_helper(tmp_path, inputs["base"], inputs["ours"], inputs["theirs"])
	_assert_ineligible(result, output_path, "duplicate_mapping_key")


def test_duplicate_nested_mapping_key_is_rejected(tmp_path: Path) -> None:
	prefix = "metadata:\n  owner: first\n  owner: second\n"
	result, output_path = _run_helper(
		tmp_path,
		_contract(["old"], prefix=prefix),
		_contract(["old", "ours"], prefix=prefix),
		_contract(["old", "theirs"], prefix=prefix),
	)
	_assert_ineligible(result, output_path, "duplicate_mapping_key")


def test_merge_expanded_duplicate_mapping_key_is_rejected(tmp_path: Path) -> None:
	prefix = "defaults: &defaults\n  owner: first\nmetadata:\n  <<: *defaults\n  owner: second\n"
	result, output_path = _run_helper(
		tmp_path,
		_contract(["old"], prefix=prefix),
		_contract(["old", "ours"], prefix=prefix),
		_contract(["old", "theirs"], prefix=prefix),
	)
	_assert_ineligible(result, output_path, "duplicate_mapping_key")


def test_duplicate_key_created_in_merged_text_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	base = _contract(["old"])
	ours = _contract(["old", "ours"])
	theirs = _contract(["old", "theirs"])
	generated_with_duplicate = _contract(["old", "ours", "theirs"]).replace(
		"purpose: Store fantasy leaderboard rankings.\n",
		"purpose: Store fantasy leaderboard rankings.\npurpose: Decoy value.\n",
		1,
	)

	import scripts.sync_contract_list_union as list_union_helper

	monkeypatch.setattr(
		list_union_helper,
		"_parse_and_merge_hunks",
		lambda _marked_text: (generated_with_duplicate, generated_with_duplicate, {"read_entrypoints"}),
	)
	args = list_union_helper.argparse.Namespace(
		path="db/contracts/fantasy_leaderboards.yml",
		base=str(tmp_path / "base.yml"),
		ours=str(tmp_path / "ours.yml"),
		theirs=str(tmp_path / "theirs.yml"),
		out=str(tmp_path / "merged.yml"),
	)
	for path, text in ((Path(args.base), base), (Path(args.ours), ours), (Path(args.theirs), theirs)):
		path.write_text(text, encoding="utf-8")

	with pytest.raises(list_union_helper.IneligibleError, match="duplicate_mapping_key"):
		list_union_helper._merge(args)
	assert not Path(args.out).exists()


@pytest.mark.parametrize(
	("safe_prefix", "changed_prefix"),
	[
		("metadata: safe\npadding: stable\n", "metadata: changed\npadding: stable\n"),
		("metadata:\n  flags: [safe]\n", "metadata:\n  flags: [safe, changed]\n"),
		("metadata: {owner: safe}\npadding: stable\n", "metadata: {owner: changed}\npadding: stable\n"),
	],
)
def test_non_entrypoint_nested_value_difference_is_rejected(safe_prefix: str, changed_prefix: str) -> None:
	import yaml

	base = _contract(["old"], prefix=safe_prefix)
	ours = _contract(["old", "ours"], prefix=safe_prefix)
	theirs = _contract(["old", "theirs"], prefix=safe_prefix)
	merged = _contract(["old", "ours", "theirs"], prefix=changed_prefix)
	with pytest.raises(list_union_helper.IneligibleError, match="list_not_union"):
		list_union_helper._validate_result(
			yaml,
			base,
			ours,
			theirs,
			merged,
			ours,
			[(5, 8)],
		)


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


@pytest.mark.parametrize("unsupported_entry", ["[nested]", "{nested: value}", "null", "7", "true"])
def test_non_string_entrypoints_are_rejected(tmp_path: Path, unsupported_entry: str) -> None:
	base = _contract(["old"])
	ours = _contract(["old", unsupported_entry])
	theirs = _contract(["old", "theirs"])
	result, output_path = _run_helper(tmp_path, base, ours, theirs)
	_assert_ineligible(result, output_path, "entrypoint_not_string")


def test_recursive_alias_entrypoint_is_rejected_without_recursive_equality(tmp_path: Path) -> None:
	prefix = "recursive: &recursive\n  - *recursive\n"
	base = _contract(["old"], prefix=prefix)
	ours = _contract(["old", "*recursive"], prefix=prefix)
	theirs = _contract(["old", "theirs"], prefix=prefix)
	result, output_path = _run_helper(tmp_path, base, ours, theirs)
	_assert_ineligible(result, output_path, "entrypoint_not_string")


def test_non_string_unaffected_write_entrypoint_is_rejected(tmp_path: Path) -> None:
	base = _contract(["old"], ["null"])
	ours = _contract(["old", "ours"], ["null"])
	theirs = _contract(["old", "theirs"], ["null"])
	result, output_path = _run_helper(tmp_path, base, ours, theirs)
	_assert_ineligible(result, output_path, "entrypoint_not_string")


def test_oversized_input_file_is_rejected_before_yaml_parsing(tmp_path: Path) -> None:
	oversized_prefix = "#" * (list_union_helper.MAX_INPUT_BYTES + 1) + "\n"
	base = _contract(["old"], prefix=oversized_prefix)
	result, output_path = _run_helper(
		tmp_path,
		base,
		_contract(["old", "ours"]),
		_contract(["old", "theirs"]),
	)
	_assert_ineligible(result, output_path, "input_too_large")


def test_oversized_entrypoint_list_is_rejected(tmp_path: Path) -> None:
	base = _contract(["old"])
	ours_entries = ["old", *[f"ours_{index}" for index in range(list_union_helper.MAX_ENTRYPOINT_ENTRIES)]]
	result, output_path = _run_helper(
		tmp_path,
		base,
		_contract(ours_entries),
		_contract(["old", "theirs"]),
	)
	_assert_ineligible(result, output_path, "entrypoint_list_too_large")


def test_oversized_entrypoint_string_is_rejected(tmp_path: Path) -> None:
	base = _contract(["old"])
	oversized_entry = "x" * (list_union_helper.MAX_ENTRYPOINT_LENGTH + 1)
	result, output_path = _run_helper(
		tmp_path,
		base,
		_contract(["old", oversized_entry]),
		_contract(["old", "theirs"]),
	)
	_assert_ineligible(result, output_path, "entrypoint_too_long")
