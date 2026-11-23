#!/usr/bin/env bash
# Test roles for the V2 IAM checks. Both are assumable only by the account's own caller identity ($TRUSTED_PRINCIPAL_ARN).
#   obo-test-v1-launcher-role   : InvokeAgentRuntime + InvokeAgentRuntimeForUser on the automation runtime only
#   obo-test-v1-noforuser-role  : InvokeAgentRuntime only (control case)
set -euo pipefail
: "${AWS_ACCOUNT_ID:?}" "${AWS_REGION:?}" "${TRUSTED_PRINCIPAL_ARN:?}" "${AUTO_RUNTIME_ID:?}"
RT="arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:runtime/${AUTO_RUNTIME_ID}"
TRUST="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"AWS\":\"${TRUSTED_PRINCIPAL_ARN}\"},\"Action\":\"sts:AssumeRole\"}]}"
mk() { aws iam get-role --role-name "$1" >/dev/null 2>&1 || aws iam create-role --role-name "$1" --assume-role-policy-document "$TRUST" >/dev/null; }
mk obo-test-v1-launcher-role
mk obo-test-v1-noforuser-role
aws iam put-role-policy --role-name obo-test-v1-launcher-role --policy-name inline --policy-document \
 "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"bedrock-agentcore:InvokeAgentRuntime\",\"bedrock-agentcore:InvokeAgentRuntimeForUser\"],\"Resource\":[\"${RT}\",\"${RT}/runtime-endpoint/*\"]}]}"
aws iam put-role-policy --role-name obo-test-v1-noforuser-role --policy-name inline --policy-document \
 "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"bedrock-agentcore:InvokeAgentRuntime\"],\"Resource\":[\"${RT}\",\"${RT}/runtime-endpoint/*\"]}]}"
sleep 10
echo ok
