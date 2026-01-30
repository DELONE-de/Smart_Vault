# expire_snapshots.py
"""
SmartVault ExpireSnapshots Lambda
==================================

PURPOSE:
    This Lambda is responsible ONLY for deleting EBS snapshots that have exceeded
    their retention period. It does NOT create, copy, or monitor snapshot creation.

TRIGGER:
    Scheduled via EventBridge (e.g., daily at 02:00 UTC)

RESPONSIBILITIES (in exact order):
    1. List Candidate Snapshots
       • Call DescribeSnapshots
       • Filter strictly by tag: CreatedBy = SmartVault
    2. Read Required Snapshot Metadata
       • RetentionDays (from snapshot tag)
       • CreatedTime (from snapshot metadata)
       • Optional: identifiers for cross-region copies
    3. Retention Evaluation Logic
       • CurrentTime – CreatedTime > RetentionDays
    4. Deletion Actions
       • Delete expired snapshot in current region
       • Delete all known cross-region copies (if identifiable)
    5. Failure Handling & Safety
       • Skip snapshots with missing/malformed RetentionDays
       • Comprehensive logging of all decisions

NON-RESPONSIBILITIES:
    ✗ Creating snapshots
    ✗ Copying snapshots
    ✗ Waiting for snapshot completion
    ✗ Backup orchestration or notifications
    ✗ Reacting to snapshot creation events

CRITICAL DESIGN:
    • Deterministic: Same input → same output
    • Idempotent: Safe to run multiple times
    • Auditable: Every deletion decision is logged
    • Safe: Never deletes without explicit RetentionDays tag

Author: SmartVault Team
Version: 1.0.0
"""

import json
import logging
import os
import boto3
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass
from botocore.exceptions import ClientError

# =============================================================================
# CONFIGURATION
# =============================================================================

# Environment variables
ENVIRONMENT = os.environ.get("ENVIRONMENT", "production")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
FORCE_DELETE_ALL_REGIONS = os.environ.get("FORCE_DELETE_ALL_REGIONS", "false").lower() == "true"

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

if len(logging.getLogger().handlers) > 0:
    logging.getLogger().setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

# Constants
SMARTVAULT_CREATED_BY_TAG = "CreatedBy"
SMARTVAULT_CREATED_BY_VALUE = "SmartVault"
RETENTION_DAYS_TAG = "RetentionDays"
SOURCE_SNAPSHOT_ID_TAG = "SmartVault:SourceSnapshotId"
SOURCE_REGION_TAG = "SmartVault:SourceRegion"
IS_CROSS_REGION_COPY_TAG = "SmartVault:IsCrossRegionCopy"

# Default retention if tag missing (safety - never delete)
DEFAULT_RETENTION_DAYS_IF_MISSING = 3650  # 10 years - effectively never delete

# Regions to check for cross-region copies (should match your backup regions)
CROSS_REGION_COPY_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1",
    "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "ap-northeast-2"
]

# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class SnapshotInfo:
    """Complete information about a snapshot for retention evaluation."""
    snapshot_id: str
    region: str
    created_time: datetime
    retention_days: int
    tags: Dict[str, str]
    description: str
    volume_id: Optional[str]
    encrypted: bool
    kms_key_id: Optional[str]
    is_cross_region_copy: bool = False
    source_snapshot_id: Optional[str] = None
    source_region: Optional[str] = None

    @property
    def age_days(self) -> float:
        """Age of snapshot in days."""
        now = datetime.now(timezone.utc)
        age = now - self.created_time.replace(tzinfo=timezone.utc)
        return age.total_seconds() / 86400

    @property
    def is_expired(self) -> bool:
        """Determine if snapshot has exceeded its retention period."""
        return self.age_days > self.retention_days

    @property
    def expiration_date(self) -> datetime:
        """Date when snapshot should be deleted."""
        return self.created_time.replace(tzinfo=timezone.utc) + timedelta(days=self.retention_days)


@dataclass
class DeletionResult:
    """Result of deletion attempt."""
    snapshot_id: str
    region: str
    deleted: bool
    error: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "region": self.region,
            "deleted": self.deleted,
            "error": self.error,
            "reason": self.reason
        }


# =============================================================================
# MAIN EXPIRER CLASS
# =============================================================================

