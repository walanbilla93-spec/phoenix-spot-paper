FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PAPER_ONLY=true PORT=8080
WORKDIR /app
COPY v2/phoenix_v2 ./phoenix_v2
COPY v2/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10002 --create-home phoenixv2 && mkdir -p /data && chown 10002:10002 /data
USER 10002:10002
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["python", "-m", "phoenix_v2.service"]
