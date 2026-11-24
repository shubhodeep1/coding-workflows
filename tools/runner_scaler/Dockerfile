# Runner scaler image, built by DigitalOcean App Platform from
# tools/runner_scaler (app.yaml: source_dir). See README "DigitalOcean runners
# for consumer pipelines".
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY runner_scaler /app/runner_scaler
COPY bootstrap /app/bootstrap

USER nobody
EXPOSE 8080
CMD ["python", "-m", "runner_scaler"]
