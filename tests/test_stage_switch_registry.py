"""Stage switch discovery and registry contract (issues #9994, #10127).

Since #10127 the watch discovers its switches at run time
(`scripts/stage_switch_watch.py` `discover_switches`): every
`vars.<NAME>_ENABLED || 'true'` expression (also the chained form
`vars.<NAME>_ENABLED || (...) || 'true'`) and every `vars.<NAME>_ENABLED !=
'false'` test in .github/workflows/*.yml and *.yaml, and every `_ENABLED` row
whose default is `true` in the README "#### Variables" and "## Required
Variables" tables. A new switch therefore needs no registry edit.
scripts/stage_switches.json keeps only detail that cannot be derived (a
hand-written `stage`, `on_values`, `off_on_purpose`); every entry and marker
must name a discovered switch or carry `pending: true`. Also locks the
workflow wiring in workflow-log-analysis.yml.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

try:
	import yaml
except ImportError:  # credential-free gate containers ship without PyYAML
	yaml = None

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import stage_switch_watch as ssw  # noqa: E402

README = ROOT / "README.md"
# workflow-templates/ is left out on purpose: those wrappers run in consumer
# repositories and read the consumer's variables, while the watch lists only
# this repository's actions/variables, so a template-only switch is unwatchable.
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
REGISTRY = ROOT / "scripts" / "stage_switches.json"
WATCH_WORKFLOW = WORKFLOWS_DIR / "workflow-log-analysis.yml"

# Aliases kept for existing callers (§10); the definitions live in the script.
SWITCH_DEFAULT_RE = ssw.SWITCH_DEFAULT_RE
SWITCH_NOT_FALSE_RE = ssw.SWITCH_NOT_FALSE_RE
README_SWITCH_TABLES = ssw.README_SWITCH_TABLES
README_NOT_REPOSITORY_VARIABLES = ssw.README_NOT_REPOSITORY_VARIABLES
WATCH_STEP_NAMES = (
	"Read stage switch variables",
	"Look up today's stage switch alert",
	"Report stage switches left off",
	"Record today's stage switch alert",
)


def _registry() -> dict:
	return json.loads(REGISTRY.read_text(encoding="utf-8"))


def _registry_names() -> set[str]:
	return {entry["name"] for entry in _registry()["switches"]}


def _workflow_true_switches(workflows_dir: Path) -> dict[str, set[str]]:
	"""Map each switch defaulting to 'true' in some workflow to those files."""
	return ssw._workflow_true_switches(workflows_dir)


def _missing_from_registry(workflows_dir: Path, names: set[str]) -> list[str]:
	return sorted(set(_workflow_true_switches(workflows_dir)) - names)


def _readme_variables_rows(heading: str = "#### Variables") -> dict[str, list[str]]:
	rows = ssw._readme_variables_rows(README, heading)
	# The repository README must keep both tables the discovery reads.
	assert rows is not None, heading
	return rows


def test_discovery_finds_sample_switches():
	found = ssw.discover_switches(ROOT)
	assert "FINAL_CI_FAILED_REDISPATCH_ENABLED" in found
	assert "CLAUDE_POOL_CAPACITY_WAIT_ENABLED" in found  # chained form
	assert "E2E_ALT_MODEL_ENABLED" in found  # `!= 'false'` form
	assert "not_false" in found["E2E_ALT_MODEL_ENABLED"]["forms"]
	assert "HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED" in found  # Required Variables table
	assert "readme" in found["HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED"]["forms"]
	# Each exclusion must still name a documented default-on README row, and
	# no excluded name is discovered.
	readme_true = set()
	for heading in README_SWITCH_TABLES:
		for name, defaults in _readme_variables_rows(heading).items():
			if name.endswith("_ENABLED") and any(d.lower().startswith("true") for d in defaults):
				readme_true.add(name)
	assert set(README_NOT_REPOSITORY_VARIABLES) <= readme_true
	assert not set(README_NOT_REPOSITORY_VARIABLES) & set(found)


def _undiscovered_without_pending(data: dict, discovered: set[str]) -> tuple[list, list]:
	"""Registry entries and markers naming an undiscovered switch without `pending: true`."""
	stray = [
		entry["name"] for entry in data["switches"]
		if entry["name"] not in discovered and entry.get("pending") is not True
	]
	stray_markers = [
		marker.get("name") for marker in data.get("off_on_purpose", [])
		if marker.get("name") not in discovered and marker.get("pending") is not True
	]
	return stray, stray_markers


def test_registry_entries_name_discovered_or_pending():
	discovered = set(ssw.discover_switches(ROOT))
	stray, stray_markers = _undiscovered_without_pending(_registry(), discovered)
	assert stray == [], "registry entries for undiscovered switches need \"pending\": true"
	assert stray_markers == [], "off_on_purpose markers for undiscovered switches need \"pending\": true"


def test_undiscovered_entry_needs_pending(tmp_path):
	workflows = tmp_path / ".github" / "workflows"
	workflows.mkdir(parents=True)
	(workflows / "a.yml").write_text("env:\n  X: ${{ vars.KNOWN_STAGE_ENABLED || 'true' }}\n", encoding="utf-8")
	discovered = set(ssw.discover_switches(tmp_path))
	data = {
		"switches": [{"name": "KNOWN_STAGE_ENABLED"}, {"name": "UNKNOWN_STAGE_ENABLED"}],
		"off_on_purpose": [{"name": "UNKNOWN_STAGE_ENABLED", "until": "2099-01-01", "reason": "r"}],
	}
	assert _undiscovered_without_pending(data, discovered) == (["UNKNOWN_STAGE_ENABLED"], ["UNKNOWN_STAGE_ENABLED"])
	data["switches"][1]["pending"] = True
	data["off_on_purpose"][0]["pending"] = True
	assert _undiscovered_without_pending(data, discovered) == ([], [])


def test_registry_check_fails_on_unlisted_workflow_switch(tmp_path):
	# Kept under its #9994 name: the same fixture is now discovered, not
	# reported missing from the registry (#10127).
	workflows = tmp_path / ".github" / "workflows"
	workflows.mkdir(parents=True)
	(workflows / "new.yml").write_text(
		"jobs:\n  x:\n    env:\n"
		"      NEW_STAGE_ENABLED: ${{ vars.NEW_STAGE_ENABLED || 'true' }}\n"
		"      CHAINED_STAGE_ENABLED: ${{ vars.CHAINED_STAGE_ENABLED || (vars.WORKFLOW_PROFILE == 'core' && 'false') || 'true' }}\n"
		"      OPT_IN_ENABLED: ${{ vars.OPT_IN_ENABLED || 'false' }}\n"
		"      MULTI_LINE_STAGE_ENABLED: >-\n        ${{ vars.MULTI_LINE_STAGE_ENABLED\n          || 'true' }}\n"
		"    if: ${{ vars.NOT_FALSE_STAGE_ENABLED != 'false' }}\n"
		"      # COMMENTED_ENABLED: ${{ vars.COMMENTED_ENABLED || 'true' }}\n",
		encoding="utf-8",
	)
	found = ssw.discover_switches(tmp_path)
	assert sorted(found) == ["CHAINED_STAGE_ENABLED", "MULTI_LINE_STAGE_ENABLED", "NEW_STAGE_ENABLED", "NOT_FALSE_STAGE_ENABLED"]
	assert "COMMENTED_ENABLED" not in found and "OPT_IN_ENABLED" not in found
	# The legacy helper still reports them missing from the real registry.
	assert _missing_from_registry(workflows, _registry_names()) == sorted(found)


def test_registry_entries_are_well_formed():
	data = _registry()
	assert data["schema"] == "stage_switches.v1"
	names = [entry["name"] for entry in data["switches"]]
	assert len(names) == len(set(names))
	for entry in data["switches"]:
		assert {"name"} <= set(entry) <= {"name", "default", "stage", "on_values", "pending"}, entry
		assert set(entry.get("on_values", [])) <= {"1", "yes", "on"}, entry
		assert re.fullmatch(r"[A-Z][A-Z0-9_]*", entry["name"])
		if "default" in entry:
			assert entry["default"] == "true", entry
		if "stage" in entry:
			assert isinstance(entry["stage"], str) and entry["stage"].strip(), entry
		if "pending" in entry:
			assert isinstance(entry["pending"], bool), entry
	assert isinstance(data["off_on_purpose"], list)
	discovered = set(ssw.discover_switches(ROOT))
	for marker in data["off_on_purpose"]:
		assert marker["name"] in discovered or marker.get("pending") is True, marker
		if "pending" in marker:
			assert isinstance(marker["pending"], bool), marker
		assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", marker["until"])
		dt.date.fromisoformat(marker["until"])
		assert str(marker.get("reason", "")).strip(), "off_on_purpose needs a reason"
	# The registry loads with discovery merged in (exit 0 in the watch).
	ssw.load_registry(REGISTRY, repo_root=ROOT)


def _scalar(text: str):
	"""Decode the plain YAML scalars the watch steps use (stdlib fallback)."""
	value = text.strip()
	if value in ("true", "false"):
		return value == "true"
	if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
		return value[1:-1]
	return value


def _collect_logs_steps_text(text: str) -> list[dict]:
	"""Minimal reader for the collect-logs steps when PyYAML is unavailable.

	Handles the shapes the steps use: `key: scalar`, one level of nested
	mappings (`env:`, `with:`) and `run: |` block scalars.
	"""
	lines = text.splitlines()
	start = lines.index("  collect-logs:")
	steps: list[dict] = []
	step: dict | None = None
	nested: dict | None = None
	block_key = None
	block_lines: list[str] = []
	for line in lines[start + 1:]:
		if line and not line.startswith(" ") or re.match(r"^  \S", line):
			break
		if block_key is not None:
			if not line.strip() or line.startswith("          "):
				block_lines.append(line[10:])
				continue
			step[block_key] = "\n".join(block_lines).strip("\n") + "\n"
			block_key = None
		if not line.strip() or line.strip().startswith("#"):
			continue
		match = re.match(r"^      - (\S[^:]*):\s?(.*)$", line)
		if match:
			step = {match.group(1): _scalar(match.group(2))}
			steps.append(step)
			nested = None
			continue
		if step is None:
			continue
		match = re.match(r"^        ([A-Za-z0-9_-]+):\s?(.*)$", line)
		if match:
			key, value = match.group(1), match.group(2)
			if value.strip() == "":
				nested = {}
				step[key] = nested
			elif value.strip() in ("|", "|-", ">"):
				nested = None
				block_key = key
				block_lines = []
			else:
				nested = None
				step[key] = _scalar(value)
			continue
		match = re.match(r"^          ([A-Za-z0-9_-]+):\s?(.*)$", line)
		if match and nested is not None:
			nested[match.group(1)] = _scalar(match.group(2))
	if block_key is not None and step is not None:
		step[block_key] = "\n".join(block_lines).strip("\n") + "\n"
	return steps


def _collect_logs_steps() -> list[dict]:
	text = WATCH_WORKFLOW.read_text(encoding="utf-8")
	if yaml is None:
		return _collect_logs_steps_text(text)
	document = yaml.safe_load(text)
	return document["jobs"]["collect-logs"]["steps"]


def test_watch_steps_are_wired_in_collect_logs():
	steps = {step.get("name"): step for step in _collect_logs_steps()}
	for name in WATCH_STEP_NAMES:
		assert name in steps, name
		step = steps[name]
		assert step.get("continue-on-error") is True, name
		assert step["if"].startswith("${{ always()"), name
		if name != "Read stage switch variables":
			assert "vars.STAGE_SWITCH_WATCH_ENABLED || 'true'" in step["if"], name
	read = steps["Read stage switch variables"]
	# The read step runs when the watch is off so the wrapper can log
	# `STAGE_SWITCH_WATCH outcome=disabled`; the wrapper reads the switch.
	assert read["env"]["STAGE_SWITCH_WATCH_ENABLED"] == "${{ vars.STAGE_SWITCH_WATCH_ENABLED || 'true' }}"
	assert read["env"]["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
	assert "TG_BOT_SECRET" not in read["env"]
	assert read["run"].strip() == "bash scripts/stage_switch_watch.sh read"
	report = steps["Report stage switches left off"]
	assert "GH_TOKEN" not in report["env"]
	assert not any("GH_PAT" in str(value) for value in report["env"].values())
	assert report["env"]["STAGE_SWITCH_OFF_ALERT_HOURS"] == "${{ vars.STAGE_SWITCH_OFF_ALERT_HOURS || '24' }}"
	assert report["run"].strip() == "bash scripts/stage_switch_watch.sh report"
	lookup = steps["Look up today's stage switch alert"]
	save = steps["Record today's stage switch alert"]
	assert lookup["uses"].startswith("actions/cache/restore@")
	assert lookup["with"]["lookup-only"] is True
	assert save["uses"].startswith("actions/cache/save@")
	assert lookup["with"]["key"] == save["with"]["key"] == "stage-switch-off-alert-${{ steps.stage_switch_read.outputs.day }}"
	assert lookup["with"]["path"] == save["with"]["path"]


def test_no_workflow_passes_tojson_vars():
	for workflow in sorted([*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")]):
		assert not re.search(r"toJSON\(\s*vars\s*\)", workflow.read_text(encoding="utf-8"), re.IGNORECASE), workflow.name


def test_readme_documents_watch_variables():
	rows = _readme_variables_rows()
	assert rows.get("STAGE_SWITCH_WATCH_ENABLED") == ["true"]
	assert rows.get("STAGE_SWITCH_OFF_ALERT_HOURS") == ["24"]
	text = README.read_text(encoding="utf-8")
	assert "off_on_purpose" in text and "scripts/stage_switches.json" in text