class ExpireSnapshots:
    """
    SmartVault EBS Snapshot Expiration Manager
    
    This class implements a safe, deterministic, and auditable snapshot
    expiration system that only deletes snapshots that have:
    - Been created by SmartVault (CreatedBy=SmartVault tag)
    - Exceeded their explicitly defined retention period
    """

    def __init__(self):
        self.ec2_clients: Dict[str, boto3.client] = {}
        self.deleted_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self.results: List[DeletionResult] = []

    def _get_ec2_client(self, region: str):
        """Get or create EC2 client for region."""
        if region not in self.ec2_clients:
            self.ec2_clients[region] = boto3.client("ec2", region_name=region)
        return self.ec2_clients[region]

    def run(self, region: str = None) -> Dict[str, Any]:
        """
        Main execution method.
        
        Args:
            region: Specific region to process. If None, processes current region.
            
        Returns:
            Summary of operations performed
        """
        if region is None:
            # Get current region from Lambda context or default
            try:
                region = boto3.session.Session().region_name
            except Exception:
                region = "us-east-1"

        logger.info("=" * 70)
        logger.info("SMARTVAULT EXPIRESNAPSHOTS - START")
        logger.info("=" * 70)
        logger.info(f"Environment: {ENVIRONMENT}")
        logger.info(f"Region: {region}")
        logger.info(f"Dry Run: {DRY_RUN}")
        logger.info(f"Force Delete All Regions: {FORCE_DELETE_ALL_REGIONS}")

        start_time = datetime.now(timezone.utc)

        try:
            # Step 1: List candidate snapshots in current region
            candidates = self.list_smartvault_snapshots(region)
            logger.info(f"Found {len(candidates)} SmartVault snapshots in {region}")

            # Step 2: Evaluate retention for each snapshot
            for snapshot_info in candidates:
                self.evaluate_and_delete(snapshot_info)

            # Step 3: Clean up cross-region copies if needed
            if FORCE_DELETE_ALL_REGIONS:
                self.cleanup_orphaned_copies(region)

        except Exception as e:
            logger.exception(f"Unhandled error in run(): {e}")
            self.error_count += 1

        end_time = datetime.now(timezone.utc)
        duration = end_time - start_time

        summary = {
            "status": "completed",
            "region": region,
            "timestamp": end_time.isoformat(),
            "duration_seconds": round(duration.total_seconds(), 2),
            "dry_run": DRY_RUN,
            "force_delete_all_regions": FORCE_DELETE_ALL_REGIONS,
            "statistics": {
                "deleted": self.deleted_count,
                "skipped": self.skipped_count,
                "errors": self.error_count,
                "total_processed": len(self.results)
            },
            "results": [r.to_dict() for r in self.results]
        }

        logger.info("=" * 70)
        logger.info("SMARTVAULT EXPIRESNAPSHOTS - COMPLETE")
        logger.info(f"Deleted: {self.deleted_count}, Skipped: {self.skipped_count}, Errors: {self.error_count}")
        logger.info("=" * 70)

        return summary

    def list_smartvault_snapshots(self, region: str) -> List[SnapshotInfo]:
        """
        Step 1: List candidate snapshots created by SmartVault.
        
        Filters strictly by CreatedBy=SmartVault tag.
        """
        logger.info(f"Listing SmartVault snapshots in {region}")

        ec2 = self._get_ec2_client(region)
        snapshots = []

        try:
            paginator = ec2.get_paginator("describe_snapshots")
            pages = paginator.paginate(
                OwnerIds=["self"],
                Filters=[
                    {
                        "Name": f"tag:{SMARTVAULT_CREATED_BY_TAG}",
                        "Values": [SMARTVAULT_CREATED_BY_VALUE]
                    }
                ]
            )

            for page in pages:
                for snap in page.get("Snapshots", []):
                    snapshot_info = self._parse_snapshot(snap, region)
                    if snapshot_info:
                        snapshots.append(snapshot_info)

        except ClientError as e:
            logger.error(f"Error listing snapshots in {region}: {e}")
            self.error_count += 1

        logger.info(f"Found {len(snapshots)} candidate snapshots in {region}")
        return snapshots

    def _parse_snapshot(self, snapshot: Dict[str, Any], region: str) -> Optional[SnapshotInfo]:
        """
        Parse raw snapshot data into SnapshotInfo object.
        
        Safety: If RetentionDays tag is missing or malformed, use very high retention.
        """
        snapshot_id = snapshot.get("SnapshotId")
        if not snapshot_id:
            logger.warning(f"Snapshot missing ID, skipping: {snapshot}")
            return None

        # Extract tags
        tags_dict = {tag["Key"]: tag["Value"] for tag in snapshot.get("Tags", [])}

        # Extract RetentionDays with safety
        retention_str = tags_dict.get(RETENTION_DAYS_TAG)
        try:
            retention_days = int(retention_str) if retention_str else DEFAULT_RETENTION_DAYS_IF_MISSING
            if retention_days <= 0:
                retention_days = DEFAULT_RETENTION_DAYS_IF_MISSING
        except (ValueError, TypeError):
            logger.warning(
                f"Invalid RetentionDays tag '{retention_str}' on {snapshot_id}, "
                f"using safe default {DEFAULT_RETENTION_DAYS_IF_MISSING} days"
            )
            retention_days = DEFAULT_RETENTION_DAYS_IF_MISSING

        # Extract cross-region copy metadata
        is_cross_region_copy = tags_dict.get(IS_CROSS_REGION_COPY_TAG, "false").lower() == "true"
        source_snapshot_id = tags_dict.get(SOURCE_SNAPSHOT_ID_TAG)
        source_region = tags_dict.get(SOURCE_REGION_TAG)

        # Parse creation time
        created_time_str = snapshot.get("StartTime")
        if not created_time_str:
            logger.warning(f"Snapshot {snapshot_id} missing StartTime, skipping")
            return None

        try:
            # Handle both string and datetime objects
            if isinstance(created_time_str, str):
                created_time = datetime.fromisoformat(created_time_str.replace("Z", "+00:00"))
            else:
                created_time = created_time_str
            if created_time.tzinfo is None:
                created_time = created_time.replace(tzinfo=timezone.utc)
        except Exception as e:
            logger.warning(f"Cannot parse StartTime for {snapshot_id}: {e}, skipping")
            return None

        return SnapshotInfo(
            snapshot_id=snapshot_id,
            region=region,
            created_time=created_time,
            retention_days=retention_days,
            tags=tags_dict,
            description=snapshot.get("Description", ""),
            volume_id=snapshot.get("VolumeId"),
            encrypted=snapshot.get("Encrypted", False),
            kms_key_id=snapshot.get("KmsKeyId"),
            is_cross_region_copy=is_cross_region_copy,
            source_snapshot_id=source_snapshot_id,
            source_region=source_region
        )

    def evaluate_and_delete(self, snapshot_info: SnapshotInfo):
        """
        Step 2-4: Evaluate retention and delete if expired.
        """
        snapshot_id = snapshot_info.snapshot_id
        region = snapshot_info.region

        # Log snapshot details
        logger.info("-" * 50)
        logger.info(f"Evaluating: {snapshot_id} in {region}")
        logger.info(f"  Created: {snapshot_info.created_time.isoformat()}")
        logger.info(f"  Age: {snapshot_info.age_days:.2f} days")
        logger.info(f"  Retention: {snapshot_info.retention_days} days")
        logger.info(f"  Expires: {snapshot_info.expiration_date.isoformat()}")
        logger.info(f"  Is Cross-Region Copy: {snapshot_info.is_cross_region_copy}")

        # Safety check: Never delete if retention is extremely high (missing tag)
        if snapshot_info.retention_days >= 3650:
            reason = f"Safety: Missing or invalid RetentionDays tag (using {snapshot_info.retention_days} days)"
            logger.warning(f"SKIPPING {snapshot_id}: {reason}")
            self.results.append(DeletionResult(
                snapshot_id=snapshot_id,
                region=region,
                deleted=False,
                reason=reason
            ))
            self.skipped_count += 1
            return

        # Check if expired
        if not snapshot_info.is_expired:
            days_remaining = snapshot_info.retention_days - snapshot_info.age_days
            reason = f"Not expired (expires in {days_remaining:.1f} days)"
            logger.info(f"SKIPPING {snapshot_id}: {reason}")
            self.results.append(DeletionResult(
                snapshot_id=snapshot_id,
                region=region,
                deleted=False,
                reason=reason
            ))
            self.skipped_count += 1
            return

        # Snapshot is expired - proceed with deletion
        logger.info(f"EXPIRED: {snapshot_id} is {snapshot_info.age_days:.2f} days old "
                    f"(retention: {snapshot_info.retention_days} days)")

        # Delete in current region
        result = self.delete_snapshot(snapshot_info)
        self.results.append(result)

        if result.deleted:
            self.deleted_count += 1
            logger.info(f"DELETED: {snapshot_id} in {region}")
        else:
            self.error_count += 1
            logger.error(f"FAILED to delete {snapshot_id} in {region}: {result.error}")

        # If this was a source snapshot, try to clean up copies
        if not snapshot_info.is_cross_region_copy and snapshot_info.source_snapshot_id is None:
            self.cleanup_cross_region_copies(snapshot_info)

    def delete_snapshot(self, snapshot_info: SnapshotInfo) -> DeletionResult:
        """
        Delete a single snapshot.
        
        Handles dry run mode and error handling.
        """
        snapshot_id = snapshot_info.snapshot_id
        region = snapshot_info.region

        if DRY_RUN:
            logger.info(f"DRY RUN: Would delete {snapshot_id} in {region}")
            return DeletionResult(
                snapshot_id=snapshot_id,
                region=region,
                deleted=False,
                reason="DRY_RUN mode enabled"
            )

        try:
            ec2 = self._get_ec2_client(region)
            ec2.delete_snapshot(SnapshotId=snapshot_id, DryRun=False)

            return DeletionResult(
                snapshot_id=snapshot_id,
                region=region,
                deleted=True
            )

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_msg = e.response.get("Error", {}).get("Message", str(e))

            # Some errors are expected and non-critical
            if error_code in ["InvalidSnapshot.NotFound", "InvalidSnapshot.InUse"]:
                logger.warning(f"Snapshot {snapshot_id} already deleted or in use: {error_code}")
                return DeletionResult(
                    snapshot_id=snapshot_id,
                    region=region,
                    deleted=True,
                    reason=f"Already deleted ({error_code})"
                )

            full_error = f"{error_code}: {error_msg}"
            logger.error(f"Failed to delete {snapshot_id}: {full_error}")
            return DeletionResult(
                snapshot_id=snapshot_id,
                region=region,
                deleted=False,
                error=full_error
            )

        except Exception as e:
            logger.error(f"Unexpected error deleting {snapshot_id}: {e}")
            return DeletionResult(
                snapshot_id=snapshot_id,
                region=region,
                deleted=False,
                error=str(e)
            )

    def cleanup_cross_region_copies(self, source_snapshot: SnapshotInfo):
        """
        Clean up cross-region copies of a deleted source snapshot.
        
        Uses SmartVault:SourceSnapshotId tag to find copies.
        """
        if source_snapshot.is_cross_region_copy:
            return  # Don't recurse

        source_id = source_snapshot.snapshot_id
        source_region = source_snapshot.region

        logger.info(f"Searching for cross-region copies of {source_id} from {source_region}")

        for region in CROSS_REGION_COPY_REGIONS:
            if region == source_region:
                continue

            try:
                ec2 = self._get_ec2_client(region)
                response = ec2.describe_snapshots(
                    OwnerIds=["self"],
                    Filters=[
                        {
                            "Name": f"tag:{SOURCE_SNAPSHOT_ID_TAG}",
                            "Values": [source_id]
                        },
                        {
                            "Name": f"tag:{SMARTVAULT_CREATED_BY_TAG}",
                            "Values": [SMARTVAULT_CREATED_BY_VALUE]
                        }
                    ]
                )

                copies = response.get("Snapshots", [])
                logger.info(f"Found {len(copies)} copies in {region}")

                for copy in copies:
                    copy_id = copy.get("SnapshotId")
                    if not copy_id:
                        continue

                    # Parse copy for safety
                    copy_tags = {t["Key"]: t["Value"] for t in copy.get("Tags", [])}
                    copy_info = SnapshotInfo(
                        snapshot_id=copy_id,
                        region=region,
                        created_time=copy.get("StartTime", datetime.now(timezone.utc)),
                        retention_days=source_snapshot.retention_days,
                        tags=copy_tags,
                        description=copy.get("Description", ""),
                        volume_id=copy.get("VolumeId"),
                        encrypted=copy.get("Encrypted", False),
                        kms_key_id=copy.get("KmsKeyId"),
                        is_cross_region_copy=True,
                        source_snapshot_id=source_id,
                        source_region=source_region
                    )

                    logger.info(f"Deleting cross-region copy {copy_id} in {region}")
                    result = self.delete_snapshot(copy_info)
                    self.results.append(result)

                    if result.deleted:
                        self.deleted_count += 1
                    else:
                        self.error_count += 1

            except Exception as e:
                logger.error(f"Error checking region {region}: {e}")

    def cleanup_orphaned_copies(self, current_region: str):
        """
        Optional: Find and delete orphaned cross-region copies
        whose source snapshot no longer exists.
        """
        logger.info("Starting orphaned copy cleanup")

        for region in CROSS_REGION_COPY_REGIONS:
            if region == current_region:
                continue

            try:
                ec2 = self._get_ec2_client(region)
                response = ec2.describe_snapshots(
                    OwnerIds=["self"],
                    Filters=[
                        {
                            "Name": f"tag:{IS_CROSS_REGION_COPY_TAG}",
                            "Values": ["true"]
                        },
                        {
                            "Name": f"tag:{SMARTVAULT_CREATED_BY_TAG}",
                            "Values": [SMARTVAULT_CREATED_BY_VALUE]
                        }
                    ]
                )

                for snap in response.get("Snapshots", []):
                    snapshot_id = snap.get("SnapshotId")
                    tags = {t["Key"]: t["Value"] for t in snap.get("Tags", [])}
                    source_id = tags.get(SOURCE_SNAPSHOT_ID_TAG)
                    source_region = tags.get(SOURCE_REGION_TAG)

                    if not source_id or not source_region:
                        continue

                    # Check if source still exists
                    try:
                        source_ec2 = self._get_ec2_client(source_region)
                        source_ec2.describe_snapshots(SnapshotIds=[source_id])
                        # Source exists - skip
                        continue
                    except ClientError as e:
                        if "InvalidSnapshot.NotFound" in str(e):
                            # Source is gone - delete copy
                            logger.warning(
                                f"Orphaned copy detected: {snapshot_id} in {region} "
                                f"(source {source_id} in {source_region} missing)"
                            )

                            copy_info = SnapshotInfo(
                                snapshot_id=snapshot_id,
                                region=region,
                                created_time=snap.get("StartTime", datetime.now(timezone.utc)),
                                retention_days=30,  # Arbitrary - it's orphaned
                                tags=tags,
                                description=snap.get("Description", ""),
                                volume_id=snap.get("VolumeId"),
                                encrypted=snap.get("Encrypted", False),
                                kms_key_id=snap.get("KmsKeyId"),
                                is_cross_region_copy=True,
                                source_snapshot_id=source_id,
                                source_region=source_region
                            )

                            result = self.delete_snapshot(copy_info)
                            self.results.append(result)

                            if result.deleted:
                                self.deleted_count += 1
                            else:
                                self.error_count += 1

            except Exception as e:
                logger.error(f"Error in orphaned cleanup for {region}: {e}")


