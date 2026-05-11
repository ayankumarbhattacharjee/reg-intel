# GSK RegIntel PoV — Learnings & Context

## What We Built

A **unified FastAPI web app** that combines three GSK Regulatory Intelligence use cases into one server and one single-page frontend.  The previous approach used three separate Streamlit scripts; this replaces all of them.

| Use Case | Endpoint | Description |
|----------|----------|-------------|
| UC1 — Regulatory Chatbot | `POST /api/uc1/chat` | RAG chatbot over a 12,957-chunk FAISS knowledge base |
| UC2 — External RegIntel Risk Analyzer | `POST /api/uc2/analyze` | Compares an external regulatory update against an internal SOP |
| UC3 — Dual-Source Analyzer | `POST /api/uc3/analyze` | Compares external *and* internal documents against an SOP |

All three stream results to the browser via **Server-Sent Events (SSE)**.

---

## Architecture Decisions

### FastAPI + SSE instead of Streamlit
Streamlit re-runs the entire script on every widget interaction and is hard to embed in a professional UI.  FastAPI gives us full control over streaming, concurrent requests, and a clean REST API that can be tested and deployed like any service.

### Local FAISS instead of Pinecone
Pinecone requires an external account and adds latency/cost for a PoV.  FAISS runs entirely in-process, the index is pre-built from `Chunks_Complete.pkl` (12,957 chunks), and subsequent starts load in ~4 s from disk.

### asyncio.to_thread for FAISS loading
`load_uc1_assets()` loads the FAISS index (~4 s) synchronously.  Calling it directly inside an `async` FastAPI endpoint blocks the entire event loop, freezing all concurrent requests.  The fix is `await asyncio.to_thread(load_uc1_assets)`.

### asyncio.get_running_loop() not get_event_loop()
Python 3.10+ deprecates `asyncio.get_event_loop()` inside a running async context; Python 3.13 raises a `DeprecationWarning` that becomes an error.  Always use `asyncio.get_running_loop()` inside `async def` functions.

---

## Key Debugging Learnings

### Python http.client chunked-transfer read accumulation
`http.client.HTTPResponse.read(n)` with chunked transfer encoding **loops internally** until exactly `n` bytes have been accumulated — it does *not* return after the first chunk arrives.

**Symptom:** Test calls `r.read(512)` on an SSE stream. The first pre-LLM status event is only ~57 bytes. The client waits for 455 more bytes, which never arrive until the LLM finishes (~15 s) → `TimeoutError`.

**Fix:** In tests, `read()` only as many bytes as the *first* SSE event: `r.read(40)` for UC1 (57-byte first event), `r.read(80)` for UC2/UC3.

### Cold-import latency on Windows
The first HTTP request to `/api/uc2/topics` imports `agents.uc2_risk_analyzer`, which in turn imports LangChain + Pydantic chains.  On Windows/Python 3.13 this takes 10–20 s.  Set test timeouts to ≥ 30 s for the first call.

### Windows Git Bash path mangling
In Git Bash, `/F` in a command like `taskkill /F /PID 1234` is rewritten to `F:/PID 1234` by MSYS path conversion.  Fix: call `taskkill` via Python subprocess or prefix with `cmd /c`.

---

## Test Suite

51/51 tests in `tests.py` pass against a running server.

```
python -m uvicorn app:app --host 0.0.0.0 --port 8000
python -X utf8 tests.py
```

---

## Deploying to AWS — Step-by-Step

### Prerequisites

| Tool | Install |
|------|---------|
| AWS CLI v2 | https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html |
| Docker Desktop | https://docs.docker.com/get-docker/ |
| AWS account with permissions for ECR + App Runner + S3 | — |

```
aws configure   # enter Access Key, Secret Key, region (e.g. us-east-1)
```

---

### Step 1 — Store the FAISS index in S3

The FAISS index is 84 MB and excluded from git.  Upload it once to S3 so CI can pull it during builds.

