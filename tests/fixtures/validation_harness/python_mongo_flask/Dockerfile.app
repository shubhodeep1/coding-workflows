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
	&& pip install --no-cache-dir flask pymongo pytest

ENV FLASK_APP="app.py" \
	FLASK_RUN_HOST="0.0.0.0" \
	FLASK_RUN_PORT="8000"

CMD ["/bin/sh", "-c", "echo $$ > /tmp/flask-app.pid; exec python -m flask --app \"${FLASK_APP}\" run --host=\"${FLASK_RUN_HOST}\" --port=\"${FLASK_RUN_PORT}\""]
