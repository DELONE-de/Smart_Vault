"""
SmartVault EBS Backup Initiator Lambda Function

This Lambda function initiates EBS backups for EC2 instances tagged with
SmartVault:backup = true. It creates snapshots and tags them appropriately,
then writes a manifest to S3 and publishes an SNS notification.

Responsibilities:
    - Discover eligible EC2 instances
    - Read backup configuration from instance tags
    - Enumerate and filter EBS volumes
    - Create and tag EBS snapshots
    - Write backup manifest to S3
    - Publish SNS notification

Non-Responsibilities:
    - Waiting for snapshot completion
    - Deleting or rotating old snapshots
    - Monitoring snapshot or copy status
"""

import boto3
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Set
from dataclasses import dataclass, field, asdict
from botocore.exceptions import ClientError

# ============================================================================
# CONFIGURATION
# ============================================================================

# Configure structured logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Formatter for CloudWatch
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter(
    '[%(levelname)s] %(asctime)s - %(name)s - %(message)s'
))
logger.addHandler(handler)


@dataclass(frozen=True)
class Config:
    """Immutable configuration loaded from environment variables."""
    manifest_s3_bucket: str
    manifest_s3_prefix: str
    sns_topic_arn: str
    aws_region: str
    
    # Tag key constants
    TAG_BACKUP_ENABLED: str = 'SmartVault:backup'
    TAG_RETENTION_DAYS: str = 'SmartVault:retention-days'
    TAG_TARGET_REGIONS: str = 'SmartVault:target-regions'
    TAG_EXCLUDED_VOLUMES: str = 'SmartVault:excluded-volumes'
    
    # Snapshot tag keys
    SNAPSHOT_TAG_CREATED_BY: str = 'CreatedBy'
    SNAPSHOT_TAG_INSTANCE_ID: str = 'InstanceId'
    SNAPSHOT_TAG_VOLUME_ID: str = 'VolumeId'
    SNAPSHOT_TAG_RETENTION_DAYS: str = 'RetentionDays'
    SNAPSHOT_TAG_SOURCE_REGION: str = 'SourceRegion'
    SNAPSHOT_TAG_TARGET_REGIONS: str = 'TargetRegions'
    SNAPSHOT_TAG_BACKUP_ID: str = 'BackupId'
    
    # Default values
    DEFAULT_RETENTION_DAYS: int = 7
    
    @classmethod
    def from_environment(cls) -> 'Config':
        """Load configuration from environment variables."""
        required_vars = ['MANIFEST_S3_BUCKET', 'SNS_TOPIC_ARN']
        missing = [var for var in required_vars if not os.environ.get(var)]
        
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}"
            )
        
        return cls(
            manifest_s3_bucket=os.environ['MANIFEST_S3_BUCKET'],
            manifest_s3_prefix=os.environ.get('MANIFEST_S3_PREFIX', 'backups/'),
            sns_topic_arn=os.environ['SNS_TOPIC_ARN'],
            aws_region=os.environ.get('AWS_REGION', 'us-east-1')
        )


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass
class BackupConfiguration:
    """Backup configuration extracted from EC2 instance tags."""
    retention_days: int
    target_regions: List[str]
    excluded_volume_ids: Set[str]
    
    @classmethod
    def from_instance_tags(cls, tags: List[Dict], config: Config) -> 'BackupConfiguration':
        """Parse backup configuration from EC2 instance tags."""
        tag_map = {tag['Key']: tag['Value'] for tag in tags} if tags else {}
        
        # Parse retention days
        retention_days = config.DEFAULT_RETENTION_DAYS
        if config.TAG_RETENTION_DAYS in tag_map:
            try:
                retention_days = int(tag_map[config.TAG_RETENTION_DAYS])
                if retention_days < 1:
                    logger.warning(
                        f"Invalid retention days value: {retention_days}. "
                        f"Using default: {config.DEFAULT_RETENTION_DAYS}"
                    )
                    retention_days = config.DEFAULT_RETENTION_DAYS
            except ValueError:
                logger.warning(
                    f"Cannot parse retention days: {tag_map[config.TAG_RETENTION_DAYS]}. "
                    f"Using default: {config.DEFAULT_RETENTION_DAYS}"
                )
        
        # Parse target regions (comma-separated)
        target_regions = []
        if config.TAG_TARGET_REGIONS in tag_map:
            target_regions = [
                region.strip() 
                for region in tag_map[config.TAG_TARGET_REGIONS].split(',')
                if region.strip()
            ]
        
        # Parse excluded volumes (comma-separated)
        excluded_volume_ids = set()
        if config.TAG_EXCLUDED_VOLUMES in tag_map:
            excluded_volume_ids = {
                vol_id.strip() 
                for vol_id in tag_map[config.TAG_EXCLUDED_VOLUMES].split(',')
                if vol_id.strip()
            }
        
        return cls(
            retention_days=retention_days,
            target_regions=target_regions,
            excluded_volume_ids=excluded_volume_ids
        )


