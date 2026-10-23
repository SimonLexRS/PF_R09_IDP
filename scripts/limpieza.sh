#!/usr/bin/env bash
# Elimina TODO lo creado por el proyecto y deja evidencia en evidencias/limpieza_<fecha>.log
#   ./scripts/limpieza.sh
set -uo pipefail

STACK="${STACK:-sars-r09}"
REGION="${AWS_REGION:-us-east-1}"
cd "$(dirname "$0")/.."
mkdir -p evidencias
LOG="evidencias/limpieza_$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee "$LOG") 2>&1

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${DOCS_BUCKET:-sars-r09-docs-${ACCOUNT:0:4}}"
ART="${ART_BUCKET:-ucb-proyecto-sars-${ACCOUNT:0:4}}"
echo "Limpieza · $(date -u +%FT%TZ) · región ${REGION} · cuenta ****${ACCOUNT: -4}"

empty_bucket() {  # borra todas las versiones (el bucket tiene versionado)
  python3 - "$1" "$REGION" <<'PY'
import sys, boto3
b = boto3.resource("s3", region_name=sys.argv[2]).Bucket(sys.argv[1])
try:
    n = sum(len(r.get("Deleted", [])) for r in b.object_versions.delete())
    print(f"  {sys.argv[1]}: {n} objetos/versiones eliminados")
except Exception as e:
    print(f"  {sys.argv[1]}: {e}")
PY
}

echo "1) Vaciando buckets"
empty_bucket "$BUCKET"
empty_bucket "$ART"

echo "2) Eliminando stack $STACK"
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK" && echo "  stack eliminado"

echo "3) Eliminando bucket de artefactos"
aws s3api delete-bucket --region "$REGION" --bucket "$ART" 2>/dev/null && echo "  $ART eliminado" || echo "  $ART no existe"

echo "4) Verificación"
aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" >/dev/null 2>&1 \
  && echo "  [!] el stack todavía existe" || echo "  OK: stack inexistente"
aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1 && echo "  [!] bucket $BUCKET existe" || echo "  OK: bucket de documentos inexistente"
aws lambda list-functions --region "$REGION" --query "Functions[?starts_with(FunctionName, '${STACK}-')].FunctionName" --output text \
  | grep -q . && echo "  [!] quedan funciones Lambda" || echo "  OK: sin funciones Lambda del proyecto"
aws dynamodb list-tables --region "$REGION" --query "TableNames[?starts_with(@, '${STACK}-')]" --output text \
  | grep -q . && echo "  [!] quedan tablas" || echo "  OK: sin tablas DynamoDB del proyecto"
aws sqs list-queues --region "$REGION" --queue-name-prefix "$STACK" --query 'QueueUrls' --output text 2>/dev/null \
  | grep -q http && echo "  [!] quedan colas" || echo "  OK: sin colas SQS del proyecto"
echo "  Recursos con etiqueta proyecto=final:"
aws resourcegroupstaggingapi get-resources --region "$REGION" --tag-filters Key=proyecto,Values=final \
  --query 'ResourceTagMappingList[].ResourceARN' --output text 2>/dev/null || echo "  (API de etiquetas no disponible en el lab)"
echo "Fin. Evidencia en $LOG — adjunta también la captura del consumo del crédito del Learner Lab."
