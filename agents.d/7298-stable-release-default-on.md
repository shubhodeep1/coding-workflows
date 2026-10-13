<!-- agents: section="Immutable consumer wrapper pins" -->
- Stable release switch defaults on (issue #7298, from #7029 / #7272). In the
  `release` jobs of `mark-stable.yml` and `test-and-mark-stable.yml` (step
  `Resolve stable release switch`), `STABLE_RELEASE_ENABLED` resolves as
  `vars.STABLE_RELEASE_ENABLED || 'true'` and `${STABLE_RELEASE_ENABLED:-true}`:
  unset, empty or `true` (any letter case) publishes, `false` turns publishing
  off, and any other value also turns it off with a `::warning::`. While off,
  the validation jobs and the CI lint gate still run; the changelog push, tags,
  GitHub Release, consumer dispatch and `sync-to-main` are skipped, and the job
  logs `STABLE_RELEASE_SWITCH enabled=false` and `STABLE_RELEASE_DISABLED`.
  This is intentional. The safety control is the fail-closed `CI / lint`
  check-run gate (`scripts/release_ci_gate.sh`, step `Require successful CI
  lint check-run`), which runs whatever the switch says; the switch is an
  operator's emergency off switch, and defaulting it off would only stop
  releases, as it did before #7029. Activation verification should not report
  the default-on fallback of `STABLE_RELEASE_ENABLED` or
  `RELEASE_TAGGING_ENABLED` as a gap (the verification of PR #7207 filed #7298
  for it); this applies to those two switches only, not to other default-on
  gates. To pause releases, set `STABLE_RELEASE_ENABLED=false` (or
  `RELEASE_TAGGING_ENABLED=false`) and `AUTO_RELEASE_STABLE_ENABLED=false`.
