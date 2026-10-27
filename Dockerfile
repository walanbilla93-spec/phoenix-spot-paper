FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY recorder.py capture_quality.py ./
ENV PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/data/sol-recorder
EXPOSE 8080
CMD ["python", "recorder.py"]

