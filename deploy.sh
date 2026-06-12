#!/bin/bash
# Deploy del chatbot a AWS Lambda usando SAM
set -e

STACK_NAME="chatbot-agendamiento"
REGION="${AWS_REGION:-us-east-1}"
S3_BUCKET="${SAM_BUCKET:-}"

echo "🚀 Deploy de $STACK_NAME en $REGION"

# Verificar SAM CLI
if ! command -v sam &> /dev/null; then
    echo "❌ SAM CLI no encontrado. Instalar: brew install aws-sam-cli"
    exit 1
fi

# Verificar credenciales
if ! aws sts get-caller-identity &> /dev/null; then
    echo "❌ No hay credenciales AWS configuradas"
    exit 1
fi

echo "👤 AWS Identity: $(aws sts get-caller-identity --query 'Arn' --output text)"

# Secretos requeridos — sin defaults: el deploy falla si no están definidos
for var in TELEGRAM_WEBHOOK_SECRET WHATSAPP_VERIFY_TOKEN; do
    if [ -z "${!var:-}" ]; then
        echo "❌ Falta la variable de entorno $var (requerida, sin default)"
        exit 1
    fi
done

# Build
echo "📦 Building..."
sam build --template-file template.yaml

# Deploy
echo "☁️  Deploying..."
DEPLOY_ARGS=(
    --stack-name "$STACK_NAME"
    --region "$REGION"
    --capabilities CAPABILITY_IAM
    --resolve-s3
    --no-confirm-changeset
    --parameter-overrides
        "TelegramBotToken=${TELEGRAM_BOT_TOKEN}"
        "TelegramWebhookSecret=${TELEGRAM_WEBHOOK_SECRET}"
        "WhatsAppToken=${WHATSAPP_TOKEN}"
        "WhatsAppPhoneNumberId=${WHATSAPP_PHONE_NUMBER_ID}"
        "WhatsAppVerifyToken=${WHATSAPP_VERIFY_TOKEN}"
)

# Optional parameters: sam rejects empty "Key=" overrides. Omitted parameters
# keep their current stack values (UsePreviousValue), so they are only passed
# when the corresponding env var is set.
if [ -z "${ADMIN_API_KEY:-}" ]; then
    echo "ℹ️  ADMIN_API_KEY no definida: se mantiene el valor actual del stack"
else
    DEPLOY_ARGS+=("AdminApiKey=${ADMIN_API_KEY}")
fi
if [ -n "${WHATSAPP_APP_SECRET:-}" ]; then
    DEPLOY_ARGS+=("WhatsAppAppSecret=${WHATSAPP_APP_SECRET}")
fi
if [ -n "${ALARM_EMAIL:-}" ]; then
    DEPLOY_ARGS+=("AlarmEmail=${ALARM_EMAIL}")
fi

sam deploy "${DEPLOY_ARGS[@]}"

# Outputs
echo ""
echo "✅ Deploy completado!"
echo ""
echo "📋 URLs:"
aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs[*].[Description,OutputValue]' \
    --output table

# Configurar webhook de Telegram
API_URL=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query 'Stacks[0].Outputs[?OutputKey==`TelegramWebhookUrl`].OutputValue' \
    --output text)

if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$API_URL" ]; then
    echo ""
    echo "🤖 Configurando webhook de Telegram..."
    curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${API_URL}&secret_token=${TELEGRAM_WEBHOOK_SECRET}" | python3 -m json.tool
fi

echo ""
echo "🎉 ¡Listo! Configura la URL de WhatsApp en Meta Developer Console."
