# One-time setup for the AKS pipeline

Run in Azure Cloud Shell (bash). Replace the three names.

```bash
RG=<resource-group-of-aks>   AKS=<aks-name>   ACR=<acr-name>

# 1) let the cluster pull images from your ACR (no image-pull secret needed)
az aks update -g $RG -n $AKS --attach-acr $ACR

# 2) check the cluster is reachable
az aks get-credentials -g $RG -n $AKS --admin && kubectl get nodes
```

## Azure Cache for Redis (once, takes ~15-20 min)
```bash
REDIS=<unique-name>   LOC=<same-region-as-aks>
az redis create -n $REDIS -g $RG -l $LOC --sku Basic --vm-size c0
HOST=$(az redis show -n $REDIS -g $RG --query hostName -o tsv)
KEY=$(az redis list-keys -n $REDIS -g $RG --query primaryKey -o tsv)
echo "rediss://:${KEY}@${HOST}:6380/0"      # paste this as the secret variable REDIS_URL (note: rediss with 2 s, port 6380)
```
`k8s/20-redis.yaml` (Redis inside the cluster) is no longer applied by the pipeline; you can delete that file.

## Azure DevOps (once)
1. **Service connections** (Project settings > Service connections):
   - Docker Registry -> Azure Container Registry -> your ACR, name it `sc-pharma-acr`
   - Azure Resource Manager (automatic) -> scope: resource group of the AKS, name it `sc-pharma-azure`
2. **Variable group** `pharma-secrets` (Pipelines > Library). Add these and lock each one (padlock = secret):
   `OPENAI_API_KEY`, `JWT_SECRET`, `AZURE_SQL_PASSWORD`, `AZURE_SEARCH_KEY`, `AZURE_STORAGE_CONNECTION_STRING`, `TAVILY_API_KEY`, `REDIS_URL`
   Then open the pipeline's first run and click "Permit" for the variable group.
3. **Environment** `pharma-prod` (Pipelines > Environments). Optional: add an Approval check.
4. In the repo: put your real values in `k8s/10-config.yaml` (SQL server/db/user, search endpoint) and the three names at the top of `azure-pipelines.yml`.
5. Pipelines > New pipeline > Azure Repos Git > Existing YAML file > `/azure-pipelines.yml`.

## After the first deploy
- Public address: `kubectl -n pharma get svc frontend` (EXTERNAL-IP). Open `http://<that-ip>`.
- Azure SQL firewall: allow the AKS outbound IP, or tick "Allow Azure services":
  `az aks show -g $RG -n $AKS --query "networkProfile.loadBalancerProfile.effectiveOutboundIPs[].id" -o tsv`
- Logs: `kubectl -n pharma logs deploy/backend --tail=100`
