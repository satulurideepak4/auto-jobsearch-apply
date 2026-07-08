#!/bin/bash
# Optional: deploy as Cloud Run Job for server-side scheduling
# Usage: bash outreach/deploy_cloudrun.sh
PROJECT_ID=$(grep "GCP_PROJECT_ID:" outreach/README.md | awk '{print $2}')
JOB_NAME="outreach-pipeline"
REGION="us-central1"
SA=$(gcloud iam service-accounts list --format='value(email)' --limit=1)

gcloud run jobs create $JOB_NAME \
  --image=python:3.11-slim \
  --command=python \
  --args=outreach/main.py \
  --region=$REGION \
  --project=$PROJECT_ID

gcloud scheduler jobs create http outreach-daily \
  --schedule="30 3 * * 1-5" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/$JOB_NAME:run" \
  --oauth-service-account-email=$SA \
  --location=$REGION

echo "Cloud Run job and scheduler created."
