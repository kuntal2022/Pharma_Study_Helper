# One-time setup for the AKS pipeline

Run in Azure Cloud Shell (bash). Replace the three names.

```bash
RG=<resource-group-of-aks>   AKS=<aks-name>   ACR=<acr-name>

# 1) let the cluster pull images from your ACR (no image-pull secret needed)
az aks update -g $RG -n $AKS --attach-acr $ACR

# 2) check the cluster is reachable
az aks get-credentials -g $RG -n $AKS --admin && kubectl get nodes
```

## Azure DevOps (once)
1. **Service connections** (Project settings > Service connections):
   - Docker Registry -> Azure Container Registry -> your ACR, name it `sc-pharma-acr`
   - Azure Resource Manager (automatic) -> scope: resource group of the AKS, name it `sc-pharma-azure`
2. **Variable group** `pharma-secrets` (Pipelines > Library). Add these and lock each one (padlock = secret):
   `OPENAI_API_KEY`, `JWT_SECRET`, `AZURE_SQL_PASSWORD`, `AZURE_SEARCH_KEY`, `AZURE_STORAGE_CONNECTION_STRING`, `TAVILY_API_KEY`
   Then open the pipeline's first run and click "Permit" for the variable group.
3. **Environment** `pharma-prod` (Pipelines > Environments). Optional: add an Approval check.
4. In the repo: put your real values in `k8s/10-config.yaml` (SQL server/db/user, search endpoint) and the three names at the top of `azure-pipelines.yml`.
5. Pipelines > New pipeline > Azure Repos Git > Existing YAML file > `/azure-pipelines.yml`.

## After the first deploy
- Public address: `kubectl -n pharma get svc frontend` (EXTERNAL-IP). Open `http://<that-ip>`.
- Azure SQL firewall: allow the AKS outbound IP, or tick "Allow Azure services":
  `az aks show -g $RG -n $AKS --query "networkProfile.loadBalancerProfile.effectiveOutboundIPs[].id" -o tsv`
- Logs: `kubectl -n pharma logs deploy/backend --tail=100`
