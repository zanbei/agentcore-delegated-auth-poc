#!/usr/bin/env bash
# V3: a trail that records AgentCore Runtime data events (InvokeAgentRuntime), to see whether runtimeUserId is logged.
# Data events are not logged by default and do not appear in Event history.
set -euo pipefail
: "${AWS_ACCOUNT_ID:?}" "${AWS_REGION:?}"
BUCKET="obo-test-v1-trail-${AWS_ACCOUNT_ID}-${AWS_REGION}"
TRAIL="obo-test-v1-trail"
aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null || {
  aws s3api create-bucket --bucket "$BUCKET" --region "$AWS_REGION" --create-bucket-configuration "LocationConstraint=$AWS_REGION" >/dev/null
  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
}
POLICY=$(cat <<P
{"Version":"2012-10-17","Statement":[
{"Sid":"Acl","Effect":"Allow","Principal":{"Service":"cloudtrail.amazonaws.com"},"Action":"s3:GetBucketAcl","Resource":"arn:aws:s3:::${BUCKET}",
 "Condition":{"StringEquals":{"aws:SourceArn":"arn:aws:cloudtrail:${AWS_REGION}:${AWS_ACCOUNT_ID}:trail/${TRAIL}"}}},
{"Sid":"Write","Effect":"Allow","Principal":{"Service":"cloudtrail.amazonaws.com"},"Action":"s3:PutObject","Resource":"arn:aws:s3:::${BUCKET}/AWSLogs/${AWS_ACCOUNT_ID}/*",
 "Condition":{"StringEquals":{"s3:x-amz-acl":"bucket-owner-full-control","aws:SourceArn":"arn:aws:cloudtrail:${AWS_REGION}:${AWS_ACCOUNT_ID}:trail/${TRAIL}"}}}]}
P
)
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "$POLICY"
aws cloudtrail get-trail --name "$TRAIL" >/dev/null 2>&1 || aws cloudtrail create-trail --name "$TRAIL" --s3-bucket-name "$BUCKET" \
  --no-is-multi-region-trail --no-include-global-service-events >/dev/null
aws cloudtrail put-event-selectors --trail-name "$TRAIL" --advanced-event-selectors '[{"Name":"agentcore-runtime-data","FieldSelectors":[
 {"Field":"eventCategory","Equals":["Data"]},{"Field":"resources.type","Equals":["AWS::BedrockAgentCore::Runtime"]}]}]' >/dev/null
aws cloudtrail start-logging --name "$TRAIL"
echo "trail $TRAIL logging to s3://$BUCKET"
