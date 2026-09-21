#!/usr/bin/env python3
"""Regression guard: release jobs must push tags via fully qualified refs/tags/ refspecs.

Bare refspecs like `git push -f origin stable` fail with
`error: src refspec stable matches more than one` whenever the runner has both
a refs/heads/<name> branch and a refs/tags/<name> tag locally (which the
release jobs trigger by checking out the `stable` branch and then creating a
`stable` tag). See PR #2031 / job-logs.txt L1500-1502 for the live failure.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = (
	REPO_ROOT / ".github" / "workflows" / "mark-stable.yml",
	REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml",
)
SCRIPT = REPO_ROOT / "scripts" / "mark-stable.sh"


def _read(p: Path) -> str:
	return p.read_text(encoding="utf-8")


def test_workflows_push_stable_tag_via_refs_tags() -> None:
	for wf in WORKFLOWS:
		text = _read(wf)
		assert "publish_tag_with_remote_verification refs/tags/stable moving" in text, (
			f"{wf.name}: stable tag publication must use fully qualified refs/tags/stable"
		)
		assert not re.search(r"^\s*git push -f origin stable\s*$", text, re.MULTILINE), (
			f"{wf.name}: bare 'git push -f origin stable' would re-introduce the "
			f"'src refspec stable matches more than one' regression"
		)


def test_workflows_push_major_tag_via_refs_tags() -> None:
	for wf in WORKFLOWS:
		text = _read(wf)
		assert 'publish_tag_with_remote_verification "refs/tags/$MAJOR" moving' in text, (
			f"{wf.name}: major-version tag publication must use fully qualified refs/tags/$MAJOR"
		)
		assert not re.search(r'^\s*git push -f origin "\$MAJOR"\s*$', text, re.MULTILINE), (
			f"{wf.name}: bare 'git push -f origin \"$MAJOR\"' would re-introduce the regression"
		)


def test_workflows_push_immutable_version_via_refs_tags() -> None:
	for wf in WORKFLOWS:
		text = _read(wf)
		assert 'publish_tag_with_remote_verification "refs/tags/$VERSION" immutable' in text, (
			f"{wf.name}: immutable version tag publication must use fully qualified refs/tags/$VERSION"
		)
		assert not re.search(r'^\s*git push origin "\$VERSION"\s*$', text, re.MULTILINE), (
			f"{wf.name}: bare 'git push origin \"$VERSION\"' would re-introduce the regression"
		)


def _workflow_publication_helper(workflow_text: str) -> str:
	match = re.search(
		r"^          publish_tag_with_remote_verification\(\) \{.*?^          \}$",
		workflow_text,
		re.MULTILINE | re.DOTALL,
	)
	assert match is not None, "release workflow must define the tag publication helper"
	return match.group(0)


def _workflow_step_script(workflow_text: str, step_name: str) -> str:
	lines = workflow_text.splitlines()
	step_marker = f"      - name: {step_name}"
	step_index = lines.index(step_marker)
	run_index = next(
		index
		for index in range(step_index + 1, len(lines))
		if lines[index] == "        run: |"
	)
	script_lines: list[str] = []
	for line in lines[run_index + 1 :]:
		if line and not line.startswith("          "):
			break
		script_lines.append(line[10:] if line else "")
	return "\n".join(script_lines) + "\n"


def _render_workflow_shell(script: str) -> str:
	return (
		script.replace("${{ needs.resolve-version.outputs.version }}", "v1.2.3")
		.replace("${{ github.repository }}", "owner/repo")
		.replace("${{ steps.changelog.outputs.notes_file }}", "/tmp/release_notes.md")
	)


def _run_version_tag_preflight_scenario(
	working_directory: Path,
	workflow_path: Path,
	preflight_scenario: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	preflight_call_log = working_directory / f"{workflow_path.stem}-{preflight_scenario}.log"
	preflight_script = "\n".join(
		(
			r'''git() {
	local preflight_mock_command="$1"
	shift
	printf '%s:%s\n' "${preflight_mock_command}" "$*" >> "${PREFLIGHT_CALL_LOG}"
	case "${preflight_mock_command}" in
		ls-remote)
			case "${PREFLIGHT_SCENARIO}" in
				tag-absent) return 2 ;;
				tag-unreadable)
					echo 'mock transport failure' >&2
					return 128
					;;
			esac
			;;
		fetch) ;;
		rev-parse)
			if [ "$1" = "HEAD" ] || [ "${PREFLIGHT_SCENARIO}" = "tag-match" ]; then
				printf '%040d\n' 1
			else
				printf '%040d\n' 2
			fi
			;;
	esac
}
''',
			_render_workflow_shell(_workflow_step_script(_read(workflow_path), "Validate existing version tag")),
		)
	)
	preflight_environment = {
		**os.environ,
		"PREFLIGHT_CALL_LOG": str(preflight_call_log),
		"PREFLIGHT_SCENARIO": preflight_scenario,
	}
	preflight_result = subprocess.run(
		["bash", "-c", preflight_script],
		capture_output=True,
		check=False,
		cwd=working_directory,
		env=preflight_environment,
		text=True,
	)
	return preflight_result, preflight_call_log.read_text(encoding="utf-8").splitlines()


def _run_tag_step_recovery_scenario(
	working_directory: Path,
	workflow_path: Path,
	tag_step_scenario: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	tag_step_call_log = working_directory / f"{workflow_path.stem}-{tag_step_scenario}.log"
	tag_step_script = "\n".join(
		(
			r'''git() {
	local tag_step_mock_command="$1"
	shift
	case "${tag_step_mock_command}" in
		config) ;;
		ls-remote)
			printf 'lookup:%s\n' "$*" >> "${TAG_STEP_CALL_LOG}"
			if [ "${TAG_STEP_SCENARIO}" = "tag-match" ] || [ "${TAG_STEP_SCENARIO}" = "tag-conflict" ]; then
				return 0
			fi
			return 2
			;;
		fetch)
			printf 'fetch:%s\n' "$*" >> "${TAG_STEP_CALL_LOG}"
			;;
		rev-parse)
			case "$1" in
				HEAD) printf '%040d\n' 1 ;;
				'v1.2.3^{commit}') [ "${TAG_STEP_SCENARIO}" = "tag-conflict" ] && printf '%040d\n' 2 || printf '%040d\n' 1 ;;
				*) printf '%040d\n' 3 ;;
			esac
			;;
		tag)
			printf 'tag:%s\n' "$*" >> "${TAG_STEP_CALL_LOG}"
			;;
		push)
			printf 'push:%s\n' "$*" >> "${TAG_STEP_CALL_LOG}"
			;;
	esac
}
''',
			_render_workflow_shell(
				_workflow_step_script(_read(workflow_path), "Tag version and update stable pointer")
			),
		)
	)
	tag_step_environment = {
		**os.environ,
		"TAG_STEP_CALL_LOG": str(tag_step_call_log),
		"TAG_STEP_SCENARIO": tag_step_scenario,
	}
	tag_step_result = subprocess.run(
		["bash", "-c", tag_step_script],
		capture_output=True,
		check=False,
		cwd=working_directory,
		env=tag_step_environment,
		text=True,
	)
	return tag_step_result, tag_step_call_log.read_text(encoding="utf-8").splitlines()


def _run_release_creation_scenario(
	working_directory: Path,
	workflow_path: Path,
	release_scenario: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	release_call_log = working_directory / f"{workflow_path.stem}-{release_scenario}.log"
	release_api_count = working_directory / f"{workflow_path.stem}-{release_scenario}.count"
	release_api_count.write_text("0\n", encoding="utf-8")
	release_script = "\n".join(
		(
			"unset -f gh_retry 2>/dev/null || true",
			r'''gh() {
	local release_mock_command="$1"
	shift
	printf '%s:%s\n' "${release_mock_command}" "$*" >> "${RELEASE_CALL_LOG}"
	case "${release_mock_command}" in
		api)
			local release_mock_count
			release_mock_count="$(cat "${RELEASE_API_COUNT}")"
			release_mock_count=$((release_mock_count + 1))
			printf '%s\n' "${release_mock_count}" > "${RELEASE_API_COUNT}"
			case "${RELEASE_SCENARIO}" in
				release-existing) printf 'v1.2.3\n' ;;
				release-absent)
					echo 'gh: Not Found (HTTP 404)' >&2
					return 1
					;;
				release-race)
					if [ "${release_mock_count}" -eq 1 ]; then
						echo 'gh: Not Found (HTTP 404)' >&2
						return 1
					fi
					printf 'v1.2.3\n'
					;;
				release-lookup-failure)
					echo 'gh: Resource not accessible by integration (HTTP 403)' >&2
					return 1
					;;
			esac
			;;
		release)
			[ "${RELEASE_SCENARIO}" != "release-race" ]
			;;
	esac
}
''',
			_render_workflow_shell(_workflow_step_script(_read(workflow_path), "Create GitHub Release")),
		)
	)
	release_environment = {
		**{key: value for key, value in os.environ.items() if key not in {"BASH_ENV", "ENV"}},
		"RELEASE_API_COUNT": str(release_api_count),
		"RELEASE_CALL_LOG": str(release_call_log),
		"RELEASE_SCENARIO": release_scenario,
		"SOURCE_BRANCH": "stable",
	}
	release_result = subprocess.run(
		["bash", "-c", release_script],
		capture_output=True,
		check=False,
		cwd=working_directory,
		env=release_environment,
		text=True,
	)
	return release_result, release_call_log.read_text(encoding="utf-8").splitlines()


def _run_publication_helper_scenario(
	working_directory: Path,
	publication_scenario: str,
	publication_mode: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	publication_call_log = working_directory / f"{publication_scenario}-{publication_mode}.log"
	workflow_publication_script = "\n".join(
		(
			"set -euo pipefail",
			_workflow_publication_helper(_read(WORKFLOWS[0])),
			r'''
git() {
	local publication_mock_command="$1"
	shift
	case "${publication_mock_command}" in
		rev-parse)
			printf '%s\n' "${PUBLICATION_HELPER_EXPECTED_OBJECT_ID}"
			;;
		push)
			printf 'push:%s\n' "$*" >> "${PUBLICATION_HELPER_CALL_LOG}"
			[ "${PUBLICATION_HELPER_SCENARIO}" = "push-success" ]
			;;
		ls-remote)
			printf 'lookup:%s\n' "$*" >> "${PUBLICATION_HELPER_CALL_LOG}"
			case "${PUBLICATION_HELPER_SCENARIO}" in
				remote-match)
					printf '%s\t%s\n' "${PUBLICATION_HELPER_EXPECTED_OBJECT_ID}" "${@: -1}"
					;;
				remote-conflict)
					printf '%040d\t%s\n' 0 "${@: -1}"
					;;
				remote-unreadable)
					printf 'mock ls-remote transport failure\n' >&2
					return 128
					;;
				remote-absent)
					return 2
					;;
			esac
			;;
	esac
}

sleep() {
	printf 'sleep:%s\n' "$1" >> "${PUBLICATION_HELPER_CALL_LOG}"
}

publish_tag_with_remote_verification refs/tags/test "${PUBLICATION_HELPER_MODE}"
''',
		)
	)
	publication_environment = os.environ.copy()
	publication_environment.pop("BASH_ENV", None)
	publication_environment.pop("ENV", None)
	for publication_environment_key in tuple(publication_environment):
		if publication_environment_key.startswith("BASH_FUNC_"):
			publication_environment.pop(publication_environment_key)
	publication_environment.update({
		"PUBLICATION_HELPER_CALL_LOG": str(publication_call_log),
		"PUBLICATION_HELPER_EXPECTED_OBJECT_ID": "a" * 40,
		"PUBLICATION_HELPER_MODE": publication_mode,
		"PUBLICATION_HELPER_SCENARIO": publication_scenario,
	})
	publication_result = subprocess.run(
		["bash", "-c", workflow_publication_script],
		capture_output=True,
		check=False,
		env=publication_environment,
		text=True,
	)
	publication_calls = publication_call_log.read_text(encoding="utf-8").splitlines()
	return publication_result, publication_calls


def test_workflow_tag_publication_helper_executes_failure_matrix() -> None:
	with tempfile.TemporaryDirectory() as temporary_directory:
		working_directory = Path(temporary_directory)

		direct_success, direct_success_calls = _run_publication_helper_scenario(
			working_directory, "push-success", "immutable"
		)
		assert direct_success.returncode == 0
		assert direct_success_calls == ["push:origin refs/tags/test"]

		ambiguous_success, ambiguous_success_calls = _run_publication_helper_scenario(
			working_directory, "remote-match", "moving"
		)
		assert ambiguous_success.returncode == 0
		assert ambiguous_success_calls == [
			"push:-f origin refs/tags/test",
			"lookup:--exit-code origin refs/tags/test",
		]

		immutable_collision, immutable_collision_calls = _run_publication_helper_scenario(
			working_directory, "remote-conflict", "immutable"
		)
		assert immutable_collision.returncode != 0
		assert immutable_collision_calls == [
			"push:origin refs/tags/test",
			"lookup:--exit-code origin refs/tags/test",
		]
		assert "refusing to overwrite it" in immutable_collision.stdout

		unreadable_remote, unreadable_remote_calls = _run_publication_helper_scenario(
			working_directory, "remote-unreadable", "immutable"
		)
		assert unreadable_remote.returncode != 0
		assert unreadable_remote_calls.count("push:origin refs/tags/test") == 5
		assert unreadable_remote_calls.count("lookup:--exit-code origin refs/tags/test") == 5
		assert [call for call in unreadable_remote_calls if call.startswith("sleep:")] == [
			"sleep:2",
			"sleep:4",
			"sleep:8",
			"sleep:16",
		]
		assert "mock ls-remote transport failure" in unreadable_remote.stderr

		absent_remote, absent_remote_calls = _run_publication_helper_scenario(
			working_directory, "remote-absent", "immutable"
		)
		assert absent_remote.returncode != 0
		assert absent_remote_calls.count("push:origin refs/tags/test") == 5
		assert absent_remote_calls.count("lookup:--exit-code origin refs/tags/test") == 5
		assert "Failed to publish and verify" in absent_remote.stdout


def test_workflow_version_tag_preflight_executes_recovery_matrix() -> None:
	with tempfile.TemporaryDirectory() as temporary_directory:
		working_directory = Path(temporary_directory)
		for workflow_path in WORKFLOWS:
			absent_tag, absent_calls = _run_version_tag_preflight_scenario(
				working_directory, workflow_path, "tag-absent"
			)
			assert absent_tag.returncode == 0
			assert all(not call.startswith("fetch:") for call in absent_calls)

			matching_tag, matching_calls = _run_version_tag_preflight_scenario(
				working_directory, workflow_path, "tag-match"
			)
			assert matching_tag.returncode == 0
			assert any(call.startswith("fetch:") for call in matching_calls)
			assert "accepting partial-release recovery" in matching_tag.stdout

			conflicting_tag, _ = _run_version_tag_preflight_scenario(
				working_directory, workflow_path, "tag-conflict"
			)
			assert conflicting_tag.returncode != 0
			assert "Refusing to retarget the immutable tag" in conflicting_tag.stdout

			unreadable_tag, _ = _run_version_tag_preflight_scenario(
				working_directory, workflow_path, "tag-unreadable"
			)
			assert unreadable_tag.returncode != 0
			assert "mock transport failure" in unreadable_tag.stderr


def test_workflow_tag_step_skips_only_matching_immutable_tag() -> None:
	with tempfile.TemporaryDirectory() as temporary_directory:
		working_directory = Path(temporary_directory)
		for workflow_path in WORKFLOWS:
			matching_tag, matching_calls = _run_tag_step_recovery_scenario(
				working_directory, workflow_path, "tag-match"
			)
			assert matching_tag.returncode == 0
			assert not any(call.startswith("tag:-a v1.2.3") for call in matching_calls)
			assert "push:origin refs/tags/v1.2.3" not in matching_calls
			assert "push:-f origin refs/tags/stable" in matching_calls
			assert "push:-f origin refs/tags/v1" in matching_calls

			conflicting_tag, _ = _run_tag_step_recovery_scenario(
				working_directory, workflow_path, "tag-conflict"
			)
			assert conflicting_tag.returncode != 0
			assert "Refusing to retarget the immutable tag" in conflicting_tag.stdout

			absent_tag, absent_calls = _run_tag_step_recovery_scenario(
				working_directory, workflow_path, "tag-absent"
			)
			assert absent_tag.returncode == 0
			assert "tag:-a v1.2.3 -m Release v1.2.3" in absent_calls
			assert "push:origin refs/tags/v1.2.3" in absent_calls


def test_workflow_release_creation_executes_recovery_matrix() -> None:
	with tempfile.TemporaryDirectory() as temporary_directory:
		working_directory = Path(temporary_directory)
		for workflow_path in WORKFLOWS:
			existing_release, existing_calls = _run_release_creation_scenario(
				working_directory, workflow_path, "release-existing"
			)
			assert existing_release.returncode == 0
			assert len(existing_calls) == 1
			assert existing_calls[0].startswith("api:repos/owner/repo/releases/tags/v1.2.3")

			absent_release, absent_calls = _run_release_creation_scenario(
				working_directory, workflow_path, "release-absent"
			)
			assert absent_release.returncode == 0
			assert any(call.startswith("release:create v1.2.3") for call in absent_calls)

			concurrent_release, concurrent_calls = _run_release_creation_scenario(
				working_directory, workflow_path, "release-race"
			)
			assert concurrent_release.returncode == 0
			assert len([call for call in concurrent_calls if call.startswith("api:")]) == 2
			assert "appeared concurrently" in concurrent_release.stdout

			lookup_failure, lookup_failure_calls = _run_release_creation_scenario(
				working_directory, workflow_path, "release-lookup-failure"
			)
			assert lookup_failure.returncode != 0
			assert all(not call.startswith("release:") for call in lookup_failure_calls)
			assert "HTTP 403" in lookup_failure.stderr


def test_workflow_release_creation_prefers_repository_pat() -> None:
	for workflow_path in WORKFLOWS:
		workflow_text = _read(workflow_path)
		step_start = workflow_text.index("      - name: Create GitHub Release")
		step_end = workflow_text.index("\n      - name:", step_start + 1)
		step_text = workflow_text[step_start:step_end]
		assert "GH_TOKEN: ${{ secrets.GH_PAT || github.token }}" in step_text, (
			f"{workflow_path.name}: release lookup and creation must prefer GH_PAT"
		)


def test_workflow_tag_publication_helper_is_bounded_verified_and_fail_closed() -> None:
	for workflow_path in WORKFLOWS:
		workflow_text = _read(workflow_path)
		helper_text = _workflow_publication_helper(workflow_text)

		assert "local max_attempts=5" in helper_text, (
			f"{workflow_path.name}: tag publication retries must be bounded to five attempts"
		)
		assert 'sleep "${backoff_seconds}"' in helper_text, (
			f"{workflow_path.name}: failed publication must back off before retrying"
		)
		assert "backoff_seconds=$((backoff_seconds * 2))" in helper_text, (
			f"{workflow_path.name}: publication retry delay must increase exponentially"
		)
		assert 'git ls-remote --exit-code origin "${tag_ref}"' in helper_text, (
			f"{workflow_path.name}: a failed push must query the exact remote tag ref"
		)
		assert 'mktemp "${RUNNER_TEMP:-/tmp}/release_tag_lsremote_err.XXXXXX"' in helper_text, (
			f"{workflow_path.name}: remote lookup diagnostics must use an isolated temporary file"
		)
		assert '2>"${remote_lookup_error_file}"' in helper_text, (
			f"{workflow_path.name}: remote lookup stderr must use the isolated temporary file"
		)
		assert 'rm -f "${remote_lookup_error_file}"' in helper_text, (
			f"{workflow_path.name}: remote lookup diagnostic files must be removed after capture"
		)
		assert 'if ! expected_object_id="$(git rev-parse "${tag_ref}"' in helper_text, (
			f"{workflow_path.name}: an unresolved local tag must fail with the helper diagnostic"
		)
		assert 'remote_object_id}" = "${expected_object_id}' in helper_text, (
			f"{workflow_path.name}: identical remote and local tag objects must recover an ambiguous push"
		)
		assert 'publication_mode}" = "immutable"' in helper_text, (
			f"{workflow_path.name}: immutable publication must have a distinct fail-closed path"
		)
		assert "exists remotely at a different object ID; refusing to overwrite it" in helper_text, (
			f"{workflow_path.name}: an immutable tag collision must fail without overwrite"
		)
		assert 'git push origin "${tag_ref}"' in helper_text, (
			f"{workflow_path.name}: immutable tag publication must remain non-forced"
		)
		assert 'git push -f origin "${tag_ref}"' in helper_text, (
			f"{workflow_path.name}: moving tag publication must retain force-update semantics"
		)
		assert helper_text.index('git push origin "${tag_ref}"') < helper_text.index(
			'git push -f origin "${tag_ref}"'
		), f"{workflow_path.name}: immutable mode must be selected before the moving force push"
		assert 'lookup_rc}" -ne 0' in helper_text, (
			f"{workflow_path.name}: unreadable remote state must not be accepted as success"
		)
		assert "Failed to publish and verify" in helper_text, (
			f"{workflow_path.name}: absent, malformed, or non-converged refs must fail after retries"
		)


def test_workflows_share_identical_publication_logic_and_call_order() -> None:
	workflow_texts = [_read(workflow_path) for workflow_path in WORKFLOWS]
	assert _workflow_publication_helper(workflow_texts[0]) == _workflow_publication_helper(
		workflow_texts[1]
	), "both release workflows must keep byte-identical tag publication helpers"
	for step_name in (
		"Validate existing version tag",
		"Tag version and update stable pointer",
		"Create GitHub Release",
	):
		assert _workflow_step_script(workflow_texts[0], step_name) == _workflow_step_script(
			workflow_texts[1], step_name
		), f"both release workflows must keep byte-identical {step_name!r} shell logic"

	for workflow_path, workflow_text in zip(WORKFLOWS, workflow_texts, strict=True):
		version_call = 'publish_tag_with_remote_verification "refs/tags/$VERSION" immutable'
		stable_call = "publish_tag_with_remote_verification refs/tags/stable moving"
		major_call = 'publish_tag_with_remote_verification "refs/tags/$MAJOR" moving'
		assert workflow_text.index(version_call) < workflow_text.index(stable_call) < workflow_text.index(
			major_call
		), f"{workflow_path.name}: tag publication order must remain version, stable, major"


def test_workflows_dispatch_peeled_release_commit_sha() -> None:
	for workflow_path in WORKFLOWS:
		workflow_text = _read(workflow_path)
		assert 'RELEASE_SHA="$(git rev-parse "${VERSION}^{commit}")"' in workflow_text, (
			f"{workflow_path.name}: release dispatch must peel the annotated version tag"
		)
		assert '[[ ! "${RELEASE_SHA}" =~ ^[0-9a-fA-F]{40}$ ]]' in workflow_text, (
			f"{workflow_path.name}: peeled release SHA must be validated before dispatch"
		)
		assert '-f "client_payload[sha]=${RELEASE_SHA}"' in workflow_text, (
			f"{workflow_path.name}: consumer dispatch must include the immutable release SHA"
		)


def test_script_pushes_all_three_tags_via_refs_tags() -> None:
	text = _read(SCRIPT)
	assert 'git push origin "refs/tags/${VERSION_TAG}"' in text, (
		"scripts/mark-stable.sh: VERSION_TAG push must use refs/tags/${VERSION_TAG}"
	)
	assert "git push -f origin refs/tags/stable" in text, (
		"scripts/mark-stable.sh: stable tag push must use refs/tags/stable"
	)
	assert 'git push -f origin "refs/tags/${MAJOR}"' in text, (
		"scripts/mark-stable.sh: major-version push must use refs/tags/${MAJOR}"
	)
	assert not re.search(r'^\s*git push origin "\$\{?VERSION_TAG\}?"\s*$', text, re.MULTILINE), (
		"scripts/mark-stable.sh: bare 'git push origin \"${VERSION_TAG}\"' would re-introduce the regression"
	)
	assert not re.search(r"^\s*git push -f origin stable\s*$", text, re.MULTILINE), (
		"scripts/mark-stable.sh: bare 'git push -f origin stable' would re-introduce the regression"
	)
	assert not re.search(r'^\s*git push -f origin "\$\{?MAJOR\}?"\s*$', text, re.MULTILINE), (
		"scripts/mark-stable.sh: bare 'git push -f origin \"${MAJOR}\"' would re-introduce the regression"
	)


def test_script_creates_annotated_release_tag_like_workflows() -> None:
	text = _read(SCRIPT)
	assert 'git tag -a "${VERSION_TAG}" -m "Release ${VERSION_TAG}" origin/stable' in text, (
		"scripts/mark-stable.sh: VERSION_TAG must be an annotated tag (-a/-m), "
		"matching the workflows' `git tag -a \"$VERSION\" -m \"Release $VERSION\"` "
		"so manual and automated release paths produce identical tag metadata"
	)
	# The workflows do not pass -f when creating the immutable VERSION tag, so a
	# rerun against an already-tagged version fails loudly. Pin the same safety
	# semantic for the script: forcing here would silently retarget the local
	# immutable tag before the push fails.
	assert not re.search(
		r'^\s*git tag -fa? "\$\{?VERSION_TAG\}?"', text, re.MULTILINE
	), (
		"scripts/mark-stable.sh: must not use 'git tag -f' / 'git tag -fa' for "
		"the immutable VERSION_TAG — that diverges from the workflow safety "
		"model (which fails if the tag already exists locally)"
	)
	assert not re.search(
		r'^\s*git tag -f "\$\{?VERSION_TAG\}?" origin/stable\s*$', text, re.MULTILINE
	), (
		"scripts/mark-stable.sh: lightweight 'git tag -f \"${VERSION_TAG}\" origin/stable' "
		"would re-introduce metadata divergence with the workflow release path"
	)


def test_script_releases_from_stable_branch_for_workflow_parity() -> None:
	text = _read(SCRIPT)
	assert "git fetch origin stable" in text, (
		"scripts/mark-stable.sh: must fetch origin/stable so manual releases "
		"match the workflow path (which releases from the stable branch)"
	)
	assert "origin/stable" in text, (
		"scripts/mark-stable.sh: VERSION_TAG must be created from origin/stable, "
		"matching the workflow release path"
	)
	# Two precise regression patterns to forbid:
	#   1. `git tag ... origin/main`    — re-introduces tagging from main
	#   2. `git fetch origin main`      — re-introduces fetching main
	# Use targeted regexes (not a broad substring match) so harmless future
	# strings or comments mentioning `origin/main` don't trip the guard.
	assert not re.search(r"^\s*git\s+tag\b[^\n]*\borigin/main\b", text, re.MULTILINE), (
		"scripts/mark-stable.sh: must not tag from origin/main — that would tag a "
		"main commit that hasn't been validated on the stable branch"
	)
	assert not re.search(r"^\s*git\s+fetch\s+origin\s+main\b", text, re.MULTILINE), (
		"scripts/mark-stable.sh: must not fetch origin/main — manual releases "
		"must match the workflow path which fetches and releases from stable"
	)


def test_script_distinguishes_missing_branch_from_other_lsremote_failures() -> None:
	text = _read(SCRIPT)
	# rc=2 from `git ls-remote --exit-code` means "no matching ref"; any other
	# non-zero is a real failure (auth/network/transport). Collapsing both into
	# "create the branch first" steers operators toward the wrong remediation
	# on auth/transport errors.
	assert re.search(r"LSREMOTE_RC.*-eq\s+2", text), (
		"scripts/mark-stable.sh: must distinguish git ls-remote rc=2 (missing) "
		"from other failures (auth/transport) so misleading 'create the branch' "
		"guidance is only emitted for the actual missing-branch case"
	)


def test_script_handles_already_published_version_tag() -> None:
	text = _read(SCRIPT)
	# 1. Existence check: must run before any tag creation.
	assert 'git ls-remote --exit-code --tags origin "refs/tags/${VERSION_TAG}"' in text, (
		"scripts/mark-stable.sh: must check that refs/tags/${VERSION_TAG} is "
		"unused on origin before creating any tags, mirroring the workflow's "
		"non-forced `git tag -a` safety semantic"
	)
	# 2. Recovery vs conflict: when the tag already exists, the script must
	#    compare its commit against origin/stable HEAD instead of
	#    unconditionally hard-failing. A rerun after a partial publish (tag
	#    pushed, moving pointers failed) is a legitimate recovery and must
	#    finish the moving-tag pushes against the existing immutable tag.
	assert '${VERSION_TAG}^{commit}' in text, (
		"scripts/mark-stable.sh: must resolve the existing tag's commit via "
		"`git rev-parse \"${VERSION_TAG}^{commit}\"` so partial-publish recovery "
		"can be distinguished from a real retarget conflict"
	)
	assert "refs/remotes/origin/stable" in text, (
		"scripts/mark-stable.sh: must compare against `refs/remotes/origin/stable` "
		"to detect the partial-publish recovery case"
	)
	assert "SKIP_VERSION_TAG_CREATE" in text, (
		"scripts/mark-stable.sh: must guard the tag-create + tag-push steps with a "
		"recovery flag (e.g. SKIP_VERSION_TAG_CREATE) so a partial-publish rerun "
		"finishes the moving-tag pushes without recreating or re-pushing the "
		"immutable VERSION_TAG"
	)


def test_script_validates_git_identity_for_annotated_tag() -> None:
	text = _read(SCRIPT)
	# `git tag -a` requires committer identity; the script must pre-check it
	# instead of failing mid-run with "Committer identity unknown".
	assert "git config --get user.email" in text, (
		"scripts/mark-stable.sh: must pre-check `git config --get user.email` "
		"so a missing identity fails fast with actionable guidance instead of "
		"mid-run with 'Committer identity unknown'"
	)
	assert "git config --get user.name" in text, (
		"scripts/mark-stable.sh: must pre-check `git config --get user.name` "
		"alongside user.email — both are required for annotated tags"
	)


def main() -> None:
	# Failures surface via uncaught AssertionError → Python's default non-zero
	# exit; on full success we just print and return None.
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")


if __name__ == "__main__":
	main()
