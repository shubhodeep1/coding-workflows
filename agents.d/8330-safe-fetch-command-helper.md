<!-- agents: section="Interactive slash-command model selection" -->
### Safe-fetch helper for command URL downloads (#8330)

`/investigate-issue`, `/analyze-log` and `/apply-url` download raw log, artifact and PDF URLs with `(cd .claude && python scripts/safe_fetch_url.py --url <url> --output /tmp/<name>)`, never `curl`. The helper's source is `workflow-templates/.claude/scripts/safe_fetch_url.py`, ported unchanged from `shubhodeep1/radateeree-resort.com:scripts/safe_fetch_url.py` (Refs shubhodeep1/radateeree-resort.com#151). It ships through the existing `.claude/` sync ("Sync .claude/ assets from upstream" in `update_workflows.yml`), lands in consumers at `.claude/scripts/safe_fetch_url.py`, and is covered by the signed sync manifest and the consumer allowlist like every other `.claude/` asset. This repo runs the byte-identical live copy at `.claude/scripts/safe_fetch_url.py`. Commands call it from `.claude` so the text contains `python scripts/safe_fetch_url.py`, which consumer command tests match.

Behaviour:
- Only HTTPS is accepted. A URL with credentials, a malformed authority, or an address that is not globally routable is refused. That covers loopback, private, link-local, reserved and multicast addresses, and their IPv4-mapped forms. A host name is refused when any address it resolves to is unsafe.
- curl is pinned to the checked address with `--resolve` and run with `-q --noproxy '*'`. Redirects are followed one hop at a time, and each hop is checked again.
- Defaults: 5 redirects (at most 20), 100 MiB (at most 1 GiB), 10 s to connect and 120 s in total (each at most 600 s). These are set with `--max-redirects`, `--max-bytes`, `--connect-timeout` and `--max-time`.
- On success it prints the final status and exits 0. On failure it prints the status (when there was one), writes `safe_fetch_url: <reason>` to stderr and exits 1. The output file is replaced atomically on success and removed on failure, so an error page is never kept as the log.
- Failure mode: it needs direct outbound HTTPS because it bypasses the proxy. In an environment that only allows traffic through the proxy, every fetch fails, and the commands record the URL as inaccessible.

`tests/test_safe_fetch_url.py` (`ci.yml` step "Safe-fetch helper and command template tests") ports the helper's tests, which use fakes and make no network calls. It also checks:
- each of the three templates contains the call, makes it only from `.claude`, and has no `curl --fail-with-body -sSL`;
- the `Bash` bullet in `apply-url.md` names the helper;
- the live copy matches the template.
