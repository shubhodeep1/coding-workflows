<!-- changelog: security -->
- **Release diagnostics no longer expose checkout credentials in Actions logs.** The checkout probe prints Git configuration key names without values, and the PAT-backed release job no longer runs the probe.
