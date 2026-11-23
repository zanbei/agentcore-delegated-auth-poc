#!/usr/bin/env bash
# Deploy the automation runtime (IAM inbound) used by the V2 checks.
# Re-running updates the existing runtime in place. Optional: GATEWAY_URL (background gateway, for the gateway mode).
# Needs: AWS_ACCOUNT_ID, AWS_REGION, KC_ISSUER (same as the v0 scripts), the v0 resources
# (credential provider obo-test-bg-3lo, downstream function obo-test-itsm), and uv.
# Everything created here is prefixed obo-test-v1 / obo_test_v1 so cleanup can find it.
set -euo pipefail
: "${AWS_ACCOUNT_ID:?}" "${AWS_REGION:?}" "${KC_ISSUER:?}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="${BUILD_DIR:-$HERE/.build}"
BUCKET="obo-test-v1-code-${AWS_ACCOUNT_ID}-${AWS_REGION}"
ROLE="obo-test-v1-auto-runtime-role"
RUNTIME="obo_test_v1_auto"
PROVIDER="obo-test-bg-3lo"
DOWNSTREAM="obo-test-itsm"
KEY="auto_agent.zip"
ARN_ROOT="arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}"

mkdir -p "$BUILD"
rm -rf "$BUILD/pkg" && mkdir -p "$BUILD/pkg"
uv pip install -q --target "$BUILD/pkg" boto3
cp "$HERE/auto_agent/main.py" "$BUILD/pkg/main.py"
(cd "$BUILD/pkg" && find . -name __pycache__ -prune -exec rm -rf {} + && rm -f "$BUILD/$KEY" && zip -qr "$BUILD/$KEY" .)
echo "package: $(du -h "$BUILD/$KEY" | cut -f1)"

aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null || {
  aws s3api create-bucket --bucket "$BUCKET" --region "$AWS_REGION" \
    --create-bucket-configuration "LocationConstraint=$AWS_REGION" >/dev/null
  aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
}
aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
VERSION=$(aws s3api put-object --bucket "$BUCKET" --key "$KEY" --body "$BUILD/$KEY" --query VersionId --output text)

SECRET_ARN=$(aws bedrock-agentcore-control get-oauth2-credential-provider --name "$PROVIDER" \
  --query 'clientSecretArn.secretArn' --output text)

TRUST=$(cat <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock-agentcore.amazonaws.com"},
"Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"${AWS_ACCOUNT_ID}"}}}]}
EOF
)
aws iam get-role --role-name "$ROLE" >/dev/null 2>&1 || aws iam create-role --role-name "$ROLE" \
  --assume-role-policy-document "$TRUST" >/dev/null

policy() {  # $1 = workload identity resource suffix (name or prefix*)
cat <<EOF
{"Version":"2012-10-17","Statement":[
 {"Sid":"Vault","Effect":"Allow","Action":["bedrock-agentcore:GetResourceOauth2Token","bedrock-agentcore:CompleteResourceTokenAuth"],
  "Resource":["${ARN_ROOT}:workload-identity-directory/default","${ARN_ROOT}:workload-identity-directory/default/workload-identity/$1",
  "${ARN_ROOT}:token-vault/default","${ARN_ROOT}:token-vault/default/oauth2credentialprovider/${PROVIDER}"]},
 {"Sid":"ProviderSecret","Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"${SECRET_ARN}"},
 {"Sid":"Downstream","Effect":"Allow","Action":"lambda:InvokeFunction","Resource":"arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${DOWNSTREAM}"},
 {"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogStreams"],
  "Resource":"arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/runtimes/*"}
]}
EOF
}
aws iam put-role-policy --role-name "$ROLE" --policy-name inline --policy-document "$(policy "${RUNTIME}*")"
sleep 10  # IAM propagation

ENVS=$(python3 -c 'import json,os,sys; e={"PROVIDER_NAME":sys.argv[1],"RETURN_URL":sys.argv[2]+"/.well-known/openid-configuration","DOWNSTREAM_FN":sys.argv[3],"SCOPES":"openid itsm"}
if os.environ.get("GATEWAY_URL"): e["GATEWAY_URL"]=os.environ["GATEWAY_URL"]
print(json.dumps(e))' "$PROVIDER" "$KC_ISSUER" "$DOWNSTREAM")
ARTIFACT=$(printf '{"codeConfiguration":{"code":{"s3":{"bucket":"%s","prefix":"%s","versionId":"%s"}},"runtime":"PYTHON_3_13","entryPoint":["main.py"]}}' \
  "$BUCKET" "$KEY" "$VERSION")
EXISTING=$(aws bedrock-agentcore-control list-agent-runtimes --query "agentRuntimes[?agentRuntimeName=='${RUNTIME}'].agentRuntimeId | [0]" --output text)
if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
  OUT=$(aws bedrock-agentcore-control update-agent-runtime --agent-runtime-id "$EXISTING" \
    --agent-runtime-artifact "$ARTIFACT" --role-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE}" \
    --network-configuration '{"networkMode":"PUBLIC"}' --environment-variables "$ENVS" --output json)
else
  OUT=$(aws bedrock-agentcore-control create-agent-runtime --agent-runtime-name "$RUNTIME" \
    --agent-runtime-artifact "$ARTIFACT" --role-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE}" \
    --network-configuration '{"networkMode":"PUBLIC"}' --environment-variables "$ENVS" --output json)
fi
echo "$OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print({k:d.get(k) for k in ("agentRuntimeId","status","workloadIdentityDetails")})'
