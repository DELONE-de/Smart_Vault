# snapshot_watcher.py
"""
SmartVault Snapshot Watcher Lambda
===================================

PURPOSE:
    This Lambda reacts ONLY after EBS snapshots complete.
    It NEVER creates snapshots and NEVER waits for completion.

TRIGGER:
    EventBridge rule listening to EBS Snapshot State Change events
    Triggers only when snapshot state becomes "completed"

RESPONSIBILITIES (in order):
    1. Validate Event - Confirm snapshot state is completed
    2. Read Snapshot Tags - RetentionDays, CopyRegions, InstanceId, VolumeId, SourceRegion
    3. Copy Snapshot to Target Regions - For each region in CopyRegions
    4. Tag Copied Snapshots Immediately - Preserve all original tags
    5. Send SNS Notifications - Success or failure with error context

NON-RESPONSIBILITIES:
    ✗ Creating snapshots (Orchestrator's job)
    ✗ Waiting/polling for snapshot completion (Event-driven)
    ✗ Retention cleanup or deletion (Retention Manager's job)
    ✗ Backup orchestration logic (Orchestrator's job)

CRITICAL DESIGN DECISIONS:
    • Region is derived from the EVENT, not from EC2 instance tags
    • Stateless, event-driven logic
    • Safe retry behavior (idempotent operations)
    • Clear separation of concerns from backup initiator

Author: SmartVault Team
Version: 1.0.0
"""

import json
import logging
import os
import boto3
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from botocore.exceptions import ClientError

# =============================================================================
# CONFIGURATION
# =============================================================================

# Environment variables
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "production")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

# If running in Lambda, configure root logger
if len(logging.getLogger().handlers) > 0:
    logging.getLogger().setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))


# =============================================================================
# CUSTOM EXCEPTIONS
# =============================================================================

class SnapshotWatcherError(Exception):
    """Base exception for Snapshot Watcher."""
    pass


class EventValidationError(SnapshotWatcherError):
    """Raised when event validation fails - non-retryable."""
    pass


class SnapshotNotFoundError(SnapshotWatcherError):
    """Raised when snapshot is not found."""
    pass


class CopyOperationError(SnapshotWatcherError):
    """Raised when snapshot copy fails."""
    pass


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class SnapshotEvent:
    """Validated event data extracted from EventBridge event."""
    snapshot_id: str
    source_region: str  # CRITICAL: This comes from the event, NOT from EC2 tags
    account_id: Optional[str] = None
    event_time: Optional[str] = None
    volume_arn: Optional[str] = None


@dataclass
class SnapshotTags:
    """Parsed snapshot tags applied by the Backup Orchestrator."""
    snapshot_id: str
    retention_days: int
    copy_regions: List[str]
    instance_id: Optional[str]
    volume_id: Optional[str]
    source_region: str
    backup_type: str
    environment: str
    name: str
    description: str
    encrypted: bool
    kms_key_id: Optional[str]
    all_tags: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/response."""
        return {
            "snapshot_id": self.snapshot_id,
            "retention_days": self.retention_days,
            "copy_regions": self.copy_regions,
            "instance_id": self.instance_id,
            "volume_id": self.volume_id,
            "source_region": self.source_region,
            "backup_type": self.backup_type,
            "environment": self.environment,
            "name": self.name,
            "encrypted": self.encrypted
        }


@dataclass
class ProcessingResult:
    """Result of snapshot processing workflow."""
    status: str = "pending"
    message: str = ""
    snapshot_id: Optional[str] = None
    source_region: Optional[str] = None
    tags: Dict[str, Any] = field(default_factory=dict)
    copy_results: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Lambda response."""
        return {
            "status": self.status,
            "message": self.message,
            "snapshot_id": self.snapshot_id,
            "source_region": self.source_region,
            "tags": self.tags,
            "copy_results": self.copy_results
        }


# =============================================================================
# STEP 1: EVENT VALIDATOR
# =============================================================================

