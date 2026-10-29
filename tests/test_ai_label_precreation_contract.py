#!/usr/bin/env python3
"""Static contract: workflows must ensure ai:* labels before mutation."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_autofix_step_scripts import expanded_review_autofix_text  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent

WORKFLOW_CONTRACT = {
	".github/workflows/orchestrate.yml": {
		"must_contain": [
			re.compile(r'(?m)^\s*(?:source|\.)\s+["\']?(?:\./)?scripts/label_helpers\.sh["\']?(?=[\s;#]|$)'),
			'ensure_label_exists "ai:orchestrator-tracking" "${{ github.repository }}"',
			'ensure_label_exists "ai:clarification" "${{ github.repository }}"',
			'ensure_label_exists "ai:orchestrator-managed" "${{ github.repository }}"',
			'ensure_label_exists "ai:orchestrator-validate-required" "${{ github.repository }}"',
		],
		"must_not_contain": [
			re.compile(r"gh\s+label\s+create\s+['\"]?ai:orchestrator-tracking['\"]?"),
			re.compile(r"gh\s+label\s+create\s+['\"]?ai:clarification['\"]?"),
			re.compile(r"gh\s+label\s+create\s+['\"]?ai:orchestrator-managed['\"]?"),
			re.compile(r"gh\s+label\s+create\s+['\"]?ai:orchestrator-validate-required['\"]?"),
		],
	},
	".github/workflows/validation-improvements-intake.yml": {
		"must_contain": [
			re.compile(r'for\s+f\s+in\s+[^;\n]*label_helpers\.sh[^;\n]*;\s*do\b'),
			re.compile(r'(?m)^\s*(?:source|\.)\s+["\']?(?:\./)?scripts/label_helpers\.sh["\']?(?=[\s;#]|$)'),
			'ensure_label_exists "ai:needs-prompt-review" "${GITHUB_REPOSITORY}"',
		],
		"must_not_contain": [
			re.compile(r'gh\s+label\s+list\s+--limit\s+500'),
			re.compile(r"gh\s+label\s+create\s+['\"]?ai:needs-prompt-review['\"]?"),
		],
	},
	".github/workflows/issue_pr_status.yml": {
		"must_contain": [
			re.compile(r'for\s+f\s+in\s+[^;\n]*label_helpers\.sh[^;\n]*;\s*do\b'),
			re.compile(r'(?m)^\s*(?:source|\.)\s+["\']?(?:\./)?scripts/label_helpers\.sh["\']?(?=[\s;#]|$)'),
			'ensure_label_exists "${FINAL_LABEL}" "${REPOSITORY}"',
		],
		"must_not_contain": [
			re.compile(r'repos/\$\{REPOSITORY\}/labels/\$\(printf\s+[\'\"]?%s[\'\"]?\s+[\'\"]?\$\{FINAL_LABEL\}[\'\"]?\)'),
		],
	},
	".github/workflows/review_autofix.yml": {
		"must_contain": [
			re.compile(r'(?m)^\s*(?:source|\.)\s+["\']?\$\{SUPPORT_SCRIPTS_DIR\}/label_helpers\.sh["\']?(?=[\s;#]|$)'),
			'ensure_label_exists "ai:resolver-escalated" "${{ github.repository }}"',
		],
		"must_not_contain": [
			re.compile(r"gh\s+label\s+create\s+['\"]?ai:resolver-escalated['\"]?"),
		],
	},
}


def _workflow_text(rel_path: str) -> str:
	path = REPO_ROOT / rel_path
	return path.read_text(encoding="utf-8")


def _contains(text: str, pattern: object) -> bool:
	if isinstance(pattern, re.Pattern):
		return bool(pattern.search(text))
	return str(pattern) in text


def test_ai_label_precreation_contract() -> None:
	for rel_path, contract in WORKFLOW_CONTRACT.items():
		text = _workflow_text(rel_path)

		for expected in contract["must_contain"]:
			assert _contains(text, expected), f"{rel_path} missing required pattern: {expected}"

		for forbidden in contract["must_not_contain"]:
			assert not _contains(text, forbidden), f"{rel_path} contains forbidden legacy probe/mutation: {forbidden}"


LABEL_CONTRACT_PATH = REPO_ROOT / ".github/ai/label_contract.v1.json"
PINNED_REVIEW_AUTOFIX_LABELS = ("ai:ready-to-merge", "ai:review-blocked")
GENERIC_LABEL_FALLBACK = ("1d76db", "AI workflow label")

_STEP_SPLIT_RE = re.compile(r"(?m)^\s*- name:")
_FORM_A_RE = re.compile(r'ensure_label_exists "(ai:[^"]+)" \S+ "([0-9a-fA-F]{6})" "([^"]*)"')
_FORM_B_RE = re.compile(r'--color "([0-9a-fA-F]{6})" --description "([^"]*)"')
_LITERAL_CREATE_RE = re.compile(r'gh label create "(ai:[^"$]+)"')
_CASE_ARM_RE = re.compile(r"^\s*(ai:[A-Za-z0-9:_-]+)\)\s*$")
_CASE_END_RE = re.compile(r";;|\besac\b")
_ENSURE_LITERAL_RE = re.compile(r'ensure_label_exists "(ai:[^"$]+)"')


def _load_label_contract_metadata() -> dict[str, tuple[str, str]]:
	contract = json.loads(LABEL_CONTRACT_PATH.read_text(encoding="utf-8"))
	labels = contract["labels"]
	metadata: dict[str, tuple[str, str]] = {}
	for label in PINNED_REVIEW_AUTOFIX_LABELS:
		assert label in labels, f"{LABEL_CONTRACT_PATH.name} has no entry for {label}"
		metadata[label] = (str(labels[label]["color"]), str(labels[label]["description"]))
	return metadata


def _review_autofix_label_metadata_sites(text: str) -> list[dict]:
	"""Return each inline label creation that carries a literal color and description."""
	normalized = re.sub(r"\\\n[ \t]*", " ", text)
	sites: list[dict] = []
	for chunk in _STEP_SPLIT_RE.split(normalized):
		lines = chunk.splitlines()
		ensured_literals = set(_ENSURE_LITERAL_RE.findall(chunk))
		chunk_label = next(iter(ensured_literals)) if len(ensured_literals) == 1 else None
		for index, line in enumerate(lines):
			context = line.strip()
			form_a = _FORM_A_RE.search(line)
			if form_a:
				sites.append({
					"label": form_a.group(1),
					"color": form_a.group(2),
					"description": form_a.group(3),
					"context": context,
					"form": "ensure_label_exists",
				})
				continue
			form_b = _FORM_B_RE.search(line)
			if not form_b:
				continue
			color, description = form_b.group(1), form_b.group(2)
			if (color, description) == GENERIC_LABEL_FALLBACK:
				continue
			label = None
			literal = _LITERAL_CREATE_RE.search(line)
			if literal:
				label = literal.group(1)
			else:
				for previous in reversed(lines[:index]):
					if _CASE_END_RE.search(previous):
						break
					arm = _CASE_ARM_RE.match(previous)
					if arm:
						label = arm.group(1)
						break
				if label is None and re.search(r'gh label create "\$', line):
					label = chunk_label
			sites.append({
				"label": label,
				"color": color,
				"description": description,
				"context": context,
				"form": "gh label create",
			})
	return sites


def test_review_autofix_label_metadata_matches_label_contract() -> None:
	text = expanded_review_autofix_text()
	contract = _load_label_contract_metadata()
	contract_values = {value for pair in contract.values() for value in pair}
	problems: list[str] = []
	checked: dict[str, int] = {label: 0 for label in PINNED_REVIEW_AUTOFIX_LABELS}
	ready_form_a_seen = False

	for site in _review_autofix_label_metadata_sites(text):
		label = site["label"]
		if label is None:
			if site["color"] in contract_values or site["description"] in contract_values:
				problems.append(
					"review_autofix.yml: cannot attribute label for metadata site "
					f"{site['context']!r} (color '{site['color']}', description '{site['description']}')"
				)
			continue
		if label not in contract:
			continue
		checked[label] += 1
		if label == "ai:ready-to-merge" and site["form"] == "ensure_label_exists":
			ready_form_a_seen = True
		expected_color, expected_description = contract[label]
		if (site["color"], site["description"]) != (expected_color, expected_description):
			problems.append(
				f"review_autofix.yml: label '{label}' metadata diverges from "
				f".github/ai/label_contract.v1.json at {site['context']!r}: "
				f"color '{site['color']}' vs contract '{expected_color}'; "
				f"description '{site['description']}' vs contract '{expected_description}'"
			)

	for label, count in checked.items():
		if count == 0:
			problems.append(f"review_autofix.yml: found no inline metadata site for label '{label}'; extraction is broken")
	if not ready_form_a_seen:
		problems.append(
			"review_autofix.yml: deterministic-skip `ensure_label_exists \"ai:ready-to-merge\" ... \"<color>\" \"<desc>\"` site not found"
		)
	assert not problems, "\n".join(problems)


if __name__ == "__main__":
	test_ai_label_precreation_contract()
	test_review_autofix_label_metadata_matches_label_contract()
	print("PASS")
