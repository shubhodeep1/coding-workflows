# Fixed, trusted build context; never COPY a PR checkout into this image.
FROM node:22.16.0-bookworm-slim
ARG OPENCODE_VERSION=1.18.23
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip python3-venv git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" --no-audit --no-fund
# Preload the OpenRouter SDK while the image has network access. The editor
# runs with --network none; only the fixed-path model relay is reachable.
RUN npm install --prefix /opt/opencode-provider @openrouter/ai-sdk-provider@3.1.0 --no-audit --no-fund
ENV NODE_PATH=/opt/opencode-provider/node_modules
ARG CODEX_VERSION=
RUN if [ -n "${CODEX_VERSION}" ]; then \
      apt-get update && apt-get install -y --no-install-recommends python3-pytest jq shellcheck \
      && rm -rf /var/lib/apt/lists/* \
      && npm install -g "@openai/codex@${CODEX_VERSION}" \
        "@openai/codex-linux-x64@npm:@openai/codex@${CODEX_VERSION#v}-linux-x64" --no-audit --no-fund; \
    fi
# Claude engine (scripts/ai_engine.sh): installed only when `prepare claude`
# passes a version; the OpenCode image is unchanged without it.
ARG CLAUDE_CLI_VERSION=
RUN if [ -n "${CLAUDE_CLI_VERSION}" ]; then \
      npm install -g "@anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}" --no-audit --no-fund; \
    fi
