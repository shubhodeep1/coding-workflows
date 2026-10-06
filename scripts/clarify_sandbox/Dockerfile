# Fixed build context: no repository files or secrets are sent to docker build.
FROM node:22.16.0-bookworm-slim
ARG CODEX_VERSION=v0.114.0
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g "@openai/codex@${CODEX_VERSION}" \
    "@openai/codex-linux-x64@npm:@openai/codex@${CODEX_VERSION#v}-linux-x64" \
    --no-audit --no-fund
# Claude engine (scripts/ai_engine.sh): installed only when the clarify Claude
# branch passes a version; the codex image is unchanged without it.
ARG CLAUDE_CLI_VERSION=
RUN if [ -n "${CLAUDE_CLI_VERSION}" ]; then \
      npm install -g "@anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}" --no-audit --no-fund; \
    fi
