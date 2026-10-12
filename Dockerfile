# SGSP salary-package voice agent only.
#   docker build -t sgsp-agent .
#   docker run -d --name sgsp-agent -p 8004:8004 --env-file .env \
#     -v sgsp_logs:/app/sgsp_logs -v sgsp_data:/app/sgsp_data sgsp-agent
# Secrets come from --env-file at run time; .env is never copied into the image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SGSP_LOGS_DIR=/app/sgsp_logs \
    SGSP_DB_PATH=/app/sgsp_data/sgsp.db

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY sgsp/ ./sgsp/

# Call logs and the SQLite campaign DB live outside the image (mount volumes here).
RUN useradd --create-home --uid 10001 sgsp \
    && mkdir -p /app/sgsp_logs /app/sgsp_data \
    && chown -R sgsp:sgsp /app
USER sgsp
VOLUME ["/app/sgsp_logs", "/app/sgsp_data"]

EXPOSE 8004

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8004/sgsp/health', timeout=4)"

# No --reload in the container (it left orphaned workers holding the port locally).
CMD ["uvicorn", "sgsp.app:app", "--host", "0.0.0.0", "--port", "8004"]
