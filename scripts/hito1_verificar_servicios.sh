#!/usr/bin/env bash
# HITO 1 · Verificación de servicios en el Learner Lab
# Crea un recurso MÍNIMO de cada servicio que usa el reto, lo prueba y lo elimina.
# Deja evidencia fechada en evidencias/hito1_<fecha>.log
#   ./scripts/hito1_verificar_servicios.sh
set -uo pipefail
REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$REGION"
cd "$(dirname "$0")/.."
mkdir -p evidencias
TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG="evidencias/hito1_${TS}.log"
exec > >(tee "$LOG") 2>&1

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/LabRole"
SUF=$(date +%s)
B="idp-hito1-${ACCOUNT}-${SUF}"; Q="idp-hito1-${SUF}"; T="idp-hito1-${SUF}"; F="idp-hito1-${SUF}"; TOP="idp-hito1-${SUF}"
PASS=0; FAILN=0
echo "HITO 1 · Verificación de servicios · $(date -u +%FT%TZ) · región ${REGION} · cuenta ****${ACCOUNT: -4}"
echo "-----------------------------------------------------------------------"

check() { local name="$1"; shift
  if out=$("$@" 2>&1); then echo "[OK]   $name"; PASS=$((PASS+1)); else echo "[FAIL] $name -> $(echo "$out" | tail -1)"; FAILN=$((FAILN+1)); fi; }

cleanup() {
  echo "-- limpieza de recursos temporales"
  aws s3 rm "s3://$B" --recursive >/dev/null 2>&1; aws s3api delete-bucket --bucket "$B" >/dev/null 2>&1
  aws sqs delete-queue --queue-url "$(aws sqs get-queue-url --queue-name "$Q" --query QueueUrl --output text 2>/dev/null)" >/dev/null 2>&1
  aws dynamodb delete-table --table-name "$T" >/dev/null 2>&1
  aws lambda delete-function --function-name "$F" >/dev/null 2>&1
  aws logs delete-log-group --log-group-name "/aws/lambda/$F" >/dev/null 2>&1
  [ -n "${API:-}" ] && aws apigateway delete-rest-api --rest-api-id "$API" >/dev/null 2>&1
  [ -n "${TOPIC:-}" ] && aws sns delete-topic --topic-arn "$TOPIC" >/dev/null 2>&1
  rm -f /tmp/hito1_fn.zip /tmp/hito1_fn.py /tmp/hito1_out.json
  echo "-- limpieza terminada"
}
trap cleanup EXIT

check "IAM: LabRole existe" aws iam get-role --role-name LabRole

# S3 privado + subida de una factura de prueba
check "S3: crear bucket privado" aws s3api create-bucket --bucket "$B"
check "S3: bloquear acceso público" aws s3api put-public-access-block --bucket "$B" --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
check "S3: subir factura de prueba" aws s3 cp datos/muestras/01_factura_limpia.png "s3://$B/incoming/01.png"

# Textract (servicio crítico del reto)
check "Textract: DetectDocumentText" aws textract detect-document-text --document "{\"S3Object\":{\"Bucket\":\"$B\",\"Name\":\"incoming/01.png\"}}"
if aws textract analyze-expense --document "{\"S3Object\":{\"Bucket\":\"$B\",\"Name\":\"incoming/01.png\"}}" > /tmp/hito1_out.json 2>/dev/null; then
  echo "[OK]   Textract: AnalyzeExpense"; PASS=$((PASS+1))
  python3 - <<'PY'
import json
r = json.load(open("/tmp/hito1_out.json"))
for d in r.get("ExpenseDocuments", []):
    for f in d.get("SummaryFields", []):
        t = f.get("Type", {}).get("Text"); l = (f.get("LabelDetection") or {}).get("Text", ""); v = (f.get("ValueDetection") or {}).get("Text", "")
        c = (f.get("ValueDetection") or {}).get("Confidence", 0)
        print(f"         {t:<22} {l[:24]:<24} = {v[:40]:<40} {c:5.1f}%")
PY
else
  echo "[FAIL] Textract: AnalyzeExpense -> usar ruta alternativa (Tesseract en contenedor Lambda/EC2)"; FAILN=$((FAILN+1))
fi

# SQS
check "SQS: crear cola" aws sqs create-queue --queue-name "$Q" --tags proyecto=final
QURL=$(aws sqs get-queue-url --queue-name "$Q" --query QueueUrl --output text 2>/dev/null)
check "SQS: enviar mensaje" aws sqs send-message --queue-url "$QURL" --message-body '{"prueba":true}'
check "SQS: recibir mensaje" aws sqs receive-message --queue-url "$QURL" --wait-time-seconds 2

# DynamoDB
check "DynamoDB: crear tabla on-demand" aws dynamodb create-table --table-name "$T" --billing-mode PAY_PER_REQUEST \
  --attribute-definitions AttributeName=docId,AttributeType=S --key-schema AttributeName=docId,KeyType=HASH --tags Key=proyecto,Value=final
check "DynamoDB: esperar tabla activa" aws dynamodb wait table-exists --table-name "$T"
check "DynamoDB: put/get item" bash -c "aws dynamodb put-item --table-name $T --item '{\"docId\":{\"S\":\"x\"}}' && aws dynamodb get-item --table-name $T --key '{\"docId\":{\"S\":\"x\"}}'"

# Lambda con LabRole
printf 'def handler(e, c):\n    return {"ok": True}\n' > /tmp/hito1_fn.py
(cd /tmp && zip -q hito1_fn.zip hito1_fn.py)
check "Lambda: crear función python3.12 con LabRole" aws lambda create-function --function-name "$F" --runtime python3.12 \
  --handler hito1_fn.handler --role "$ROLE_ARN" --zip-file fileb:///tmp/hito1_fn.zip --tags proyecto=final
check "Lambda: esperar función activa" aws lambda wait function-active-v2 --function-name "$F"
check "Lambda: invocar" aws lambda invoke --function-name "$F" /tmp/hito1_out.json

# API Gateway REST + clave
API=$(aws apigateway create-rest-api --name "$F" --query id --output text 2>/dev/null)
[ -n "$API" ] && { echo "[OK]   API Gateway: crear REST API"; PASS=$((PASS+1)); } || { echo "[FAIL] API Gateway: crear REST API"; FAILN=$((FAILN+1)); }

# SNS y CloudWatch
TOPIC=$(aws sns create-topic --name "$TOP" --query TopicArn --output text 2>/dev/null)
[ -n "$TOPIC" ] && { echo "[OK]   SNS: crear tema"; PASS=$((PASS+1)); } || { echo "[FAIL] SNS: crear tema"; FAILN=$((FAILN+1)); }
check "CloudWatch: publicar métrica" aws cloudwatch put-metric-data --namespace IDP/Hito1 --metric-name Prueba --value 1
check "CloudWatch Logs: listar grupos" aws logs describe-log-groups --limit 1
check "CloudFormation: validar plantilla" aws cloudformation validate-template --template-body file://infra/template.yaml

echo "-----------------------------------------------------------------------"
echo "Resultado: ${PASS} OK · ${FAILN} FALLIDOS"
[ "$FAILN" -eq 0 ] && echo "Ruta principal viable: no se necesita ruta alternativa." || echo "Revisar fallos: documentar ruta alternativa en el informe."
echo "Evidencia: $LOG"