class EventValidator:
    """
    Validates incoming EventBridge events.
    
    CRITICAL: Region is extracted from the event itself, NOT from EC2 instance tags.
    
    Expected Event Structure:
    {
        "version": "0",
        "id": "event-id",
        "detail-type": "EBS Snapshot Notification",
        "source": "aws.ec2",
        "account": "123456789012",
        "time": "2024-01-15T10:30:00Z",
        "region": "us-east-1",              ← SOURCE REGION COMES FROM HERE
        "resources": ["arn:aws:ec2:..."],
        "detail": {
            "event": "createSnapshot",
            "result": "succeeded",
            "cause": "",
            "snapshot_id": "snap-abc123",   ← SNAPSHOT ID COMES FROM HERE
            "source": "arn:aws:ec2:...",
            "startTime": "...",
            "endTime": "..."
        }
    }
    """

    EXPECTED_SOURCE = "aws.ec2"
    EXPECTED_DETAIL_TYPE = "EBS Snapshot Notification"
    EXPECTED_EVENT_TYPE = "createSnapshot"
    EXPECTED_RESULT = "succeeded"

    def validate(self, event: Dict[str, Any]) -> SnapshotEvent:
        """
        Validate event and extract snapshot information.
        
        Args:
            event: Raw EventBridge event
            
        Returns:
            SnapshotEvent with validated data
            
        Raises:
            EventValidationError: If event is invalid or snapshot not completed
        """
        logger.debug(f"Validating event: {json.dumps(event)}")

        # Validate event source
        source = event.get("source")
        if source != self.EXPECTED_SOURCE:
            raise EventValidationError(
                f"Invalid source: expected '{self.EXPECTED_SOURCE}', got '{source}'"
            )

        # Validate detail-type
        detail_type = event.get("detail-type")
        if detail_type != self.EXPECTED_DETAIL_TYPE:
            raise EventValidationError(
                f"Invalid detail-type: expected '{self.EXPECTED_DETAIL_TYPE}', "
                f"got '{detail_type}'"
            )

        # =========================================================
        # CRITICAL: Extract region from EVENT, not from EC2 tags!
        # =========================================================
        source_region = event.get("region")
        if not source_region:
            raise EventValidationError("Missing 'region' in event")

        # Extract and validate detail section
        detail = event.get("detail", {})

        # Validate event type (createSnapshot, copySnapshot, etc.)
        event_type = detail.get("event")
        if event_type != self.EXPECTED_EVENT_TYPE:
            raise EventValidationError(
                f"Ignoring event type: '{event_type}' "
                f"(only processing '{self.EXPECTED_EVENT_TYPE}')"
            )

        # Validate result - MUST be succeeded (completed state)
        result = detail.get("result")
        if result != self.EXPECTED_RESULT:
            raise EventValidationError(
                f"Snapshot not completed: result='{result}' "
                f"(expected '{self.EXPECTED_RESULT}'). Exiting immediately."
            )

        # Extract snapshot ID
        snapshot_id = detail.get("snapshot_id")
        if not snapshot_id:
            raise EventValidationError("Missing 'snapshot_id' in event detail")

        # Validate snapshot ID format
        if not snapshot_id.startswith("snap-"):
            raise EventValidationError(
                f"Invalid snapshot ID format: '{snapshot_id}'"
            )

        logger.info(
            f"Event validated: snapshot_id={snapshot_id}, "
            f"source_region={source_region} (from event)"
        )

        return SnapshotEvent(
            snapshot_id=snapshot_id,
            source_region=source_region,
            account_id=event.get("account"),
            event_time=event.get("time"),
            volume_arn=detail.get("source")
        )


# =============================================================================
# STEP 2: TAG READER
# =============================================================================

