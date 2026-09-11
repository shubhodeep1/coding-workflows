<!-- changelog: security -->
- **Review-autofix now reliably stops a runaway isolated editor before restoring workspace ownership.** The stall guard and wall/idle watchdog share runner-owned process-group metadata, use privileged signalling only for the `nobody` editor group, and fail closed when signalling fails or an editor process survives.
