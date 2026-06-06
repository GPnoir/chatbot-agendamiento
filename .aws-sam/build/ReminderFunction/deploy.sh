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
        "TelegramWebhookSecret=${TELEGRAM_WEBHOOK_SECRET:-telegram_secret_change_me}"
        "WhatsAppToken=${WHATSAPP_TOKEN}"
        "WhatsAppPhoneNumberId=${WHATSAPP_PHONE_NUMBER_ID}"
        "WhatsAppVerifyToken=${WHATSAPP_VERIFY_TOKEN:-mi_token_secreto}"
        "WhatsAppAppSecret=${WHATSAPP_APP_SECRET:-}"
)

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
    curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${API_URL}&secret_token=${TELEGRAM_WEBHOOK_SECRET:-telegram_secret_change_me}" | python3 -m json.tool
fi

echo ""
echo "🎉 ¡Listo! Configura la URL de WhatsApp en Meta Developer Console."