class TagReader:
    """
    Reads and parses tags from completed EBS snapshots.
    
    Expected Tags (applied by Backup Orchestrator):
        - RetentionDays: Number of days to retain snapshot
        - CopyRegions: Comma-separated list of target regions for cross-region copy
        - InstanceId: Source EC2 instance ID
        - VolumeId: Source EBS volume ID
        - SourceRegion: Region where snapshot was created
        - BackupType: Type of backup (scheduled, manual, on-demand)
        - Environment: Environment tag (production, staging, development)
        - Name: Human-readable snapshot name
    """

    DEFAULT_RETENTION_DAYS = "30"

    def __init__(self):
        self._ec2_clients: Dict[str, Any] = {}

    def _get_ec2_client(self, region: str):
        """Get or create EC2 client for specified region."""
        if region not in self._ec2_clients:
            self._ec2_clients[region] = boto3.client("ec2", region_name=region)
        return self._ec2_clients[region]

    def read_tags(self, snapshot_id: str, region: str) -> SnapshotTags:
        """
        Read and parse tags from snapshot.
        
        Args:
            snapshot_id: The snapshot ID to read tags from
            region: Region where snapshot exists (from event)
            
        Returns:
            SnapshotTags object with parsed tag values
            
        Raises:
            SnapshotNotFoundError: If snapshot doesn't exist
        """
        logger.info(f"Reading tags for snapshot {snapshot_id} in {region}")

        ec2 = self._get_ec2_client(region)

        try:
            response = ec2.describe_snapshots(
                SnapshotIds=[snapshot_id],
                OwnerIds=["self"]
            )
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "InvalidSnapshot.NotFound":
                raise SnapshotNotFoundError(
                    f"Snapshot {snapshot_id} not found in region {region}"
                )
            raise

        snapshots = response.get("Snapshots", [])
        if not snapshots:
            raise SnapshotNotFoundError(
                f"Snapshot {snapshot_id} not found in region {region}"
            )

        snapshot = snapshots[0]

        # Convert tags list to dictionary
        tags_list = snapshot.get("Tags", [])
        tags_dict = {tag["Key"]: tag["Value"] for tag in tags_list}

        logger.info(f"Raw tags retrieved: {json.dumps(tags_dict)}")

        # Parse and return structured tags
        return self._parse_tags(
            tags_dict=tags_dict,
            snapshot_id=snapshot_id,
            region=region,
            volume_id=snapshot.get("VolumeId"),
            description=snapshot.get("Description", ""),
            encrypted=snapshot.get("Encrypted", False),
            kms_key_id=snapshot.get("KmsKeyId")
        )

    def _parse_tags(
        self,
        tags_dict: Dict[str, str],
        snapshot_id: str,
        region: str,
        volume_id: Optional[str],
        description: str,
        encrypted: bool,
        kms_key_id: Optional[str]
    ) -> SnapshotTags:
        """Parse raw tags dictionary into SnapshotTags object."""

        # Parse CopyRegions (comma-separated string)
        copy_regions_str = tags_dict.get("CopyRegions", "")
        copy_regions = self._parse_copy_regions(copy_regions_str)

        # Parse RetentionDays with validation
        retention_days = self._parse_retention_days(
            tags_dict.get("RetentionDays", self.DEFAULT_RETENTION_DAYS)
        )

        return SnapshotTags(
            snapshot_id=snapshot_id,
            retention_days=retention_days,
            copy_regions=copy_regions,
            instance_id=tags_dict.get("InstanceId"),
            volume_id=tags_dict.get("VolumeId") or volume_id,
            source_region=tags_dict.get("SourceRegion") or region,
            backup_type=tags_dict.get("BackupType", "unknown"),
            environment=tags_dict.get("Environment", "unknown"),
            name=tags_dict.get("Name", f"snapshot-{snapshot_id}"),
            description=description,
            encrypted=encrypted,
            kms_key_id=kms_key_id,
            all_tags=tags_dict  # Preserve all original tags
        )

    def _parse_copy_regions(self, regions_str: str) -> List[str]:
        """Parse comma-separated region string into list."""
        if not regions_str or not regions_str.strip():
            return []

        regions = [r.strip() for r in regions_str.split(",")]
        
        # Filter empty strings and validate region format
        valid_regions = []
        for region in regions:
            if region and self._is_valid_region(region):
                valid_regions.append(region)
            elif region:
                logger.warning(f"Invalid region format, skipping: '{region}'")

        return valid_regions

    def _is_valid_region(self, region: str) -> bool:
        """Basic validation of AWS region format (e.g., us-east-1, eu-west-1)."""
        parts = region.split("-")
        return len(parts) >= 3 and len(parts[0]) == 2

    def _parse_retention_days(self, value: str) -> int:
        """Parse retention days string to integer with validation."""
        try:
            days = int(value)
            if days < 1:
                logger.warning(f"Invalid retention days {days}, using default 30")
                return 30
            if days > 3650:  # 10 years max
                logger.warning(f"Retention days {days} exceeds max, capping at 3650")
                return 3650
            return days
        except (ValueError, TypeError):
            logger.warning(f"Cannot parse retention days '{value}', using default 30")
            return 30


