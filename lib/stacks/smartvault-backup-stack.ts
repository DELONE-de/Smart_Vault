import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { BackupOrchestrator } from '../constructs/backup-orchestrator';
import { SmartVaultIam } from '../constructs/iam-policies';
import { SnapshotWatcher } from '../constructs/snapshot-watcher';
import { ManifestBucket } from '../constructs/storage';
import { SmartVaultSns } from '../constructs/notifications';

export class SmartVaultBackupStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const iam = new SmartVaultIam(this, 'Iam');
    const manifestBucket = new ManifestBucket(this, 'ManifestBucket');
    const sns = new SmartVaultSns(this, 'SmartVaultSns', { email: 'you@example.com' });

    new BackupOrchestrator(this, 'BackupOrchestrator', {
      schedule: cdk.aws_events.Schedule.cron({
        minute: '0',
        hour: '2',  
      }),
      manifestBucket: manifestBucket.bucket,
      snsTopic: sns.topic,
      role: iam.backupRole
    });
  }
}


export class SnapshotWatcherStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const iam = new SmartVaultIam(this, 'Iam');
    const manifestBucket = new ManifestBucket(this, 'ManifestBucket');
    const sns = new SmartVaultSns(this, 'Notifications');

    new SnapshotWatcher(this, 'SnapshotWatcher', {
      schedule: cdk.aws_events.Schedule.cron({
        minute: '0',
        hour: '1',  
      }),
      manifestBucket: manifestBucket.bucket,
      snsTopic: sns.topic,
      role: iam.watcherRole
    });
  }
}