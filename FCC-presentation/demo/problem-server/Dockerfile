FROM python:3.12-slim

# Install stress-ng for CPU/memory simulation + procps for pgrep
RUN apt-get update && apt-get install -y --no-install-recommends \
    stress-ng procps && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask==3.1.0

WORKDIR /app
COPY monitor.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 9100
CMD ["/app/entrypoint.sh"]
