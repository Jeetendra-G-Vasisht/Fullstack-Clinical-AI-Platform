/*
 * kBuddhi AI — AWS CDK Stack
 * Region: us-east-2 (ACM cert lives separately in us-east-1 via KbuddhiCertStack)
 *
 * ── ESTIMATED MONTHLY COST AT 200 ACTIVE USERS ─────────────────────────────
 *   S3 static bucket      ~$0.05   (< 10 MB site, few thousand requests)
 *   S3 uploads bucket     ~$0.30   (10 GB @ $0.023/GB)
 *   CloudFront            ~$0.85   (10 GB transfer @ $0.0085/GB, free tier year 1)
 *   ACM certificate        $0.00   (free)
 *   Cognito User Pool      $0.00   (free ≤ 50 000 MAU)
 *   SES                   ~$0.20   (200 users × 10 emails/mo = 2 000 @ $0.10/1 000)
 *   Lambda (4 fns)         $0.00   (well within 1 M req/mo free tier)
 *   DynamoDB on-demand    ~$0.01   (< 10 000 writes/mo)
 *   API Gateway REST      ~$0.02   ($3.50/M calls; < 10 000 calls/mo)
 *   Route 53              ~$0.51   ($0.50/hosted zone + $0.40/M queries)
 *   Glue (on-demand)       $0.00   (not scheduled; pay only when triggered)
 *   Athena                 $0.00   (pay per query; not in active use yet)
 *   ─────────────────────────────────────────────────────────────────────────
 *   TOTAL                 ~$2–3 / month
 * ────────────────────────────────────────────────────────────────────────────
 */

import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as ses from 'aws-cdk-lib/aws-ses';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as glue from 'aws-cdk-lib/aws-glue';
import * as athena from 'aws-cdk-lib/aws-athena';
import { Construct } from 'constructs';

const DOMAIN = 'kbuddhiai.com';
const SES_FROM = `noreply@${DOMAIN}`;
const ALLOWED_ORIGIN = `https://${DOMAIN}`;

export class KbuddhiStack extends cdk.Stack {
  /** Exposed so the cert stack can import the zone for DNS validation */
  readonly hostedZone: route53.PublicHostedZone;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ── Context variables (optional — populated in later deploy phases) ───────
    const certArn = this.node.tryGetContext('certArn') as string | undefined;

    // ═══════════════════════════════════════════════════════════════════════════
    // ROUTE 53 — hosted zone (output nameservers → GoDaddy)
    // ═══════════════════════════════════════════════════════════════════════════
    this.hostedZone = new route53.PublicHostedZone(this, 'HostedZone', {
      zoneName: DOMAIN,
      comment: 'kBuddhi AI — managed by CDK',
    });

    new cdk.CfnOutput(this, 'HostedZoneId', {
      value: this.hostedZone.hostedZoneId,
      description: 'Route 53 hosted zone ID — needed to deploy KbuddhiCertStack',
    });
    new cdk.CfnOutput(this, 'NameServers', {
      value: cdk.Fn.join(', ', this.hostedZone.hostedZoneNameServers!),
      description: 'Point these 4 nameservers in GoDaddy DNS Management',
    });

    // ═══════════════════════════════════════════════════════════════════════════
    // SES — domain identity (auto-adds DKIM CNAME records to Route 53)
    // NOTE: SES starts in sandbox mode. Request production access via the
    //       AWS console (SES → Account dashboard → Request production access).
    //       Until then, OTP emails only reach SES-verified addresses.
    // ═══════════════════════════════════════════════════════════════════════════
    new ses.EmailIdentity(this, 'SesDomainIdentity', {
      identity: ses.Identity.publicHostedZone(this.hostedZone),
      dkimSigning: true,
      mailFromDomain: `mail.${DOMAIN}`,
    });

    new cdk.CfnOutput(this, 'SesSandboxNote', {
      value: 'Request SES production access at: console.aws.amazon.com/ses → Account dashboard',
      description: 'SES is in sandbox until production access is granted',
    });

