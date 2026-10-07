<!-- changelog: security -->
- **Check-failure triage rejects forged routing metadata in generated issues.** Check names and other header metadata are flattened before display, and the complete issue body is validated after redaction. Unsafe routing directives or markers stop issue creation; raw check names still determine de-duplication.
