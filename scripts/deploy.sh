#!/usr/bin/env bash
# Despliegue con UN comando (CloudShell o terminal con credenciales del Learner Lab):
#   ./scripts/deploy.sh                     # stack sars-r09 en us-east-1
#   ALERT_EMAIL=tu@ucb.edu.bo ./scripts/deploy.sh
# Variables: STACK (sars-r09), DOCS_BUCKET, ART_BUCKET, AWS_REGION (us-east-1), THRESHOLD (90), ALERT_EMAIL (vacío), FAULT_INJECTION (true)
set -euo pipefail

STACK="${STACK:-sars-r09}"
REGION="${AWS_REGION:-us-east-1}"
THRESHOLD="${THRESHOLD:-90}"
ALERT_EMAIL="${ALERT_EMAIL:-}"
FAULT_INJECTION="${FAULT_INJECTION:-true}"
cd "$(dirname "$0")/.."

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
DOCS_BUCKET="${DOCS_BUCKET:-sars-r09-docs-${ACCOUNT:0:4}}"
ART="${ART_BUCKET:-ucb-proyecto-sars-${ACCOUNT:0:4}}"
echo "==> Cuenta ****${ACCOUNT: -4} · región ${REGION} · stack ${STACK}"

# Bucket privado para los artefactos (zip de las Lambdas)
if ! aws s3api head-bucket --bucket "$ART" 2>/dev/null; then
  echo "==> Creando bucket de artefactos $ART"
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$ART" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$ART" --region "$REGION" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "$ART" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-tagging --bucket "$ART" --tagging 'TagSet=[{Key=proyecto,Value=final}]'
fi

mkdir -p build
find src -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
echo "==> Empaquetando Lambdas"
aws cloudformation package --region "$REGION" --template-file infra/template.yaml \
  --s3-bucket "$ART" --s3-prefix lambda --output-template-file build/packaged.yaml >/dev/null

echo "==> Desplegando CloudFormation (puede tardar 2-3 min)"
aws cloudformation deploy --region "$REGION" --stack-name "$STACK" \
  --template-file build/packaged.yaml \
  --parameter-overrides DocsBucketName="$DOCS_BUCKET" ConfidenceThreshold="$THRESHOLD" AlertEmail="$ALERT_EMAIL" EnableFaultInjection="$FAULT_INJECTION" \
  --tags proyecto=final --no-fail-on-empty-changeset

out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
          --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

# CloudFormation no vuelve a publicar la API en actualizaciones: forzamos un deployment
aws apigateway create-deployment --region "$REGION" --rest-api-id "$(out RestApiId)" --stage-name prod \
  --description "deploy $(date -u +%FT%TZ)" >/dev/null

echo
echo "================= LISTO ================="
echo "Bucket         : $(out BucketName)"
echo "Vista revisión : $(out ReviewUiUrl)"
echo "Dashboard      : $(out DashboardUrl)"
echo "Clave de API   : aws apigateway get-api-key --api-key $(out ApiKeyId) --include-value --query value --output text"
echo
echo "Siguiente paso : python3 scripts/prueba_aceptacion.py --stack $STACK"
