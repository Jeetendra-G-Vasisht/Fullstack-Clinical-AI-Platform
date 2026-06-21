import * as cdk from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as route53 from 'aws-cdk-lib/aws-route53';
import { Construct } from 'constructs';

export interface CertStackProps extends cdk.StackProps {
  /** Route 53 hosted zone ID — obtained from KbuddhiStack outputs after Phase 1 deploy */
  hostedZoneId?: string;
}

export class KbuddhiCertStack extends cdk.Stack {
  readonly certificate: acm.Certificate;

  constructor(scope: Construct, id: string, props: CertStackProps) {
    super(scope, id, props);

    const hostedZoneId = props.hostedZoneId
      ?? this.node.tryGetContext('hostedZoneId') as string | undefined;

    if (!hostedZoneId) {
      // Emit a placeholder output so cdk synth succeeds before Phase 1 is deployed
      new cdk.CfnOutput(this, 'CertNote', {
        value: 'Deploy KbuddhiStack first, then redeploy with --context hostedZoneId=Z...',
        description: 'hostedZoneId context variable is required for cert DNS validation',
      });
      // Create a self-referencing cert as placeholder to allow synth without the zone
      this.certificate = new acm.Certificate(this, 'PlaceholderCert', {
        domainName: 'kbuddhiai.com',
      });
      return;
    }

    // Import the hosted zone by ID (zone lives in us-east-2, cert in us-east-1 —
    // Route 53 is a global service so cross-region lookups work fine).
    const hostedZone = route53.HostedZone.fromHostedZoneAttributes(this, 'Zone', {
      hostedZoneId,
      zoneName: 'kbuddhiai.com',
    });

    // ACM cert covering apex + www — DNS validated (auto-adds CNAME records to R53)
    this.certificate = new acm.Certificate(this, 'SiteCert', {
      domainName: 'kbuddhiai.com',
      subjectAlternativeNames: ['www.kbuddhiai.com'],
      validation: acm.CertificateValidation.fromDns(hostedZone),
    });

    new cdk.CfnOutput(this, 'CertificateArn', {
      value: this.certificate.certificateArn,
      description: 'ACM cert ARN — pass as --context certArn=... when redeploying KbuddhiStack',
      exportName: 'KbuddhiCertArn',
    });
  }
}
