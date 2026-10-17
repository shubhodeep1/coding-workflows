<!-- changelog: fixed -->
- **The validation harness sandbox now stages untracked files, env templates and a credential-free `.npmrc`, so consumer validations that need them run again.** Real `.env` files, `.npmrc` files with credentials, keys and certificates stay out of the sandbox copy.

`scripts/validation_harness_sandbox.sh` builds the screened copy of the consumer checkout that the sandboxed harness runs from, and compose templates build their image from that copy (`context: ..`, `COPY . /workspace`). It used to stage only tracked files plus `validation/` and `.ai/validate.yml`, and it dropped every `.env*` and `.npmrc` name. Four consumer validations failed because of that: btc_sweeper and mongo-explorer never passed the healthcheck because the untracked `scripts/run_validation_repo_checks.sh` stub the harness seeds was missing, fun-token-multi-chain's `npm ci` failed without its tracked `legacy-peer-deps=true` `.npmrc`, and a hylifegroup.com test could not read the tracked `.env.example`. The copy now also holds untracked files that git does not ignore, `.env.example`, `.env.sample` and `.env.template` in any directory, and any `.npmrc` that passes a credential screen.

| The numbers that matter | Value |
| --- | --- |
| Name listing | `git ls-files -z --cached --others --exclude-standard` |
| Env templates admitted | `.env.example`, `.env.sample`, `.env.template` (exact names) |
| `.npmrc` size limit | 64 KiB, regular file only, UTF-8 |
| `.npmrc` reject markers | lines starting with `//`, URL userinfo, `_auth*`, `_password`, `password`, `username`, `email`, `certfile`, `keyfile`, `cafile`, `token`, `secret`, `${`, `-----begin`, keys `cert`/`key`/`ca`/`otp` |
| Consumer validations unblocked | 4 |

What this means for operators: the validation log now shows `VALIDATION_HARNESS_SANDBOX phase=stage outcome=ok reason=env_template_admitted|npmrc_admitted path=<name>` for each extra file staged, and `outcome=skip reason=npmrc_credential` (or `npmrc_symlink`, `npmrc_too_large`, `npmrc_undecodable`) when an `.npmrc` was left out, and `outcome=skip reason=env_template_not_regular` for a symlinked env template. A project whose `npm ci` needs a registry token still has to supply it another way; the sandbox never receives it. Files you do not want in the sandbox copy belong in `.gitignore`.

### For contributors

The admissions live only in the staging program (`STAGE_PY`). `codex_isolated_workspace.readonly_allowed` is unchanged, because agent sandboxes use it and an `.npmrc` can carry `_authToken`. In `validate.yml` the untracked files include the workflow support files staged into the consumer checkout, which the harness also saw when it ran on the host. Tests: the `test_stage_copy_*` cases in `tests/test_validation_harness_sandbox.py`.
