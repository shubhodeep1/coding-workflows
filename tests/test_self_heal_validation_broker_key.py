#!/usr/bin/env python3
"""scripts/self_heal_validation.sh reuses the caller's model-provider broker.

validate_process.sh starts the broker and then unsets OPENROUTER_API_KEY, so
the self-heal it launches never sees the key. The script used to require the
key unconditionally and stopped before doing anything ("OPENROUTER_API_KEY is
required", project #3965 validation run 36288327878). The key is now required
only when no broker is running and the script would have to start its own.

Both cases stop at the attempt budget check, which runs before any broker or
Codex launch, so the tests need no network and no credentials.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SELF_HEAL_PATH = REPO_ROOT / "scripts" / "self_heal_validation.sh"
MISSING_KEY_MESSAGE = "OPENROUTER_API_KEY is required when no model provider broker is running"
BUDGET_MESSAGE = "self-heal: budget exhausted"


def _run(tmp_path: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	runtime_dir = tmp_path / "runtime"
	runtime_dir.mkdir()
	env = {
		key: value
		for key, value in os.environ.items()
		if key not in {"OPENROUTER_API_KEY", "MODEL_PROVIDER_BROKER_TOKEN", "MODEL_PROVIDER_BROKER_BASE_URL"}
	}
	env.update(
		{
			"RUNTIME_DIR": str(runtime_dir),
			"SELF_HEAL_ATTEMPT": "2",
			"MAX_SELF_HEAL_ATTEMPTS": "2",
			"SELF_HEAL_PATCHES_FILE": str(runtime_dir / "self_heal_patches.jsonl"),
			"SELF_HEAL_PROMPT_OVERRIDE_DIR": str(runtime_dir / "prompt-overrides"),
			"SUPPORT_PROMPTS_DIR": str(REPO_ROOT / "prompts"),
			"MODEL_EDITOR": "openai/test-model",
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	env.update(extra_env)
	return subprocess.run(
		["bash", str(SELF_HEAL_PATH)],
		cwd=tmp_path,
		env=env,
		text=True,
		capture_output=True,
		timeout=60,
	)


def test_self_heal_reuses_running_broker_without_provider_key(tmp_path: Path) -> None:
	result = _run(
		tmp_path,
		{
			"MODEL_PROVIDER_BROKER_TOKEN": "broker-token",
			"MODEL_PROVIDER_BROKER_BASE_URL": "http://127.0.0.1:9/api/v1",
		},
	)
	assert MISSING_KEY_MESSAGE not in result.stderr, result.stderr
	assert BUDGET_MESSAGE in result.stderr, result.stderr
	assert result.returncode == 2


def test_self_heal_requires_provider_key_without_broker(tmp_path: Path) -> None:
	result = _run(tmp_path, {})
	assert MISSING_KEY_MESSAGE in result.stderr, result.stderr
	assert BUDGET_MESSAGE not in result.stderr, result.stderr
	assert result.returncode != 0


def test_self_heal_requires_provider_key_with_partial_broker_env(tmp_path: Path) -> None:
	result = _run(tmp_path, {"MODEL_PROVIDER_BROKER_TOKEN": "broker-token"})
	assert MISSING_KEY_MESSAGE in result.stderr, result.stderr
	assert result.returncode != 0


def test_self_heal_accepts_provider_key_without_broker(tmp_path: Path) -> None:
	result = _run(tmp_path, {"OPENROUTER_API_KEY": "test-key"})
	assert MISSING_KEY_MESSAGE not in result.stderr, result.stderr
	assert BUDGET_MESSAGE in result.stderr, result.stderr
	assert result.returncode == 2


def main() -> int:
	import inspect
	import tempfile

	for name, func in sorted(globals().items()):
		if name.startswith("test_") and callable(func):
			if "tmp_path" in inspect.signature(func).parameters:
				with tempfile.TemporaryDirectory() as fixture_tmp_dir:
					func(tmp_path=Path(fixture_tmp_dir))
			else:
				func()
	print("OK: self_heal_validation.sh broker key requirement")
	return 0


if __name__ == "__main__":
	sys.exit(main())
