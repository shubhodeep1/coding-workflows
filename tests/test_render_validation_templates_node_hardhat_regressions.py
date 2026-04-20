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

        rpc_probe = (output_root / "tests" / "20_rpc_probe.sh").read_text(encoding="utf-8")
        assert 'type == "object"' in rpc_probe
        assert 'has("result") and (.result != null) and (.result | type == "string") and (.result | length > 0)' in rpc_probe
        assert "jq -e '.'" not in rpc_probe


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
