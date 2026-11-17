FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tradeagent ./tradeagent

# Secrets werden über Env / docker secret bereitgestellt, niemals im Bild.
EXPOSE 8000

CMD ["python", "-m", "tradeagent.api.app"]
