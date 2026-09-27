<!-- changelog: fixed -->
- **Isolated validation renderer imports from pull-request code.**

Validation now installs renderer dependencies in a runner-side virtual environment and checks their resolved origins before running the renderer. Its Python probes and renderer run in isolated mode from a trusted directory, preventing a pull request's shadow modules or Python path settings from executing with validation credentials. Missing or externally resolved dependencies retain the existing harness-error reporting path.
