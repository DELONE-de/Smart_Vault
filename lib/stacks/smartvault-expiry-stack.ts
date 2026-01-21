import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { ExpireSnapshots } from '../constructs/expire-snapshots';
import { SmartVaultIam } from '../constructs/iam-policies';
import { ManifestBucket } from '../constructs/storage';
import { SmartVaultSns } from '../constructs/notifications';

export class SmartVaultExpiryStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const iam = new SmartVaultIam(this, 'Iam');
    const manifestBucket = new ManifestBucket(this, 'ManifestBucket');
    const sns = new SmartVaultSns(this, 'Notifications');

    new ExpireSnapshots(this, 'ExpireSnapshots', {
      schedule: cdk.aws_events.Schedule.cron({
        minute: '0',
        hour: '3',  
      }),
      manifestBucket: manifestBucket.bucket,
      snsTopic: sns.topic,
      role: iam.expiryRole
    });
  }
}