#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { KbuddhiStack } from '../lib/kbuddhiai-stack';
import { KbuddhiCertStack } from '../lib/cert-stack';

const app = new cdk.App();

const account = process.env.CDK_DEFAULT_ACCOUNT || '699092321120';

// ── Phase 1: Main stack — all resources in us-east-2 ──────────────────────────
const mainStack = new KbuddhiStack(app, 'KbuddhiStack', {
  env: { account, region: 'us-east-2' },
  description: 'kBuddhi AI — HIPAA portal (Cognito · SES · S3 · CloudFront · API Gateway)',
  crossRegionReferences: true,
});

// ── Phase 1: ACM cert — must live in us-east-1 for CloudFront ────────────────
// Deploy AFTER Phase 1 when Route 53 nameservers are pointed from GoDaddy:
//   npm run deploy:cert
// Then redeploy main stack with the cert ARN (see DEPLOY.md).
const certStack = new KbuddhiCertStack(app, 'KbuddhiCertStack', {
  env: { account, region: 'us-east-1' },
  description: 'kBuddhi AI — ACM SSL certificate for CloudFront (must be us-east-1)',
  crossRegionReferences: true,
  hostedZoneId: app.node.tryGetContext('hostedZoneId') as string,
});

app.synth();
