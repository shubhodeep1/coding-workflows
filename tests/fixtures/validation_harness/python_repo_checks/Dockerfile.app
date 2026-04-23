FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1

WORKDIR /workspace

RUN apt-get update \
	&& apt-get install -y --no-install-recommends bash jq \
	&& rm -rf /var/lib/apt/lists/*

COPY . /workspace

RUN pip install --no-cache-dir pyyaml jsonschema jinja2
