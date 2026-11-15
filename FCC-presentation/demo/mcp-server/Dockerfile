FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 3001
CMD ["gunicorn", "--bind", "0.0.0.0:3001", "--workers", "2", "--timeout", "30", "server:app"]
