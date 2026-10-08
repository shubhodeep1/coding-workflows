<!-- changelog: security -->
- **API-write permissions now require confirmation outside session-owned Cloudflare Worker scripts.** DigitalOcean writes always prompt; Cloudflare DNS, zone, route, secret and other account operations prompt too.

Only canonical Worker script writes whose account ID matches `FUNTOKEN_IO_CF` or `FT_GAMES_CF` retain the silent path. This ships to consumer repos on their next `@stable` sync. The hook does not inspect request bodies: a Worker upload or settings update can still declare a secret binding in its body. Refs #3576.