# =============================================================================
# STEP 3 & 4: SNAPSHOT COPIER (includes immediate tagging)
# =============================================================================

class SnapshotCopier:
    """
    Handles cross-region snapshot copying with immediate tagging.
    
    IMPORTANT: The CopySnapshot API must be called in the DESTINATION region,
    specifying the SOURCE region and snapshot ID.
    
    Tags are applied immediately during the copy operation using TagSpecifications.
    """

    COPY_METADATA_PREFIX = "SmartVault"
    
    # Tags that should NOT be copied (prevent recursive copies)
    EXCLUDED_TAGS = {"CopyRegions"}

    def __init__(self):
        self._ec2_clients: Dict[str, Any] = {}

    def _get_ec2_client(self, region: str):
        """Get or create EC2 client for specified region."""
        if region not in self._ec2_clients:
            self._ec2_clients[region] = boto3.client("ec2", region_name=region)
        return self._ec2_clients[region]

    def copy_snapshot(
        self,
        source_snapshot_id: str,
        source_region: str,
        target_region: str,
        original_tags: SnapshotTags
    ) -> Dict[str, Any]:
        """
        Copy snapshot to target region with immediate tagging.
        
        Args:
            source_snapshot_id: ID of source snapshot
            source_region: Region from EVENT (not from EC2 tags!)
            target_region: Destination region for copy
            original_tags: Tags from source snapshot to preserve
            
        Returns:
            Dictionary with copy operation details
            
        Raises:
            CopyOperationError: If copy operation fails
        """
        logger.info(
            f"Initiating copy: {source_snapshot_id} from {source_region} to {target_region}"
        )

        # Get EC2 client for DESTINATION region (required for CopySnapshot API)
        ec2_target = self._get_ec2_client(target_region)

        # Build description for copied snapshot
        description = self._build_copy_description(
            original_description=original_tags.description,
            source_snapshot_id=source_snapshot_id,
            source_region=source_region
        )

        # Build tags for copied snapshot (Step 4: Tag immediately)
        copy_tags = self._build_copy_tags(
            original_tags=original_tags,
            source_snapshot_id=source_snapshot_id,
            source_region=source_region,
            target_region=target_region
        )

        try:
            # Call CopySnapshot in DESTINATION region
            response = ec2_target.copy_snapshot(
                SourceRegion=source_region,
                SourceSnapshotId=source_snapshot_id,
                Description=description,
                Encrypted=True,  # Always encrypt copies for security
                TagSpecifications=[
                    {
                        "ResourceType": "snapshot",
                        "Tags": copy_tags
                    }
                ]
            )

            copied_snapshot_id = response["SnapshotId"]

            logger.info(
                f"Copy initiated successfully: {copied_snapshot_id} in {target_region}"
            )

            return {
                "target_region": target_region,
                "copied_snapshot_id": copied_snapshot_id,
                "source_snapshot_id": source_snapshot_id,
                "source_region": source_region,
                "status": "initiated",
                "encrypted": True,
                "tags_applied": len(copy_tags)
            }

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))
            
            full_error = f"Failed to copy to {target_region}: [{error_code}] {error_msg}"
            logger.error(full_error)
            raise CopyOperationError(full_error) from e

        except Exception as e:
            error_msg = f"Unexpected error copying to {target_region}: {str(e)}"
            logger.error(error_msg)
            raise CopyOperationError(error_msg) from e

    def _build_copy_description(
        self,
        original_description: str,
        source_snapshot_id: str,
        source_region: str
    ) -> str:
        """Build description for copied snapshot."""
        copy_info = f"Cross-region copy of {source_snapshot_id} from {source_region}"

        if original_description:
            return f"{original_description} | {copy_info}"
        return copy_info

    def _build_copy_tags(
        self,
        original_tags: SnapshotTags,
        source_snapshot_id: str,
        source_region: str,
        target_region: str
    ) -> List[Dict[str, str]]:
        """
        Build complete tag set for copied snapshot.
        
        Strategy:
        1. Preserve ALL original tags (except CopyRegions to prevent recursion)
        2. Add copy-specific metadata
        3. Update region-specific information
        """
        tags = {}
        timestamp = datetime.now(timezone.utc).isoformat()

        # Step 1: Copy ALL original tags (except excluded ones)
        for key, value in original_tags.all_tags.items():
            if key not in self.EXCLUDED_TAGS:
                tags[key] = value

        # Step 2: Update/add copy metadata
        tags.update({
            # Core identification
            "Name": self._build_copy_name(original_tags.name, target_region),

            # Copy tracking metadata
            f"{self.COPY_METADATA_PREFIX}:SourceSnapshotId": source_snapshot_id,
            f"{self.COPY_METADATA_PREFIX}:SourceRegion": source_region,
            f"{self.COPY_METADATA_PREFIX}:CopyTimestamp": timestamp,
            f"{self.COPY_METADATA_PREFIX}:IsCrossRegionCopy": "true",
            f"{self.COPY_METADATA_PREFIX}:CopiedToRegion": target_region,
            f"{self.COPY_METADATA_PREFIX}:ManagedBy": "snapshot-watcher",

            # Preserve/update standard tags
            "SourceRegion": source_region,
            "RetentionDays": str(original_tags.retention_days),
        })

        # Step 3: Preserve critical original identifiers with prefix
        if original_tags.instance_id:
            tags["InstanceId"] = original_tags.instance_id
            tags[f"{self.COPY_METADATA_PREFIX}:OriginalInstanceId"] = original_tags.instance_id

        if original_tags.volume_id:
            tags["VolumeId"] = original_tags.volume_id
            tags[f"{self.COPY_METADATA_PREFIX}:OriginalVolumeId"] = original_tags.volume_id

        if original_tags.backup_type and original_tags.backup_type != "unknown":
            tags["BackupType"] = original_tags.backup_type

        if original_tags.environment and original_tags.environment != "unknown":
            tags["Environment"] = original_tags.environment

        # Convert to AWS API format
        return [{"Key": k, "Value": str(v)} for k, v in tags.items()]

    def _build_copy_name(self, original_name: str, target_region: str) -> str:
        """Build name for copied snapshot."""
        if original_name and original_name != "unknown":
            # Avoid duplicate suffixes if re-processed
            if f"-copy-{target_region}" in original_name:
                return original_name
            return f"{original_name}-copy-{target_region}"
        return f"cross-region-copy-{target_region}"


