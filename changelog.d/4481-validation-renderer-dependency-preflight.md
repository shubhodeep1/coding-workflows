<!-- changelog: fixed -->
- **Validation stops before template rendering when renderer dependencies are unavailable.**

The reusable validate workflow now confirms that PyYAML, jsonschema, and Jinja2 can be imported by the Python interpreter used for validation. If dependency installation fails or imports are unavailable, validation reports the existing harness-error outcome without invoking the template renderer. The tracking-issue comment includes the preflight diagnostic, while the workflow still collects status and artifacts.

What this means for operators: missing renderer dependencies produce a clear failure report rather than an attempted render with a misleading Python environment probe.
