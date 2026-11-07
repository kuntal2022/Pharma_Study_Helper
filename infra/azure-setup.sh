#!/usr/bin/env bash
# ONE-TIME Azure setup (run in Azure Cloud Shell, bash). Creates: resource group, ACR, Container Apps environment,
# Azure Cache for Redis, backend app (internal) and frontend app (public). The pipeline then only swaps images.
# Before running, export the secrets (do NOT commit them):
#   export OPENAI_API_KEY=... JWT_SECRET=... AZURE_SQL_PASSWORD=... AZURE_SEARCH_KEY=... AZURE_STORAGE_CONNECTION_STRING=... TAVILY_API_KEY=...
#   export AZURE_SQL_SERVER=xxx.database.windows.net AZURE_SQL_DATABASE=... AZURE_SQL_USERNAME=...
#   export AZURE_SEARCH_ENDPOINT=https://xxx.search.windows.net AZURE_SEARCH_INDEX_NAME=pharma-study-chunks
set -euo pipefail

SUFFIX="${SUFFIX:-$RANDOM}"
RG="${RG:-rg-pharma-study}"; LOC="${LOC:-centralindia}"
ACR="${ACR:-pharmaacr$SUFFIX}"; ENVN="${ENVN:-cae-pharma}"; REDIS="${REDIS:-pharma-redis-$SUFFIX}"
BACK=pharma-backend; FRONT=pharma-frontend
for v in OPENAI_API_KEY JWT_SECRET AZURE_SQL_PASSWORD AZURE_SEARCH_KEY AZURE_STORAGE_CONNECTION_STRING TAVILY_API_KEY \
         AZURE_SQL_SERVER AZURE_SQL_DATABASE AZURE_SQL_USERNAME AZURE_SEARCH_ENDPOINT AZURE_SEARCH_INDEX_NAME; do
  [ -n "${!v:-}" ] || { echo "Missing env var: $v"; exit 1; }
done

az extension add --name containerapp --upgrade -y
az provider register -n Microsoft.App --wait; az provider register -n Microsoft.OperationalInsights --wait

az group create -n "$RG" -l "$LOC"
az acr create -n "$ACR" -g "$RG" --sku Basic
az containerapp env create -n "$ENVN" -g "$RG" -l "$LOC"

# Redis (answer cache + chat memory). TLS port 6380 -> rediss://
az redis create -n "$REDIS" -g "$RG" -l "$LOC" --sku Basic --vm-size c0
RHOST=$(az redis show -n "$REDIS" -g "$RG" --query hostName -o tsv)
RKEY=$(az redis list-keys -n "$REDIS" -g "$RG" --query primaryKey -o tsv)
REDIS_URL="rediss://:${RKEY}@${RHOST}:6380/0"

# Backend: internal only (the frontend reaches it as http://pharma-backend). Starts with a placeholder image; the pipeline replaces it.
az containerapp create -n "$BACK" -g "$RG" --environment "$ENVN" \
  --image mcr.microsoft.com/k8se/quickstart:latest --target-port 8000 --ingress internal \
  --system-assigned --min-replicas 1 --max-replicas 3 --cpu 1.0 --memory 2.0Gi \
  --secrets openai-key="$OPENAI_API_KEY" jwt="$JWT_SECRET" sqlpw="$AZURE_SQL_PASSWORD" searchkey="$AZURE_SEARCH_KEY" \
            storage="$AZURE_STORAGE_CONNECTION_STRING" tavily="$TAVILY_API_KEY" redis="$REDIS_URL" \
  --env-vars OPENAI_API_KEY=secretref:openai-key JWT_SECRET=secretref:jwt AZURE_SQL_PASSWORD=secretref:sqlpw \
             AZURE_SEARCH_KEY=secretref:searchkey AZURE_STORAGE_CONNECTION_STRING=secretref:storage \
             TAVILY_API_KEY=secretref:tavily REDIS_URL=secretref:redis \
             AZURE_SQL_SERVER="$AZURE_SQL_SERVER" AZURE_SQL_DATABASE="$AZURE_SQL_DATABASE" AZURE_SQL_USERNAME="$AZURE_SQL_USERNAME" \
             AZURE_SEARCH_ENDPOINT="$AZURE_SEARCH_ENDPOINT" AZURE_SEARCH_INDEX_NAME="$AZURE_SEARCH_INDEX_NAME" \
             AZURE_STORAGE_CONTAINER=pharmastudy OPENAI_CHAT_MODEL=gpt-4.1-mini OPENAI_EMBEDDING_MODEL=text-embedding-3-small

# Frontend: public
az containerapp create -n "$FRONT" -g "$RG" --environment "$ENVN" \
  --image mcr.microsoft.com/k8se/quickstart:latest --target-port 8080 --ingress external \
  --system-assigned --min-replicas 1 --max-replicas 2 --cpu 0.25 --memory 0.5Gi \
  --env-vars BACKEND_URL=http://$BACK

# Let both apps pull from ACR with their managed identity (no registry password)
ACR_ID=$(az acr show -n "$ACR" --query id -o tsv)
for APP in "$BACK" "$FRONT"; do
  PID=$(az containerapp show -n "$APP" -g "$RG" --query identity.principalId -o tsv)
  az role assignment create --assignee-object-id "$PID" --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR_ID"
  az containerapp registry set -n "$APP" -g "$RG" --server "$ACR.azurecr.io" --identity system
done

# Azure SQL must accept the backend: add these outbound IPs to the SQL server firewall (or tick "Allow Azure services")
echo "Backend outbound IPs (add to Azure SQL firewall): $(az containerapp show -n "$BACK" -g "$RG" --query properties.outboundIpAddresses -o tsv)"
echo
echo "DONE. Put these in azure-pipelines.yml:  acrName: $ACR   resourceGroup: $RG"
echo "Frontend URL: https://$(az containerapp show -n "$FRONT" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
