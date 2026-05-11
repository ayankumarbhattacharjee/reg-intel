# GSK Regulatory Intelligence Platform

A unified web application combining three Regulatory Intelligence use cases into a single FastAPI backend with a streaming HTML/JS frontend.

| Use Case | Description |
|----------|-------------|
| UC1 — Regulatory Chatbot | RAG-powered chatbot over a 12,957-chunk FAISS knowledge base |
| UC2 — External RegIntel Risk Analyzer | Compares an external regulatory update against an internal SOP |
| UC3 — Dual-Source Analyzer | Compares external and internal documents against an SOP |

All use cases stream results to the browser in real time via Server-Sent Events (SSE).

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.11+ | 3.13 supported |
| Docker Desktop | any recent | Required for container deployment |
| Azure CLI | 2.x | Azure deployment only |
| AWS CLI | v2 | AWS deployment only |

---

## 1. Clone & Configure Locally

```bash
git clone https://github.com/ayankumarbhattacharjee/reg-intel.git
cd reg-intel
```

### Create config.json

`config.json` is excluded from the repository (it contains secrets). Copy the template and fill in your values:

```bash
cp config.template.json config.json
```

Edit `config.json`:

```json
{
  "azure_openai": {
    "api_key": "<YOUR-AZURE-OPENAI-API-KEY>",
    "endpoint": "https://<YOUR-RESOURCE>.cognitiveservices.azure.com",
    "api_version": "2024-10-21",
    "chat_deployment": "gpt-4o",
    "mini_deployment": "gpt-4o",
    "embedding_deployment": "text-embedding-3-small"
  },
  "uc1": {
    "chunks_pkl_path": "<PATH-TO-Chunks_Complete.pkl>",
    "faiss_index_path": "uc1_faiss_index",
    "assets_path": "assets/uc1",
    "top_k": 5,
    "pages_returned": 3,
    "qte_temperature": 0.5,
    "qte_max_tokens": 200,
    "rag_temperature": 0.6,
    "rag_max_tokens": 2000
  },
  "uc2": { "assets_path": "assets/uc2", "chunk_size": 10000, "chunk_overlap": 1000 },
  "uc3": { "assets_path": "assets/uc3", "chunk_size": 10000, "chunk_overlap": 1000 }
}
```

### Install dependencies & run

```bash
pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** in your browser.

> **FAISS index:** If `uc1_faiss_index/` does not exist, UC1 will build it automatically on the first request using your Azure OpenAI embedding endpoint and `chunks_pkl_path`. This takes ~15 minutes for 12,957 chunks.

---

## 2. Deploy to Azure (Azure Container Apps)

### Step 1 — Build & push the image to Azure Container Registry

```bash
# Log in
az login
az acr login --name <YOUR-ACR-NAME>

# Build and push (run from the repo root; uc1_faiss_index/ must exist locally)
az acr build \
  --registry <YOUR-ACR-NAME> \
  --image gsk-regintel:latest \
  .
```

> If you don't have an ACR yet:
> ```bash
> az group create --name rg-regintel --location eastus
> az acr create --resource-group rg-regintel --name <YOUR-ACR-NAME> --sku Basic
> ```

### Step 2 — Create an Azure Container Apps environment

```bash
az containerapp env create \
  --name regintel-env \
  --resource-group rg-regintel \
  --location eastus
```

### Step 3 — Deploy the container app

```bash
az containerapp create \
  --name gsk-regintel \
  --resource-group rg-regintel \
  --environment regintel-env \
  --image <YOUR-ACR-NAME>.azurecr.io/gsk-regintel:latest \
  --registry-server <YOUR-ACR-NAME>.azurecr.io \
  --target-port 8000 \
  --ingress external \
  --min-replicas 1 \
  --max-replicas 3 \
  --env-vars \
      AZURE_OPENAI_API_KEY=secretref:openai-key \
      AZURE_OPENAI_ENDPOINT="https://<YOUR-RESOURCE>.cognitiveservices.azure.com" \
      AZURE_OPENAI_API_VERSION="2024-10-21" \
      AZURE_CHAT_DEPLOYMENT="gpt-4o" \
      AZURE_MINI_DEPLOYMENT="gpt-4o" \
      AZURE_EMBEDDING_DEPLOYMENT="text-embedding-3-small" \
  --secrets openai-key="<YOUR-AZURE-OPENAI-API-KEY>"
```

The command prints a public HTTPS URL when complete (e.g. `https://gsk-regintel.eastus.azurecontainerapps.io`).

### Step 4 — (Optional) Continuous deployment from GitHub

In the Azure Portal → your Container App → **Continuous deployment** → connect your GitHub repo (`ayankumarbhattacharjee/reg-intel`, branch `main`). Azure will rebuild and redeploy on every push.

---

## 3. Deploy to AWS (App Runner via ECR)

