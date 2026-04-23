FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1

WORKDIR /workspace

RUN apt-get update \
	&& apt-get install -y --no-install-recommends curl jq \
	&& rm -rf /var/lib/apt/lists/*

COPY . /workspace

RUN if [ -f "requirements.txt" ]; then \
		pip install --no-cache-dir -r "requirements.txt"; \
	fi \
	&& pip install --no-cache-dir flask jinja2 jsonschema pyyaml pymongo pytest


CMD ["/bin/sh", "-c", "trap 'exit 0' TERM INT; while :; do sleep 5; done"]

