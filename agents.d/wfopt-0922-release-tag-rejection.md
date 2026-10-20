<!-- agents: section="Stable log prefixes (contractual)" -->
- `RELEASE_TAG_PUBLISH` (`mark-stable.yml` and `test-and-mark-stable.yml` "Tag version and update stable pointer", `publish_tag_with_remote_verification`: `outcome=failed error_class=workflow_scope tag_ref=<ref>`; GitHub refused the tag push because the release credential lacks the `workflow` scope or `workflows: write`, so the helper stops after one attempt instead of retrying five times; every other push failure keeps the retry and remote verification)
LOG_PREFIX.name=RELEASE_TAG_PUBLISH
