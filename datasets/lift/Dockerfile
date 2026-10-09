FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
COPY toolbox.py /opt/darwin-tools/toolbox.py
RUN mkdir -p /opt/darwin-state /workspace/task
WORKDIR /workspace/task
CMD ["python", "-c", "import time; time.sleep(86400)"]
