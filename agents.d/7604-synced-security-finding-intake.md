<!-- agents: section="Workflow architecture" -->
Synced-file security findings, retired files and category spellings (#7604
follow-up). This refines the "Synced-file security findings in the heal intake"
paragraph in this section, whose "anything else is rejected" now means anything
outside these spellings. `normalize_synced_security_category` accepts `A01`..`A10` with
or without the `:2021` year and with any name after it; a code followed by
another year (`A01:2017`, `A01 2017`, `A01 (2017)`) is rejected, because 2017
numbering differs; a year later in the name, such as a CVE reference, does
not count. A STRIDE term may carry a `STRIDE:` prefix, and in a
slash-separated list the first term decides; anything else is still rejected.
In `_synced_security_finding_intake`, a group whose file is listed in the
release's `workflow-templates/retired_files.txt` or in HEAD's copy (read
with `git show <wrapper_sha>:...` and `git show HEAD:...`; an absent list means
nothing is retired) is skipped before
the budget decision with `skip reason=retired_file file=<path>`: coding-workflows
no longer ships the file and the consumer's next sync deletes byte-identical
copies, so there is no source to fix. It counts as skipped but does not raise
the Telegram level.
