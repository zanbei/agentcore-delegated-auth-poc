#!/bin/zsh
# Tear down everything created for the AgentCore Gateway OBO test (region and account come from your AWS CLI configuration).
# Required env: GATEWAY_ID TARGET_ID CF_ID INSTANCE_ID SG_ID SG_RULE_ID
# Run: zsh scripts/cleanup.sh   (review every line first; it deletes resources)
set -u
: "${GATEWAY_ID:?}" "${TARGET_ID:?}" "${CF_ID:?}" "${INSTANCE_ID:?}" "${SG_ID:?}" "${SG_RULE_ID:?}"
GW=$GATEWAY_ID; T=$TARGET_ID; I=$INSTANCE_ID

echo "== AgentCore"
aws bedrock-agentcore-control delete-gateway-target --gateway-identifier "$GW" --target-id "$T"
sleep 15
aws bedrock-agentcore-control delete-gateway --gateway-identifier "$GW"
aws bedrock-agentcore-control delete-oauth2-credential-provider --name obo-test-keycloak

echo "== 3LO / Token Vault test"
aws bedrock-agentcore-control delete-oauth2-credential-provider --name obo-test-bg-3lo
aws bedrock-agentcore-control delete-workload-identity --name obo-test-bg-agent
for r in obo-test-scheduler-role obo-test-noforuser-role; do
  aws iam delete-role-policy --role-name $r --policy-name obo-test-bg
  aws iam delete-role --role-name $r
done

echo "== Lambda (function URL and public API Gateway were already removed)"
aws lambda delete-function --function-name obo-test-itsm
aws logs delete-log-group --log-group-name /aws/lambda/obo-test-itsm

echo "== IAM"
aws iam delete-role-policy --role-name obo-test-gateway-role --policy-name obo-test-gateway-runtime
aws iam delete-role --role-name obo-test-gateway-role
aws iam detach-role-policy --role-name obo-test-itsm-lambda-role --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name obo-test-itsm-lambda-role

echo "== Keycloak container + security group rule"
aws ec2 revoke-security-group-ingress --group-id "$SG_ID" --security-group-rule-ids "$SG_RULE_ID"
aws ssm send-command --instance-ids "$I" --document-name AWS-RunShellScript \
  --parameters 'commands=["docker rm -f keycloak","docker volume rm kc-data","rm -f /opt/kc-admin.env"]' --query Command.CommandId --output text

echo "== CloudFront (disable, wait, delete)"
aws cloudfront get-distribution-config --id "$CF_ID" > cf.json
ETAG=$(python3 -c 'import json;print(json.load(open("cf.json"))["ETag"])')
python3 -c 'import json;d=json.load(open("cf.json"))["DistributionConfig"];d["Enabled"]=False;json.dump(d,open("cf-off.json","w"))'
aws cloudfront update-distribution --id "$CF_ID" --if-match "$ETAG" --distribution-config file://cf-off.json >/dev/null
aws cloudfront wait distribution-deployed --id "$CF_ID"
ETAG=$(aws cloudfront get-distribution --id "$CF_ID" --query ETag --output text)
aws cloudfront delete-distribution --id "$CF_ID" --if-match "$ETAG"
FETAG=$(aws cloudfront describe-function --name obo-test-block-admin --query ETag --output text)
aws cloudfront delete-function --name obo-test-block-admin --if-match "$FETAG"

echo "Done. Delete your local secrets file when finished."
