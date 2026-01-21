import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { SmartVaultSns } from '../constructs/notifications';

export class SmartVaultNotificationsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    new SmartVaultSns(this, 'Notifications');
  }
}
