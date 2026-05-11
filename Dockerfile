FROM python:3.11-slim

# System deps for faiss-cpu and PDF parsing
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app.py .
COPY agents/ agents/
COPY utils/ utils/
COPY static/ static/
COPY assets/ assets/

# Copy pre-built FAISS index (84 MB; built locally before docker build)
# If the index is not present, UC1 will attempt to rebuild it on first request
# using the Azure OpenAI embedding endpoint.
COPY uc1_faiss_index/ uc1_faiss_index/

# config.json is NOT copied — credentials are injected via environment variables.
# At startup, llm_client.py reads AZURE_OPENAI_* env vars and writes a runtime config.

EXPOSE 8000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
