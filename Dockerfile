FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CODEX_HOME=/codex \
    DATA_DIR=/app/data \
    DASHBOARD_PORT=8765

WORKDIR /app

COPY collect_codex_usage.py server.py ./
COPY dashboard ./dashboard

RUN mkdir -p /app/data /codex && \
    useradd --create-home --uid 10001 dashboard && \
    chown -R dashboard:dashboard /app

USER dashboard

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).read()" || exit 1

CMD ["python","server.py","--host","0.0.0.0","--port","8765"]
