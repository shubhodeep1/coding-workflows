FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1

WORKDIR /workspace

RUN apt-get update \
	&& apt-get install -y --no-install-recommends bash ca-certificates curl git jq \
	&& rm -rf /var/lib/apt/lists/*

COPY . /workspace

RUN python3 -m pip install --no-cache-dir pyyaml jsonschema jinja2 pytest

CMD ["sh", "-c", "while :; do sleep 3600; done"]
