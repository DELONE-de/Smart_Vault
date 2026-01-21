import { Construct } from 'constructs';
import { aws_cloudwatch as cw, aws_lambda as lambda, Duration } from 'aws-cdk-lib';

interface SmartVaultMonitoringProps {
  lambdas: lambda.Function[];
}

export class SmartVaultMonitoring extends Construct {
  constructor(scope: Construct, id: string, props: SmartVaultMonitoringProps) {
    super(scope, id);

    props.lambdas.forEach(fn => {
      // CloudWatch Alarm for Lambda Errors
      new cw.Alarm(this, `${fn.node.id}-ErrorAlarm`, {
        alarmName: `${fn.node.id}-ErrorAlarm`,
        metric: fn.metricErrors({
          period: Duration.minutes(5),
        }),
        threshold: 1, // triggers on 1 error
        evaluationPeriods: 1,
        alarmDescription: `SmartVault alarm: ${fn.node.id} encountered errors`,
        treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      });

      // CloudWatch Alarm for Duration (optional)
      new cw.Alarm(this, `${fn.node.id}-DurationAlarm`, {
        alarmName: `${fn.node.id}-DurationAlarm`,
        metric: fn.metricDuration({
          period: Duration.minutes(5),
        }),
        threshold: fn.timeout?.toSeconds() || 300, // warn if Lambda is close to timeout
        evaluationPeriods: 1,
        alarmDescription: `SmartVault alarm: ${fn.node.id} execution time exceeded`,
        treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      });
    });
  }
}
