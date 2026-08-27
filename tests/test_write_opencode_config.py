#!/usr/bin/env python3
"""Contract tests for the role-scoped OpenCode configuration writer."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WRITER = REPO_ROOT / "scripts" / "write_opencode_config.sh"
MODEL_SLUG = "vendor/model-test"


def _fixture_files(root: Path) -> tuple[Path, Path]:
	catalog = root / "catalog.json"
	models = root / "models.json"
	catalog.write_text(
		json.dumps({"models": [{"slug": MODEL_SLUG, "context_window": 123456}]}),
		encoding="utf-8",
	)
	models.write_text(
		json.dumps(
			{
				"openrouter": {
					"models": {MODEL_SLUG: {"limit": {"context": 999999, "output": 65432}}}
				}
			}
		),
		encoding="utf-8",
	)
	return catalog, models


def _run(
	root: Path,
	role: str = "reviewer",
	serena: str = "off",
	model: str = MODEL_SLUG,
	extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
	catalog, models = _fixture_files(root)
	config = root / "config.json"
	env = os.environ.copy()
	env.update(
		{
			"OPENCODE_MODEL_CATALOG_PATH": str(catalog),
			"OPENCODE_MODELS_PATH": str(models),
		}
	)
	if extra_env:
		env.update(extra_env)
	result = subprocess.run(
		[
			"bash",
			str(WRITER),
			"--role",
			role,
			"--model",
			model,
			"--project-path",
			str(root),
			"--config-path",
			str(config),
			"--serena",
			serena,
		],
		env=env,
		text=True,
		capture_output=True,
		check=False,
	)
	return result, config


def test_reviewer_configuration_is_read_only_and_model_pinned() -> None:
	with tempfile.TemporaryDirectory() as directory:
		result, config_path = _run(Path(directory))
		assert result.returncode == 0, result.stderr
		config = json.loads(config_path.read_text(encoding="utf-8"))
		qualified = f"openrouter/{MODEL_SLUG}"
		assert config["model"] == qualified
		assert config["small_model"] == qualified
		assert config["default_agent"] == "reviewer"
		assert config["autoupdate"] is False
		assert config["share"] == "disabled"
		assert config["provider"]["openrouter"]["npm"] == "@openrouter/ai-sdk-provider"
		assert config["provider"]["openrouter"]["options"] == {
			"apiKey": "{env:OPENROUTER_API_KEY}",
			"baseURL": "https://openrouter.ai/api/v1",
		}
		limits = config["provider"]["openrouter"]["models"][MODEL_SLUG]["limit"]
		assert limits == {"context": 123456, "output": 65432}
		variants = config["provider"]["openrouter"]["models"][MODEL_SLUG]["variants"]
		assert variants == {
			effort: {"reasoning": {"effort": effort}}
			for effort in ("none", "low", "medium", "high", "xhigh")
		}
		permission = config["agent"]["reviewer"]["permission"]
		assert permission["edit"] == "deny"
		assert permission["bash"] == "allow"
		assert permission["read"] == "allow"
		assert permission["task"] == "deny"
		assert permission["webfetch"] == "deny"
		tools = config["agent"]["reviewer"]["tools"]
		assert tools["bash"] is True
		assert tools["edit"] is False
		assert tools["apply_patch"] is False
		assert tools["task"] is False
		assert "mcp" not in config


def test_writer_configuration_allows_the_full_tool_surface() -> None:
	with tempfile.TemporaryDirectory() as directory:
		result, config_path = _run(Path(directory), role="writer")
		assert result.returncode == 0, result.stderr
		config = json.loads(config_path.read_text(encoding="utf-8"))
		assert config["permission"] == "allow"
		assert config["agent"]["writer"]["permission"] == "allow"
		assert all(config["tools"].values())
		assert all(config["agent"]["writer"]["tools"].values())


def test_serena_configuration_uses_the_resolved_local_binary() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		serena = bin_dir / "serena"
		serena.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
		serena.chmod(0o755)
		result, config_path = _run(
			root,
			serena="on",
			extra_env={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
		)
		assert result.returncode == 0, result.stderr
		server = json.loads(config_path.read_text(encoding="utf-8"))["mcp"]["serena"]
		assert server["type"] == "local"
		assert server["cwd"] == str(root)
		assert server["command"] == [
			str(serena),
			"start-mcp-server",
			"--context=codex",
			"--project-from-cwd",
			"--transport",
			"stdio",
		]


def test_serena_configuration_rejects_a_non_executable_binary() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		serena = bin_dir / "serena"
		serena.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
		serena.chmod(0o644)
		result, _ = _run(
			root,
			serena="on",
			extra_env={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
		)
		assert result.returncode == 2
		assert "requires an executable serena binary" in result.stderr


def test_invalid_inputs_and_metadata_fail_with_annotations() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		result, _ = _run(root, role="reader")
		assert result.returncode == 2
		assert "::error::" in result.stderr and "invalid --role" in result.stderr

		result, _ = _run(root, model="missing/model")
		assert result.returncode != 0
		assert "missing or duplicated" in result.stderr

		catalog, models = _fixture_files(root)
		models.write_text("not json", encoding="utf-8")
		config = root / "malformed.json"
		result = subprocess.run(
			[
				"bash",
				str(WRITER),
				"--role",
				"reviewer",
				"--model",
				MODEL_SLUG,
				"--project-path",
				str(root),
				"--config-path",
				str(config),
				"--serena",
				"off",
			],
			env={
				**os.environ,
				"OPENCODE_MODEL_CATALOG_PATH": str(catalog),
				"OPENCODE_MODELS_PATH": str(models),
			},
			text=True,
			capture_output=True,
			check=False,
		)
		assert result.returncode != 0
		assert "invalid models.dev cache" in result.stderr


def test_failed_render_preserves_an_existing_configuration() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		catalog, models = _fixture_files(root)
		models.write_text(json.dumps({"openrouter": {"models": {}}}), encoding="utf-8")
		config = root / "config.json"
		original = '{"preserve": true}\n'
		config.write_text(original, encoding="utf-8")
		result = subprocess.run(
			[
				"bash",
				str(WRITER),
				"--role",
				"reviewer",
				"--model",
				MODEL_SLUG,
				"--project-path",
				str(root),
				"--config-path",
				str(config),
				"--serena",
				"off",
			],
			env={
				**os.environ,
				"OPENCODE_MODEL_CATALOG_PATH": str(catalog),
				"OPENCODE_MODELS_PATH": str(models),
			},
			text=True,
			capture_output=True,
			check=False,
		)
		assert result.returncode != 0
		assert config.read_text(encoding="utf-8") == original


def main() -> int:
	tests = [value for key, value in sorted(globals().items()) if key.startswith("test_") and callable(value)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
