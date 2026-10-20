#!/usr/bin/env python3
"""Regression guard: release jobs must push tags via fully qualified refs/tags/ refspecs.

Bare refspecs like `git push -f origin stable` fail with
`error: src refspec stable matches more than one` whenever the runner has both
a refs/heads/<name> branch and a refs/tags/<name> tag locally (which the
release jobs trigger by checking out the `stable` branch and then creating a
`stable` tag). See PR #2031 / job-logs.txt L1500-1502 for the live failure.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
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
			if [ "$*" = "origin refs/heads/stable" ]; then
				printf '%040d\n' 1
				return 0
			fi
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
		"RELEASE_TESTED_SHA": "0" * 39 + "1",
		"SOURCE_BRANCH": "stable",
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
				release-draft|release-prerelease)
					echo 'release-is-draft-or-prerelease' >&2
					return 1
					;;
				release-absent)
					echo 'gh: Not Found (HTTP 404)' >&2
					return 1
					;;
				release-race|release-race-draft|release-race-prerelease)
					if [ "${release_mock_count}" -eq 1 ]; then
						echo 'gh: Not Found (HTTP 404)' >&2
						return 1
					fi
					if [ "${RELEASE_SCENARIO}" = "release-race" ]; then
						printf 'v1.2.3\n'
					else
						echo 'release-is-draft-or-prerelease' >&2
						return 1
					fi
					return 0
					;;
				release-lookup-failure)
					echo 'gh: Resource not accessible by integration (HTTP 403)' >&2
					return 1
					;;
			esac
			;;
		release)
			case "${RELEASE_SCENARIO}" in
				release-race|release-race-draft|release-race-prerelease) return 1 ;;
			esac
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
	workflow_path: Path = WORKFLOWS[0],
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	publication_call_log = (
		working_directory / f"{workflow_path.stem}-{publication_scenario}-{publication_mode}.log"
	)
	workflow_publication_script = "\n".join(
		(
			"set -euo pipefail",
			_workflow_publication_helper(_read(workflow_path)),
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
			case "${PUBLICATION_HELPER_SCENARIO}" in
				push-success)
					return 0
					;;
				push-workflow-scope)
					printf '%s\n' ' ! [remote rejected] test -> test (refusing to allow an OAuth App to create or update workflow `.github/workflows/ci.yml` without `workflow` scope)' >&2
					return 1
					;;
				push-workflows-permission)
					printf '%s\n' ' ! [remote rejected] test -> test (REFUSING TO ALLOW a GitHub App to create or update workflow `.github/workflows/x.yml` without `Workflows` permission)' >&2
					return 1
					;;
				push-generic-error)
					printf '%s\n' 'remote: Internal Server Error' >&2
					return 1
					;;
				*)
					return 1
					;;
			esac
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
				remote-absent|push-generic-error)
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


def test_workflow_tag_publication_helper_stops_on_workflow_scope_refusal() -> None:
	with tempfile.TemporaryDirectory() as temporary_directory:
		working_directory = Path(temporary_directory)
		for workflow_path in WORKFLOWS:
			for scenario, mode, expected_push in (
				("push-workflow-scope", "immutable", "push:origin refs/tags/test"),
				("push-workflow-scope", "moving", "push:-f origin refs/tags/test"),
				("push-workflows-permission", "immutable", "push:origin refs/tags/test"),
			):
				refused, refused_calls = _run_publication_helper_scenario(
					working_directory, scenario, mode, workflow_path
				)
				assert refused.returncode != 0, f"{workflow_path.name} {scenario}/{mode}"
				assert refused_calls == [expected_push], (
					f"{workflow_path.name} {scenario}/{mode}: a workflow-scope refusal must stop "
					f"after one push with no lookup or retry sleep, got {refused_calls}"
				)
				assert (
					"::error::RELEASE_TAG_PUBLISH outcome=failed error_class=workflow_scope "
					"tag_ref=refs/tags/test"
				) in refused.stdout, f"{workflow_path.name} {scenario}/{mode}"
				assert "GH_PAT" in refused.stdout
				assert "refusing to allow" in refused.stderr.lower(), (
					f"{workflow_path.name}: git's push stderr must still be printed"
				)
			scoped, _ = _run_publication_helper_scenario(
				working_directory, "push-workflow-scope", "immutable", workflow_path
			)
			assert "without `workflow` scope" in scoped.stderr

			generic, generic_calls = _run_publication_helper_scenario(
				working_directory, "push-generic-error", "immutable", workflow_path
			)
			assert generic.returncode != 0
			assert generic_calls.count("push:origin refs/tags/test") == 5
			assert generic_calls.count("lookup:--exit-code origin refs/tags/test") == 5
			assert [call for call in generic_calls if call.startswith("sleep:")] == [
				"sleep:2",
				"sleep:4",
				"sleep:8",
				"sleep:16",
			], f"{workflow_path.name}: generic push failures must keep the retry backoff"
			assert "Failed to publish and verify" in generic.stdout
			assert "RELEASE_TAG_PUBLISH" not in generic.stdout + generic.stderr
			assert "Internal Server Error" in generic.stderr


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
			release_step_script = _workflow_step_script(_read(workflow_path), "Create GitHub Release")
			published_release_filter = (
				"--jq 'if (.draft == false and .prerelease == false) then .tag_name "
				'else error("release-is-draft-or-prerelease") end\''
			)
			assert release_step_script.count(published_release_filter) == 2

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

			for unpublished_scenario in ("release-draft", "release-prerelease"):
				unpublished_release, unpublished_calls = _run_release_creation_scenario(
					working_directory, workflow_path, unpublished_scenario
				)
				assert unpublished_release.returncode != 0
				assert all(not call.startswith("release:") for call in unpublished_calls)
				assert "release-is-draft-or-prerelease" in unpublished_release.stderr

			for concurrent_unpublished_scenario in (
				"release-race-draft",
				"release-race-prerelease",
			):
				concurrent_unpublished, concurrent_unpublished_calls = _run_release_creation_scenario(
					working_directory, workflow_path, concurrent_unpublished_scenario
				)
				assert concurrent_unpublished.returncode != 0
				assert len(
					[call for call in concurrent_unpublished_calls if call.startswith("api:")]
				) == 2
				assert any(
					call.startswith("release:create v1.2.3")
					for call in concurrent_unpublished_calls
				)
				assert "appeared concurrently" not in concurrent_unpublished.stdout
				assert "release-is-draft-or-prerelease" in concurrent_unpublished.stderr

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


def test_workflow_release_checkout_prefers_repository_pat_for_git_transport() -> None:
	for workflow_path in WORKFLOWS:
		workflow_text = _read(workflow_path)
		assert "      - name: Checkout source branch" in workflow_text, (
			f"{workflow_path.name}: release checkout step is missing"
		)
		step_start = workflow_text.index("      - name: Checkout source branch")
		step_end = workflow_text.index("\n      - name:", step_start + 1)
		step_text = workflow_text[step_start:step_end]
		assert "token: ${{ secrets.GH_PAT || github.token }}" in step_text, (
			f"{workflow_path.name}: release Git transport must prefer GH_PAT"
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
		assert 'mktemp "${RUNNER_TEMP:-/tmp}/release_tag_push_err.XXXXXX"' in helper_text, (
			f"{workflow_path.name}: push diagnostics must use an isolated temporary file"
		)
		assert '2>"${push_error_file}"' in helper_text, (
			f"{workflow_path.name}: push stderr must be captured for classification"
		)
		assert "grep -qi 'refusing to allow'" in helper_text, (
			f"{workflow_path.name}: the workflow-scope refusal must be matched case-insensitively"
		)
		classification_index = helper_text.index("error_class=workflow_scope")
		sleep_index = helper_text.index('sleep "${backoff_seconds}"')
		assert classification_index < sleep_index, (
			f"{workflow_path.name}: the workflow-scope refusal must be classified before the retry sleep"
		)
		assert "return 1" in helper_text[classification_index:sleep_index], (
			f"{workflow_path.name}: the workflow-scope refusal must return before retrying"
		)
		assert classification_index < helper_text.index('git ls-remote --exit-code origin "${tag_ref}"'), (
			f"{workflow_path.name}: the workflow-scope refusal must stop before remote verification"
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


# ──────────────────────────────────────────────────────────────────
# Issue #6532: CI lint check-run gate and the STABLE_RELEASE_ENABLED switch
# ──────────────────────────────────────────────────────────────────

CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
TESTED_SHA = "a" * 40
# The publishing steps also carry the RELEASE_TAGGING_ENABLED kill switch (#7029).
PUBLISHING_STEP_IF = (
	"        if: ${{ !inputs.dry_run && steps.release_switch.outputs.enabled == 'true'"
	" && steps.release_tagging.outputs.enabled == 'true' }}\n"
)
CI_SUITE_ID = 7
CI_WORKFLOW_RUNS_BODY = json.dumps({
	"total_count": 1,
	"workflow_runs": [{"head_sha": TESTED_SHA, "check_suite_id": CI_SUITE_ID, "path": ".github/workflows/ci.yml"}],
})
CI_LINT_GATE_MOCK = r'''gh() {
	local gate_mock_count gate_mock_file
	printf 'gh:%s\n' "$*" >> "${GATE_MOCK_CALL_LOG}"
	case "$*" in
		*"/actions/workflows/ci.yml/runs?"*)
			gate_mock_file="${GATE_MOCK_WORKFLOW_RUNS}"
			;;
		*)
			gate_mock_count="$(cat "${GATE_MOCK_COUNT}")"
			gate_mock_count=$((gate_mock_count + 1))
			printf '%s\n' "${gate_mock_count}" > "${GATE_MOCK_COUNT}"
			gate_mock_file="${GATE_MOCK_RESPONSES}/${gate_mock_count}"
			[ -f "${gate_mock_file}" ] || gate_mock_file="${GATE_MOCK_RESPONSES}/last"
			;;
	esac
	if [ "$(cat "${gate_mock_file}")" = "__FAIL__" ]; then
		echo 'gh: Resource not accessible by integration (HTTP 403)' >&2
		return 1
	fi
	cat "${gate_mock_file}"
}
sleep() {
	printf 'sleep:%s\n' "$1" >> "${GATE_MOCK_CALL_LOG}"
}
'''


def _top_level_job_block(workflow_text: str, job_name: str) -> str:
	match = re.search(
		rf"^  {re.escape(job_name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
		workflow_text,
		re.MULTILINE | re.DOTALL,
	)
	assert match is not None, f"job {job_name!r} is missing"
	return match.group(1)


def _step_block(job_text: str, step_name: str) -> str:
	start = job_text.index(f"      - name: {step_name}\n")
	end = job_text.find("\n      - name: ", start + 1)
	return job_text[start:] if end == -1 else job_text[start:end]


def _clean_bash_environment(**extra: str) -> dict[str, str]:
	environment = {
		key: value
		for key, value in os.environ.items()
		if key not in {"BASH_ENV", "ENV"} and not key.startswith("BASH_FUNC_")
	}
	environment.update(extra)
	return environment


def _check_runs_body(*runs: dict, total_count: int | None = None) -> str:
	return json.dumps({
		"total_count": len(runs) if total_count is None else total_count,
		"check_runs": list(runs),
	})


_LINT_RUN_IDS = itertools.count(1000)


def _lint_run(
	status: str = "completed",
	conclusion: str | None = "success",
	slug: str = "github-actions",
	suite_id: int = CI_SUITE_ID,
	run_id: int | None = None,
) -> dict:
	# Ids increase with each call, so a later call is the newer check-run.
	return {
		"id": next(_LINT_RUN_IDS) if run_id is None else run_id,
		"name": "lint",
		"status": status,
		"conclusion": conclusion,
		"app": {"slug": slug},
		"check_suite": {"id": suite_id},
	}


def _run_ci_lint_gate_scenario(
	working_directory: Path,
	workflow_path: Path,
	responses: list[str],
	*,
	wait_secs: str = "0",
	tested_sha: str = TESTED_SHA,
	workflow_runs: str = CI_WORKFLOW_RUNS_BODY,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	scenario_directory = Path(tempfile.mkdtemp(dir=working_directory))
	response_directory = scenario_directory / "responses"
	response_directory.mkdir()
	for index, body in enumerate(responses, start=1):
		(response_directory / str(index)).write_text(body, encoding="utf-8")
	(response_directory / "last").write_text(responses[-1], encoding="utf-8")
	workflow_runs_file = scenario_directory / "workflow_runs.json"
	workflow_runs_file.write_text(workflow_runs, encoding="utf-8")
	call_log = scenario_directory / "calls.log"
	call_log.write_text("", encoding="utf-8")
	call_count = scenario_directory / "count"
	call_count.write_text("0\n", encoding="utf-8")
	gate_script = "\n".join(
		(
			"unset -f gh_retry 2>/dev/null || true",
			CI_LINT_GATE_MOCK,
			_workflow_step_script(_read(workflow_path), "Require successful CI lint check-run"),
		)
	)
	gate_result = subprocess.run(
		["bash", "-c", gate_script],
		capture_output=True,
		check=False,
		cwd=scenario_directory,
		env=_clean_bash_environment(
			CI_CHECK_NAME="lint",
			GATE_MOCK_CALL_LOG=str(call_log),
			GATE_MOCK_COUNT=str(call_count),
			GATE_MOCK_RESPONSES=str(response_directory),
			GATE_MOCK_WORKFLOW_RUNS=str(workflow_runs_file),
			GITHUB_REPOSITORY="owner/repo",
			RELEASE_TESTED_SHA=tested_sha,
			RUNNER_TEMP=str(scenario_directory),
			STABLE_RELEASE_CI_WAIT_SECS=wait_secs,
		),
		text=True,
	)
	return gate_result, call_log.read_text(encoding="utf-8").splitlines()


def test_ci_lint_gate_step_is_identical_and_ordered_in_both_release_jobs() -> None:
	gate_scripts = []
	switch_scripts = []
	for workflow_path in WORKFLOWS:
		workflow_text = _read(workflow_path)
		release_job = _top_level_job_block(workflow_text, "release")
		order = [
			release_job.index("      - name: Record the tested commit\n"),
			release_job.index("      - name: Resolve stable release switch\n"),
			release_job.index("      - name: Require successful CI lint check-run\n"),
			release_job.index("      - name: Assemble changelog fragments\n"),
			release_job.index("      - name: Tag version and update stable pointer\n"),
		]
		assert order == sorted(order), (
			f"{workflow_path.name}: the CI gate must run after recording the SHA and before any push"
		)
		gate_step = _step_block(release_job, "Require successful CI lint check-run")
		assert "\n        if:" not in gate_step, (
			f"{workflow_path.name}: the CI gate must run even when publishing is off"
		)
		gate_script = _workflow_step_script(workflow_text, "Require successful CI lint check-run")
		switch_script = _workflow_step_script(workflow_text, "Resolve stable release switch")
		assert "${{" not in gate_script and "${{" not in switch_script
		assert "check_name=${gate_check_name}&filter=latest&per_page=100" in gate_script
		assert "actions/workflows/ci.yml/runs?head_sha=${gate_sha}&per_page=100" in gate_script
		assert "gate_wait_secs=$((10#${gate_wait_secs}))" in gate_script
		gate_scripts.append(gate_script)
		switch_scripts.append(switch_script)
	assert gate_scripts[0] == gate_scripts[1], "both release workflows must keep a byte-identical CI lint gate"
	assert switch_scripts[0] == switch_scripts[1], "both release workflows must resolve the switch identically"


def test_ci_lint_gate_executes_fail_closed_matrix() -> None:
	if shutil.which("jq") is None:
		print("skip: jq is not installed")
		return
	other_name = {
		"name": "static-checks",
		"status": "completed",
		"conclusion": "success",
		"app": {"slug": "github-actions"},
		"check_suite": {"id": CI_SUITE_ID},
	}
	scenarios = {
		"pass": ([_check_runs_body(_lint_run(), _lint_run())], True, "outcome=pass"),
		"missing": ([_check_runs_body()], False, "reason=missing"),
		"incomplete": ([_check_runs_body(_lint_run(status="in_progress", conclusion=None))], False, "reason=incomplete"),
		"failure": ([_check_runs_body(_lint_run(conclusion="failure"))], False, "reason=failed"),
		"cancelled": ([_check_runs_body(_lint_run(conclusion="cancelled"))], False, "reason=failed"),
		"mixed": ([_check_runs_body(_lint_run(), _lint_run(conclusion="failure"))], False, "reason=failed"),
		"api_error": (["__FAIL__"], False, "reason=unreadable"),
		"malformed": (["{not json"], False, "reason=unreadable"),
		"no_array": ([json.dumps({"total_count": 1, "check_runs": None})], False, "reason=unreadable"),
		"partial_page": ([_check_runs_body(_lint_run(), total_count=2)], False, "reason=unreadable"),
		"foreign_app": ([_check_runs_body(_lint_run(slug="third-party"))], False, "reason=missing"),
		"other_name": ([_check_runs_body(other_name)], False, "reason=missing"),
		# A `lint` job of another workflow (lint-plan-archival.yml) must not count.
		"other_workflow_failed": (
			[_check_runs_body(_lint_run(), _lint_run(conclusion="failure", suite_id=99))],
			True,
			"outcome=pass",
		),
		"other_workflow_only": ([_check_runs_body(_lint_run(suite_id=99))], False, "reason=missing"),
		# Like scripts/release_ci_gate.sh, the newest check-run (highest id) decides.
		"rerun_passed": (
			[_check_runs_body(_lint_run(conclusion="failure", run_id=1), _lint_run(run_id=2))],
			True,
			"outcome=pass",
		),
		"rerun_failed": (
			[_check_runs_body(_lint_run(run_id=2), _lint_run(conclusion="cancelled", run_id=3))],
			False,
			"reason=failed",
		),
		"rerun_running": (
			[_check_runs_body(_lint_run(run_id=1), _lint_run(status="in_progress", conclusion=None, run_id=4))],
			False,
			"reason=incomplete",
		),
		"no_id": (
			[_check_runs_body({**_lint_run(), "id": None})],
			False,
			"reason=missing",
		),
	}
	expected_calls = [
		f"gh:api repos/owner/repo/actions/workflows/ci.yml/runs?head_sha={TESTED_SHA}&per_page=100",
		f"gh:api repos/owner/repo/commits/{TESTED_SHA}/check-runs?check_name=lint&filter=latest&per_page=100",
	]
	with tempfile.TemporaryDirectory() as temporary_directory:
		working_directory = Path(temporary_directory)
		for workflow_path in WORKFLOWS:
			for scenario_name, (responses, should_pass, expected_text) in scenarios.items():
				result, calls = _run_ci_lint_gate_scenario(working_directory, workflow_path, responses)
				label = f"{workflow_path.name}:{scenario_name}"
				assert (result.returncode == 0) is should_pass, (label, result.stdout, result.stderr)
				assert expected_text in result.stdout, (label, result.stdout)
				assert [call for call in calls if call.startswith("gh:")] == expected_calls, label
				assert not [call for call in calls if call.startswith("sleep:")], label

			for runs_name, runs_body in (
				("runs_api_error", "__FAIL__"),
				("runs_malformed", "{not json"),
				("runs_partial", json.dumps({"total_count": 2, "workflow_runs": []})),
			):
				result, calls = _run_ci_lint_gate_scenario(
					working_directory, workflow_path, [_check_runs_body(_lint_run())], workflow_runs=runs_body
				)
				label = f"{workflow_path.name}:{runs_name}"
				assert result.returncode != 0, (label, result.stdout)
				assert "reason=unreadable" in result.stdout, (label, result.stdout)

			leading_zero, _ = _run_ci_lint_gate_scenario(
				working_directory, workflow_path, [_check_runs_body(_lint_run())], wait_secs="08"
			)
			assert leading_zero.returncode == 0, (leading_zero.stdout, leading_zero.stderr)

			# Waits above 2400 seconds (the RELEASE_CI_GATE_WAIT_SECS range) fall back to 600.
			for oversized in ("2401", "99999999999999999999999"):
				capped, capped_calls = _run_ci_lint_gate_scenario(
					working_directory,
					workflow_path,
					[_check_runs_body(_lint_run(status="queued", conclusion=None))],
					wait_secs=oversized,
				)
				assert capped.returncode != 0, (oversized, capped.stdout, capped.stderr)
				assert "is above 2400; using 600" in capped.stdout, (oversized, capped.stdout)
				assert capped_calls.count("sleep:30") == 20, (oversized, capped_calls.count("sleep:30"))

			# Zero-padded in-range values keep their value; positive budgets below one
			# 30-second poll still re-check once; 0 never waits.
			for padded_wait, expected_sleeps in (("00060", 2), ("002400", 80), ("10", 1), ("29", 1), ("000", 0)):
				padded, padded_calls = _run_ci_lint_gate_scenario(
					working_directory,
					workflow_path,
					[_check_runs_body(_lint_run(status="queued", conclusion=None))],
					wait_secs=padded_wait,
				)
				assert padded.returncode != 0, (padded_wait, padded.stdout)
				assert "is above 2400" not in padded.stdout, (padded_wait, padded.stdout)
				assert padded_calls.count("sleep:30") == expected_sleeps, (padded_wait, padded_calls.count("sleep:30"))

			bad_sha, bad_sha_calls = _run_ci_lint_gate_scenario(
				working_directory, workflow_path, [_check_runs_body(_lint_run())], tested_sha="not-a-sha"
			)
			assert bad_sha.returncode != 0
			assert "reason=invalid_sha" in bad_sha.stdout
			assert bad_sha_calls == []


def test_ci_lint_gate_waits_a_bounded_time_for_incomplete_ci() -> None:
	if shutil.which("jq") is None:
		print("skip: jq is not installed")
		return
	incomplete = _check_runs_body(_lint_run(status="queued", conclusion=None))
	with tempfile.TemporaryDirectory() as temporary_directory:
		working_directory = Path(temporary_directory)
		for workflow_path in WORKFLOWS:
			recovered, recovered_calls = _run_ci_lint_gate_scenario(
				working_directory, workflow_path, [incomplete, _check_runs_body(_lint_run())], wait_secs="60"
			)
			assert recovered.returncode == 0, recovered.stdout
			assert "outcome=pass" in recovered.stdout
			assert recovered_calls.count("sleep:30") == 1

			exhausted, exhausted_calls = _run_ci_lint_gate_scenario(
				working_directory, workflow_path, [incomplete], wait_secs="60"
			)
			assert exhausted.returncode != 0
			assert "reason=incomplete" in exhausted.stdout
			assert exhausted_calls.count("sleep:30") == 2
			assert len([call for call in exhausted_calls if "/check-runs?" in call]) == 3

			# CI that has not created its check-run yet is waited for, not failed at once.
			missing_then_pass, missing_calls = _run_ci_lint_gate_scenario(
				working_directory, workflow_path, [_check_runs_body(), _check_runs_body(_lint_run())], wait_secs="60"
			)
			assert missing_then_pass.returncode == 0, missing_then_pass.stdout
			assert "outcome=wait reason=missing" in missing_then_pass.stdout
			assert missing_calls.count("sleep:30") == 1

			missing_exhausted, missing_exhausted_calls = _run_ci_lint_gate_scenario(
				working_directory, workflow_path, [_check_runs_body()], wait_secs="60"
			)
			assert missing_exhausted.returncode != 0
			assert "outcome=fail reason=missing" in missing_exhausted.stdout
			assert missing_exhausted_calls.count("sleep:30") == 2

			failed, failed_calls = _run_ci_lint_gate_scenario(
				working_directory, workflow_path, [_check_runs_body(_lint_run(conclusion="failure"))], wait_secs="60"
			)
			assert failed.returncode != 0
			assert not [call for call in failed_calls if call.startswith("sleep:")], (
				"only incomplete results may be re-polled"
			)


def test_release_switch_resolution_is_exact_and_defaults_on() -> None:
	# Unset or empty publishes (the default was off until it held every
	# release since v1.31.0 back, #7029). Only `true` publishes when a value is
	# set; `false` and unrecognised values keep publishing off, so a typo
	# never publishes.
	cases = {
		None: "true",
		"": "true",
		"false": "false",
		"FALSE": "false",
		"true": "true",
		"TRUE": "true",
		"yes": "false",
		"true ": "false",
		"1": "false",
	}
	unrecognised = {"yes", "true ", "1"}
	with tempfile.TemporaryDirectory() as temporary_directory:
		working_directory = Path(temporary_directory)
		for workflow_path in WORKFLOWS:
			switch_script = _workflow_step_script(_read(workflow_path), "Resolve stable release switch")
			for index, (raw_value, expected) in enumerate(cases.items()):
				output_file = working_directory / f"{workflow_path.stem}-{index}.out"
				environment = _clean_bash_environment(GITHUB_OUTPUT=str(output_file))
				environment.pop("STABLE_RELEASE_ENABLED", None)
				if raw_value is not None:
					environment["STABLE_RELEASE_ENABLED"] = raw_value
				result = subprocess.run(
					["bash", "-c", switch_script], capture_output=True, check=False, env=environment, text=True
				)
				assert result.returncode == 0, result.stderr
				assert output_file.read_text(encoding="utf-8") == f"enabled={expected}\n", (
					workflow_path.name,
					raw_value,
				)
				assert f"STABLE_RELEASE_SWITCH enabled={expected}" in result.stdout
				assert ("::warning::STABLE_RELEASE_ENABLED has an unrecognised value" in result.stdout) == (
					raw_value in unrecognised
				), (workflow_path.name, raw_value)


def test_release_job_disabled_path_skips_every_publishing_step() -> None:
	for workflow_path in WORKFLOWS:
		release_job = _top_level_job_block(_read(workflow_path), "release")
		# The job-level block also carries the release-manifest attestation scopes
		# (tests/test_release_manifest_workflow_contract.py pins the full set).
		assert "    permissions:\n      contents: write\n" in release_job, workflow_path.name
		assert "\n      checks: read\n" in release_job.split("    outputs:", 1)[0], workflow_path.name
		assert "      STABLE_RELEASE_ENABLED: ${{ vars.STABLE_RELEASE_ENABLED || 'true' }}\n" in release_job
		assert "      STABLE_RELEASE_CI_WAIT_SECS: ${{ vars.STABLE_RELEASE_CI_WAIT_SECS || '600' }}\n" in release_job
		assert (
			"      release_enabled: ${{ steps.release_switch.outputs.enabled == 'true'"
			" && steps.release_tagging.outputs.enabled == 'true' && 'true' || 'false' }}\n"
		) in release_job
		for step_name in (
			"Tag version and update stable pointer",
			"Create GitHub Release",
			"Notify consumer repos via repository_dispatch",
		):
			assert _step_block(release_job, step_name).startswith(
				f"      - name: {step_name}\n{PUBLISHING_STEP_IF}"
			), f"{workflow_path.name}: {step_name} must be skipped while publishing is off"
		assemble = _step_block(release_job, "Assemble changelog fragments")
		assert (
			"DRY_RUN: ${{ (inputs.dry_run || steps.release_switch.outputs.enabled != 'true') && 'true' || 'false' }}"
			in assemble
		), f"{workflow_path.name}: changelog assembly must not push while publishing is off"
		disabled = _step_block(release_job, "Release disabled summary")
		assert "if: ${{ !inputs.dry_run && steps.release_switch.outputs.enabled != 'true' }}" in disabled
		assert "STABLE_RELEASE_DISABLED" in disabled

	gate_text = _read(WORKFLOWS[1])
	sync_if = _top_level_job_block(gate_text, "sync-to-main").split("    runs-on:", 1)[0]
	assert "!inputs.gate_only" in sync_if
	assert "!inputs.dry_run" in sync_if
	assert "needs.release.outputs.release_enabled == 'true'" in sync_if
	notify_job = _top_level_job_block(gate_text, "notify")
	assert "RELEASE_ENABLED: ${{ needs.release.outputs.release_enabled }}" in notify_job
	assert "publishing disabled (STABLE_RELEASE_ENABLED or RELEASE_TAGGING_ENABLED off)" in notify_job


def test_disabled_switch_assembles_changelog_without_committing() -> None:
	with tempfile.TemporaryDirectory() as temporary_directory:
		repository = Path(temporary_directory)
		git_environment = _clean_bash_environment(
			GIT_AUTHOR_NAME="t",
			GIT_AUTHOR_EMAIL="t@example.invalid",
			GIT_COMMITTER_NAME="t",
			GIT_COMMITTER_EMAIL="t@example.invalid",
		)
		for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
			git_environment.pop(key, None)
		(repository / "scripts").mkdir()
		(repository / "scripts" / "assemble_changelog.py").write_text(
			"from pathlib import Path\nPath('CHANGELOG.md').write_text('assembled\\n')\n", encoding="utf-8"
		)
		(repository / "CHANGELOG.md").write_text("original\n", encoding="utf-8")

		def git(*arguments: str) -> str:
			return subprocess.run(
				["git", *arguments], cwd=repository, check=True, env=git_environment, capture_output=True, text=True
			).stdout.strip()

		git("init", "-q")
		git("add", "-A")
		git("commit", "-qm", "base")
		base_head = git("rev-parse", "HEAD")
		for workflow_path in WORKFLOWS:
			assemble_script = _workflow_step_script(_read(workflow_path), "Assemble changelog fragments")
			result = subprocess.run(
				["bash", "-c", assemble_script],
				capture_output=True,
				check=False,
				cwd=repository,
				env={
					**git_environment,
					"DRY_RUN": "true",
					"SOURCE_BRANCH": "stable",
					"VERSION": "v1.2.3",
					"RELEASE_TESTED_SHA": base_head,
				},
				text=True,
			)
			assert result.returncode == 0, result.stderr
			assert "Dry run" in result.stdout
			assert git("rev-parse", "HEAD") == base_head, (
				f"{workflow_path.name}: no commit may be made while publishing is off"
			)


def test_ci_lint_job_keeps_its_check_run_name() -> None:
	lint_job = _top_level_job_block(_read(CI_WORKFLOW), "lint")
	assert not re.search(r"^    name:", lint_job, re.MULTILINE), (
		"ci.yml's aggregate job must stay unnamed so its check-run is `lint`, which the release gate requires"
	)


def test_full_clone_release_jobs_have_room_for_the_checkout() -> None:
	# A 2-minute resolve-version timeout cancelled a release mid-clone after
	# stable had already moved (test-and-mark-stable run 38717659089).
	import yaml

	for name in ("mark-stable.yml", "test-and-mark-stable.yml"):
		jobs = yaml.safe_load((REPO_ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))["jobs"]
		for job_name, job in jobs.items():
			full_clone = any(
				isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/checkout")
				and str((step.get("with") or {}).get("fetch-depth")) == "0"
				for step in job.get("steps") or []
			)
			if full_clone:
				assert job.get("timeout-minutes", 360) >= 10, f"{name} jobs.{job_name}: a full clone needs timeout-minutes >= 10"


PREFLIGHT_SCRIPT = REPO_ROOT / "scripts" / "release_tag_preflight.sh"
PREFLIGHT_STEP_NAME = "Preflight release tag credential"
PREFLIGHT_SENTINEL_TOKEN = "ghp_preflightSentinelTokenValue0123456789"


def _run_release_tag_preflight_scenario(
	*,
	scopes: str | None,
	workflow_change: bool,
	gh_fails: bool = False,
	token: str = PREFLIGHT_SENTINEL_TOKEN,
	base_override: str | None = None,
	extra_env: dict[str, str] | None = None,
	rename_out: bool = False,
) -> subprocess.CompletedProcess[str]:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		repo = root / "repo"
		repo.mkdir()

		def git(*args: str) -> str:
			return subprocess.run(
				["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
			).stdout.strip()

		git("init", "-q")
		git("config", "user.name", "t")
		git("config", "user.email", "t@example.invalid")
		(repo / "README.md").write_text("base\n", encoding="utf-8")
		if rename_out:
			(repo / ".github" / "workflows").mkdir(parents=True)
			(repo / ".github" / "workflows" / "moved.yml").write_text("name: moved\non: push\n", encoding="utf-8")
		git("add", "-A")
		git("commit", "-q", "-m", "base")
		git("tag", "stable")
		base = git("rev-parse", "HEAD")
		if rename_out:
			(repo / "scripts").mkdir()
			git("mv", ".github/workflows/moved.yml", "scripts/moved.yml")
		elif workflow_change:
			(repo / ".github" / "workflows").mkdir(parents=True)
			(repo / ".github" / "workflows" / "x.yml").write_text("name: x\n", encoding="utf-8")
		else:
			(repo / "scripts").mkdir()
			(repo / "scripts" / "x.sh").write_text("echo x\n", encoding="utf-8")
		git("add", "-A")
		git("commit", "-q", "-m", "head")
		head = git("rev-parse", "HEAD")

		bin_dir = root / "bin"
		bin_dir.mkdir()
		if gh_fails:
			gh_body = "echo 'HTTP 502: Bad Gateway' >&2\nexit 1\n"
		else:
			header = "" if scopes is None else f"X-Oauth-Scopes: {scopes}\\r\\n"
			gh_body = (
				"printf 'HTTP/2.0 200 OK\\r\\nContent-Type: application/json\\r\\n"
				+ header
				+ "\\r\\n{\"login\":\"x-oauth-scopes: repo, workflow\"}\\n'\n"
			)
		gh = bin_dir / "gh"
		gh.write_text("#!/usr/bin/env bash\n" + gh_body, encoding="utf-8")
		gh.chmod(0o755)

		env = _clean_bash_environment(
			PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
			GH_TOKEN=token,
			RUNNER_TEMP=str(root),
			RELEASE_TAG_PREFLIGHT_BASE_SHA=base if base_override is None else base_override,
			RELEASE_TAG_PREFLIGHT_HEAD_SHA=head,
			RELEASE_TAG_PREFLIGHT_GIT_DIR=str(repo),
		)
		for key in ("RELEASE_TAG_PREFLIGHT_ENABLED", "STABLE_RELEASE_ENABLED", "RELEASE_TAGGING_ENABLED"):
			env.pop(key, None)
		env.update(extra_env or {})
		return subprocess.run(
			["bash", str(PREFLIGHT_SCRIPT)], env=env, capture_output=True, text=True, check=False,
		)


def test_release_tag_preflight_script_is_executable_with_tabs() -> None:
	assert PREFLIGHT_SCRIPT.is_file()
	assert os.access(PREFLIGHT_SCRIPT, os.X_OK), "release_tag_preflight.sh must be mode 0755"
	text = _read(PREFLIGHT_SCRIPT)
	assert text.startswith("#!/usr/bin/env bash\n")
	assert "§14" in text, "the GET /user call must carry its §14 API-audit comment"
	assert not re.search(r"^ {2,}\S", text, re.MULTILINE), "indent the script with tabs"


def test_release_tag_preflight_executes_decision_matrix() -> None:
	cases = [
		(dict(scopes="repo, workflow", workflow_change=True), 0, "result=pass error_class=none reason=workflow_scope_present"),
		(dict(scopes="repo", workflow_change=False), 0, "result=pass error_class=none reason=no_workflow_changes"),
		(dict(scopes="repo", workflow_change=True), 1, "result=fail error_class=workflow_scope reason=classic_pat_missing_workflow_scope"),
		(dict(scopes="", workflow_change=True), 1, "result=fail error_class=workflow_scope"),
		(dict(scopes="repo", workflow_change=False, rename_out=True), 1, "result=fail error_class=workflow_scope reason=classic_pat_missing_workflow_scope"),
		(dict(scopes=None, workflow_change=True), 0, "result=unknown error_class=none reason=no_scope_header"),
		(dict(scopes="repo", workflow_change=True, gh_fails=True), 0, "result=unknown error_class=none reason=api_failed"),
		(dict(scopes="repo", workflow_change=True, token=""), 0, "result=unknown error_class=none reason=gh_pat_missing"),
		(dict(scopes="repo", workflow_change=True, base_override="not-a-sha"), 0, "result=unknown error_class=none reason=invalid_range"),
		(dict(scopes="repo", workflow_change=True, base_override="0" * 40), 0, "result=unknown error_class=none reason=git_failed"),
		(dict(scopes="repo", workflow_change=True, extra_env={"RELEASE_TAG_PREFLIGHT_ENABLED": "FALSE"}), 0, "result=skipped error_class=none reason=disabled"),
		(dict(scopes="repo", workflow_change=True, extra_env={"RELEASE_TAG_PREFLIGHT_ENABLED": "bogus-value"}), 0, "result=skipped error_class=none reason=invalid_switch"),
		(dict(scopes="repo", workflow_change=True, extra_env={"RELEASE_TAG_PREFLIGHT_ENABLED": "True"}), 1, "result=fail error_class=workflow_scope"),
		(dict(scopes="repo", workflow_change=True, extra_env={"RELEASE_TAGGING_ENABLED": "false"}), 0, "result=skipped error_class=none reason=publishing_disabled"),
		(dict(scopes="repo", workflow_change=True, extra_env={"STABLE_RELEASE_ENABLED": "false"}), 0, "result=skipped error_class=none reason=publishing_disabled"),
	]
	for kwargs, expected_rc, expected_line in cases:
		result = _run_release_tag_preflight_scenario(**kwargs)
		combined = result.stdout + result.stderr
		assert result.returncode == expected_rc, (kwargs, result.returncode, combined)
		assert f"RELEASE_TAG_PREFLIGHT {expected_line}" in result.stdout, (kwargs, combined)
		assert PREFLIGHT_SENTINEL_TOKEN not in combined, f"token leaked: {kwargs}"
		assert "Content-Type" not in combined and "HTTP/2.0" not in combined, f"headers leaked: {kwargs}"
		if expected_rc == 1:
			assert "::error::" in result.stdout and "workflow" in result.stdout, combined
		if "bogus-value" in str(kwargs):
			assert "::warning::" in combined and "bogus-value" not in combined, combined


def _preflight_step_text(workflow_text: str) -> str:
	job = _top_level_job_block(workflow_text, "resolve-version")
	match = re.search(
		rf"^      - name: {re.escape(PREFLIGHT_STEP_NAME)}\n(.*?)(?=^      - |^      #|^[ \t]*$|\Z)",
		job,
		re.MULTILINE | re.DOTALL,
	)
	assert match is not None, "resolve-version must carry the release tag preflight step"
	return match.group(0)


def test_release_tag_preflight_wired_into_resolve_version_before_release() -> None:
	run_bodies = []
	for wf in WORKFLOWS:
		text = _read(wf)
		job = _top_level_job_block(text, "resolve-version")
		assert job.index("      - name: Resolve version tag\n") < job.index(f"      - name: {PREFLIGHT_STEP_NAME}\n"), wf.name
		step = _preflight_step_text(text)
		assert "        id: release_tag_preflight\n" in step
		assert "GH_TOKEN: ${{ secrets.GH_PAT }}\n" in step, f"{wf.name}: pass GH_PAT only, no github.token fallback"
		assert "RELEASE_TAG_PREFLIGHT_ENABLED: ${{ vars.RELEASE_TAG_PREFLIGHT_ENABLED || 'true' }}" in step
		assert "continue-on-error" not in step, f"{wf.name}: a preflight failure must fail the job"
		if_line = re.search(r"^        if: (.*)$", step, re.MULTILINE)
		assert if_line is not None and "!inputs.dry_run" in if_line.group(1), wf.name
		if wf.name == "test-and-mark-stable.yml":
			assert "!inputs.gate_only" in if_line.group(1)
		run_body = step[step.index("        run: |\n"):]
		assert "${{" not in run_body, f"{wf.name}: pass expression values through env:"
		assert "bash scripts/release_tag_preflight.sh" in run_body
		run_bodies.append(run_body)
		release_job = _top_level_job_block(text, "release")
		needs = re.search(r"^    needs: \[(.*)\]$", release_job, re.MULTILINE)
		assert needs is not None and "resolve-version" in [n.strip() for n in needs.group(1).split(",")], wf.name
	assert run_bodies[0] == run_bodies[1], "both workflows must run the identical preflight body"


def test_workflows_stay_under_size_guard_after_preflight() -> None:
	for wf in WORKFLOWS:
		assert wf.stat().st_size < 480_000, f"{wf.name} is at or above the 480,000-byte guard"


def main() -> None:
	# Failures surface via uncaught AssertionError → Python's default non-zero
	# exit; on full success we just print and return None.
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")


if __name__ == "__main__":
	main()
