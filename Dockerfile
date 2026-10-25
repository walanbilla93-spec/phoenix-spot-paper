FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PAPER_ONLY=true DATA_DIR=/data PORT=8080
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY phoenix.py server.py service_v2.py rotation.py selector.py refill.py dashboard.html dashboard_v2.html ledger.html settings.html settings_model.py settings_auth.py ./
RUN useradd --uid 10001 --create-home phoenix && mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["python", "service_v2.py"]
