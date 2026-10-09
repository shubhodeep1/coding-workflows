<!-- changelog: fixed -->
- **Project validations no longer fail before running any test on GitHub-hosted runners.** The isolated validation sandbox now installs its rootless Docker packages even though the runner image ships without Docker's apt source.

Since #6569, every project validation ended in `harness_error` with `VALIDATION_HARNESS_SANDBOX phase=provision outcome=fail reason=rootless_packages_unavailable`, because GitHub's Ubuntu runner images install Docker from download.docker.com and then delete that apt source, leaving `docker-ce-rootless-extras` with no install candidate. `scripts/validation_harness_sandbox.sh` now adds Docker's repository for that one install, trusts its signing key only at the pinned fingerprint, installs the rootless extras at the runner's `docker-ce` version, and removes the source and key again. The sandbox stays fail-closed: if the packages still cannot be installed, validation reports `harness_error` and never falls back to running tests on the host.

| The numbers that matter | Value |
| --- | --- |
| Pinned Docker apt key fingerprint | `9DC858229FC7DD38854AE2D88D81803C0EBFCD88` |
| New success log | `VALIDATION_HARNESS_SANDBOX phase=provision outcome=ok reason=rootless_packages_from_docker_repo` |
| New failure reasons before `rootless_packages_unavailable` | `docker_repo_distro_unsupported`, `docker_repo_platform_unknown`, `docker_repo_key_download_failed`, `docker_repo_key_fingerprint_mismatch`, `docker_repo_source_write_failed`, `docker_repo_update_failed`, `docker_repo_install_failed` |

What this means for operators: projects stuck in `ai:validation-failed` with the sandbox `harness_error` can be revalidated with `/revalidate` once this reaches their workflows.

### For contributors

`tests/test_validation_harness_sandbox.py` was not run by CI before; `ci.yml` now runs it in its own step. The script runs `main` only when executed, so tests can source it and call `install_rootless_packages` against fake `sudo`, `apt-get`, `curl`, `gpg` and `dpkg`.
