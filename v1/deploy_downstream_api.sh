#!/usr/bin/env bash
# Token-gated HTTP API in front of the downstream Lambda, so a gateway target can reach it (V8 fallback).
# The API Gateway JWT authorizer rejects requests without a valid Keycloak token (aud itsm-api) before the Lambda runs,
# and the Lambda verifies the token again itself. Needs: AWS_ACCOUNT_ID, AWS_REGION, KC_ISSUER.
set -euo pipefail
: "${AWS_ACCOUNT_ID:?}" "${AWS_REGION:?}" "${KC_ISSUER:?}"
FN="obo-test-itsm"
NAME="obo-test-v1-itsm-api"
FN_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${FN}"
API_ID=$(aws apigatewayv2 get-apis --query "Items[?Name=='${NAME}'].ApiId | [0]" --output text)
if [ "$API_ID" = "None" ] || [ -z "$API_ID" ]; then
  API_ID=$(aws apigatewayv2 create-api --name "$NAME" --protocol-type HTTP --query ApiId --output text)
  AUTH_ID=$(aws apigatewayv2 create-authorizer --api-id "$API_ID" --name keycloak --authorizer-type JWT \
    --identity-source '$request.header.Authorization' \
    --jwt-configuration "Audience=itsm-api,Issuer=${KC_ISSUER}" --query AuthorizerId --output text)
  INT_ID=$(aws apigatewayv2 create-integration --api-id "$API_ID" --integration-type AWS_PROXY --integration-uri "$FN_ARN" \
    --payload-format-version 2.0 --query IntegrationId --output text)
  for R in "GET /incidents" "POST /incidents/{id}/approve"; do
    aws apigatewayv2 create-route --api-id "$API_ID" --route-key "$R" --target "integrations/${INT_ID}" \
      --authorization-type JWT --authorizer-id "$AUTH_ID" >/dev/null
  done
  aws apigatewayv2 create-stage --api-id "$API_ID" --stage-name '$default' --auto-deploy >/dev/null
  aws lambda add-permission --function-name "$FN" --statement-id obo-test-v1-apigw --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com --source-arn "arn:aws:execute-api:${AWS_REGION}:${AWS_ACCOUNT_ID}:${API_ID}/*/*" >/dev/null
fi
echo "api_id=${API_ID} url=https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com"
