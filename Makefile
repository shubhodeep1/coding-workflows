.PHONY: generate generate-check

generate:
	python3 scripts/generate_codex_model_reference.py --write
	python3 tools/repo_tree/update_repo_tree.py --write

generate-check:
	python3 scripts/generate_codex_model_reference.py --check
	python3 tools/repo_tree/update_repo_tree.py --check
