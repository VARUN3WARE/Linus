# Sandbox for running untrusted PR code. Tests run with --network none,
# a read-only source mount, and CPU/memory limits (see linus/sandbox.py).
FROM python:3.12-slim
RUN pip install --no-cache-dir "pytest==8.*" \
 && useradd --create-home runner
USER runner
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /work
