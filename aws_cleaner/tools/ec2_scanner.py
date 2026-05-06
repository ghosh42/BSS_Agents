"""EC2 instance scanner - finds long-stopped instances."""
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

import boto3

logger = logging.getLogger(__name__)


def scan_ec2_stopped(session: boto3.Session, stopped_days: int = 30) -> List[Dict[str, Any]]:
    """Scan for EC2 instances stopped longer than threshold.

    Args:
        session: boto3 Session with credentials
        stopped_days: Days stopped to flag as unused

    Returns:
        List of dicts with instance info.
    """
    ec2 = session.client("ec2")
    cutoff = datetime.now(timezone.utc) - timedelta(days=stopped_days)

    response = ec2.describe_instances(
        Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}]
    )

    results = []
    for reservation in response.get("Reservations", []):
        for instance in reservation["Instances"]:
            instance_id = instance["InstanceId"]
            instance_type = instance["InstanceType"]

            # Parse stop time from StateTransitionReason
            stop_time = _parse_stop_time(instance.get("StateTransitionReason", ""))

            name_tag = next(
                (t["Value"] for t in instance.get("Tags", []) if t["Key"] == "Name"),
                None,
            )

            # Only flag if stopped before cutoff (or if we can't determine stop time)
            if stop_time is None or stop_time < cutoff:
                days_stopped = (datetime.now(timezone.utc) - stop_time).days if stop_time else "unknown"
                results.append({
                    "resource_id": instance_id,
                    "service": "ec2",
                    "instance_id": instance_id,
                    "instance_type": instance_type,
                    "name_tag": name_tag,
                    "stopped_since": stop_time.isoformat() if stop_time else None,
                    "days_stopped": days_stopped,
                    "reason": f"stopped for {days_stopped} days",
                })

    logger.info(f"Found {len(results)} long-stopped EC2 instances")
    return results


def _parse_stop_time(reason: str):
    """Parse stop timestamp from StateTransitionReason string.

    Example: 'User initiated (2024-01-15 10:30:00 GMT)'
    """
    match = re.search(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} GMT)\)", reason)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None
