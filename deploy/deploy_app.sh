#!/usr/bin/env bash
# Build the web app image and roll it out to Azure Container Apps.
# Run ./deploy/provision_app.sh first if the infrastructure does not exist yet.
#
# Usage:  ./deploy/deploy_app.sh [tag]
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] || { echo "No .env found. Copy .env.example to .env and fill it in."; exit 1; }
set -a; . ./.env; set +a

: "${AZURE_RESOURCE_GROUP:?set in .env}"
: "${ACR_NAME:?set in .env}"
: "${CONTAINERAPP_NAME:?set in .env}"
: "${IMAGE_NAME:?set in .env}"

TAG="${1:-$(date +%Y%m%d-%H%M%S)}"

echo "Building ${IMAGE_NAME}:${TAG} in ACR ${ACR_NAME}…"
# Built in Azure rather than locally: no Docker daemon needed, and the image is
# always linux/amd64 regardless of the developer's machine.
az acr build \
  --registry "$ACR_NAME" \
  --image "${IMAGE_NAME}:${TAG}" \
  --image "${IMAGE_NAME}:latest" \
  --platform linux/amd64 \
  . >/dev/null

echo "Rolling out to ${CONTAINERAPP_NAME}…"
az containerapp update \
  --name "$CONTAINERAPP_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --image "${ACR_NAME}.azurecr.io/${IMAGE_NAME}:${TAG}" \
  -o none

FQDN=$(az containerapp show -n "$CONTAINERAPP_NAME" -g "$AZURE_RESOURCE_GROUP" \
  --query "properties.configuration.ingress.fqdn" -o tsv)

echo
echo "Deployed ${IMAGE_NAME}:${TAG}"
echo "  https://${FQDN}"
echo
echo "Sign-in is required, so a browser request should return 302:"
echo "  curl -s -o /dev/null -w '%{http_code}\\n' -A Mozilla -H 'Accept: text/html' https://${FQDN}/"
