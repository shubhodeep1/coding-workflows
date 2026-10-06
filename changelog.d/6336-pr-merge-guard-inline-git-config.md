<!-- changelog: security -->
- **The merged-PR guard now checks the effective destination of pushes with inline Git configuration.** A per-command remote URL or push mode can change which branch `git push` updates; the guard now applies those options when checking PR history and blocks configurations it cannot safely resolve.

Both the live hook and the consumer template read the selected push remote rather than an unrelated origin, using its saved push URL when it already exists and the inline URL when it is newly defined. A configured positional remote overrides `--repo`, and unresolvable inline configuration, including option-prefixed `env GIT_CONFIG_*` overrides, `env -S` split strings (including `\_` word separators), unrecognized `env` options, and bulk pushes, is blocked. The guard skips `env -a`/`--argv0` values before locating Git, including through `/bin/env`. Inline remote-URL overrides cannot redirect commit checks to another repository. Pushes without inline configuration keep their previous behavior.

What this means for operators: consumer repos receive the protection through the next stable workflow sync; no configuration is required.
