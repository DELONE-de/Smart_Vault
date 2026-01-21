import { Construct } from 'constructs';
import { Bucket, BucketEncryption, BlockPublicAccess } from 'aws-cdk-lib/aws-s3';
import { RemovalPolicy, Duration } from 'aws-cdk-lib';

export class ManifestBucket extends Construct {
  public readonly bucket: Bucket;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    this.bucket = new Bucket(this, 'SmartVaultManifestBucket', {
      bucketName: `smartvault-manifests-${scope.node.tryGetContext('account')}`,
      encryption: BucketEncryption.S3_MANAGED,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN, // DO NOT DESTROY BACKUP HISTORY
      lifecycleRules: [
        {
          expiration: Duration.days(365), // keep manifests 1 year
        },
      ],
    });
  }
}
