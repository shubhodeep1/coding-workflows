<!-- changelog: security -->
- **Merged-PR push checks now use the actual destination repository.** Positional remotes, URLs, `--repo` and git's push-remote settings are resolved before PR lookup; unmappable destinations are blocked. Refs #6263; related to #3576.

The guard checks each configured push URL's GitHub repository, and its git-history fallback uses the matching destination remote rather than the checkout's origin. Ordinary local commits and MCP pushes retain their existing behavior.

An `env -C` commit whose directory cannot be resolved requests confirmation instead of using an unrelated checkout's PR history. Positional push repositories take precedence over `--repo`; an unmappable destination is blocked, while a GitHub URL uses that repository's PR history.

What this means for operators: pushes to non-GitHub or unknown destinations require an explicit guard override; the hook no longer silently checks origin for a push to another repository.
