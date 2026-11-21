"""README documents every stuck-states kill switch with the workflow default (Refs #8291)."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

# Every kill switch and tuning knob the stuck-states project added
# (docs/plans/remove-unattended-pipeline-stuck-states-plan.md, "Rollout",
# plus P8c, P12, P13a and P13b).
STUCK_STATES_KILL_SWITCHES = (
	"MERGE_TRAIN_CONFLICT_CHECK_ENABLED",
	"MERGE_TRAIN_HEAD_MAX_AGE_HOURS",
	"MERGE_TRAIN_PRIORITY_LABELS",
	"SUPPORT_SCRIPTS_UNIFIED_ENABLED",
	"STALL_FINGERPRINT_BREAKER_ENABLED",
	"STALL_FINGERPRINT_REPEAT_MAX",
	"E2E_PHASE4B_QUEUED_GRACE_ENABLED",
	"E2E_RETRY_QUEUED_GRACE_MINUTES",
	"PROMOTE_CYCLE_HOLD_RELEASE_ENABLED",
	"PROMOTE_CYCLE_HOLD_MAX_HOURS",
	"RELEASE_BLOCKER_HEAL_ENABLED",
	"SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED",
	"SINGLE_ISSUE_SECURITY_PASS_CONSOLIDATE_ENABLED",
	"UNBLOCK_MAX_ROUNDS_PER_ITEM",
	"NEEDS_HUMAN_DIGEST_ENABLED",
	"SCOPE_GUARD_AUTO_ALLOW_PATHS",
	"HARNESS_HEAL_AUTO_REVALIDATE_ENABLED",
	"DEPENDENCY_WAIT_RESUME_ENABLED",
	"DEPENDENCY_WAIT_ESCALATE_DAYS",
	"CLARIFY_SNAPSHOT_ALL_TEXT_ENABLED",
	"CLARIFY_BLOCKED_VERIFY_ENABLED",
	"OPERATOR_STEP_AUTO_RESOLVE_ENABLED",
	"RETIRED_LABEL_DRAIN_ENABLED",
	"ZOMBIE_RUN_REAPER_ENABLED",
	"ZOMBIE_RUN_MAX_QUEUED_HOURS",
	"ORCH_SYNC_CONTRACT_LIST_UNION_ENABLED",
	"SECURITY_AUDIT_LINE_OWNERSHIP",
	"SECURITY_PASS_REBIND_ENABLED",
)

KILL_SWITCH_SECTION_HEADING = "### Stuck-state recovery: kill switches"


def _readme_lines():
	return README.read_text(encoding="utf-8").splitlines()


def _section_body(heading, stop_pattern):
	lines = _readme_lines()
	starts = [i for i, line in enumerate(lines) if line == heading]
	assert len(starts) == 1, f"expected exactly one {heading!r} heading, found {len(starts)}"
	body = []
	for line in lines[starts[0] + 1:]:
		if re.match(stop_pattern, line):
			break
		body.append(line)
	return body


def _variables_table_rows():
	"""Map each variable in the exact "#### Variables" section to its Default cells."""
	rows = {}
	for line in _section_body("#### Variables", r"^#"):
		if not line.startswith("| `"):
			continue
		cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
		if len(cells) < 3:
			continue
		name = cells[0].strip("`")
		rows.setdefault(name, []).append(cells[2].replace("`", "").strip())
	return rows


def _workflow_defaults(name):
	pattern = re.compile(r"vars\." + re.escape(name) + r" \|\| '([^']*)'")
	defaults = set()
	for workflow in sorted(WORKFLOWS_DIR.glob("*.yml")):
		for line in workflow.read_text(encoding="utf-8").splitlines():
			if line.strip().startswith("#"):
				continue
			defaults.update(pattern.findall(line))
	return defaults


def test_kill_switch_tuple_has_no_duplicates():
	assert len(STUCK_STATES_KILL_SWITCHES) == len(set(STUCK_STATES_KILL_SWITCHES)) == 28


def test_every_kill_switch_has_one_variables_row():
	rows = _variables_table_rows()
	missing = [name for name in STUCK_STATES_KILL_SWITCHES if name not in rows]
	duplicated = [name for name in STUCK_STATES_KILL_SWITCHES if len(rows.get(name, [])) > 1]
	assert not missing, f"no #### Variables row for: {missing}"
	assert not duplicated, f"more than one #### Variables row for: {duplicated}"


def test_variables_row_default_matches_workflow_default():
	rows = _variables_table_rows()
	mismatches = []
	for name in STUCK_STATES_KILL_SWITCHES:
		defaults = _workflow_defaults(name)
		assert len(defaults) == 1, f"{name}: expected one workflow default, found {sorted(defaults)}"
		readme_default = rows.get(name, [None])[0]
		(workflow_default,) = defaults
		if readme_default != workflow_default:
			mismatches.append((name, readme_default, workflow_default))
	assert not mismatches, f"README default differs from workflow default: {mismatches}"


def test_zombie_reaper_documented_as_opt_in():
	rows = _variables_table_rows()
	assert rows["ZOMBIE_RUN_REAPER_ENABLED"] == ["false"]
	body = "\n".join(_section_body(KILL_SWITCH_SECTION_HEADING, r"^#{1,3} "))
	assert "`ZOMBIE_RUN_REAPER_ENABLED` (`false`, opt-in)" in body


def test_kill_switch_section_lists_every_switch():
	body = "\n".join(_section_body(KILL_SWITCH_SECTION_HEADING, r"^#{1,3} "))
	missing = [name for name in STUCK_STATES_KILL_SWITCHES if f"`{name}`" not in body]
	assert not missing, f"{KILL_SWITCH_SECTION_HEADING!r} does not name: {missing}"
	assert "Refs #8291" in body
	for subsection in (
		"#### Clarify snapshot and BLOCKED verification",
		"#### Dependency waits",
		"#### Operator-step auto-resolve",
		"#### Retired-label drain",
		"#### Zombie run reaper",
	):
		assert subsection in body.splitlines(), f"missing subsection {subsection!r}"


def test_retired_labels_bullet_mentions_drain():
	body = _section_body("### Labels", r"^#{1,3} ")
	bullets = [line for line in body if line.startswith("- The contract's `retired_labels`")]
	assert len(bullets) == 1
	bullet = bullets[0]
	assert "drains the open issues and PRs" in bullet
	assert "`RETIRED_LABEL_DRAIN_ENABLED=false`" in bullet
	assert "deletes each one from a repo when present" not in bullet
	labels_text = "\n".join(body)
	assert "`ai:needs-human`" in labels_text
	assert "`ai:waiting-on-dependency`" in labels_text
