#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# bootstrap.sh — one-time setup to deploy the CodePipeline infrastructure
#
# Prerequisites:
#   1. AWS CLI configured (aws configure)
#   2. GitHub CodeStar connection created in the AWS console:
#      → CodePipeline > Settings > Connections > Create connection
#      → Copy the ARN and paste below
#
# Usage:
#   chmod +x bootstrap.sh
#   ./bootstrap.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── EDIT THESE ────────────────────────────────────────────────────────────────
STACK_NAME="data-pipeline-cicd"
GITHUB_OWNER="Sumitgirnar2009"
GITHUB_REPO="Data-Pipeline"
GITHUB_BRANCH="main"
GITHUB_CONNECTION_ARN="arn:aws:codeconnections:ap-south-1:287972983990:connection/e77c3b72-17fc-43cd-b046-6f1a3ae86cc5"
AWS_REGION="ap-south-1"   # change to your region
# ─────────────────────────────────────────────────────────────────────────────

echo "=== Deploying CI/CD pipeline stack: $STACK_NAME ==="

aws cloudformation deploy \
  --stack-name         "$STACK_NAME" \
  --template-file      "infra/pipeline.yaml" \
  --capabilities       CAPABILITY_NAMED_IAM \
  --region             "$AWS_REGION" \
  --parameter-overrides \
    GitHubOwner="$GITHUB_OWNER" \
    GitHubRepo="$GITHUB_REPO" \
    GitHubBranch="$GITHUB_BRANCH" \
    GitHubConnectionArn="$GITHUB_CONNECTION_ARN" \
  --no-fail-on-empty-changeset

echo ""
echo "=== Stack outputs ==="
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region     "$AWS_REGION" \
  --query      "Stacks[0].Outputs" \
  --output     table

echo ""
echo "✓ Done! Push code to $GITHUB_BRANCH to trigger the pipeline."
echo "  Monitor at: https://console.aws.amazon.com/codesuite/codepipeline/pipelines"