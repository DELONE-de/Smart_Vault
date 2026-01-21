import { Construct } from 'constructs';
import {
  aws_iam as iam,
} from 'aws-cdk-lib';

export class SmartVaultIam extends Construct {
  public readonly backupRole: iam.Role;
  public readonly watcherRole: iam.Role;
  public readonly expiryRole: iam.Role;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    // ---------- Backup Orchestrator Role ----------
    this.backupRole = new iam.Role(this, 'BackupRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'SmartVault backup orchestrator role',
    });

    this.backupRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName(
        'service-role/AWSLambdaBasicExecutionRole'
      )
    );

    this.backupRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'ec2:DescribeInstances',
          'ec2:DescribeVolumes',
          'ec2:CreateSnapshot',
          'ec2:CreateTags',
        ],
        resources: ['*'],
      })
    );

    // ---------- Snapshot Watcher Role ----------
    this.watcherRole = new iam.Role(this, 'WatcherRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'SmartVault snapshot watcher role',
    });

    this.watcherRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName(
        'service-role/AWSLambdaBasicExecutionRole'
      )
    );

    this.watcherRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'ec2:DescribeSnapshots',
          'ec2:CopySnapshot',
          'ec2:CreateTags',
        ],
        resources: ['*'],
      })
    );

    // ---------- Expiry Role ----------
    this.expiryRole = new iam.Role(this, 'ExpiryRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'SmartVault snapshot expiry role',
    });

    this.expiryRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName(
        'service-role/AWSLambdaBasicExecutionRole'
      )
    );

    this.expiryRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'ec2:DescribeSnapshots',
          'ec2:DeleteSnapshot',
        ],
        resources: ['*'],
      })
    );
  }
}
