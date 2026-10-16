<!-- agents: section="Stable log prefixes (contractual)" -->
- `CLAUDE_POOL_MODULE_PIN` (`plan.yml` and `clarify.yml` "Pin claude-pool-token action module type", #7434 / #7476: `outcome=written`, `outcome=skip reason=present|action_dir_unavailable`, `outcome=failed reason=write_failed`; writes `{"type":"commonjs"}` as `package.json` into the support checkout's `.github/actions/claude-pool-token/` so a consumer root `package.json` with `"type": "module"` cannot make Node load the action's CommonJS `main.js` / `post.js` as ES modules; never overwrites an existing file or follows a symlink; temporary until the action ships its own `package.json`)

LOG_PREFIX.name=CLAUDE_POOL_MODULE_PIN
