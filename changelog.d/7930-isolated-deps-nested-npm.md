<!-- changelog: added -->
- **The isolated implement sandbox now installs nested Node projects that have their own lockfile.** Before, `codex_isolated_exec.sh prepare --deps` installed only the repository root's `package.json`, so an implementer asked to build a project such as `cloudflare/<worker>/` had no `node_modules` and, with no network, could not run `npm ci`.

The dependency phase now also finds `package.json` files below the root that sit next to a `package-lock.json`, `yarn.lock` or `pnpm-lock.yaml`, installs each one in the same credential-free, network-isolated container through the existing registry proxy (`npm ci`, `yarn install --frozen-lockfile` or `pnpm install --frozen-lockfile`, always with `--ignore-scripts`), and hands each `node_modules` to the agent in that project's directory. The agent can then run the project's own build offline. On 2026-10-22 the implementer for tele-funtoken-msg-scoring #4770 stopped with `BLOCKED: npm registry unavailable for npm ci` because the worker's dependencies were never installed (`stage-deps node=none`).

| The numbers that matter | Value |
| --- | --- |
| Nested projects installed per job | at most 8 (sorted by path) |
| Search depth | 4 directories below the root |
| Directories never searched | dot directories, `node_modules`, `dist`, `build`, `coverage`, `vendor` |
| New log fields | `stage-deps ... nested_node=<n>`, `stage-deps nested_node_skipped=<n> reason=project_limit`, `handoff-nested moved=<n> skipped=<n>` |

What this means for consumer repos: a repository with nested npm projects spends more time in the implement dependency phase, and the nested installs share its existing 900-second budget. A nested project without a lockfile is not installed. A failed nested install warns like a failed root install; the agent then marks validations it cannot run as UNVERIFIED.

### For contributors

`stage-deps` stages each project's `package.json` and lockfile under `STAGE/nested/<n>/` and lists `<n> <mode> <dir>` in `STAGE/.codex-deps/nested`; only those manifests enter the networked container, never source. The new `handoff-nested STAGE WORK` subcommand moves each `node_modules` into WORK without following symlinks and skips any target that already exists. `prep-finalize` then treats each one as a prep root, the same as the root `node_modules`, so nothing from it is copied back to the host.
