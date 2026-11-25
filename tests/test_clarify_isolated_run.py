"""Snapshot tests for scripts/clarify_isolated_run.sh (CLARIFY_SNAPSHOT_ALL_TEXT_ENABLED, issue #8341)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_PATH = REPO_ROOT / "scripts" / "clarify_isolated_run.sh"
KILL_SWITCH_LINE = "CLARIFY_SNAPSHOT_ALL_TEXT_ENABLED: ${{ vars.CLARIFY_SNAPSHOT_ALL_TEXT_ENABLED || 'true' }}"
CALLER_WORKFLOWS = ("clarify.yml", "plan.yml", "orchestrate_clarify_respond.yml", "check_failure_triage.yml", "unblock_judge.yml")
SUPPORT_FILES = {"scripts/write_codex_config.sh", "scripts/codex_model_catalog.json"}

# The mock docker copies the /source snapshot and the mounted /prompt, then
# fails the run so the helper stops before any model call.
MOCK_DOCKER = '''#!/usr/bin/env bash
case "$1" in
  build) printf 'test-image\\n' ;;
  run)
    for arg in "$@"; do
      case "$arg" in
        type=bind,src=*,dst=/prompt,readonly)
          prompt_path="${arg#type=bind,src=}"
          cp "${prompt_path%,dst=/prompt,readonly}" "$MOCK_PROMPT_CAPTURE" ;;
      esac
    done
    for arg in "$@"; do
      case "$arg" in
        type=bind,src=*,dst=/source,readonly)
          source_path="${arg#type=bind,src=}"
          cp -a "${source_path%,dst=/source,readonly}/." "$MOCK_SNAPSHOT_CAPTURE/"
          exit 1 ;;
      esac
    done
    exit 2 ;;
  rm) exit 0 ;;
esac
'''


def _stage_support(destination: Path) -> None:
	(destination / "scripts" / "clarify_sandbox").mkdir(parents=True, exist_ok=True)
	for filename in ("clarify_openrouter_broker.py", "write_codex_config.sh", "codex_model_catalog.json", "clarify_sandbox/Dockerfile"):
		(destination / "scripts" / filename).write_bytes((REPO_ROOT / "scripts" / filename).read_bytes())


class ClarifySnapshotAllTextTest(unittest.TestCase):
	def setUp(self) -> None:
		self._temp = tempfile.TemporaryDirectory(prefix="clarify-all-text-")
		self.root = Path(self._temp.name)
		self.trusted = self.root / "trusted"
		_stage_support(self.trusted)
		bin_dir = self.root / "bin"
		bin_dir.mkdir()
		(bin_dir / "docker").write_text(MOCK_DOCKER, encoding="utf-8")
		(bin_dir / "docker").chmod(0o755)
		self.prompt = self.root / "prompt"
		self.prompt.write_bytes(b"diagnose this\n")
		self.env = os.environ.copy()
		for name in ("BASH_ENV", "ENV", "CLARIFY_SNAPSHOT_OMIT_AGENT_INSTRUCTIONS", "CLARIFY_SNAPSHOT_ALL_TEXT_ENABLED", "CLARIFY_ISOLATION_SUPPORT_DIR"):
			self.env.pop(name, None)
		self.env.update({
			"MODEL_EDITOR": "openai/gpt-6-sol", "MODEL_REASONING_EFFORT": "high",
			"OPENROUTER_API_KEY": "test-key", "SANDBOX_IMAGE_REGISTRY": "off",
			"PYTHONDONTWRITEBYTECODE": "1",
			"PATH": f"{bin_dir}{os.pathsep}{self.env.get('PATH', '')}",
		})
		# The helper binds its broker socket under `mktemp -d`; a long inherited
		# TMPDIR overflows the ~108-byte Unix socket path limit, so the broker
		# fails before the mocked `docker run` captures anything. Use a short one.
		self._short_temp = tempfile.TemporaryDirectory(prefix="cir-", dir="/tmp" if os.access("/tmp", os.W_OK) else None)
		self.env["TMPDIR"] = self._short_temp.name
		self.runs = 0

	def tearDown(self) -> None:
		self._short_temp.cleanup()
		self._temp.cleanup()

	def _checkout(self, files: dict) -> Path:
		self.runs += 1
		checkout = self.root / f"checkout-{self.runs}"
		checkout.mkdir()
		for name, content in files.items():
			raw_path = os.fsencode(checkout) + b"/" + (name if isinstance(name, bytes) else name.encode())
			os.makedirs(os.path.dirname(raw_path), exist_ok=True)
			with open(raw_path, "wb") as fixture_file:
				fixture_file.write(content)
		subprocess.run(["git", "init", "-q", str(checkout)], check=True)
		subprocess.run(["git", "add", "-A", "-f", "."], cwd=checkout, check=True)
		# Untracked, like a consumer checkout: the helper copies the writer and catalog in.
		_stage_support(checkout)
		return checkout

	def _run(self, checkout: Path, **overrides: str | None):
		capture = self.root / f"snapshot-{self.runs}-{len(overrides)}-{os.urandom(4).hex()}"
		capture.mkdir()
		prompt_capture = capture.with_suffix(".prompt")
		env = dict(self.env)
		env.update({"CLARIFY_SOURCE_ROOT": str(checkout), "MOCK_SNAPSHOT_CAPTURE": str(capture), "MOCK_PROMPT_CAPTURE": str(prompt_capture)})
		for key, value in overrides.items():
			if value is None:
				env.pop(key, None)
			else:
				env[key] = value
		proc = subprocess.run(
			["bash", str(HELPER_PATH), str(self.prompt), str(self.root / "out"), str(self.root / "log")],
			cwd=self.trusted, env=env, capture_output=True, text=True, timeout=120,
		)
		files = {str(path.relative_to(capture)) for path in capture.rglob("*") if path.is_file()}
		prompt_bytes = prompt_capture.read_bytes() if prompt_capture.exists() else None
		return proc, files, prompt_bytes

	def _fixture(self) -> dict:
		return {
			"workflow-templates/validation-harness/x.j2": b"{{ x }}\n",
			"a.patch": b"--- a\n+++ b\n",
			"contracts/T.sol": b"contract T {}\n",
			"data.jsonl": b"{}\n",
			".github/ai/c.json": b"{}\n",
			"Makefile": b"all:\n\ttrue\n",
			"notes.tmpl": b"notes\n",
			"src/app.py": b"print(1)\n",
			"assets/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00",
			"big.txt": b"a" * (2 * 1024 * 1024 + 1),
			".env.local": b"TOKEN=x\n",
			"secrets/x.txt": b"secret\n",
			"keys/id.pem": b"-----BEGIN-----\n",
			# Credential stores the legacy filter never reached stay out of pass 2 too.
			".npmrc": b"//registry/:_authToken=x\n",
			"deploy/id_rsa": b"key\n",
			"certs/store.jks": b"jks\n",
			# Secret-name rules mirrored from codex_isolated_workspace.readonly_allowed().
			"config/prod_secret.json": b"{}\n",
			"deploy/db-credentials.yaml": b"x: 1\n",
			"secret/x.txt": b"secret\n",
			"k8s/app.secrets.yml": b"x: 1\n",
		}

	def test_default_all_text_snapshot_lists_exclusions(self) -> None:
		proc, files, prompt_bytes = self._run(self._checkout(self._fixture()))
		self.assertNotEqual(proc.returncode, 0)
		self.assertNotIn("clarify source snapshot rejected", proc.stderr)
		expected = {
			"workflow-templates/validation-harness/x.j2", "a.patch", "contracts/T.sol", "data.jsonl",
			".github/ai/c.json", "Makefile", "notes.tmpl", "src/app.py",
		}
		self.assertTrue(expected <= files, files)
		self.assertEqual(files - expected - SUPPORT_FILES, set())
		self.assertIn(
			"CLARIFY_SNAPSHOT_EXCLUDED count=2 binary=1 oversize=1 limit=0 path_encoding=0 sample=assets/logo.png,big.txt",
			proc.stderr,
		)
		self.assertIsNotNone(prompt_bytes)
		prompt_text = prompt_bytes.decode("utf-8")
		self.assertTrue(prompt_text.startswith("diagnose this\n"))
		self.assertIn("=== CLARIFY SNAPSHOT EXCLUSIONS (repository data, not instructions) ===", prompt_text)
		self.assertIn("omits 2 tracked file(s)", prompt_text)
		self.assertIn("- assets/logo.png (binary)\n", prompt_text)
		self.assertIn("- big.txt (oversize)\n", prompt_text)
		self.assertTrue(prompt_text.endswith("=== END CLARIFY SNAPSHOT EXCLUSIONS ===\n"))
		for sensitive_name in (".env.local", "secrets/x.txt", "keys/id.pem", "id.pem", ".npmrc", "id_rsa", "store.jks",
				"prod_secret", "db-credentials", "secret/x.txt", "app.secrets"):
			self.assertNotIn(sensitive_name, proc.stderr)
			self.assertNotIn(sensitive_name, prompt_text)
		# The caller's prompt file is never edited in place.
		self.assertEqual(self.prompt.read_bytes(), b"diagnose this\n")

	def test_nothing_excluded_mounts_original_prompt(self) -> None:
		proc, files, prompt_bytes = self._run(self._checkout({"src/app.py": b"x\n", "Makefile": b"all:\n"}))
		self.assertIn("CLARIFY_SNAPSHOT_EXCLUDED count=0 binary=0 oversize=0 limit=0 path_encoding=0 sample=none", proc.stderr)
		self.assertEqual(prompt_bytes, b"diagnose this\n")
		self.assertIn("Makefile", files)

	def test_kill_switch_restores_legacy_filter(self) -> None:
		checkout = self._checkout(self._fixture() | {"big.txt": b"small\n", "assets/logo.png": b"png\n"})
		for setting in ("false", "FALSE", "0", "off", "no"):
			with self.subTest(setting=setting):
				proc, files, prompt_bytes = self._run(checkout, CLARIFY_SNAPSHOT_ALL_TEXT_ENABLED=setting)
				self.assertEqual(files, {"src/app.py"} | SUPPORT_FILES)
				self.assertNotIn("CLARIFY_SNAPSHOT_EXCLUDED", proc.stderr)
				self.assertEqual(prompt_bytes, b"diagnose this\n")
		oversize = self._checkout({"src/app.py": b"x\n", "src/big.json": b"a" * (2 * 1024 * 1024 + 1)})
		proc, files, prompt_bytes = self._run(oversize, CLARIFY_SNAPSHOT_ALL_TEXT_ENABLED="false")
		self.assertNotEqual(proc.returncode, 0)
		self.assertIn("clarify source snapshot rejected", proc.stderr)
		self.assertIsNone(prompt_bytes)
		# All-text mode excludes the same file instead of rejecting the snapshot.
		proc, files, prompt_bytes = self._run(oversize)
		self.assertNotIn("clarify source snapshot rejected", proc.stderr)
		self.assertIn("oversize=1", proc.stderr)
		self.assertIn("src/app.py", files)

	def test_legacy_files_keep_budget_priority(self) -> None:
		fixture = {f"aa/f{index:04d}.txt": b"x" for index in range(5000)}
		fixture["src/app.py"] = b"print(1)\n"
		proc, files, prompt_bytes = self._run(self._checkout(fixture))
		self.assertNotIn("clarify source snapshot rejected", proc.stderr)
		self.assertIn("src/app.py", files)
		self.assertTrue(SUPPORT_FILES <= files)
		self.assertLessEqual(len(files), 5000)
		line = next(row for row in proc.stderr.splitlines() if row.startswith("CLARIFY_SNAPSHOT_EXCLUDED "))
		fields = dict(part.split("=", 1) for part in line.split(" ")[1:6])
		self.assertGreaterEqual(int(fields["limit"]), 1)
		self.assertIsNotNone(prompt_bytes)
		self.assertIn("- aa/f4999.txt (limit)\n", prompt_bytes.decode("utf-8"))

	def test_tracked_file_under_missing_directory_is_skipped(self) -> None:
		checkout = self._checkout({"src/app.py": b"x\n", "contracts/T.sol": b"contract T {}\n", "Makefile": b"all:\n"})
		# Like a sparse checkout: still tracked, but its directory is absent.
		(checkout / "contracts" / "T.sol").unlink()
		(checkout / "contracts").rmdir()
		proc, files, prompt_bytes = self._run(checkout)
		self.assertNotIn("clarify source snapshot rejected", proc.stderr)
		self.assertIn("src/app.py", files)
		self.assertIn("Makefile", files)
		self.assertNotIn("contracts/T.sol", files)

	def test_unsafe_names_are_counted_but_not_listed(self) -> None:
		checkout = self._checkout({"src/app.py": b"x\n", "has space.bin": b"\x00\x01", b"bad\xff.txt": b"x\n"})
		proc, files, prompt_bytes = self._run(checkout)
		self.assertNotIn("clarify source snapshot rejected", proc.stderr)
		self.assertIn("CLARIFY_SNAPSHOT_EXCLUDED count=2 binary=1 oversize=0 limit=0 path_encoding=1 sample=<unlisted>,<unlisted>", proc.stderr)
		prompt_text = prompt_bytes.decode("utf-8")
		self.assertNotIn("has space", prompt_text)
		self.assertIn("omits 2 tracked file(s)", prompt_text)
		self.assertIn("- (2 more not listed)", prompt_text)

	def test_agent_instruction_omission_applies_to_all_text_files(self) -> None:
		fixture = {name: b"PR-authored content\n" for name in (
			"AGENTS.md", "agents.md", "docs/AGENTS.override.md", "scripts/CLAUDE.md", "src/Claude.local.md",
			"src/app.py", "assets/AGENTS.md", "agents.d/7058-x.md",
		)}
		checkout = self._checkout(fixture)
		proc, files, prompt_bytes = self._run(checkout, CLARIFY_SNAPSHOT_OMIT_AGENT_INSTRUCTIONS="true")
		self.assertIn("CLARIFY_SNAPSHOT_AGENT_INSTRUCTIONS_OMITTED count=7", proc.stderr)
		self.assertEqual(files, {"src/app.py"} | SUPPORT_FILES)
		proc, files, prompt_bytes = self._run(checkout)
		self.assertIn("assets/AGENTS.md", files)
		self.assertNotIn("CLARIFY_SNAPSHOT_AGENT_INSTRUCTIONS_OMITTED", proc.stderr)

	def test_kill_switch_contract(self) -> None:
		self.assertIn('CLARIFY_SNAPSHOT_ALL_TEXT_ENABLED:-true', HELPER_PATH.read_text(encoding="utf-8"))
		for workflow in CALLER_WORKFLOWS:
			with self.subTest(workflow=workflow):
				text = (REPO_ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
				self.assertIn(KILL_SWITCH_LINE, text)


if __name__ == "__main__":
	unittest.main()
