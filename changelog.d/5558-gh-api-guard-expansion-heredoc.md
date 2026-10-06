<!-- changelog: fixed -->
- **The `gh api` permission guard prompts when an unquoted argument expansion could inject a flag, and no longer hides calls behind quoted heredoc markers.**

An unquoted variable appended to a read endpoint could split into `-Fbody=@<file>` or `-XDELETE` after the guard classified it. A quoted or escaped `<<EOF` could instead make the guard discard the following command as a heredoc body, even though Bash would execute it. The hook now checks raw argument quoting and only strips actual heredoc bodies; the consumer hook copy stays identical. Double-quoted dynamic read endpoints keep their existing no-decision behavior in loops. No GitHub API calls or new runtime dependencies were added.
