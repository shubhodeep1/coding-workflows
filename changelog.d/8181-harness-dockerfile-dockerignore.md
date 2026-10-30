<!-- changelog: fixed -->
- **Runtime validation no longer loses the project when a consumer has a restrictive root `.dockerignore`.** Every validation harness family now renders `validation/Dockerfile.app.dockerignore`, so the test image always contains the whole screened copy of the repository.

The harness builds `validation/Dockerfile.app` from the repository root with `COPY . /workspace`. Docker applied the consumer's root `.dockerignore` to that build as well. fun-token-multi-chain's integration branch carries one for its session-server image: `**` plus a few `!services/session-server/...` lines. With it, `/workspace` held only those files, and `30_hardhat_test.sh` failed with `npm error enoent ... /workspace/package.json`. The run was reported as `raw_status=harness_error`, which parks the project until someone runs `/revalidate`. BuildKit reads `<Dockerfile>.dockerignore` instead of the root file, so the rendered file, which excludes nothing, restores the full build context. The consumer's own image builds still use its root `.dockerignore`.

| The numbers that matter | Value |
| --- | --- |
| Harness families covered | 5 (`node-hardhat-solidity`, `node-runtime`, `python-mongo-flask`, `python-mongo-repo-checks`, `python-repo-checks`) |
| Patterns in the rendered ignore file | 0 (comments only) |
| Failing run | fun-token-multi-chain Actions run 40264847133 (tracking issue 579) |

What this means for consumer repos: after the next `@stable` sync, a root `.dockerignore` written for production images no longer breaks runtime validation. A consumer that relied on its root `.dockerignore` to shrink the harness build context loses that trimming. The sandbox's screened copy already leaves out git-ignored files.
