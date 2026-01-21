import { Stack, StackProps, Duration, aws_events as events, aws_events_targets as targets } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { BackupOrchestrator } from './constructs/backup-orchestrator';
import { SnapshotWatcher } from './constructs/snapshot-watcher';
import { ExpireSnapshots } from './constructs/expire-snapshots';
import { ManifestBucket } from './constructs/storage';
import { SmartVaultIam } from './constructs/iam-policies';
import { SmartVaultSns } from './constructs/notifications';
import { SmartVaultMonitoring } from './constructs/monitoring';

export class SmartVaultBackupStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    // -------------------------
    // 1️⃣ IAM
    // -------------------------
    const iam = new SmartVaultIam(this, 'Iam');

    // -------------------------
    // 2️⃣ Manifest S3 bucket
    // -------------------------
    const manifestBucket = new ManifestBucket(this, 'ManifestBucket');

    // -------------------------
    // 3️⃣ SNS for alerts
    // -------------------------
    const sns = new SmartVaultSns(this, 'SmartVaultSns', {
      email: 'alerts@example.com', // your alert email
    });

    // -------------------------
    // 4️⃣ Backup Orchestrator Lambda
    // -------------------------
    const orchestrator = new BackupOrchestrator(this, 'BackupOrchestrator', {
      schedule: events.Schedule.cron({ hour: '2', minute: '0' }),
      role: iam.backupRole,
      manifestBucket: manifestBucket.bucket,
      snsTopic: sns.topic,
    });

    // -------------------------
    // 5️⃣ Snapshot Watcher Lambda
    // -------------------------
    const watcher = new SnapshotWatcher(this, 'SnapshotWatcher', {
      schedule: events.Schedule.cron({ hour: '1', minute: '0' }),
      manifestBucket: manifestBucket.bucket,
      snsTopic: sns.topic,
      role: iam.backupRole,
    });

    // -------------------------
    // 6️⃣ Expiry Lambda
    // -------------------------
    const expiry = new ExpireSnapshots(this, 'ExpiryLambda', {
      schedule: events.Schedule.cron({ hour: '3', minute: '0' }),
      role: iam.backupRole,
      manifestBucket: manifestBucket.bucket,
      snsTopic: sns.topic,
    });

    // -------------------------
    // 7️⃣ Monitoring
    // -------------------------
    new SmartVaultMonitoring(this, 'Monitoring', {
      lambdas: [orchestrator.function, watcher.function, expiry.function],
    });
  }
}
