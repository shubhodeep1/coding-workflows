"""Read agents.md the way the pipeline sees it: with pending agents.d/ fragments folded in.

PRs document new behaviour in ``agents.d/<pr>-<slug>.md`` fragments (CLAUDE.md
§30); ``scripts/assemble_agents.py`` folds them into ``agents.md`` at release
time. A test that asserts something is documented in ``agents.md`` must accept
it while it is still waiting in a fragment, so it reads through ``agents_text``
instead of opening the file directly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / "scripts" / "assemble_agents.py"
_MODULE_NAME = "assemble_agents_for_tests"
if _MODULE_NAME in sys.modules:
	_assembler = sys.modules[_MODULE_NAME]
else:
	_SPEC = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
	_assembler = importlib.util.module_from_spec(_SPEC)
	# dataclasses resolve annotations through sys.modules.
	sys.modules[_MODULE_NAME] = _assembler
	_SPEC.loader.exec_module(_assembler)


def agents_text(repo_root: Path = REPO_ROOT) -> str:
	"""agents.md (or AGENTS.md) with every valid pending fragment folded in."""
	agents_path = _assembler.resolve_agents_path(repo_root)
	text = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
	fragments = _assembler.iter_fragment_paths(repo_root / _assembler.FRAGMENTS_DIRNAME)
	return _assembler.fold(text, fragments)[0]
