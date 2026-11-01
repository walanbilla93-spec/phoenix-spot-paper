FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY recorder.py capture_quality.py setup_evidence.py setup_review.py research_engine.py research_policy.json research_replay.py ./
COPY paper_trials.py paper_signals.py paper_paths.py paper_capture.py paper_audit.py trial_protocol.json ./
ENV PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/data/sol-recorder
EXPOSE 8080
CMD ["python", "recorder.py"]




