.PHONY: generate generate-check

generate:
	PYTHONDONTWRITEBYTECODE=1 python3 scripts/generate_codex_model_reference.py --write
	PYTHONDONTWRITEBYTECODE=1 python3 tools/repo_tree/update_repo_tree.py --write

generate-check:
	PYTHONDONTWRITEBYTECODE=1 python3 scripts/generate_codex_model_reference.py --check
	PYTHONDONTWRITEBYTECODE=1 python3 tools/repo_tree/update_repo_tree.py --check
