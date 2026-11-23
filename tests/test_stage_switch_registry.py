"""scripts/stage_switches.json stays in step with the workflows and README (issue #9994).

Every `vars.<NAME>_ENABLED || 'true'` expression (also the chained form
`vars.<NAME>_ENABLED || (...) || 'true'`) in .github/workflows/*.yml and every
README "#### Variables" `_ENABLED` row whose default is `true` must be listed
in the registry, so a new switch cannot be added without the stage switch
watch covering it. Also locks the workflow wiring in workflow-log-analysis.yml.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

try:
	import yaml
except ImportError:  # credential-free gate containers ship without PyYAML
	yaml = None

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
REGISTRY = ROOT / "scripts" / "stage_switches.json"
WATCH_WORKFLOW = WORKFLOWS_DIR / "workflow-log-analysis.yml"

SWITCH_DEFAULT_RE = re.compile(r"vars\.([A-Z][A-Z0-9_]*_ENABLED) \|\| (?:\([^()]*\) \|\| )*'([^']*)'")
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
	found: dict[str, set[str]] = {}
	for workflow in sorted(workflows_dir.glob("*.yml")):
		for line in workflow.read_text(encoding="utf-8").splitlines():
			if line.strip().startswith("#"):
				continue
			for name, default in SWITCH_DEFAULT_RE.findall(line):
				if default == "true":
					found.setdefault(name, set()).add(workflow.name)
	return found


def _missing_from_registry(workflows_dir: Path, names: set[str]) -> list[str]:
	return sorted(set(_workflow_true_switches(workflows_dir)) - names)


def _readme_variables_rows() -> dict[str, list[str]]:
	lines = README.read_text(encoding="utf-8").splitlines()
	starts = [i for i, line in enumerate(lines) if line == "#### Variables"]
	assert len(starts) == 1
	rows: dict[str, list[str]] = {}
	for line in lines[starts[0] + 1:]:
		if re.match(r"^#", line):
			break
		if not line.startswith("| `"):
			continue
		cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
		if len(cells) < 3:
			continue
		rows.setdefault(cells[0].strip("`"), []).append(cells[2].replace("`", "").strip())
	return rows


def test_every_workflow_true_switch_is_in_registry():
	found = _workflow_true_switches(WORKFLOWS_DIR)
	assert found, "no vars.<NAME>_ENABLED || 'true' expressions found"
	assert "FINAL_CI_FAILED_REDISPATCH_ENABLED" in found
	assert "CLAUDE_POOL_CAPACITY_WAIT_ENABLED" in found  # chained form
	assert _missing_from_registry(WORKFLOWS_DIR, _registry_names()) == []


def test_registry_check_fails_on_unlisted_workflow_switch(tmp_path):
	(tmp_path / "new.yml").write_text(
		"jobs:\n  x:\n    env:\n"
		"      NEW_STAGE_ENABLED: ${{ vars.NEW_STAGE_ENABLED || 'true' }}\n"
		"      CHAINED_STAGE_ENABLED: ${{ vars.CHAINED_STAGE_ENABLED || (vars.WORKFLOW_PROFILE == 'core' && 'false') || 'true' }}\n"
		"      OPT_IN_ENABLED: ${{ vars.OPT_IN_ENABLED || 'false' }}\n"
		"      # COMMENTED_ENABLED: ${{ vars.COMMENTED_ENABLED || 'true' }}\n",
		encoding="utf-8",
	)
	assert _missing_from_registry(tmp_path, _registry_names()) == ["CHAINED_STAGE_ENABLED", "NEW_STAGE_ENABLED"]


def test_every_readme_true_switch_is_in_registry():
	names = _registry_names()
	missing = []
	for name, defaults in _readme_variables_rows().items():
		if name.endswith("_ENABLED") and any(d.lower().startswith("true") for d in defaults) and name not in names:
			missing.append(name)
	assert missing == []


def test_registry_entries_are_well_formed():
	data = _registry()
	assert data["schema"] == "stage_switches.v1"
	names = [entry["name"] for entry in data["switches"]]
	assert len(names) == len(set(names))
	for entry in data["switches"]:
		assert set(entry) == {"name", "default", "stage"}, entry
		assert re.fullmatch(r"[A-Z][A-Z0-9_]*", entry["name"])
		assert entry["default"] == "true"
		assert entry["stage"].strip()
	assert isinstance(data["off_on_purpose"], list)
	for marker in data["off_on_purpose"]:
		assert marker["name"] in names
		assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", marker["until"])
		dt.date.fromisoformat(marker["until"])
		assert str(marker.get("reason", "")).strip(), "off_on_purpose needs a reason"


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
		assert "vars.STAGE_SWITCH_WATCH_ENABLED || 'true'" in step["if"], name
		assert step["if"].startswith("${{ always()"), name
	read = steps["Read stage switch variables"]
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
	for workflow in sorted(WORKFLOWS_DIR.glob("*.yml")):
		assert not re.search(r"toJSON\(\s*vars\s*\)", workflow.read_text(encoding="utf-8"), re.IGNORECASE), workflow.name


def test_readme_documents_watch_variables():
	rows = _readme_variables_rows()
	assert rows.get("STAGE_SWITCH_WATCH_ENABLED") == ["true"]
	assert rows.get("STAGE_SWITCH_OFF_ALERT_HOURS") == ["24"]
	text = README.read_text(encoding="utf-8")
	assert "off_on_purpose" in text and "scripts/stage_switches.json" in text
