import { Construct } from 'constructs';
import { Topic } from 'aws-cdk-lib/aws-sns';
import { EmailSubscription } from 'aws-cdk-lib/aws-sns-subscriptions';
import { RemovalPolicy } from 'aws-cdk-lib';

export interface SmartVaultSnsProps {
  email?: string; // optional email for subscription
}

export class SmartVaultSns extends Construct {
  public readonly topic: Topic;

  constructor(scope: Construct, id: string, props?: SmartVaultSnsProps) {
    super(scope, id);

    this.topic = new Topic(this, 'SmartVaultTopic', {
      displayName: 'SmartVault Backup Alerts',
      topicName: 'SmartVaultBackupTopic',
    });

    this.topic.applyRemovalPolicy(RemovalPolicy.RETAIN);

    // Optional email subscription
    if (props?.email) {
      this.topic.addSubscription(new EmailSubscription(props.email));
    }
  }
}