@dataclass
class VolumeInfo:
    """Information about an EBS volume attached to an instance."""
    volume_id: str
    device_name: str
    instance_id: str
    
    
@dataclass
class SnapshotResult:
    """Result of a snapshot creation operation."""
    snapshot_id: str
    volume_id: str
    instance_id: str
    status: str
    start_time: str
    retention_days: int
    target_regions: List[str]
    source_region: str
    tags: Dict[str, str]
    
    
@dataclass
class BackupManifest:
    """Complete backup manifest containing all snapshot results."""
    backup_id: str
    initiated_at: str
    source_region: str
    total_instances_processed: int
    total_volumes_processed: int
    total_snapshots_created: int
    total_volumes_excluded: int
    total_errors: int
    snapshots: List[Dict[str, Any]]
    errors: List[Dict[str, str]]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert manifest to dictionary."""
        return asdict(self)


# ============================================================================
# AWS CLIENT WRAPPER
# ============================================================================

class AWSClients:
    """Centralized AWS client management with lazy initialization."""
    
    def __init__(self, region: str):
        self._region = region
        self._ec2: Optional[Any] = None
        self._s3: Optional[Any] = None
        self._sns: Optional[Any] = None
    
    @property
    def ec2(self):
        if self._ec2 is None:
            self._ec2 = boto3.client('ec2', region_name=self._region)
        return self._ec2
    
    @property
    def s3(self):
        if self._s3 is None:
            self._s3 = boto3.client('s3', region_name=self._region)
        return self._s3
    
    @property
    def sns(self):
        if self._sns is None:
            self._sns = boto3.client('sns', region_name=self._region)
        return self._sns


# ============================================================================
# CORE BACKUP LOGIC
# ============================================================================

class SmartVaultBackupInitiator:
    """
    Main class responsible for initiating EBS backups.
    
    This class follows a strict sequence of operations:
    1. Discover eligible EC2 instances
    2. Read backup configuration from tags
    3. Enumerate EBS volumes
    4. Create and tag snapshots
    5. Generate and upload manifest
    6. Publish SNS notification
    """
    
    def __init__(self, config: Config, clients: AWSClients):
        self.config = config
        self.clients = clients
        self.backup_id = str(uuid.uuid4())
        self.initiated_at = datetime.now(timezone.utc).isoformat()
        
    def execute(self) -> BackupManifest:
        """
        Execute the backup initiation workflow.
        
        Returns:
            BackupManifest containing all backup operation results.
        """
        logger.info(f"Starting SmartVault backup initiation. BackupId: {self.backup_id}")
        
        snapshots: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        total_instances = 0
        total_volumes = 0
        total_excluded = 0
        
        try:
            # Step 1: Discover eligible EC2 instances
            instances = self._discover_instances()
            total_instances = len(instances)
            logger.info(f"Discovered {total_instances} instances eligible for backup")
            
            # Process each instance
            for instance in instances:
                instance_id = instance['InstanceId']
                logger.info(f"Processing instance: {instance_id}")
                
                try:
                    # Step 2: Read backup configuration from tags
                    backup_config = BackupConfiguration.from_instance_tags(
                        instance.get('Tags', []),
                        self.config
                    )
                    
                    # Step 3: Enumerate EBS volumes
                    volumes = self._enumerate_volumes(instance)
                    
                    # Step 4: Filter excluded volumes
                    eligible_volumes, excluded_count = self._filter_volumes(
                        volumes, 
                        backup_config.excluded_volume_ids
                    )
                    total_excluded += excluded_count
                    
                    # Step 5: Create snapshots for eligible volumes
                    for volume in eligible_volumes:
                        total_volumes += 1
                        try:
                            snapshot_result = self._create_snapshot(
                                volume,
                                backup_config
                            )
                            snapshots.append(asdict(snapshot_result))
                            logger.info(
                                f"Created snapshot {snapshot_result.snapshot_id} "
                                f"for volume {volume.volume_id}"
                            )
                        except ClientError as e:
                            error_msg = f"Failed to create snapshot for volume {volume.volume_id}: {str(e)}"
                            logger.error(error_msg)
                            errors.append({
                                'instance_id': instance_id,
                                'volume_id': volume.volume_id,
                                'error': str(e)
                            })
                            
                except Exception as e:
                    error_msg = f"Failed to process instance {instance_id}: {str(e)}"
                    logger.error(error_msg)
                    errors.append({
                        'instance_id': instance_id,
                        'volume_id': 'N/A',
                        'error': str(e)
                    })
            
        except Exception as e:
            logger.error(f"Critical error during backup initiation: {str(e)}")
            errors.append({
                'instance_id': 'N/A',
                'volume_id': 'N/A',
                'error': f"Critical error: {str(e)}"
            })
        
        # Create manifest
        manifest = BackupManifest(
            backup_id=self.backup_id,
            initiated_at=self.initiated_at,
            source_region=self.config.aws_region,
            total_instances_processed=total_instances,
            total_volumes_processed=total_volumes,
            total_snapshots_created=len(snapshots),
            total_volumes_excluded=total_excluded,
            total_errors=len(errors),
            snapshots=snapshots,
            errors=errors
        )
        
        # Step 6: Upload manifest to S3
        self._upload_manifest(manifest)
        
        # Step 7: Publish SNS notification
        self._publish_notification(manifest)
        
        logger.info(
            f"Backup initiation complete. "
            f"Snapshots: {len(snapshots)}, Errors: {len(errors)}"
        )
        
        return manifest
    
    def _discover_instances(self) -> List[Dict]:
        """
        Discover EC2 instances tagged for SmartVault backup.
        
        Calls DescribeInstances with filter SmartVault:backup = true
        
        Returns:
            List of EC2 instance dictionaries.
        """
        logger.info("Discovering EC2 instances with SmartVault:backup = true")
        
        instances = []
        paginator = self.clients.ec2.get_paginator('describe_instances')
        
        page_iterator = paginator.paginate(
            Filters=[
                {
                    'Name': f'tag:{self.config.TAG_BACKUP_ENABLED}',
                    'Values': ['true', 'True', 'TRUE']
                },
                {
                    'Name': 'instance-state-name',
                    'Values': ['running', 'stopped']  # Only backup running/stopped instances
                }
            ]
        )
        
        for page in page_iterator:
            for reservation in page.get('Reservations', []):
                instances.extend(reservation.get('Instances', []))
        
        return instances
    
    def _enumerate_volumes(self, instance: Dict) -> List[VolumeInfo]:
        """
        Extract EBS volumes from instance block device mappings.
        
        Args:
            instance: EC2 instance dictionary from DescribeInstances.
            
        Returns:
            List of VolumeInfo objects.
        """
        instance_id = instance['InstanceId']
        volumes = []
        
        block_device_mappings = instance.get('BlockDeviceMappings', [])
        
        for mapping in block_device_mappings:
            # Skip instance store volumes (they don't have EBS info)
            if 'Ebs' not in mapping:
                continue
                
            ebs = mapping['Ebs']
            volume_id = ebs.get('VolumeId')
            
            if volume_id:
                volumes.append(VolumeInfo(
                    volume_id=volume_id,
                    device_name=mapping.get('DeviceName', 'unknown'),
                    instance_id=instance_id
                ))
        
        logger.debug(
            f"Enumerated {len(volumes)} EBS volumes for instance {instance_id}"
        )
        
        return volumes
    
    def _filter_volumes(
        self, 
        volumes: List[VolumeInfo], 
        excluded_ids: Set[str]
    ) -> tuple[List[VolumeInfo], int]:
        """
        Filter out excluded volumes.
        
        Args:
            volumes: List of VolumeInfo objects.
            excluded_ids: Set of volume IDs to exclude.
            
        Returns:
            Tuple of (eligible_volumes, excluded_count).
        """
        if not excluded_ids:
            return volumes, 0
            
        eligible = []
        excluded_count = 0
        
        for volume in volumes:
            if volume.volume_id in excluded_ids:
                logger.info(
                    f"Excluding volume {volume.volume_id} "
                    f"(instance: {volume.instance_id})"
                )
                excluded_count += 1
            else:
                eligible.append(volume)
        
        return eligible, excluded_count
    
    def _create_snapshot(
        self, 
        volume: VolumeInfo, 
        backup_config: BackupConfiguration
    ) -> SnapshotResult:
        """
        Create an EBS snapshot with proper tags.
        
        Calls CreateSnapshot and immediately applies mandatory tags.
        
        Args:
            volume: VolumeInfo object for the volume to snapshot.
            backup_config: BackupConfiguration for this instance.
            
        Returns:
            SnapshotResult with snapshot details.
        """
        # Prepare snapshot description
        description = (
            f"SmartVault backup | "
            f"Instance: {volume.instance_id} | "
            f"Volume: {volume.volume_id} | "
            f"Device: {volume.device_name} | "
            f"BackupId: {self.backup_id}"
        )
        
        # Prepare mandatory tags
        tags = {
            self.config.SNAPSHOT_TAG_CREATED_BY: 'SmartVault',
            self.config.SNAPSHOT_TAG_INSTANCE_ID: volume.instance_id,
            self.config.SNAPSHOT_TAG_VOLUME_ID: volume.volume_id,
            self.config.SNAPSHOT_TAG_RETENTION_DAYS: str(backup_config.retention_days),
            self.config.SNAPSHOT_TAG_SOURCE_REGION: self.config.aws_region,
            self.config.SNAPSHOT_TAG_BACKUP_ID: self.backup_id,
        }
        
        # Add target regions if specified
        if backup_config.target_regions:
            tags[self.config.SNAPSHOT_TAG_TARGET_REGIONS] = ','.join(
                backup_config.target_regions
            )
        
        # Convert tags to AWS format
        tag_specifications = [
            {
                'ResourceType': 'snapshot',
                'Tags': [
                    {'Key': k, 'Value': v} for k, v in tags.items()
                ]
            }
        ]
        
        # Create snapshot with tags (atomic operation)
        response = self.clients.ec2.create_snapshot(
            VolumeId=volume.volume_id,
            Description=description,
            TagSpecifications=tag_specifications
        )
        
        return SnapshotResult(
            snapshot_id=response['SnapshotId'],
            volume_id=volume.volume_id,
            instance_id=volume.instance_id,
            status=response['State'],
            start_time=response['StartTime'].isoformat(),
            retention_days=backup_config.retention_days,
            target_regions=backup_config.target_regions,
            source_region=self.config.aws_region,
            tags=tags
        )
    
    def _upload_manifest(self, manifest: BackupManifest) -> None:
        """
        Upload backup manifest to S3.
        
        Args:
            manifest: BackupManifest to upload.
        """
        # Generate S3 key with date partitioning
        date_prefix = datetime.now(timezone.utc).strftime('%Y/%m/%d')
        s3_key = (
            f"{self.config.manifest_s3_prefix}"
            f"{date_prefix}/"
            f"manifest-{self.backup_id}.json"
        )
        
        try:
            self.clients.s3.put_object(
                Bucket=self.config.manifest_s3_bucket,
                Key=s3_key,
                Body=json.dumps(manifest.to_dict(), indent=2, default=str),
                ContentType='application/json',
                ServerSideEncryption='AES256',
                Metadata={
                    'backup-id': self.backup_id,
                    'snapshot-count': str(manifest.total_snapshots_created),
                    'error-count': str(manifest.total_errors)
                }
            )
            logger.info(
                f"Uploaded manifest to s3://{self.config.manifest_s3_bucket}/{s3_key}"
            )
        except ClientError as e:
            logger.error(f"Failed to upload manifest to S3: {str(e)}")
            raise
    
    def _publish_notification(self, manifest: BackupManifest) -> None:
        """
        Publish SNS notification for backup initiation.
        
        Args:
            manifest: BackupManifest containing backup results.
        """
        # Determine notification status
        if manifest.total_errors > 0:
            status = "PARTIAL_SUCCESS" if manifest.total_snapshots_created > 0 else "FAILED"
        else:
            status = "SUCCESS"
        
        subject = f"SmartVault Backup Initiated - {status}"
        
        message = {
            'default': json.dumps({
                'backup_id': manifest.backup_id,
                'status': status,
                'initiated_at': manifest.initiated_at,
                'source_region': manifest.source_region,
                'summary': {
                    'instances_processed': manifest.total_instances_processed,
                    'volumes_processed': manifest.total_volumes_processed,
                    'snapshots_created': manifest.total_snapshots_created,
                    'volumes_excluded': manifest.total_volumes_excluded,
                    'errors': manifest.total_errors
                },
                'manifest_location': (
                    f"s3://{self.config.manifest_s3_bucket}/"
                    f"{self.config.manifest_s3_prefix}"
                    f"{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/"
                    f"manifest-{self.backup_id}.json"
                )
            }, default=str),
            'email': self._format_email_notification(manifest, status),
            'sms': f"SmartVault: {manifest.total_snapshots_created} snapshots created, {manifest.total_errors} errors"
        }
        
        try:
            self.clients.sns.publish(
                TopicArn=self.config.sns_topic_arn,
                Subject=subject[:100],  # SNS subject limit
                Message=json.dumps(message),
                MessageStructure='json',
                MessageAttributes={
                    'backup_id': {
                        'DataType': 'String',
                        'StringValue': self.backup_id
                    },
                    'status': {
                        'DataType': 'String',
                        'StringValue': status
                    },
                    'snapshot_count': {
                        'DataType': 'Number',
                        'StringValue': str(manifest.total_snapshots_created)
                    }
                }
            )
            logger.info(f"Published SNS notification. Status: {status}")
        except ClientError as e:
            logger.error(f"Failed to publish SNS notification: {str(e)}")
            # Don't raise - notification failure shouldn't fail the backup
    
    def _format_email_notification(
        self, 
        manifest: BackupManifest, 
        status: str
    ) -> str:
        """Format a human-readable email notification."""
        lines = [
            "=" * 60,
            "SmartVault EBS Backup Initiation Report",
            "=" * 60,
            "",
            f"Backup ID:     {manifest.backup_id}",
            f"Status:        {status}",
            f"Initiated At:  {manifest.initiated_at}",
            f"Source Region: {manifest.source_region}",
            "",
            "-" * 40,
            "Summary",
            "-" * 40,
            f"Instances Processed:  {manifest.total_instances_processed}",
            f"Volumes Processed:    {manifest.total_volumes_processed}",
            f"Snapshots Created:    {manifest.total_snapshots_created}",
            f"Volumes Excluded:     {manifest.total_volumes_excluded}",
            f"Errors:               {manifest.total_errors}",
            "",
        ]
        
        if manifest.errors:
            lines.extend([
                "-" * 40,
                "Errors",
                "-" * 40,
            ])
            for error in manifest.errors[:10]:  # Limit to first 10 errors
                lines.append(
                    f"  - Instance: {error.get('instance_id', 'N/A')}, "
                    f"Volume: {error.get('volume_id', 'N/A')}"
                )
                lines.append(f"    Error: {error.get('error', 'Unknown')}")
            
            if len(manifest.errors) > 10:
                lines.append(f"  ... and {len(manifest.errors) - 10} more errors")
        
        lines.extend([
            "",
            "=" * 60,
            "This is an automated message from SmartVault Backup System",
            "=" * 60,
        ])
        
        return "\n".join(lines)


# ============================================================================
# LAMBDA HANDLER
# ============================================================================

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AWS Lambda entry point for SmartVault EBS Backup Initiator.
    
    This function:
    1. Loads configuration from environment variables
    2. Initializes AWS clients
    3. Executes the backup initiation workflow
    4. Returns a summary of the operation
    
    Args:
        event: Lambda event (can contain override configuration)
        context: Lambda context object
        
    Returns:
        Dictionary containing operation summary and status.
    """
    logger.info(f"SmartVault Backup Initiator invoked. Event: {json.dumps(event)}")
    
    try:
        # Load configuration
        config = Config.from_environment()
        
        # Initialize AWS clients
        clients = AWSClients(config.aws_region)
        
        # Execute backup initiation
        initiator = SmartVaultBackupInitiator(config, clients)
        manifest = initiator.execute()
        
        # Determine response status code
        if manifest.total_errors == 0:
            status_code = 200
            status = "SUCCESS"
        elif manifest.total_snapshots_created > 0:
            status_code = 207  # Multi-Status
            status = "PARTIAL_SUCCESS"
        else:
            status_code = 500
            status = "FAILED"
        
        return {
            'statusCode': status_code,
            'body': {
                'status': status,
                'backup_id': manifest.backup_id,
                'initiated_at': manifest.initiated_at,
                'summary': {
                    'instances_processed': manifest.total_instances_processed,
                    'volumes_processed': manifest.total_volumes_processed,
                    'snapshots_created': manifest.total_snapshots_created,
                    'volumes_excluded': manifest.total_volumes_excluded,
                    'errors': manifest.total_errors
                },
                'manifest_bucket': config.manifest_s3_bucket
            }
        }
        
    except EnvironmentError as e:
        logger.error(f"Configuration error: {str(e)}")
        return {
            'statusCode': 500,
            'body': {
                'status': 'CONFIGURATION_ERROR',
                'error': str(e)
            }
        }
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        return {
            'statusCode': 500,
            'body': {
                'status': 'ERROR',
                'error': str(e)
            }
        }