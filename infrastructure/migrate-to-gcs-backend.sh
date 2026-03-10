#!/usr/bin/env bash
# ============================================================================
# Pulumi Backend Migration: Pulumi Cloud → GCS Bucket
# ============================================================================
# This script:
#   1. Creates a GCS bucket for Pulumi state
#   2. Exports current stack state from Pulumi Cloud
#   3. Logs into the new GCS backend
#   4. Re-creates the stack and imports state
#
# Prerequisites:
#   - gcloud CLI authenticated with permissions to create buckets
#   - pulumi CLI installed and currently logged into Pulumi Cloud
#   - Run from the infrastructure/ directory
# ============================================================================

set -euo pipefail

PROJECT_ID="ai-incident-triage"
REGION="europe-west4"
BUCKET_NAME="${PROJECT_ID}-pulumi-state"
STACK_NAME="dev"

echo "============================================"
echo "  Pulumi Backend Migration → GCS"
echo "============================================"
echo ""
echo "  GCP Project:  ${PROJECT_ID}"
echo "  GCS Bucket:   gs://${BUCKET_NAME}"
echo "  Stack:        ${STACK_NAME}"
echo ""

# -----------------------------------------------
# Step 0: Ensure Application Default Credentials
# -----------------------------------------------
echo "[0/5] Checking GCP Application Default Credentials..."
if ! gcloud auth application-default print-access-token &>/dev/null; then
    echo "  ⚠️  No ADC found. Running 'gcloud auth application-default login'..."
    gcloud auth application-default login --project "${PROJECT_ID}"
fi
echo "  ✅ ADC available."

# -----------------------------------------------
# Step 1: Export current state from Pulumi Cloud
# -----------------------------------------------
echo "[1/5] Exporting current stack state from Pulumi Cloud..."
if pulumi stack select ${STACK_NAME} 2>/dev/null; then
    pulumi stack export --file /tmp/pulumi-state-backup.json
    echo "  ✅ State exported to /tmp/pulumi-state-backup.json"
else
    echo "  ⚠️  No existing stack found. Will create a fresh stack."
    touch /tmp/pulumi-state-backup.json
fi

# -----------------------------------------------
# Step 2: Create GCS bucket (if it doesn't exist)
# -----------------------------------------------
echo "[2/5] Creating GCS bucket gs://${BUCKET_NAME}..."
if gsutil ls -b "gs://${BUCKET_NAME}" 2>/dev/null; then
    echo "  ✅ Bucket already exists."
else
    gsutil mb -p "${PROJECT_ID}" -l "${REGION}" -b on "gs://${BUCKET_NAME}"
    # Enable versioning for state safety
    gsutil versioning set on "gs://${BUCKET_NAME}"
    echo "  ✅ Bucket created with versioning enabled."
fi

# Grant CI/CD service account access to the bucket
echo "     Granting CI/CD service account objectAdmin access..."
gsutil iam ch \
    serviceAccount:github-actions-sa@${PROJECT_ID}.iam.gserviceaccount.com:roles/storage.objectAdmin \
    "gs://${BUCKET_NAME}"
echo "  ✅ IAM permissions granted."

# -----------------------------------------------
# Step 3: Login to GCS backend
# -----------------------------------------------
echo "[3/5] Switching Pulumi backend to GCS..."
pulumi login "gs://${BUCKET_NAME}"
echo "  ✅ Logged into GCS backend."

# -----------------------------------------------
# Step 4: Initialize the stack on the new backend
# -----------------------------------------------
echo "[4/5] Initializing stack '${STACK_NAME}' on GCS backend..."
pulumi stack init ${STACK_NAME} 2>/dev/null || pulumi stack select ${STACK_NAME}

# -----------------------------------------------
# Step 5: Import state (if we had an export)
# -----------------------------------------------
if [ -s /tmp/pulumi-state-backup.json ]; then
    echo "[5/5] Importing state into new backend..."
    pulumi stack import --file /tmp/pulumi-state-backup.json
    echo "  ✅ State imported successfully."
else
    echo "[5/5] No previous state to import. Fresh stack created."
    echo "  ⚠️  You'll need to run 'pulumi up' or import resources manually."
fi

echo ""
echo "============================================"
echo "  Migration Complete!"
echo "============================================"
echo ""
echo "  Backend:   gs://${BUCKET_NAME}"
echo "  Stack:     ${STACK_NAME}"
echo ""
echo "  Next steps:"
echo "    1. Run 'pulumi preview' to verify"
echo "    2. Set PULUMI_BACKEND_URL=gs://${BUCKET_NAME}"
echo "       in your GitHub Actions repo secrets"
echo "    3. Remove PULUMI_ACCESS_TOKEN from GitHub Secrets"
echo ""
