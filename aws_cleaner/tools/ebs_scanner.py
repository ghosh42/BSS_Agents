"""EBS volume scanner - finds unattached volumes."""
import logging
from typing import List, Dict, Any

import boto3

logger = logging.getLogger(__name__)


def scan_ebs_volumes(session: boto3.Session, **kwargs) -> List[Dict[str, Any]]:
    """Scan for unattached EBS volumes (status=available).

    Args:
        session: boto3 Session with credentials

    Returns:
        List of dicts with volume info.
    """
    ec2 = session.client("ec2")

    response = ec2.describe_volumes(
        Filters=[{"Name": "status", "Values": ["available"]}]
    )
    volumes = response.get("Volumes", [])

    results = []
    for vol in volumes:
        name_tag = next(
            (t["Value"] for t in vol.get("Tags", []) if t["Key"] == "Name"),
            None,
        )
        results.append({
            "resource_id": vol["VolumeId"],
            "service": "ebs",
            "volume_id": vol["VolumeId"],
            "size_gb": vol["Size"],
            "volume_type": vol["VolumeType"],
            "created": vol["CreateTime"].isoformat(),
            "availability_zone": vol["AvailabilityZone"],
            "name_tag": name_tag,
            "reason": "unattached (status=available)",
        })

    logger.info(f"Found {len(results)} unattached EBS volumes")
    return results
