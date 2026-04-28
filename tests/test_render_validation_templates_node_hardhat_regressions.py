#!/usr/bin/env python3
"""Regression tests for node-hardhat-solidity validation harness templates."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "render_validation_templates.py"
SCHEMA_PATH = REPO_ROOT / "scripts" / "templates" / "slot_manifest.schema.json"
TEMPLATES_ROOT = REPO_ROOT / "workflow-templates" / "validation-harness"


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _manifest_payload() -> dict:
    return {
        "type": "node-hardhat-solidity",
        "entry": "hardhat.config.ts",
        "port": 8545,
        "slots": {
            "project_name": "demo-project",
            "canary_tools": ["curl", "jq", "node", "npx", "forge", "cast"],
            "tap_plan": 3,
        },
    }


def _run_renderer(manifest_path: Path, output_root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            "python3",
            str(SCRIPT_PATH),
            "--manifest",
            str(manifest_path),
            "--schema",
            str(SCHEMA_PATH),
            "--templates-root",
            str(TEMPLATES_ROOT),
            "--output-root",
            str(output_root),
        ],
        text=True,
        capture_output=True,
        env=env,
    )


def test_log7_foundry_path_persists_in_non_login_shells() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        _write_yaml(manifest_path, _manifest_payload())

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        dockerfile_text = (output_root / "Dockerfile.app").read_text(encoding="utf-8")
        assert "ENV PATH=/root/.foundry/bin:${PATH}" in dockerfile_text


def test_node_compose_pins_foundry_image_version() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        _write_yaml(manifest_path, _manifest_payload())

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
        assert "image: ghcr.io/foundry-rs/foundry:v1.3.1" in compose_text
        assert "ghcr.io/foundry-rs/foundry:latest" not in compose_text


def test_log8_rpc_probe_requires_object_result() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        _write_yaml(manifest_path, _manifest_payload())

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        rpc_probe = (output_root / "tests" / "25_rpc_probe.sh").read_text(encoding="utf-8")
        assert 'type == "object"' in rpc_probe
        assert 'has("result") and (.result != null) and (.result | type == "string") and (.result | length > 0)' in rpc_probe
        assert "jq -e '.'" not in rpc_probe


def test_import_audit_runner_and_helper_rendered_with_safe_subprocess_probe() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        _write_yaml(manifest_path, _manifest_payload())

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        runner_text = (output_root / "tests" / "20_import_audit.sh").read_text(encoding="utf-8")
        helper_text = (output_root / "tests" / "_lib" / "import_audit.py").read_text(encoding="utf-8")

        assert "python3" in runner_text
        assert "_lib/import_audit.py" in runner_text
        assert "subprocess.run" in helper_text
        assert "sys.executable" in helper_text
        assert "importlib.import_module" in helper_text


def test_log9_validate_env_values_are_double_quoted() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        _write_yaml(manifest_path, _manifest_payload())

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        env_text = (output_root / "validate.env").read_text(encoding="utf-8")
        for line in env_text.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            assert '="' in line and line.endswith('"'), f"unquoted validate.env line: {line}"

        assert 'ANVIL_PORT="8545"' in env_text
        assert 'CANARY_TOOLS="curl jq node npx forge cast"' in env_text

        custom_manifest = _manifest_payload()
        custom_manifest["port"] = 9555
        custom_manifest["slots"]["canary_tools"] = ["curl", "jq", "node"]
        _write_yaml(manifest_path, custom_manifest)

        custom_result = _run_renderer(manifest_path, output_root)
        assert custom_result.returncode == 0, custom_result.stderr
        custom_env_text = (output_root / "validate.env").read_text(encoding="utf-8")
        assert 'ANVIL_PORT="9555"' in custom_env_text
        assert 'CANARY_TOOLS="curl jq node"' in custom_env_text
        assert 'RPC_URL=""' in env_text
        assert 'RPC_URL=""' in custom_env_text


def test_shutdown_helper_is_rendered_and_invoked() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        _write_yaml(manifest_path, _manifest_payload())

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        helper_file = output_root / "_lib" / "graceful_shutdown.sh"
        test_file = output_root / "tests" / "30_hardhat_test.sh"
        assert helper_file.exists(), "missing graceful shutdown helper"
        assert test_file.exists(), "missing hardhat test runner"

        helper_text = helper_file.read_text(encoding="utf-8")
        assert "graceful_shutdown()" in helper_text
        assert "kill -TERM" in helper_text

        test_text = test_file.read_text(encoding="utf-8")
        assert '. "${ROOT_DIR}/_lib/graceful_shutdown.sh"' in test_text
        assert "graceful_shutdown" in test_text
        assert "npx hardhat test --network localhost" in test_text


def test_compose_dockerfile_path_tracks_output_root_name() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "custom-output"
        _write_yaml(manifest_path, _manifest_payload())

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
        assert "dockerfile: custom-output/Dockerfile.app" in compose_text


def test_env_overrides_propagate_into_validate_env_and_compose() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        payload = _manifest_payload()
        payload["env_overrides"] = {
            "CI": "true",
            "HARDHAT_NETWORK": "hardhat",
            "HARDHAT_TEST_CMD": "npx hardhat test",
            "RPC_URL_ETHEREUM": "https://example.invalid/eth",
        }
        _write_yaml(manifest_path, payload)

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        env_text = (output_root / "validate.env").read_text(encoding="utf-8")
        assert 'CI="true"' in env_text, env_text
        assert 'HARDHAT_NETWORK="hardhat"' in env_text, env_text
        assert 'HARDHAT_TEST_CMD="npx hardhat test"' in env_text, env_text
        assert 'RPC_URL_ETHEREUM="https://example.invalid/eth"' in env_text, env_text
        for line in env_text.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            assert '="' in line and line.endswith('"'), f"unquoted validate.env line: {line}"

        compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
        # Manifest overrides must beat the compose host-shell fallbacks. When the
        # manifest sets an override, the compose `environment:` block must carry
        # the literal override value (not a `${VAR:-default}` interpolation that
        # depends on the harness host shell).
        assert 'HARDHAT_NETWORK: "hardhat"' in compose_text, compose_text
        assert 'HARDHAT_TEST_CMD: "npx hardhat test"' in compose_text, compose_text
        assert 'CI: "true"' in compose_text, compose_text
        assert 'RPC_URL_ETHEREUM: "https://example.invalid/eth"' in compose_text, compose_text
        # The manifest override must replace the host-shell fallback for the
        # exact key it covers; any remaining `${HARDHAT_NETWORK:-...}` would
        # silently revert the override at compose interpolation time.
        assert "${HARDHAT_NETWORK:-localhost}" not in compose_text, compose_text
        assert "${HARDHAT_TEST_CMD:-" not in compose_text, compose_text


def test_env_overrides_default_compose_falls_back_to_host_shell_when_unset() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        _write_yaml(manifest_path, _manifest_payload())

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
        # No env_overrides → preserve previous default behavior so existing
        # consumer manifests render byte-compatibly.
        assert 'HARDHAT_NETWORK: "${HARDHAT_NETWORK:-localhost}"' in compose_text, compose_text
        assert 'HARDHAT_TEST_CMD: "${HARDHAT_TEST_CMD:-npx hardhat test --network localhost}"' in compose_text, compose_text


def test_rpc_probe_is_skipped_for_in_process_hardhat_network() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        payload = _manifest_payload()
        payload["env_overrides"] = {
            "HARDHAT_NETWORK": "hardhat",
        }
        _write_yaml(manifest_path, payload)

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        rpc_probe = (output_root / "tests" / "25_rpc_probe.sh").read_text(encoding="utf-8")
        assert "# SKIP manifest selects in-process Hardhat network" in rpc_probe, rpc_probe
        # The skip path must not perform any Anvil RPC traffic.
        assert "docker compose" not in rpc_probe, rpc_probe
        assert "http://anvil" not in rpc_probe, rpc_probe


def test_rpc_probe_runs_when_manifest_does_not_select_in_process_hardhat() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        _write_yaml(manifest_path, _manifest_payload())

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        rpc_probe = (output_root / "tests" / "25_rpc_probe.sh").read_text(encoding="utf-8")
        assert "# SKIP manifest selects in-process Hardhat network" not in rpc_probe
        assert "docker compose" in rpc_probe
        # Failure diagnostics must surface curl exit/stderr so a regression of
        # the "empty response" mode is actionable rather than opaque.
        assert "##CURL_EXIT=" in rpc_probe
        assert "curl exit" in rpc_probe


def test_hardhat_test_runner_does_not_clobber_container_env_with_host_shell_value() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        _write_yaml(manifest_path, _manifest_payload())

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        runner_text = (output_root / "tests" / "30_hardhat_test.sh").read_text(encoding="utf-8")
        # `docker compose exec -T -e HARDHAT_TEST_CMD=...` would re-export the
        # harness host shell value into the container and silently override the
        # manifest-driven container env. The runner must rely on the container
        # env (set via env_file + compose environment) instead.
        assert "-e HARDHAT_TEST_CMD" not in runner_text, runner_text
        # Runner must source validate.env so failure diagnostics report the
        # actual command that ran inside the container.
        assert '"${ROOT_DIR}/validate.env"' in runner_text, runner_text
        # `eval` was replaced with `sh -c "${cmd}"` to avoid double-expansion
        # surprises; assert the eval form does not regress.
        assert "eval " not in runner_text, runner_text


def test_env_overrides_escape_shell_metacharacters_in_validate_env() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        payload = _manifest_payload()
        # validate.env is sourced by 30_hardhat_test.sh, so values containing
        # shell metacharacters must be escaped to prevent host-side command
        # substitution / variable expansion.
        payload["env_overrides"] = {
            "WITH_DOLLAR": "value with $HOME and ${PATH}",
            "WITH_BACKTICK": "value with `whoami` substitution",
            "WITH_QUOTE": 'value with "quoted" segment',
            "WITH_BACKSLASH": "value with \\backslash",
        }
        _write_yaml(manifest_path, payload)

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        env_text = (output_root / "validate.env").read_text(encoding="utf-8")
        # `$` is escaped as `\$` so bash double-quoted sourcing keeps the literal.
        assert r'WITH_DOLLAR="value with \$HOME and \${PATH}"' in env_text, env_text
        # Backticks are escaped as `\`` to avoid command substitution on source.
        assert r'WITH_BACKTICK="value with \`whoami\` substitution"' in env_text, env_text
        # Embedded double-quotes are escaped.
        assert r'WITH_QUOTE="value with \"quoted\" segment"' in env_text, env_text
        # Backslashes are doubled so a single literal `\` round-trips.
        assert r'WITH_BACKSLASH="value with \\backslash"' in env_text, env_text


def test_env_overrides_escape_dollar_for_compose_interpolation() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        payload = _manifest_payload()
        # Docker Compose performs `${VAR}` interpolation on values in the
        # `environment:` block; `$` must be escaped as `$$` so a manifest
        # override that itself contains a `$` is preserved literally inside the
        # container instead of being substituted from the harness host env.
        payload["env_overrides"] = {
            "HARDHAT_TEST_CMD": "npx hardhat test $@",
            "HARDHAT_NETWORK": "fork-of-${UPSTREAM_NETWORK}",
            "EXTRA_VAR": "literal-$value",
        }
        _write_yaml(manifest_path, payload)

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
        assert 'HARDHAT_TEST_CMD: "npx hardhat test $$@"' in compose_text, compose_text
        assert 'HARDHAT_NETWORK: "fork-of-$${UPSTREAM_NETWORK}"' in compose_text, compose_text
        assert 'EXTRA_VAR: "literal-$$value"' in compose_text, compose_text

        # Compose YAML must still round-trip through PyYAML cleanly.
        parsed = yaml.safe_load(compose_text)
        assert parsed["services"]["app"]["environment"]["EXTRA_VAR"] == "literal-$$value"


def test_app_service_and_rpc_url_env_overrides_take_effect_in_compose() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        payload = _manifest_payload()
        # Compose's `environment:` block beats `env_file`, so APP_SERVICE and
        # RPC_URL overrides in validate.env alone would be silently ignored.
        # The compose template must bake manifest overrides for these keys too.
        payload["env_overrides"] = {
            "APP_SERVICE": "custom-app",
            "RPC_URL": "https://example.invalid/rpc",
        }
        _write_yaml(manifest_path, payload)

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        compose_text = (output_root / "docker-compose.test.yml").read_text(encoding="utf-8")
        assert 'APP_SERVICE: "custom-app"' in compose_text, compose_text
        assert 'RPC_URL: "https://example.invalid/rpc"' in compose_text, compose_text
        # Default fallbacks must be replaced when the manifest overrides them.
        assert "${APP_SERVICE:-app}" not in compose_text, compose_text
        assert "${RPC_URL:-http://anvil:" not in compose_text, compose_text


def test_rpc_probe_inner_shell_captures_curl_exit_without_set_e() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        _write_yaml(manifest_path, _manifest_payload())

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        rpc_probe = (output_root / "tests" / "25_rpc_probe.sh").read_text(encoding="utf-8")
        # Locate the inner `/bin/sh -c '...'` block and check it specifically;
        # the outer harness script keeps `set -euo pipefail` at the top.
        inner_marker = "/bin/sh -c '"
        inner_start = rpc_probe.find(inner_marker)
        assert inner_start != -1, rpc_probe
        inner_block_end = rpc_probe.find("' >", inner_start)
        assert inner_block_end != -1, rpc_probe
        inner_block = rpc_probe[inner_start:inner_block_end]

        # The inner shell must NOT use `set -e` — that would abort before
        # `printf "##CURL_EXIT=..."` runs on curl failures, so the diagnostic
        # marker would never be written.
        assert "set -eu" not in inner_block, inner_block
        assert "set +e" in inner_block, inner_block
        # Inner shell must capture the curl exit code into a variable, emit it,
        # then exit 0 so docker compose exec succeeds and the host script can
        # parse the marker for diagnostics.
        assert "curl_rc=$?" in inner_block, inner_block
        assert '##CURL_EXIT=%s' in inner_block, inner_block
        assert "exit 0" in inner_block, inner_block


def test_rpc_probe_stderr_diagnostics_prefix_every_line() -> None:
    with tempfile.TemporaryDirectory(prefix="render-validation-node-hardhat-") as td:
        temp_root = Path(td)
        manifest_path = temp_root / "validate.yml"
        output_root = temp_root / "out"
        _write_yaml(manifest_path, _manifest_payload())

        result = _run_renderer(manifest_path, output_root)
        assert result.returncode == 0, result.stderr

        rpc_probe = (output_root / "tests" / "25_rpc_probe.sh").read_text(encoding="utf-8")
        # A multi-line stderr must yield TAP-comment lines for every line, not
        # just the first; loop over lines so consumers see consistent `# ` prefixes.
        assert "while IFS= read -r line" in rpc_probe, rpc_probe
        assert "printf '# stderr: %s\\n' \"${line}\"" in rpc_probe, rpc_probe


def main() -> int:
    tests = [func for name, func in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for func in tests:
        name = func.__name__
        try:
            func()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # pragma: no cover
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