# =============================================================================
# LAMBDA ENTRY POINT
# =============================================================================

_expirer_instance: Optional[ExpireSnapshots] = None


def get_expirer() -> ExpireSnapshots:
    """Get or create ExpireSnapshots instance (singleton for Lambda reuse)."""
    global _expirer_instance
    if _expirer_instance is None:
        _expirer_instance = ExpireSnapshots()
    return _expirer_instance


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AWS Lambda entry point.
    
    Scheduled via EventBridge (e.g., daily at 02:00 UTC)
    
    Args:
        event: EventBridge scheduled event
        context: Lambda context
        
    Returns:
        Summary of deletion operations
    """
    logger.info("=" * 70)
    logger.info("SMARTVAULT EXPIRESNAPSHOTS - INVOCATION START")
    logger.info("=" * 70)
    logger.info(f"Event: {json.dumps(event)}")
    
    if context:
        logger.info(
            f"Context: function={context.function_name}, "
            f"request_id={context.aws_request_id}"
        )

    try:
        expirer = get_expirer()
        result = expirer.run()

        logger.info("=" * 70)
        logger.info("SMARTVAULT EXPIRESNAPSHOTS - INVOCATION COMPLETE")
        logger.info(f"Summary: {result['statistics']}")
        logger.info("=" * 70)

        return result

    except Exception as e:
        logger.exception(f"Unhandled exception: {e}")
        return {
            "status": "error",
            "message": f"Unhandled error: {str(e)}",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }


# =============================================================================
# LOCAL TESTING
# =============================================================================

if __name__ == "__main__":
    """Local testing with dry run."""
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    # Set dry run for local testing
    os.environ["DRY_RUN"] = "true"
    os.environ["ENVIRONMENT"] = "local-test"
    
    print("\n" + "=" * 70)
    print("LOCAL TEST - EXPIRESNAPSHOTS (DRY RUN)")
    print("=" * 70 + "\n")
    
    class MockContext:
        function_name = "expire-snapshots-local-test"
        aws_request_id = "local-test-123"
    
    result = lambda_handler({}, MockContext())
    
    print("\n" + "=" * 70)
    print("TEST RESULT:")
    print("=" * 70)
    print(json.dumps(result, indent=2, default=str))