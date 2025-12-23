#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { SmartVaultBackupStack } from '../lib/stacks/smartvault-backup-stack';
import { SmartVaultExpiryStack } from '../lib/stacks/smartvault-expiry-stack';
import { SmartVaultMonitoringStack } from '../lib/stacks/smartvault-monitoring-stack';
import { SmartVaultNotificationsStack } from '../lib/stacks/smartvault-notifications-stack';

const app = new cdk.App();

new SmartVaultBackupStack(app, 'SmartVaultBackupStack');
new SmartVaultExpiryStack(app, 'SmartVaultExpiryStack');
new SmartVaultMonitoringStack(app, 'SmartVaultMonitoringStack');
new SmartVaultNotificationsStack(app, 'SmartVaultNotificationsStack');