### Step 1 — Upload the FAISS index to S3

The FAISS index (84 MB) is excluded from git. Store it in S3 so CI can retrieve it during builds.

```bash
aws s3 mb s3://<YOUR-BUCKET-NAME>
aws s3 cp uc1_faiss_index/ s3://<YOUR-BUCKET-NAME>/uc1_faiss_index/ --recursive
```

### Step 2 — Create an ECR repository

```bash
aws ecr create-repository --repository-name gsk-regintel --region us-east-1
# Note the repositoryUri printed, e.g.:
#   123456789012.dkr.ecr.us-east-1.amazonaws.com/gsk-regintel
```

### Step 3 — Build & push the Docker image

```bash
# Authenticate Docker to ECR
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin \
    123456789012.dkr.ecr.us-east-1.amazonaws.com

# Build (uc1_faiss_index/ must exist locally)
docker build -t gsk-regintel .

# Tag and push
docker tag  gsk-regintel:latest \
            123456789012.dkr.ecr.us-east-1.amazonaws.com/gsk-regintel:latest
docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/gsk-regintel:latest
```

### Step 4 — Create an App Runner service

In the AWS Console → **App Runner → Create service**:

| Setting | Value |
|---------|-------|
| Source | Container registry → Amazon ECR |
| Image URI | `123456789012.dkr.ecr.us-east-1.amazonaws.com/gsk-regintel:latest` |
| Deployment trigger | Automatic |
| Port | `8000` |
| CPU / Memory | 1 vCPU / 2 GB (minimum) |

Under **Environment variables**, add:

| Key | Value |
|-----|-------|
| `AZURE_OPENAI_API_KEY` | your real key |
| `AZURE_OPENAI_ENDPOINT` | `https://<YOUR-RESOURCE>.cognitiveservices.azure.com` |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` |
| `AZURE_CHAT_DEPLOYMENT` | `gpt-4o` |
| `AZURE_MINI_DEPLOYMENT` | `gpt-4o` |
| `AZURE_EMBEDDING_DEPLOYMENT` | `text-embedding-3-small` |

Click **Create & deploy**. App Runner provides an HTTPS URL when the service is live.

### Step 5 — Set up GitHub Actions CI/CD

Add these secrets to your GitHub repo (**Settings → Secrets → Actions**):

| Secret | Value |
|--------|-------|
| `AWS_ACCESS_KEY_ID` | IAM user access key |
| `AWS_SECRET_ACCESS_KEY` | IAM user secret key |
| `APPRUNNER_SERVICE_ARN` | ARN from the App Runner console |
| `FAISS_S3_BUCKET` | Bucket name from Step 1 |

Every push to `main` will now automatically build, push to ECR, and trigger an App Runner redeployment. The workflow is defined in [.github/workflows/deploy-aws.yml](.github/workflows/deploy-aws.yml).

---

## Environment Variables Reference

All credentials are supplied at runtime via environment variables — no secrets are stored in the image or repository.

| Variable | Description |
|----------|-------------|
| `AZURE_OPENAI_API_KEY` | Azure OpenAI API key |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource endpoint URL |
| `AZURE_OPENAI_API_VERSION` | API version (e.g. `2024-10-21`) |
| `AZURE_CHAT_DEPLOYMENT` | Deployment name for the chat model (e.g. `gpt-4o`) |
| `AZURE_MINI_DEPLOYMENT` | Deployment name for the fast/mini model |
| `AZURE_EMBEDDING_DEPLOYMENT` | Deployment name for the embedding model (e.g. `text-embedding-3-small`) |

For local development, set these in a `.env` file (see `.env.example`) or in `config.json`.

---

## Project Structure

```
reg-intel/
├── app.py                   # FastAPI application (SSE streaming endpoints)
├── agents/
│   ├── uc1_chatbot.py       # UC1: RAG chatbot with FAISS retrieval
│   ├── uc2_risk_analyzer.py # UC2: External regulatory risk analysis
│   └── uc3_dual_analyzer.py # UC3: Dual-source comparison analysis
├── utils/
│   ├── llm_client.py        # Azure OpenAI client factory (reads env vars)
│   └── pdf_utils.py         # PDF extraction and chunking helpers
├── static/
│   └── index.html           # Single-page frontend
├── assets/
│   ├── uc1/                 # UC1 prompt templates (.md)
│   ├── uc2/                 # UC2 prompt templates (.md)
│   └── uc3/                 # UC3 prompt templates (.md)
├── config.template.json     # Config template (safe to commit)
├── Dockerfile               # Container definition
├── .github/workflows/
│   └── deploy-aws.yml       # GitHub Actions CI/CD for AWS App Runner
└── requirements.txt
```

---

## Running the Test Suite

With the server running on port 8000:

```bash
python -X utf8 tests.py
```

Expected result: **51/51 tests passing**.
