<!-- changelog: fixed -->
- **Workflow failure heal now knows who wrote an `ai:scope-blocked` issue.** A guard rejection that the current guard already fixes is diagnosed as `already-fixed` instead of `consumer-config`.

The automation-path guard decides by author: on main, an issue the pipeline account wrote without a `files_touched` block may edit `.github/` and `scripts/`, while older releases rejected it. hylifegroup.com#75 hit the older guard, and the heal diagnosis, which never saw the issue author, called it a consumer configuration problem and suggested adding a `files_touched` block that would have blocked the rest of the change. Reports for `ai:scope-blocked` now carry the issue author, its association and the pipeline account, the diagnosis prompt shows them, and the prompt forbids that `files_touched` suggestion.

What this means for operators: a scope-blocked heal report on a consumer that has not yet synced the newer guard says `already-fixed` and names the fixing commit. The report makes no extra GitHub API call; reports from older reporters show the author as `unknown`.
