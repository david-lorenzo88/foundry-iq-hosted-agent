#!/usr/bin/env bash
# Provision the Azure resources that host the web app, and put Entra sign-in in
# front of it. Idempotent: every step checks first, so re-running is safe and is
# the quickest way to verify an existing deployment still matches this script.
#
# It does NOT create the Foundry project, the model deployment, the Azure AI Search
# service or the knowledge base -- see "Fresh setup" in the README for those.
#
# Usage:  ./deploy/provision_app.sh
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] || { echo "No .env found. Copy .env.example to .env and fill it in."; exit 1; }
set -a; . ./.env; set +a

: "${AZURE_RESOURCE_GROUP:?set in .env}"
: "${AZURE_LOCATION:?set in .env}"
: "${ACR_NAME:?set in .env}"
: "${CONTAINERAPP_ENV:?set in .env}"
: "${CONTAINERAPP_NAME:?set in .env}"
: "${IMAGE_NAME:?set in .env}"
: "${FOUNDRY_PROJECT_ENDPOINT:?set in .env}"
: "${FOUNDRY_HOSTED_AGENT_NAME:?set in .env}"
: "${AZURE_AI_MODEL_DEPLOYMENT_NAME:?set in .env}"
: "${FOUNDRY_PROJECT_RESOURCE_ID:?set in .env}"

RG="$AZURE_RESOURCE_GROUP"
# The Foundry ACCOUNT is the parent of the project; role assignments go there.
FOUNDRY_ACCOUNT_ID="${FOUNDRY_PROJECT_RESOURCE_ID%/projects/*}"
ACR_ID=$(az acr show -n "$ACR_NAME" -g "$RG" --query id -o tsv)

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
have() { [ -n "${1:-}" ] && [ "$1" != "null" ]; }

step "Container Apps environment: $CONTAINERAPP_ENV"
if az containerapp env show -n "$CONTAINERAPP_ENV" -g "$RG" -o none 2>/dev/null; then
  echo "    already exists"
else
  az containerapp env create -n "$CONTAINERAPP_ENV" -g "$RG" \
    --location "$AZURE_LOCATION" --logs-destination none -o none
  echo "    created"
fi

step "Container app: $CONTAINERAPP_NAME"
if az containerapp show -n "$CONTAINERAPP_NAME" -g "$RG" -o none 2>/dev/null; then
  echo "    already exists"
else
  # Needs an image to start from. Build one first if the tag is missing.
  if ! az acr repository show -n "$ACR_NAME" --image "${IMAGE_NAME}:latest" -o none 2>/dev/null; then
    echo "    building ${IMAGE_NAME}:latest first…"
    az acr build --registry "$ACR_NAME" --image "${IMAGE_NAME}:latest" --platform linux/amd64 . >/dev/null
  fi
  az containerapp create -n "$CONTAINERAPP_NAME" -g "$RG" \
    --environment "$CONTAINERAPP_ENV" \
    --image "${ACR_NAME}.azurecr.io/${IMAGE_NAME}:latest" \
    --system-assigned \
    --registry-server "${ACR_NAME}.azurecr.io" --registry-identity system \
    --target-port 8000 --ingress external \
    --min-replicas 0 --max-replicas 2 --cpu 0.5 --memory 1Gi \
    --env-vars \
      FOUNDRY_PROJECT_ENDPOINT="$FOUNDRY_PROJECT_ENDPOINT" \
      FOUNDRY_HOSTED_AGENT_NAME="$FOUNDRY_HOSTED_AGENT_NAME" \
      AZURE_AI_MODEL_DEPLOYMENT_NAME="$AZURE_AI_MODEL_DEPLOYMENT_NAME" \
    -o none
  echo "    created"
fi

MI=$(az containerapp show -n "$CONTAINERAPP_NAME" -g "$RG" --query identity.principalId -o tsv)
FQDN=$(az containerapp show -n "$CONTAINERAPP_NAME" -g "$RG" \
  --query properties.configuration.ingress.fqdn -o tsv)
echo "    managed identity: $MI"
echo "    url: https://$FQDN"

assign_role() {  # <role> <scope>
  local role="$1" scope="$2"
  local n
  n=$(az role assignment list --assignee "$MI" --scope "$scope" \
        --query "[?roleDefinitionName=='$role'] | length(@)" -o tsv 2>/dev/null || echo 0)
  if [ "${n:-0}" != "0" ]; then
    echo "    '$role' already assigned"
  else
    az role assignment create --assignee-object-id "$MI" \
      --assignee-principal-type ServicePrincipal --role "$role" --scope "$scope" -o none
    echo "    '$role' assigned"
  fi
}

step "Role assignments for the container app identity"
# Pulls the image from the registry.
assign_role "AcrPull" "$ACR_ID"
# Calls the hosted agent's endpoint.
assign_role "Foundry User" "$FOUNDRY_ACCOUNT_ID"

step "Entra app registration for sign-in"
APP_NAME="${ENTRA_APP_NAME:-$CONTAINERAPP_NAME-auth}"
REDIRECT="https://$FQDN/.auth/login/aad/callback"
APP_ID=$(az ad app list --display-name "$APP_NAME" --query "[0].appId" -o tsv 2>/dev/null || true)
if have "$APP_ID"; then
  echo "    reusing '$APP_NAME' ($APP_ID)"
  # Make sure the redirect URI matches this app's hostname.
  az ad app update --id "$APP_ID" --web-redirect-uris "$REDIRECT" --enable-id-token-issuance true -o none
else
  APP_ID=$(az ad app create --display-name "$APP_NAME" \
    --sign-in-audience AzureADMyOrg \
    --web-redirect-uris "$REDIRECT" \
    --enable-id-token-issuance true \
    --query appId -o tsv)
  echo "    created '$APP_NAME' ($APP_ID)"
fi
az ad sp show --id "$APP_ID" -o none 2>/dev/null || az ad sp create --id "$APP_ID" -o none

step "Built-in authentication (Easy Auth)"
CONFIGURED=$(az containerapp auth show -n "$CONTAINERAPP_NAME" -g "$RG" \
  --query "identityProviders.azureActiveDirectory.registration.clientId" -o tsv 2>/dev/null || true)
if [ "$CONFIGURED" = "$APP_ID" ]; then
  echo "    already configured for $APP_ID"
else
  # A client secret is required: without it the auth sidecar cannot complete its
  # configuration and every request to the app returns 503.
  TENANT=$(az account show --query tenantId -o tsv)
  SECRET=$(az ad app credential reset --id "$APP_ID" \
    --display-name "${APP_NAME}-easyauth" --years 1 --query password -o tsv)
  az containerapp auth microsoft update -n "$CONTAINERAPP_NAME" -g "$RG" \
    --client-id "$APP_ID" --client-secret "$SECRET" --tenant-id "$TENANT" --yes -o none
  echo "    provider configured (client secret expires in 1 year)"
fi

az containerapp auth update -n "$CONTAINERAPP_NAME" -g "$RG" \
  --enabled true --action RedirectToLoginPage \
  --redirect-provider azureactivedirectory --require-https true -o none
echo "    sign-in required for all requests"

step "Done"
echo "    App:      https://$FQDN"
echo "    Identity: $MI"
echo "    Entra app: $APP_NAME ($APP_ID)"
echo
echo "    Deploy code changes with:  ./deploy/deploy_app.sh"
