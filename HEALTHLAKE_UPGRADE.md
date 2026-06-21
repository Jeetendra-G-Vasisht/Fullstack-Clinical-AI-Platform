# HealthLake Upgrade Guide

This document describes exactly what to do when you are ready to activate AWS
HealthLake for FHIR data storage and querying.

The CDK stack has already deployed:
- `HealthLakeReadyRole` — IAM role with `AmazonHealthLakeFullAccess` and S3 access to the FHIR prefix
- FHIR S3 path: `s3://kbuddhiai-uploads-{account}/fhir/`

---

## When to activate HealthLake

Activate HealthLake when:
1. You are ingesting FHIR R4 resources (Patient, Observation, Condition, etc.)
2. You need FHIR-native search (e.g. `GET /Patient?birthdate=1990-01-01`)
3. You require HL7 FHIR compliance for integration with EHR systems

---

## Step 1 — Check HealthLake availability in your region

HealthLake is available in `us-east-1` and `us-west-2`. If your data must remain
in `us-east-2`, check the [current availability list](https://docs.aws.amazon.com/healthlake/latest/devguide/what-is-amazon-health-lake.html).

For HIPAA compliance, HealthLake is HIPAA-eligible in all regions where it is
available. A Business Associate Agreement (BAA) with AWS is required.

---

## Step 2 — Create the HealthLake FHIR datastore

### Via AWS Console

1. Open **AWS Console → HealthLake**
2. Click **Create FHIR datastore**
3. Configure:
   - **Name:** `kbuddhiai-fhir`
   - **FHIR version:** R4
   - **SSE encryption:** AWS-owned key (or CMK for HIPAA)
   - **Preloaded data:** None (start empty)
4. Note the **Datastore ID** and **Datastore endpoint** from the output

### Via AWS CLI

```bash
aws healthlake create-fhir-datastore \
  --datastore-type-version R4 \
  --datastore-name kbuddhiai-fhir \
  --sse-configuration KmsEncryptionConfig={CmkType=AWS_OWNED_KMS_KEY} \
  --region us-east-1
```

Output includes `datastoreId` and `datastoreArn`. Save both.

---

## Step 3 — Update the HealthLakeReadyRole

The CDK deployed `HealthLakeReadyRole` with generic HealthLake permissions.
Scope it down to your specific datastore for least-privilege:

```bash
DATASTORE_ARN="arn:aws:healthlake:us-east-1:699092321120:datastore/fhir/YOUR_DATASTORE_ID"

aws iam put-role-policy \
  --role-name HealthLakeReadyRole \
  --policy-name ScopedHealthLakePolicy \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": [
        \"healthlake:CreateResource\",
        \"healthlake:ReadResource\",
        \"healthlake:UpdateResource\",
        \"healthlake:DeleteResource\",
        \"healthlake:SearchWithGet\",
        \"healthlake:SearchWithPost\"
      ],
      \"Resource\": \"${DATASTORE_ARN}\"
    }]
  }"
```

---

## Step 4 — Set up S3 → HealthLake import

To bulk-import existing FHIR NDJSON files from the FHIR S3 prefix:

```bash
DATASTORE_ID="YOUR_DATASTORE_ID"
ROLE_ARN="arn:aws:iam::699092321120:role/HealthLakeReadyRole"
INPUT_S3="s3://kbuddhiai-uploads-699092321120/fhir/"
OUTPUT_S3="s3://kbuddhiai-uploads-699092321120/healthlake-output/"

aws healthlake start-fhir-import-job \
  --input-data-config S3Uri=${INPUT_S3} \
  --datastore-id ${DATASTORE_ID} \
  --role-arn ${ROLE_ARN} \
  --job-output-data-config '{"S3Configuration":{"S3Uri":"'${OUTPUT_S3}'","KmsKeyId":"AWS_OWNED_KMS_KEY"}}' \
  --region us-east-1
```

Monitor the job:
```bash
aws healthlake list-fhir-import-jobs --datastore-id ${DATASTORE_ID} --region us-east-1
```

---

## Step 5 — Wire HealthLake into the chat Lambda (optional)

To allow the AI chat to query FHIR resources instead of (or in addition to)
raw uploaded files, update the chat Lambda:

1. Add the HealthLake endpoint to Lambda environment variables:
   ```bash
   aws lambda update-function-configuration \
     --function-name kbuddhiai-chat \
     --region us-east-2 \
     --environment "Variables={
       HEALTHLAKE_ENDPOINT=https://healthlake.us-east-1.amazonaws.com/datastore/YOUR_DATASTORE_ID/r4,
       HEALTHLAKE_REGION=us-east-1
     }"
   ```

2. Add HealthLake permissions to `kbuddhiai-lambda-role`:
   ```bash
   aws iam attach-role-policy \
     --role-name kbuddhiai-lambda-role \
     --policy-arn arn:aws:iam::aws:policy/AmazonHealthLakeReadOnlyAccess
   ```

3. In the chat Lambda, add a FHIR search call alongside the existing S3 read:
   ```python
   import boto3
   healthlake = boto3.client('healthlake', region_name=os.environ['HEALTHLAKE_REGION'])
   response = healthlake.search_with_get(
       DatastoreId='YOUR_DATASTORE_ID',
       ResourceType='Patient',
       RequestPath='/Patient',
       RequestQuery='birthdate=1990-01-01',
   )
   ```

---

## Step 6 — Add HealthLake to the CDK stack

When HealthLake is fully activated, add the datastore to the CDK stack so it is
managed as infrastructure-as-code:

```typescript
// In infra/lib/kbuddhiai-stack.ts
import * as healthlake from 'aws-cdk-lib/aws-healthlake';

const fhirDatastore = new healthlake.CfnFHIRDatastore(this, 'FhirDatastore', {
  datastoreName: 'kbuddhiai-fhir',
  datastoreTypeVersion: 'R4',
  sseConfiguration: {
    kmsEncryptionConfig: { cmkType: 'AWS_OWNED_KMS_KEY' },
  },
});
```

---

## HIPAA checklist for HealthLake

Before going live with PHI in HealthLake:

- [ ] AWS Business Associate Agreement (BAA) is signed
      → AWS Console → My Account → AWS Artifact → AWS BAA
- [ ] HealthLake datastore uses SSE with a Customer Managed Key (CMK)
- [ ] CloudTrail is enabled for HealthLake API calls
- [ ] VPC endpoint for HealthLake is configured (prevents data leaving AWS network)
- [ ] Access logs are stored in a separate S3 bucket with 6-year retention
- [ ] IAM policies follow least-privilege (role per function, not wildcard)

---

## FHIR S3 folder structure

FHIR NDJSON files should be placed at:

```
s3://kbuddhiai-uploads-699092321120/fhir/
  Patient/
    patient-001.ndjson
    patient-002.ndjson
  Observation/
    obs-001.ndjson
  Condition/
    cond-001.ndjson
```

Each `.ndjson` file contains one FHIR resource per line, e.g.:
```json
{"resourceType":"Patient","id":"patient-001","name":[{"family":"Smith","given":["John"]}],"birthDate":"1975-03-15"}
```
