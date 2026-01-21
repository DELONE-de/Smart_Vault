import { Construct } from 'constructs';
import {
  aws_lambda as lambda,
  aws_events as events,
  aws_events_targets as targets,
   aws_s3 as s3,
  aws_iam as iam,
  aws_sns as sns,
  Duration,
} from 'aws-cdk-lib';

export interface expireSnapshotsProps {
  schedule: events.Schedule;
   manifestBucket: s3.Bucket;
    snsTopic: sns.Topic;
  role: iam.Role;
}

export class ExpireSnapshots extends Construct {
  public readonly function: lambda.Function;

  constructor(scope: Construct, id: string, props: expireSnapshotsProps) {
    super(scope, id);

    this.function = new lambda.Function(this, 'ExpireSnapshotsFn', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('lambda/expire-snapshots'),
      role: props.role,
       environment: {
        MANIFEST_BUCKET: props.manifestBucket.bucketName,
        SNS_TOPIC_ARN: props.snsTopic.topicArn, 
      },
      timeout: Duration.minutes(15),
    });

    new events.Rule(this, 'ExpireSnapshotsSchedule', {
      schedule: props.schedule,
      targets: [new targets.LambdaFunction(this.function)],
    });

     props.manifestBucket.grantWrite(this.function);

  }
}
