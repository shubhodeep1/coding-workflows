<!-- agents: section="Isolated Codex agents" -->
- **Image-build retries (#7196).** `build_image` in
  `scripts/codex_isolated_exec.sh` retries only the image build, up to
  `CODEX_ISOLATED_IMAGE_BUILD_ATTEMPTS` (default `1`, capped at `5`; an
  invalid value warns and uses `1`), waiting
  `CODEX_ISOLATED_IMAGE_BUILD_RETRY_SLEEP_SECS` (default `15`) times the
  attempt number between tries. Implement's two `prepare --deps` calls set
  it to `3` as an inline prefix, never a step `env:` or `GITHUB_ENV` entry,
  so `run` builds and the Claude engine keep one attempt and its exit-75
  fallback. Every other isolation failure is still not retried, and an
  exhausted build fails closed as before. Logs
  `CODEX_ISOLATION image_build attempt=<n>/<max> outcome=ok|retry|fail`
  (only when more than one attempt is allowed).
