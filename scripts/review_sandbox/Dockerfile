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
