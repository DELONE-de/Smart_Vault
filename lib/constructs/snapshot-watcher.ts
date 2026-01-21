import { Construct } from 'constructs';
import {
  aws_lambda as lambda,
  aws_events as events,
  aws_events_targets as targets,
  aws_s3 as s3,
  aws_sns as sns,
  aws_iam as iam,
  Duration,
} from 'aws-cdk-lib';

export interface snapshotWatcherProps {
  schedule: events.Schedule;
   manifestBucket: s3.Bucket;
    snsTopic: sns.Topic;
  role: iam.Role;
}

export class SnapshotWatcher extends Construct {
  public readonly function: lambda.Function;

  constructor(scope: Construct, id: string, props: snapshotWatcherProps) {
    super(scope, id);

    this.function = new lambda.Function(this, 'SnapshotWatcherFn', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('lambda/snapshot-watcher'),
      role: props.role,
      environment: {
        MANIFEST_BUCKET: props.manifestBucket.bucketName,
        SNS_TOPIC_ARN: props.snsTopic.topicArn, 
      },
      timeout: Duration.minutes(15),
    });

    new events.Rule(this, 'SnapshotSchedule', {
      schedule: props.schedule,
      targets: [new targets.LambdaFunction(this.function)],
    });

      props.manifestBucket.grantWrite(this.function);

  }
}