```bash
aws s3 mb s3://gsk-regintel-assets              # create bucket (name must be globally unique)
aws s3 cp uc1_faiss_index/ s3://gsk-regintel-assets/uc1_faiss_index/ --recursive
```

---

### Step 2 — Create an ECR repository

```bash
aws ecr create-repository --repository-name gsk-regintel --region us-east-1
# Note the repositoryUri printed, e.g.:
#   123456789012.dkr.ecr.us-east-1.amazonaws.com/gsk-regintel
```

---

### Step 3 — Build & push the Docker image locally (first time)

```bash
# Authenticate Docker to ECR
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin \
    123456789012.dkr.ecr.us-east-1.amazonaws.com

# Build (from the project root — uc1_faiss_index/ must exist here)
docker build -t gsk-regintel .

# Tag and push
docker tag  gsk-regintel:latest 123456789012.dkr.ecr.us-east-1.amazonaws.com/gsk-regintel:latest
docker push               123456789012.dkr.ecr.us-east-1.amazonaws.com/gsk-regintel:latest
```

---

### Step 4 — Create an App Runner service

In the AWS Console → **App Runner → Create service**:

| Setting | Value |
|---------|-------|
| Source | Container registry → Amazon ECR |
| Image URI | `123456789012.dkr.ecr.us-east-1.amazonaws.com/gsk-regintel:latest` |
| Deployment trigger | Automatic (on push to ECR) |
| Port | `8000` |
| CPU / Memory | 1 vCPU / 2 GB (minimum; increase for production) |

Under **Environment variables**, add:

| Key | Value |
|-----|-------|
| `AZURE_OPENAI_API_KEY` | your real key |
| `AZURE_OPENAI_ENDPOINT` | `https://YOUR-RESOURCE.cognitiveservices.azure.com` |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` |
| `AZURE_CHAT_DEPLOYMENT` | `gpt-4o` |
| `AZURE_MINI_DEPLOYMENT` | `gpt-4o` |
| `AZURE_EMBEDDING_DEPLOYMENT` | `text-embedding-3-small` |

Click **Create & deploy**.  App Runner provisions the service and gives you an HTTPS URL (e.g. `https://abc123.us-east-1.awsapprunner.com`).

---

### Step 5 — Set up GitHub Actions for continuous deployment

Add the following **GitHub repository secrets** (Settings → Secrets → Actions):

| Secret | Value |
|--------|-------|
| `AWS_ACCESS_KEY_ID` | IAM user access key with ECR + App Runner + S3 read permissions |
| `AWS_SECRET_ACCESS_KEY` | corresponding secret |
| `APPRUNNER_SERVICE_ARN` | ARN from the App Runner console (starts with `arn:aws:apprunner:…`) |
| `FAISS_S3_BUCKET` | bucket name from Step 1 (e.g. `gsk-regintel-assets`) |

Every push to `main` now:
1. Downloads the FAISS index from S3
2. Builds the Docker image
3. Pushes to ECR
4. Triggers an App Runner redeployment

The workflow file is at [.github/workflows/deploy-aws.yml](.github/workflows/deploy-aws.yml).

---

### IAM permissions required for the CI user

Attach an inline policy to the IAM user used by GitHub Actions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["apprunner:StartDeployment"],
      "Resource": "arn:aws:apprunner:*:*:service/gsk-regintel/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::gsk-regintel-assets",
        "arn:aws:s3:::gsk-regintel-assets/*"
      ]
    }
  ]
}
```

---

## Security Notes

- `config.json` is in `.gitignore` — it must **never** be committed.
- The Azure OpenAI API key lives only in App Runner environment variables (encrypted at rest by AWS) and GitHub Actions secrets (encrypted by GitHub).
- The Docker image itself contains **no credentials** — `utils/llm_client.py` reads from env vars at runtime.
- `config.template.json` is safe to commit; it contains no real keys.
- The FAISS index contains no PII or credentials — it is safe to store in S3.
