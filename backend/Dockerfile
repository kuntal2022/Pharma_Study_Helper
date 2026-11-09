# syntax=docker/dockerfile:1
# BACKEND image (FastAPI API only; the UI is served by the frontend container)
#   build (from the project root): docker build -f backend/Dockerfile -t pharma-backend .
#   normally started through docker-compose.yml
# Secrets (.env) are NOT copied into the image; pass them at run time.

ARG PYTHON_VERSION=3.12

# ---------- stage 1: build the Python packages (compilers live only here) ----------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential unixodbc-dev \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements-api.txt .
# CPU-only torch first (the default wheel pulls ~3 GB of CUDA libraries the pod never uses; sentence-transformers then reuses it)
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install -r requirements-api.txt
# semantic cache model baked into the image (no download from Hugging Face at runtime)
ENV HF_HOME=/opt/hf
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

# ---------- stage 2: small runtime image ----------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/opt/venv/bin:$PATH"

# ODBC Driver 18 for SQL Server (needed by pyodbc to reach Azure SQL)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg ca-certificates unixodbc \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hf /opt/hf
ENV HF_HOME=/opt/hf

# run as a normal user, not root
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --chown=app:app backend ./backend
COPY --chown=app:app application_work ./application_work
USER app
WORKDIR /app/backend

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/openapi.json', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
