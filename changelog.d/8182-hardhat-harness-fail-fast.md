<!-- changelog: fixed -->
- **The Hardhat validation harness now names a missing project tree instead of failing with npm ENOENT.** When `package.json` does not reach the `node-hardhat-solidity` app image, the build stops with a `harness_error` line, and the test step reports it before running the suite.

In fun-token-multi-chain tracking issue 579, the consumer's root `.dockerignore` stripped `package.json` from the harness image. `Dockerfile.app` skipped `npm install` without an error, and the failure showed up later as `npm error enoent ... /workspace/package.json` in `hardhat_test.log`, which a diagnosis then misread. #8181 stopped the root `.dockerignore` from applying. This change makes the same class of problem fail loudly: the install step in `workflow-templates/validation-harness/node-hardhat-solidity/Dockerfile.app.j2` exits 1 with `harness_error: package.json missing from build context /workspace (check the repository .dockerignore)`, and `tests/30_hardhat_test.sh` prints `not ok 1 - hardhat test suite` with `# harness_error: project tree not staged at /workspace` when the file is absent in the running container. The validation sandbox also logs one `VALIDATION_HARNESS_SANDBOX phase=stage outcome=ok reason=staged_count staged=<n>` line per run, a member count with no file names.

| The numbers that matter | Value |
| --- | --- |
| Harness families changed | 1 (`node-hardhat-solidity`) |
| New log lines per validation run | 1 (`reason=staged_count`) |
| Precheck exit code that means "file missing" | 3 (any other precheck failure runs the suite as before) |

What this means for consumer repos: a Hardhat project with `package.json` at the repository root sees no change. A repository whose `node-hardhat-solidity` manifest points at a project without a root `package.json` now fails the image build at startup; before, it built with no dependencies installed.
