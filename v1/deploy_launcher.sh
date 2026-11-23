#!/usr/bin/env bash
# Registry table, launcher Lambda and the role EventBridge Scheduler uses to call it.
# Needs: AWS_ACCOUNT_ID, AWS_REGION, AUTO_RUNTIME_ID (from deploy_auto_runtime.sh), uv, zip.
set -euo pipefail
: "${AWS_ACCOUNT_ID:?}" "${AWS_REGION:?}" "${AUTO_RUNTIME_ID:?}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="${BUILD_DIR:-$HERE/.build}/launcher"
TABLE="obo-test-v1-registry"
FN="obo-test-v1-launcher"
FN_ROLE="obo-test-v1-launcher-fn-role"
SCH_ROLE="obo-test-v1-scheduler-invoke-role"
RT="arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:runtime/${AUTO_RUNTIME_ID}"

aws dynamodb describe-table --table-name "$TABLE" >/dev/null 2>&1 || {
  aws dynamodb create-table --table-name "$TABLE" --attribute-definitions AttributeName=pk,AttributeType=S \
    --key-schema AttributeName=pk,KeyType=HASH --billing-mode PAY_PER_REQUEST >/dev/null
  aws dynamodb wait table-exists --table-name "$TABLE"
}
TABLE_ARN="arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${TABLE}"

role() {  # name, service
  aws iam get-role --role-name "$1" >/dev/null 2>&1 || aws iam create-role --role-name "$1" --assume-role-policy-document \
   "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"$2\"},\"Action\":\"sts:AssumeRole\"}]}" >/dev/null
}
role "$FN_ROLE" lambda.amazonaws.com
role "$SCH_ROLE" scheduler.amazonaws.com
aws iam put-role-policy --role-name "$FN_ROLE" --policy-name inline --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
 {\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:/aws/lambda/${FN}:*\"},
 {\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:UpdateItem\",\"dynamodb:PutItem\"],\"Resource\":\"${TABLE_ARN}\"},
 {\"Effect\":\"Allow\",\"Action\":[\"bedrock-agentcore:InvokeAgentRuntime\",\"bedrock-agentcore:InvokeAgentRuntimeForUser\"],\"Resource\":[\"${RT}\",\"${RT}/runtime-endpoint/*\"]}]}"

rm -rf "$BUILD" && mkdir -p "$BUILD/pkg"
uv pip install -q --target "$BUILD/pkg" boto3   # bundled: the Lambda-provided boto3 may predate runtimeUserId
cp "$HERE/launcher/handler.py" "$BUILD/pkg/handler.py"
(cd "$BUILD/pkg" && find . -name __pycache__ -prune -exec rm -rf {} + && zip -qr "$BUILD/launcher.zip" .)
sleep 10  # IAM propagation
ENV="Variables={REGISTRY=${TABLE},RUNTIME_ARN=${RT}}"
if aws lambda get-function --function-name "$FN" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FN" --zip-file "fileb://$BUILD/launcher.zip" >/dev/null
  aws lambda wait function-updated --function-name "$FN"
else
  aws lambda create-function --function-name "$FN" --runtime python3.13 --handler handler.handler --timeout 120 --memory-size 256 \
    --role "arn:aws:iam::${AWS_ACCOUNT_ID}:role/${FN_ROLE}" --environment "$ENV" --zip-file "fileb://$BUILD/launcher.zip" >/dev/null
  aws lambda wait function-active --function-name "$FN"
fi
aws iam put-role-policy --role-name "$SCH_ROLE" --policy-name inline --policy-document \
 "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",\"Resource\":\"arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${FN}\"}]}"
echo "registry=$TABLE launcher=$FN scheduler-role=$SCH_ROLE"
