<!-- agents: section="Immutable consumer wrapper pins" -->
- Release tagging switch defaults on (issue #7272, from #7029). In the
  `release` jobs of `mark-stable.yml` and `test-and-mark-stable.yml`,
  `RELEASE_TAGGING_ENABLED` resolves as `vars.RELEASE_TAGGING_ENABLED || 'true'`
  and `${RELEASE_TAGGING_ENABLED-true}`: unset, empty or `true` (any letter
  case) publishes, and any other value skips the changelog commit, tag pushes,
  release manifest build/attestation/upload, GitHub Release and consumer
  dispatch with a `::warning::`. This is intentional. The safety control is
  the fail-closed `CI / lint` check-run gate (`scripts/release_ci_gate.sh`)
  in `validate` and `release`; the switch is an operator's emergency off
  switch, and defaulting it off would only stop releases. Activation
  verification should not report the default-on fallback as a gap. To pause
  releases, set `RELEASE_TAGGING_ENABLED=false` and
  `AUTO_RELEASE_STABLE_ENABLED=false`.
