<!-- changelog: security -->
- **`/investigate-issue`, `/analyze-log` and `/apply-url` now download external URLs through a safe fetch helper instead of raw `curl`.** The helper refuses plain HTTP and any host that resolves to a private or internal address, including redirect targets.

The three synced commands used `curl --fail-with-body -sSL -o <path> <url>` for raw log, artifact and PDF URLs, which followed redirects anywhere, including into internal networks. They now run `(cd .claude && python scripts/safe_fetch_url.py --url <url> --output <absolute path>)`. The helper ships with the existing `.claude/` sync in `update_workflows.yml`, so every consumer gets `.claude/scripts/safe_fetch_url.py` on its next sync with nothing to install. It checks every DNS answer and every redirect hop, pins the connection to the address it checked, prints the final HTTP status, and exits 1 without keeping the error page on any failure or non-2xx status. The `/apply-url` `Bash` permission now names the helper instead of `curl`, and the PDF flow still runs `pdftotext` on the downloaded file.

| The numbers that matter | Value |
| --- | --- |
| Command files converted (template and live copy) | 3 + 3 |
| Allowed scheme | `https://` only |
| Redirects followed | 5 by default, 20 at most |
| Cap per request (each redirect hop is one request) | 100 MiB and 120 s by default |

What this means for consumer repos: after the next `@stable` sync, a fetch of an `http://` URL or of a host on a private network fails on purpose instead of downloading. Fetch GitHub Actions logs through the MCP tools or `gh run view` as before. shubhodeep1/radateeree-resort.com can turn on its `SAFE_FETCH_COMMAND_GRANT_UNSET_OPERATOR_STEP` check once the converted files reach its `main`.

### For contributors

The helper is a port of shubhodeep1/radateeree-resort.com `scripts/safe_fetch_url.py`; the commands keep the literal `python scripts/safe_fetch_url.py`, which that repo's test looks for. `tests/test_safe_fetch_url.py` covers the helper, the six command files and the documented invocation, and runs in `ci.yml` and both release gates.