# =============================================================================
# STEP 5: NOTIFIER
# =============================================================================

class Notifier:
    """
    Handles SNS notifications for snapshot operations.
    
    Notification Types:
        - SUCCESS: All copy operations completed successfully
        - FAILURE: All copy operations failed
        - PARTIAL_FAILURE: Some copies succeeded, some failed
        
    All failure notifications include detailed error context.
    """

    def __init__(self):
        self._sns_client = None

    @property
    def sns_client(self):
        """Lazy initialization of SNS client."""
        if self._sns_client is None:
            self._sns_client = boto3.client("sns")
        return self._sns_client

    def send_success(self, result: ProcessingResult) -> None:
        """Send success notification."""
        if not SNS_TOPIC_ARN:
            logger.warning("SNS_TOPIC_ARN not configured - skipping notification")
            return

        subject = self._build_subject("SUCCESS", result)
        message = self._build_success_message(result)

        self._publish(subject, message, "SUCCESS")

    def send_failure(
        self,
        result: ProcessingResult,
        failed_copies: Optional[List[Dict]] = None
    ) -> None:
        """Send failure notification with detailed error context."""
        if not SNS_TOPIC_ARN:
            logger.warning("SNS_TOPIC_ARN not configured - skipping notification")
            return

        status = "PARTIAL_FAILURE" if result.status == "partial_failure" else "FAILURE"
        subject = self._build_subject(status, result)
        message = self._build_failure_message(result, failed_copies or [])

        self._publish(subject, message, status)

    def _build_subject(self, status: str, result: ProcessingResult) -> str:
        """Build notification subject line (max 100 chars for SNS)."""
        subject = (
            f"[{status}] SmartVault Snapshot {result.snapshot_id} - "
            f"{result.source_region}"
        )
        return subject[:100]

    def _build_success_message(self, result: ProcessingResult) -> str:
        """Build success notification message body."""
        successful_copies = [
            r for r in result.copy_results
            if r.get("status") == "initiated"
        ]

        notification = {
            "notification_type": "SNAPSHOT_PROCESSING_COMPLETE",
            "status": "SUCCESS",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "environment": ENVIRONMENT,
            "snapshot": {
                "id": result.snapshot_id,
                "source_region": result.source_region,
            },
            "message": result.message,
            "copy_operations": {
                "total_requested": len(result.copy_results),
                "successful": len(successful_copies),
                "failed": 0,
                "details": [
                    {
                        "target_region": r.get("target_region"),
                        "copied_snapshot_id": r.get("copied_snapshot_id"),
                        "status": r.get("status")
                    }
                    for r in successful_copies
                ]
            },
            "original_tags": result.tags
        }

        return json.dumps(notification, indent=2, default=str)

    def _build_failure_message(
        self,
        result: ProcessingResult,
        failed_copies: List[Dict]
    ) -> str:
        """Build failure notification message with error context."""
        successful_copies = [
            r for r in result.copy_results
            if r.get("status") == "initiated"
        ]

        notification = {
            "notification_type": "SNAPSHOT_PROCESSING_COMPLETE",
            "status": result.status.upper(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "environment": ENVIRONMENT,
            "snapshot": {
                "id": result.snapshot_id,
                "source_region": result.source_region,
            },
            "message": result.message,
            "error_context": {
                "total_attempts": len(result.copy_results),
                "successful_count": len(successful_copies),
                "failed_count": len(failed_copies),
                "failed_regions": [f.get("target_region") for f in failed_copies],
                "errors": [
                    {
                        "region": f.get("target_region"),
                        "error": f.get("error"),
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                    for f in failed_copies
                ]
            },
            "successful_copies": [
                {
                    "target_region": r.get("target_region"),
                    "copied_snapshot_id": r.get("copied_snapshot_id")
                }
                for r in successful_copies
            ],
            "original_tags": result.tags,
            "remediation": "Review failed regions and check IAM permissions, KMS key access, and region availability."
        }

        return json.dumps(notification, indent=2, default=str)

    def _publish(self, subject: str, message: str, status: str) -> None:
        """Publish message to SNS topic."""
        try:
            response = self.sns_client.publish(
                TopicArn=SNS_TOPIC_ARN,
                Subject=subject,
                Message=message,
                MessageAttributes={
                    "Environment": {
                        "DataType": "String",
                        "StringValue": ENVIRONMENT
                    },
                    "Service": {
                        "DataType": "String",
                        "StringValue": "SmartVault-SnapshotWatcher"
                    },
                    "Status": {
                        "DataType": "String",
                        "StringValue": status
                    }
                }
            )

            logger.info(f"SNS notification sent: MessageId={response['MessageId']}")

        except Exception as e:
            # Log but don't raise - notification failure shouldn't fail main operation
            logger.error(f"Failed to send SNS notification: {e}")


# =============================================================================
# MAIN ORCHESTRATOR
# =============================================================================

class SnapshotWatcher:
    """
    Main orchestrator for the Snapshot Watcher workflow.
    
    Coordinates all steps in the exact required order:
    1. Validate event (confirm completed state)
    2. Read snapshot tags
    3. Copy to target regions
    4. Tag copied snapshots (done during copy)
    5. Send notifications
    
    This class NEVER:
    - Creates snapshots
    - Waits/polls for completion
    - Handles retention cleanup
    - Performs backup orchestration
    """

    def __init__(self):
        self.validator = EventValidator()
        self.tag_reader = TagReader()
        self.copier = SnapshotCopier()
        self.notifier = Notifier()

    def process(self, event: Dict[str, Any]) -> ProcessingResult:
        """
        Main processing workflow.
        
        Args:
            event: EventBridge event for EBS Snapshot Notification
            
        Returns:
            ProcessingResult with status and details
        """
        result = ProcessingResult()

        # =====================================================================
        # STEP 1: VALIDATE EVENT
        # Confirm snapshot state is completed. Exit immediately for other states.
        # =====================================================================
        logger.info("=" * 60)
        logger.info("STEP 1: Validating event")
        logger.info("=" * 60)

        try:
            snapshot_event = self.validator.validate(event)
            result.snapshot_id = snapshot_event.snapshot_id
            result.source_region = snapshot_event.source_region

        except EventValidationError as e:
            logger.warning(f"Event validation failed (expected for non-completed): {e}")
            result.status = "skipped"
            result.message = str(e)
            return result

        logger.info(
            f"✓ Validated: snapshot={snapshot_event.snapshot_id}, "
            f"region={snapshot_event.source_region} (from event)"
        )

        # =====================================================================
        # STEP 2: READ SNAPSHOT TAGS
        # Read RetentionDays, CopyRegions, InstanceId, VolumeId, SourceRegion
        # =====================================================================
        logger.info("=" * 60)
        logger.info("STEP 2: Reading snapshot tags")
        logger.info("=" * 60)

        try:
            snapshot_tags = self.tag_reader.read_tags(
                snapshot_id=snapshot_event.snapshot_id,
                region=snapshot_event.source_region
            )
            result.tags = snapshot_tags.to_dict()

        except SnapshotNotFoundError as e:
            logger.error(f"Snapshot not found: {e}")
            result.status = "error"
            result.message = str(e)
            self.notifier.send_failure(result)
            return result

        logger.info(f"✓ Tags retrieved: {json.dumps(result.tags, indent=2)}")

        # Check if cross-region copy is configured
        if not snapshot_tags.copy_regions:
            logger.info("No CopyRegions tag configured - no cross-region copy needed")
            result.status = "success"
            result.message = "Snapshot completed successfully. No cross-region copy configured."
            self.notifier.send_success(result)
            return result

        logger.info(f"CopyRegions configured: {snapshot_tags.copy_regions}")

        # =====================================================================
        # STEP 3 & 4: COPY SNAPSHOT TO TARGET REGIONS (with immediate tagging)
        # For each region in CopyRegions, call CopySnapshot
        # Tags are applied immediately during the copy operation
        # =====================================================================
        logger.info("=" * 60)
        logger.info("STEP 3 & 4: Copying to target regions (with tags)")
        logger.info("=" * 60)

        copy_results = []
        
        for target_region in snapshot_tags.copy_regions:
            # Skip if target is same as source
            if target_region == snapshot_event.source_region:
                logger.info(f"Skipping {target_region} - same as source region")
                continue

            logger.info(f"Processing copy to: {target_region}")

            try:
                copy_result = self.copier.copy_snapshot(
                    source_snapshot_id=snapshot_event.snapshot_id,
                    source_region=snapshot_event.source_region,
                    target_region=target_region,
                    original_tags=snapshot_tags
                )
                copy_results.append(copy_result)
                logger.info(
                    f"✓ Copy initiated: {target_region} -> "
                    f"{copy_result['copied_snapshot_id']}"
                )

            except CopyOperationError as e:
                logger.error(f"✗ Copy failed to {target_region}: {e}")
                copy_results.append({
                    "target_region": target_region,
                    "status": "failed",
                    "error": str(e),
                    "source_snapshot_id": snapshot_event.snapshot_id,
                    "source_region": snapshot_event.source_region
                })

        result.copy_results = copy_results

        # =====================================================================
        # STEP 5: SEND SNS NOTIFICATIONS
        # Success for successful copies, failure with error context for failures
        # =====================================================================
        logger.info("=" * 60)
        logger.info("STEP 5: Sending notifications")
        logger.info("=" * 60)

        successful_copies = [r for r in copy_results if r.get("status") == "initiated"]
        failed_copies = [r for r in copy_results if r.get("status") == "failed"]

        if failed_copies:
            if successful_copies:
                result.status = "partial_failure"
                result.message = (
                    f"Partial success: {len(successful_copies)} copies initiated, "
                    f"{len(failed_copies)} failed. "
                    f"Failed regions: {[f['target_region'] for f in failed_copies]}"
                )
            else:
                result.status = "failure"
                result.message = (
                    f"All {len(failed_copies)} copy operations failed. "
                    f"Failed regions: {[f['target_region'] for f in failed_copies]}"
                )
            self.notifier.send_failure(result, failed_copies)
            logger.warning(f"✗ {result.message}")
        else:
            result.status = "success"
            if successful_copies:
                result.message = (
                    f"Successfully initiated {len(successful_copies)} cross-region copies"
                )
            else:
                result.message = "No cross-region copies needed (all targets same as source)"
            self.notifier.send_success(result)
            logger.info(f"✓ {result.message}")

        logger.info("=" * 60)
        logger.info(f"Processing complete: status={result.status}")
        logger.info("=" * 60)

        return result


# =============================================================================
# LAMBDA ENTRY POINT
# =============================================================================

# Global instance for Lambda container reuse (warm starts)
_watcher_instance: Optional[SnapshotWatcher] = None


def get_watcher() -> SnapshotWatcher:
    """Get or create SnapshotWatcher instance (singleton pattern for Lambda reuse)."""
    global _watcher_instance
    if _watcher_instance is None:
        _watcher_instance = SnapshotWatcher()
    return _watcher_instance


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AWS Lambda entry point.
    
    Triggered by EventBridge when EBS snapshot state changes to completed.
    
    Args:
        event: EventBridge event containing snapshot notification
        context: Lambda context object
        
    Returns:
        Processing result as dictionary
        
    Event Structure:
        {
            "source": "aws.ec2",
            "detail-type": "EBS Snapshot Notification",
            "region": "us-east-1",           ← Source region (USE THIS!)
            "detail": {
                "event": "createSnapshot",
                "result": "succeeded",
                "snapshot_id": "snap-xxx"    ← Snapshot ID
            }
        }
    """
    logger.info("=" * 70)
    logger.info("SMARTVAULT SNAPSHOT WATCHER - INVOCATION START")
    logger.info("=" * 70)
    logger.info(f"Event: {json.dumps(event)}")
    
    if context:
        logger.info(
            f"Context: function={context.function_name}, "
            f"request_id={context.aws_request_id}, "
            f"remaining_time={context.get_remaining_time_in_millis()}ms"
        )

    try:
        watcher = get_watcher()
        result = watcher.process(event)

        response = result.to_dict()
        
        logger.info("=" * 70)
        logger.info("SMARTVAULT SNAPSHOT WATCHER - INVOCATION COMPLETE")
        logger.info(f"Response: {json.dumps(response)}")
        logger.info("=" * 70)
        
        return response

    except Exception as e:
        logger.exception(f"Unhandled exception: {e}")
        
        error_response = {
            "status": "error",
            "message": f"Unhandled error: {str(e)}",
            "snapshot_id": event.get("detail", {}).get("snapshot_id", "unknown"),
            "source_region": event.get("region", "unknown"),
            "error_type": type(e).__name__
        }
        
        logger.info("=" * 70)
        logger.info("SMARTVAULT SNAPSHOT WATCHER - INVOCATION FAILED")
        logger.info(f"Error Response: {json.dumps(error_response)}")
        logger.info("=" * 70)
        
        return error_response


# =============================================================================
# LOCAL TESTING
# =============================================================================

if __name__ == "__main__":
    """Local testing with sample event."""
    
    # Configure logging for local testing
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    # Sample EventBridge event for testing
    test_event = {
        "version": "0",
        "id": "12345678-1234-1234-1234-123456789012",
        "detail-type": "EBS Snapshot Notification",
        "source": "aws.ec2",
        "account": "123456789012",
        "time": "2024-01-15T10:30:00Z",
        "region": "us-east-1",  # SOURCE REGION - from event!
        "resources": [
            "arn:aws:ec2:us-east-1::snapshot/snap-0123456789abcdef0"
        ],
        "detail": {
            "event": "createSnapshot",
            "result": "succeeded",
            "cause": "",
            "snapshot_id": "snap-0123456789abcdef0",
            "source": "arn:aws:ec2:us-east-1:123456789012:volume/vol-0123456789abcdef0",
            "startTime": "2024-01-15T10:25:00.000Z",
            "endTime": "2024-01-15T10:30:00.000Z"
        }
    }
    
    print("\n" + "=" * 70)
    print("LOCAL TEST - SNAPSHOT WATCHER")
    print("=" * 70 + "\n")
    
    # Mock context
    class MockContext:
        function_name = "snapshot-watcher-local-test"
        aws_request_id = "local-test-123"
        def get_remaining_time_in_millis(self):
            return 300000  # 5 minutes
    
    result = lambda_handler(test_event, MockContext())
    
    print("\n" + "=" * 70)
    print("TEST RESULT:")
    print("=" * 70)
    print(json.dumps(result, indent=2))