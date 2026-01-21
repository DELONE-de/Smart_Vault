import { Construct } from 'constructs';
import {
  aws_lambda as lambda,
  aws_events as events,
  aws_events_targets as targets,
  aws_iam as iam,
  aws_s3 as s3,
  aws_sns as sns,
  Duration,
} from 'aws-cdk-lib';

export interface BackupOrchestratorProps {
  schedule: events.Schedule;
  manifestBucket: s3.Bucket;
  snsTopic: sns.Topic;
  role: iam.Role;
}

export class BackupOrchestrator extends Construct {
  public readonly function: lambda.Function;

  constructor(scope: Construct, id: string, props: BackupOrchestratorProps) {
    super(scope, id);

    this.function = new lambda.Function(this, 'BackupOrchestratorFn', {
      runtime: lambda.Runtime.PYTHON_3_11,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('lambda/backup-orchestrator'),
      role: props.role,
      environment: {
        MANIFEST_BUCKET: props.manifestBucket.bucketName,
        SNS_TOPIC_ARN: props.snsTopic.topicArn, 
      },
      timeout: Duration.minutes(15),
    });

    new events.Rule(this, 'BackupSchedule', {
      schedule: props.schedule,
      targets: [new targets.LambdaFunction(this.function)],
    });
     props.manifestBucket.grantWrite(this.function);
  }
}
