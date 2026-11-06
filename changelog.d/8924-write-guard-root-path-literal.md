<!-- changelog: fixed -->
- **Write-guard config errors name the right file when the repository path contains `*`, `?` or `[`.** The `config=` path in `WRITE_GUARD_CONFIG_ERROR` is now repo-relative only for a config file that is really under the repository root.

`write_guard_check()` in `scripts/write_guard.sh` stripped the repository root from the logged config path with an unquoted pattern, so glob characters in the root were treated as wildcards. A root such as `root[ab]*?` could fail to strip its own path, or strip a sibling directory's path that only matched as a glob. The root is now quoted inside both expansions and matched literally. Test: `tests/test_write_guard_root_pattern_chars.py`. Refs #8924.
