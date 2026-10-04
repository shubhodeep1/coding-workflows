<!-- changelog: changed -->
- **CLAUDE.md §25 now allows a scheduled pull request status check when the user asks for one.** Interactive sessions still never subscribe to PR activity and never act on a PR's CI or review activity unprompted.

Since §26 retired on 2026-10-03, §25 read as banning every scheduled look at a PR, including an hourly check the user explicitly requested ("check hourly and get these PRs to completion"). §25.B now bans only polls the user did not ask for. §25.C allows a requested check through `send_later` or a Routine. The check acts only within the request, under plain §12, is never armed unprompted, and stops when the PR merges or closes or when the user says to stop. The `subscribe_pr_activity` ban and `.claude/hooks/pr_watch_guard.py` are unchanged.

What this means for consumer repos: sessions there follow the same rule after the next `@stable` sync, because `workflow-templates/CLAUDE.md` points at this file.
