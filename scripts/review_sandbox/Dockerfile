# Fixed, trusted build context; never COPY a PR checkout into this image.
FROM node:22.16.0-bookworm-slim
ARG OPENCODE_VERSION=1.18.23
# jq: the review-side pre-push gate runs shell-script tests that call jq.
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip python3-venv git ca-certificates gawk shellcheck jq \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" --no-audit --no-fund
# Pre-push affected-test gate lint (plan P4). Non-fatal: a failed install must
# never break the editor image; the gate then reports tool_unavailable.
RUN python3 -m pip install --break-system-packages --no-cache-dir ruff==0.17.2 || echo "ruff unavailable"
# Preload the OpenRouter SDK while the image has network access. The editor
# runs with --network none; only the fixed-path model relay is reachable.
RUN npm install --prefix /opt/opencode-provider @openrouter/ai-sdk-provider@3.1.0 --no-audit --no-fund
ENV NODE_PATH=/opt/opencode-provider/node_modules
# Claude engine (scripts/ai_engine.sh): installed only when `prepare claude`
# passes a version; the OpenCode image is unchanged without it.
ARG CLAUDE_CLI_VERSION=
RUN if [ -n "${CLAUDE_CLI_VERSION}" ]; then \
      npm install -g "@anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}" --no-audit --no-fund; \
    fi
