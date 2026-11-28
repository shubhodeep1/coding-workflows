"""scripts/stage_switches.json stays in step with the discovered switches (issues #9994, #10170).

The watch discovers the default-on switches at run time
(`stage_switch_watch.discover_switches`): every `vars.<NAME>_ENABLED || 'true'`
expression (also the chained form `vars.<NAME>_ENABLED || (...) || 'true'`)
and every `vars.<NAME>_ENABLED != 'false'` test in .github/workflows/*.yml and
*.yaml, and every `_ENABLED` row whose default is `true` in the README
"#### Variables" and "## Required Variables" tables. The registry JSON holds
only detail that cannot be derived; every entry and off_on_purpose marker must
name a discovered switch or carry `"pending": true`. Also locks the workflow
wiring in workflow-log-analysis.yml.
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
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
REGISTRY = ROOT / "scripts" / "stage_switches.json"
WATCH_WORKFLOW = WORKFLOWS_DIR / "workflow-log-analysis.yml"

# Aliases for the names that moved to scripts/stage_switch_watch.py (#10170).
SWITCH_DEFAULT_RE = ssw.SWITCH_DEFAULT_RE
SWITCH_NOT_FALSE_RE = ssw.SWITCH_NOT_FALSE_RE
README_SWITCH_TABLES = ssw.README_SWITCH_TABLES
README_NOT_REPOSITORY_VARIABLES = ssw.README_NOT_REPOSITORY_VARIABLES
_workflow_true_switches = ssw._workflow_true_switches
WATCH_STEP_NAMES = (
	"Read stage switch variables",
	"Look up today's stage switch alert",
	"Report stage switches left off",
	"Record today's stage switch alert",
)
REGISTRY_KEYS = {"name", "default", "stage", "on_values", "pending"}


def _registry() -> dict:
	return json.loads(REGISTRY.read_text(encoding="utf-8"))


def _registry_names() -> set[str]:
	return {entry["name"] for entry in _registry()["switches"]}


def _missing_from_registry(workflows_dir: Path, names: set[str]) -> list[str]:
	return sorted(set(_workflow_true_switches(workflows_dir)) - names)


def _readme_variables_rows(heading: str = "#### Variables") -> dict[str, list[str]]:
	lines = README.read_text(encoding="utf-8").splitlines()
	assert len([line for line in lines if line == heading]) == 1, heading
	rows = ssw._readme_variables_rows(README, heading)
	assert rows, heading
	return rows


def _undiscovered_without_pending(registry: dict, discovered) -> list[str]:
	"""Entry and marker names neither discovered nor validly pending."""
	bad = []
	for kind, items in (("switch", registry.get("switches", [])), ("off_on_purpose", registry.get("off_on_purpose", []))):
		for item in items:
			name = item.get("name")
			if name in discovered:
				continue
			if item.get("pending") is True and ssw._is_switch_name(name):
				continue
			bad.append(f"{kind}:{name}")
	return sorted(bad)


def test_discovery_finds_sample_switches():
	found = ssw.discover_switches(ROOT)
	assert found, "no default-on switches discovered"
	assert "FINAL_CI_FAILED_REDISPATCH_ENABLED" in found
	assert "CLAUDE_POOL_CAPACITY_WAIT_ENABLED" in found  # chained form
	assert "not_false" in found["E2E_ALT_MODEL_ENABLED"]["forms"]  # `!= 'false'` form
	assert "readme" in found["HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED"]["forms"]  # Required Variables table
	assert not set(found) & set(README_NOT_REPOSITORY_VARIABLES)
	# Issue #9920 alias: README env name, not a repository variable.
	assert "GITHUB_AI_PLAN_GRANT_WRITE_ENABLED" not in found
	assert "AI_PLAN_GRANT_WRITE_ENABLED" in _registry_names()
	for name, info in found.items():
		assert ssw._is_switch_name(name), name
		assert info["files"] == sorted(info["files"]) and info["files"], name
		assert set(info["forms"]) <= {"default_true", "not_false", "readme"}, name
	# An exclusion must still name a documented default-on row.
	seen = set()
	for heading in README_SWITCH_TABLES:
		for name, defaults in _readme_variables_rows(heading).items():
			if name.endswith("_ENABLED") and any(d.lower().startswith("true") for d in defaults):
				seen.add(name)
	assert set(README_NOT_REPOSITORY_VARIABLES) <= seen


def test_registry_entries_name_discovered_or_pending():
	bad = _undiscovered_without_pending(_registry(), ssw.discover_switches(ROOT))
	assert bad == [], (
		f"scripts/stage_switches.json names switches that are not discovered: {', '.join(bad)}. "
		"Add \"pending\": true while the switch's PR is open, or delete the entry."
	)


def test_discovered_or_pending_rule_rejects_unknown_names(tmp_path):
	discovered = ssw.discover_switches(tmp_path)
	assert discovered == {}
	registry = {
		"switches": [
			{"name": "PLAIN_ENABLED"},
			{"name": "FOO_ENABLED", "pending": True},
			{"name": "FOO_FLAG", "pending": True},
			{"name": "BAR_ENABLED", "pending": False},
		],
		"off_on_purpose": [
			{"name": "MARK_ENABLED", "until": "2099-01-01", "reason": "x"},
			{"name": "PEND_ENABLED", "until": "2099-01-01", "reason": "x", "pending": True},
			{"name": "foo", "until": "2099-01-01", "reason": "x", "pending": True},
		],
	}
	assert _undiscovered_without_pending(registry, discovered) == [
		"off_on_purpose:MARK_ENABLED", "off_on_purpose:foo",
		"switch:BAR_ENABLED", "switch:FOO_FLAG", "switch:PLAIN_ENABLED",
	]
	assert _undiscovered_without_pending(registry, {"PLAIN_ENABLED": {}, "MARK_ENABLED": {}}) == [
		"off_on_purpose:foo", "switch:BAR_ENABLED", "switch:FOO_FLAG",
	]


def test_registry_check_fails_on_unlisted_workflow_switch(tmp_path):
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
	expected = ["CHAINED_STAGE_ENABLED", "MULTI_LINE_STAGE_ENABLED", "NEW_STAGE_ENABLED", "NOT_FALSE_STAGE_ENABLED"]
	found = ssw.discover_switches(tmp_path)
	assert sorted(found) == expected
	assert found["NOT_FALSE_STAGE_ENABLED"] == {"files": ["new.yml"], "forms": ["not_false"]}
	assert found["NEW_STAGE_ENABLED"] == {"files": ["new.yml"], "forms": ["default_true"]}
	# The old alias still reports the same names as missing from the registry.
	assert _missing_from_registry(workflows, _registry_names()) == expected
	(workflows / "other.yaml").write_text("x: ${{ vars.YAML_STAGE_ENABLED || 'true' }}\n", encoding="utf-8")
	(workflows / "notes.txt").write_text("x: ${{ vars.TXT_STAGE_ENABLED || 'true' }}\n", encoding="utf-8")
	found = ssw.discover_switches(tmp_path)
	assert "YAML_STAGE_ENABLED" in found and found["YAML_STAGE_ENABLED"]["files"] == ["other.yaml"]
	assert "TXT_STAGE_ENABLED" not in found


def test_discovery_reads_readme_tables(tmp_path):
	(tmp_path / "README.md").write_text(
		"# Title\n\n#### Variables\n\n"
		"| Variable | Required | Default | Used by | Purpose |\n"
		"| --- | --- | --- | --- | --- |\n"
		"| `README_STAGE_ENABLED` | No | `true` | x | y |\n"
		"| `LESSONS_LEARNED_ENABLED` | No | `true` | x | y |\n"
		"| `OPT_IN_STAGE_ENABLED` | No | `false` | x | y |\n"
		"| `SOME_LIMIT` | No | `true` | x | y |\n"
		"\n## Required Variables\n\n"
		"| Variable | Default | Purpose |\n"
		"| --- | --- | --- |\n"
		"| `REQUIRED_STAGE_ENABLED` | `true` (unset) | z |\n",
		encoding="utf-8",
	)
	found = ssw.discover_switches(tmp_path)
	assert sorted(found) == ["README_STAGE_ENABLED", "REQUIRED_STAGE_ENABLED"]
	assert found["README_STAGE_ENABLED"] == {"files": ["README.md"], "forms": ["readme"]}


def test_discovery_fails_open_on_missing_root(tmp_path):
	assert ssw.discover_switches(tmp_path / "absent") == {}


def test_registry_entries_are_well_formed():
	data = _registry()
	assert data["schema"] == "stage_switches.v1"
	names = [entry["name"] for entry in data["switches"]]
	assert len(names) == len(set(names))
	for entry in data["switches"]:
		assert {"name"} <= set(entry) <= REGISTRY_KEYS, entry
		assert set(entry.get("on_values", [])) <= {"1", "yes", "on"}, entry
		assert re.fullmatch(r"[A-Z][A-Z0-9_]*", entry["name"])
		if "default" in entry:
			assert entry["default"] == "true"
		if "stage" in entry:
			assert entry["stage"].strip()
		if "pending" in entry:
			assert isinstance(entry["pending"], bool), entry
	assert isinstance(data["off_on_purpose"], list)
	for marker in data["off_on_purpose"]:
		assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", marker["until"])
		dt.date.fromisoformat(marker["until"])
		assert str(marker.get("reason", "")).strip(), "off_on_purpose needs a reason"
		if "pending" in marker:
			assert isinstance(marker["pending"], bool), marker


def test_real_registry_loads_with_discovery_merged():
	raw = _registry()
	merged = ssw.load_registry(REGISTRY, ROOT)
	names = [entry["name"] for entry in merged["switches"]]
	assert names == sorted(names)
	assert set(ssw.discover_switches(ROOT)) <= set(names)
	assert {entry["name"] for entry in raw["switches"]} <= set(names)
	by_name = {entry["name"]: entry for entry in merged["switches"]}
	raw_by_name = {entry["name"]: entry for entry in raw["switches"]}
	activation = by_name["ACTIVATION_VERIFY_ENABLED"]
	assert activation["stage"] == raw_by_name["ACTIVATION_VERIFY_ENABLED"]["stage"]
	assert activation["on_values"] == ["1", "yes", "on"]
	for entry in merged["switches"]:
		assert entry["default"] == "true" and entry["stage"].strip(), entry
	assert merged["off_on_purpose"] == raw["off_on_purpose"]


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
