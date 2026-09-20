<!-- changelog: security -->
- **Security-pass waivers and comprehensive promotion markers now fail closed on ambiguous or forged evidence.** Waivers require exact code-context fingerprints plus current maintain/admin authorization, while promotion markers are keyring-signed, bot-ID-bound, and checked against the live smoke run's coverage-affecting inputs before promotion.