    // ═══════════════════════════════════════════════════════════════════════════
    // S3 — static site bucket (private; CloudFront OAC serves it)
    // ═══════════════════════════════════════════════════════════════════════════
    const staticBucket = new s3.Bucket(this, 'StaticBucket', {
      bucketName: `kbuddhiai-static-${this.account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // ═══════════════════════════════════════════════════════════════════════════
    // S3 — uploads bucket
    // Folder structure: uploads/user_id={cognito_sub}/year={YYYY}/month={MM}/filename
    // This layout is Athena/Glue partition-compatible.
    // ═══════════════════════════════════════════════════════════════════════════
    const uploadsBucket = new s3.Bucket(this, 'UploadsBucket', {
      bucketName: `kbuddhiai-uploads-${this.account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      cors: [
        {
          allowedOrigins: [ALLOWED_ORIGIN, 'https://www.kbuddhiai.com'],
          allowedMethods: [s3.HttpMethods.POST, s3.HttpMethods.GET, s3.HttpMethods.PUT],
          allowedHeaders: ['*'],
          maxAge: 3000,
        },
      ],
      lifecycleRules: [
        {
          // Transition large objects to IA after 90 days to reduce cost
          transitions: [{ storageClass: s3.StorageClass.INFREQUENT_ACCESS, transitionAfter: cdk.Duration.days(90) }],
        },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ── Glue scripts bucket (Phase 2 scaffold) ─────────────────────────────
    const glueScriptsBucket = new s3.Bucket(this, 'GlueScriptsBucket', {
      bucketName: `kbuddhiai-glue-scripts-${this.account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // Upload Glue PySpark script
    new s3deploy.BucketDeployment(this, 'GlueScriptDeploy', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '../glue'))],
      destinationBucket: glueScriptsBucket,
      destinationKeyPrefix: 'scripts/',
    });

    // ═══════════════════════════════════════════════════════════════════════════
    // CLOUDFRONT — OAC + distribution
    // ═══════════════════════════════════════════════════════════════════════════
    const oac = new cloudfront.S3OriginAccessControl(this, 'OAC', {
      description: 'kBuddhi AI static site OAC',
      signing: cloudfront.Signing.SIGV4_NO_OVERRIDE,
    });

    // Use ACM cert when certArn context is set (Phase 3 deploy),
    // otherwise fall back to the default *.cloudfront.net certificate.
    const siteCert = certArn
      ? acm.Certificate.fromCertificateArn(this, 'SiteCert', certArn)
      : undefined;

    const distribution = new cloudfront.Distribution(this, 'SiteDistribution', {
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(staticBucket, {
          originAccessControl: oac,
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      },
      domainNames: siteCert ? [DOMAIN, `www.${DOMAIN}`] : undefined,
      certificate: siteCert,
      defaultRootObject: 'index.html',
      errorResponses: [
        { httpStatus: 403, responseHttpStatus: 200, responsePagePath: '/index.html' },
        { httpStatus: 404, responseHttpStatus: 200, responsePagePath: '/index.html' },
      ],
      comment: 'kBuddhi AI — static portal',
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
    });

    // Allow CloudFront OAC to read from the static bucket
    staticBucket.addToResourcePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [staticBucket.arnForObjects('*')],
        principals: [new iam.ServicePrincipal('cloudfront.amazonaws.com')],
        conditions: {
          StringEquals: {
            'AWS:SourceArn': `arn:aws:cloudfront::${this.account}:distribution/${distribution.distributionId}`,
          },
        },
      }),
    );

    // Route 53 A record → CloudFront (only when certArn is provided)
    if (certArn) {
      new route53.ARecord(this, 'SiteARecord', {
        zone: this.hostedZone,
        recordName: DOMAIN,
        target: route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(distribution)),
      });
      new route53.ARecord(this, 'SiteWwwARecord', {
        zone: this.hostedZone,
        recordName: `www.${DOMAIN}`,
        target: route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(distribution)),
      });
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // COGNITO — User Pool (replaces Firebase Authentication)
    // ═══════════════════════════════════════════════════════════════════════════
    const userPool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: 'kbuddhiai-users',
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      autoVerify: { email: true },
      standardAttributes: {
        email: { required: true, mutable: false },
      },
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
        tempPasswordValidity: cdk.Duration.days(7),
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      // Use Cognito's built-in email for account verification & password reset.
      // After SES production access is granted, swap to:
      //   email: cognito.UserPoolEmail.withSES({ sesRegion: 'us-east-2', fromEmail: SES_FROM, fromName: 'kBuddhi AI' })
      email: cognito.UserPoolEmail.withCognito(),
    });

    // Public app client — no secret (browser-side SignUp / ForgotPassword calls).
    // OAuth is disabled: we use ADMIN_USER_PASSWORD_AUTH via Lambda, not hosted UI.
    const userPoolClient = new cognito.UserPoolClient(this, 'UserPoolClient', {
      userPool,
      userPoolClientName: 'kbuddhiai-web',
      generateSecret: false,
      authFlows: {
        adminUserPassword: true,  // Used by send-otp Lambda (AdminInitiateAuth)
        userSrp: true,            // Available for SDK-based flows
      },
      disableOAuth: true,
      accessTokenValidity: cdk.Duration.hours(1),
      idTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(30),
    });

    // ═══════════════════════════════════════════════════════════════════════════
    // DYNAMODB — OTP table (replaces sessionStorage OTP, replaces EmailJS)
    // ═══════════════════════════════════════════════════════════════════════════
    const otpTable = new dynamodb.Table(this, 'OtpTable', {
      tableName: 'kbuddhiai-otp-codes',
      partitionKey: { name: 'email', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expiry',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // ═══════════════════════════════════════════════════════════════════════════
    // IAM — shared Lambda execution role
    // ═══════════════════════════════════════════════════════════════════════════
    const lambdaRole = new iam.Role(this, 'LambdaRole', {
      roleName: 'kbuddhiai-lambda-role',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
      inlinePolicies: {
        KbuddhaiLambdaPolicy: new iam.PolicyDocument({
          statements: [
            // S3 — uploads bucket
            new iam.PolicyStatement({
              actions: ['s3:PutObject', 's3:GetObject', 's3:ListBucket', 's3:DeleteObject'],
              resources: [uploadsBucket.bucketArn, `${uploadsBucket.bucketArn}/*`],
            }),
            // DynamoDB — OTP table
            new iam.PolicyStatement({
              actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:DeleteItem', 'dynamodb:UpdateItem'],
              resources: [otpTable.tableArn],
            }),
            // Cognito — admin auth (send-otp Lambda needs this)
            new iam.PolicyStatement({
              actions: [
                'cognito-idp:AdminInitiateAuth',
                'cognito-idp:AdminGetUser',
                'cognito-idp:AdminConfirmSignUp',
              ],
              resources: [userPool.userPoolArn],
            }),
            // SES — send email
            new iam.PolicyStatement({
              actions: ['ses:SendEmail', 'ses:SendRawEmail'],
              resources: ['*'],
            }),
            // Glue — start job runs (triggered post-upload)
            new iam.PolicyStatement({
              actions: ['glue:StartJobRun', 'glue:GetJobRun'],
              resources: ['*'],
            }),
          ],
        }),
      },
    });

    // Common Lambda environment variables
    const commonEnv = {
      BUCKET_NAME: uploadsBucket.bucketName,
      BUCKET_REGION: this.region,
      ALLOWED_ORIGIN,
    };

    // ═══════════════════════════════════════════════════════════════════════════
    // LAMBDA — send-otp  (NEW)
    // ═══════════════════════════════════════════════════════════════════════════
    const sendOtpFn = new lambda.Function(this, 'SendOtpFn', {
      functionName: 'kbuddhiai-send-otp',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'lambda_function.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambdas/send-otp')),
      role: lambdaRole,
      timeout: cdk.Duration.seconds(15),
      environment: {
        ...commonEnv,
        COGNITO_USER_POOL_ID: userPool.userPoolId,
        COGNITO_CLIENT_ID: userPoolClient.userPoolClientId,
        DYNAMODB_TABLE: otpTable.tableName,
        SES_SENDER: SES_FROM,
      },
    });

    // ═══════════════════════════════════════════════════════════════════════════
    // LAMBDA — verify-otp  (NEW)
    // ═══════════════════════════════════════════════════════════════════════════
    const verifyOtpFn = new lambda.Function(this, 'VerifyOtpFn', {
      functionName: 'kbuddhiai-verify-otp',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'lambda_function.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambdas/verify-otp')),
      role: lambdaRole,
      timeout: cdk.Duration.seconds(10),
      environment: {
        ...commonEnv,
        DYNAMODB_TABLE: otpTable.tableName,
      },
    });

    // ═══════════════════════════════════════════════════════════════════════════
    // LAMBDA — get-upload-url  (UPDATED: partitioned path + user_sub)
    // ═══════════════════════════════════════════════════════════════════════════
    const getUploadUrlFn = new lambda.Function(this, 'GetUploadUrlFn', {
      functionName: 'kbuddhiai-get-upload-url',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'lambda_function.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambdas/get-upload-url')),
      role: lambdaRole,
      timeout: cdk.Duration.seconds(10),
      environment: commonEnv,
    });

    // ═══════════════════════════════════════════════════════════════════════════
    // LAMBDA — chat  (UPDATED env vars; logic unchanged)
    // ═══════════════════════════════════════════════════════════════════════════
    const chatFn = new lambda.Function(this, 'ChatFn', {
      functionName: 'kbuddhiai-chat',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'lambda_function.lambda_handler',
      // Package chat Lambda with its dependencies using Docker bundling.
      // If Docker is unavailable, run: pip install -r requirements.txt -t lambdas/chat/ first.
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambdas/chat'), {
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          local: {
            tryBundle(outputDir: string): boolean {
              try {
                const { execSync } = require('child_process');
                const srcDir = path.join(__dirname, '../lambdas/chat');
                // Force Linux-compatible wheels so macOS-compiled binaries (e.g. lxml)
                // don't land in the Lambda package and crash on Amazon Linux.
                execSync(
                  `pip install -r requirements.txt -t "${outputDir}" ` +
                  `--platform manylinux2014_x86_64 --only-binary=:all: ` +
                  `--python-version 3.12 --implementation cp --quiet ` +
                  `&& cp -r "${srcDir}/." "${outputDir}"`,
                  { cwd: srcDir, stdio: 'pipe' },
                );
                return true;
              } catch {
                return false;
              }
            },
          },
          command: [
            'bash', '-c',
            'pip install -r requirements.txt -t /asset-output --quiet && cp -au . /asset-output',
          ],
        },
      }),
      role: lambdaRole,
      timeout: cdk.Duration.minutes(5),
      memorySize: 1024,
      environment: {
        ...commonEnv,
        // OPENROUTER_API_KEY is set manually via AWS Console or Secrets Manager
        // to avoid committing the key to source control.
        OPENROUTER_API_KEY: '',
      },
    });

    // ═══════════════════════════════════════════════════════════════════════════
    // API GATEWAY — REST API
    // ═══════════════════════════════════════════════════════════════════════════
    const api = new apigateway.RestApi(this, 'Api', {
      restApiName: 'kbuddhiai-api',
      description: 'kBuddhi AI — HIPAA portal REST API',
      defaultCorsPreflightOptions: {
        allowOrigins: [ALLOWED_ORIGIN, 'https://www.kbuddhiai.com'],
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: ['Content-Type', 'Authorization', 'X-Requested-With'],
        maxAge: cdk.Duration.hours(1),
      },
      deployOptions: {
        stageName: 'prod',
        throttlingBurstLimit: 50,
        throttlingRateLimit: 20,
      },
    });

    const addLambdaPost = (resource: apigateway.Resource, fn: lambda.Function) => {
      resource.addMethod('POST', new apigateway.LambdaIntegration(fn, { proxy: true }));
    };

    addLambdaPost(api.root.addResource('send-otp'),      sendOtpFn);
    addLambdaPost(api.root.addResource('verify-otp'),    verifyOtpFn);
    addLambdaPost(api.root.addResource('get-upload-url'), getUploadUrlFn);
    addLambdaPost(api.root.addResource('chat'),          chatFn);

    // ═══════════════════════════════════════════════════════════════════════════
    // STATIC SITE DEPLOYMENT — upload frontend files to S3
    // ═══════════════════════════════════════════════════════════════════════════
    // config.js is generated here with the real CDK token values
    const configJs = `/* Auto-generated by CDK — do not edit manually */
window.APP_CONFIG = {
  COGNITO_REGION:      "${this.region}",
  COGNITO_USER_POOL_ID: "${userPool.userPoolId}",
  COGNITO_CLIENT_ID:   "${userPoolClient.userPoolClientId}",
  API_BASE_URL:        "${api.url.replace(/\/$/, '')}",
  UPLOADS_BUCKET:      "${uploadsBucket.bucketName}",
  STATIC_BUCKET:       "${staticBucket.bucketName}",
  SES_SENDER:          "${SES_FROM}",
};`;

    new s3deploy.BucketDeployment(this, 'SiteDeployment', {
      sources: [
        s3deploy.Source.asset(path.join(__dirname, '../../'), {
          exclude: ['infra/**', 'node_modules/**', '.git/**', '*.md', 'CNAME', '.env*', 'PROJECT_SUMMARY.txt'],
        }),
        s3deploy.Source.data('config.js', configJs),
      ],
      destinationBucket: staticBucket,
      distribution,
      distributionPaths: ['/*'],
      prune: false,
    });

    // ═══════════════════════════════════════════════════════════════════════════
    // PHASE 2 SCAFFOLD — Glue, Athena, HealthLake IAM role
    // These are created but NOT wired into the live application yet.
    // ═══════════════════════════════════════════════════════════════════════════

    // ── Glue IAM role ──────────────────────────────────────────────────────────
    const glueJobRole = new iam.Role(this, 'GlueJobRole', {
      roleName: 'kbuddhiai-glue-job-role',
      assumedBy: new iam.ServicePrincipal('glue.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSGlueServiceRole'),
      ],
      inlinePolicies: {
        GlueS3Policy: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:ListBucket'],
              resources: [
                uploadsBucket.bucketArn,
                `${uploadsBucket.bucketArn}/*`,
                glueScriptsBucket.bucketArn,
                `${glueScriptsBucket.bucketArn}/*`,
              ],
            }),
          ],
        }),
      },
    });

    // ── Glue Job — on-demand, NOT scheduled ──────────────────────────────────
    new glue.CfnJob(this, 'ConvertToParquetJob', {
      name: 'kbuddhiai-convert-to-parquet',
      role: glueJobRole.roleArn,
      command: {
        name: 'glueetl',
        scriptLocation: `s3://${glueScriptsBucket.bucketName}/scripts/convert_to_parquet.py`,
        pythonVersion: '3',
      },
      defaultArguments: {
        '--job-language': 'python',
        '--enable-job-insights': 'true',
        '--enable-auto-scaling': 'false',
        '--TempDir': `s3://${glueScriptsBucket.bucketName}/temp/`,
        '--SOURCE_BUCKET': uploadsBucket.bucketName,
        '--PARQUET_PREFIX': 'parquet/',
      },
      glueVersion: '4.0',
      maxCapacity: 2,
      maxRetries: 0,
      timeout: 60,
      description: 'Converts uploaded files (CSV/Excel) to Parquet format for Athena querying. ' +
                   'Triggered on-demand via Lambda after upload — NOT scheduled.',
    });

    // ── Glue Data Catalog database ────────────────────────────────────────────
    new glue.CfnDatabase(this, 'GlueDatabase', {
      catalogId: this.account,
      databaseInput: {
        name: 'kbuddhiai_data',
        description: 'kBuddhi AI — Athena/Glue catalog for uploaded file data',
      },
    });

    // ── Glue Table — points at Parquet prefix in uploads bucket ──────────────
    new glue.CfnTable(this, 'ParquetTable', {
      catalogId: this.account,
      databaseName: 'kbuddhiai_data',
      tableInput: {
        name: 'uploads',
        description: 'Parquet-converted user uploads, partitioned by user_id/year/month',
        tableType: 'EXTERNAL_TABLE',
        parameters: {
          'classification': 'parquet',
          'has_encrypted_data': 'false',
        },
        partitionKeys: [
          { name: 'user_id', type: 'string' },
          { name: 'year',    type: 'string' },
          { name: 'month',   type: 'string' },
        ],
        storageDescriptor: {
          location: `s3://${uploadsBucket.bucketName}/parquet/`,
          inputFormat: 'org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat',
          outputFormat: 'org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat',
          serdeInfo: {
            serializationLibrary: 'org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe',
            parameters: { 'serialization.format': '1' },
          },
          columns: [
            { name: 'row_index', type: 'bigint' },
            { name: 'content',   type: 'string' },
            { name: 'filename',  type: 'string' },
            { name: 'uploaded_at', type: 'timestamp' },
          ],
        },
      },
    });

    // ── Athena workgroup ──────────────────────────────────────────────────────
    new athena.CfnWorkGroup(this, 'AthenaWorkgroup', {
      name: 'kbuddhiai',
      description: 'kBuddhi AI — Athena workgroup for querying Parquet uploads',
      workGroupConfiguration: {
        resultConfiguration: {
          outputLocation: `s3://${glueScriptsBucket.bucketName}/athena-results/`,
        },
        enforceWorkGroupConfiguration: true,
        publishCloudWatchMetricsEnabled: false,
        bytesScannedCutoffPerQuery: 1073741824,  // 1 GB scan limit per query
      },
      state: 'ENABLED',
    });

    // ── HealthLake IAM role (scaffold — NOT deployed yet) ─────────────────────
    // Activate HealthLake by following the steps in HEALTHLAKE_UPGRADE.md.
    new iam.Role(this, 'HealthLakeReadyRole', {
      roleName: 'HealthLakeReadyRole',
      assumedBy: new iam.ServicePrincipal('healthlake.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonHealthLakeFullAccess'),
      ],
      description: 'Pre-created role for future HealthLake FHIR datastore. See HEALTHLAKE_UPGRADE.md.',
      inlinePolicies: {
        HealthLakeS3Policy: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ['s3:GetObject', 's3:PutObject', 's3:ListBucket'],
              resources: [
                uploadsBucket.bucketArn,
                `${uploadsBucket.bucketArn}/fhir/*`,
              ],
            }),
          ],
        }),
      },
    });

    // ═══════════════════════════════════════════════════════════════════════════
    // CDK OUTPUTS
    // ═══════════════════════════════════════════════════════════════════════════
    new cdk.CfnOutput(this, 'CloudFrontDomain', {
      value: distribution.distributionDomainName,
      description: 'CloudFront domain (usable immediately — kbuddhiai.com alias active after cert deploy)',
    });
    new cdk.CfnOutput(this, 'ApiGatewayUrl', {
      value: api.url,
      description: 'API Gateway base URL — baked into config.js automatically',
    });
    new cdk.CfnOutput(this, 'CognitoUserPoolId', {
      value: userPool.userPoolId,
      description: 'Cognito User Pool ID — baked into config.js automatically',
    });
    new cdk.CfnOutput(this, 'CognitoClientId', {
      value: userPoolClient.userPoolClientId,
      description: 'Cognito App Client ID — baked into config.js automatically',
    });
    new cdk.CfnOutput(this, 'StaticBucketName', {
      value: staticBucket.bucketName,
    });
    new cdk.CfnOutput(this, 'UploadsBucketName', {
      value: uploadsBucket.bucketName,
    });
    new cdk.CfnOutput(this, 'FhirS3Path', {
      value: `s3://${uploadsBucket.bucketName}/fhir/`,
      description: 'FHIR-ready S3 prefix for future HealthLake integration',
    });
    new cdk.CfnOutput(this, 'ParquetS3Path', {
      value: `s3://${uploadsBucket.bucketName}/parquet/`,
      description: 'Parquet output prefix for Glue job',
    });
  }
}
