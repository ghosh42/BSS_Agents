"""AWS Cost Explorer - maps resources to their monthly spend."""
import logging
from datetime import datetime, timedelta
from typing import Dict

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def get_resource_costs(session: boto3.Session, resource_ids: list = None) -> Dict[str, float]:
    """Get monthly cost breakdown by SERVICE from Cost Explorer.

    Note: RESOURCE_ID grouping requires granular billing to be enabled, which is not available
    in this account. We return service-level costs instead (e.g., "Amazon Simple Storage Service").

    Args:
        session: boto3 Session with credentials
        resource_ids: Accepted for API compatibility, but costs are returned at service level.

    Returns:
        Dict mapping service_name -> monthly cost in USD (e.g., {"Amazon S3": 12.34}).
    """
    ce = session.client("ce")

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    try:
        response = ce.get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )

    except ClientError as e:
        logger.warning(f"Cost Explorer query failed: {e}")
        return {}

    costs = {}
    for result in response.get("ResultsByTime", []):
        for group in result.get("Groups", []):
            service_name = group["Keys"][0]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if amount > 0:
                costs[service_name] = costs.get(service_name, 0) + amount

    logger.info(f"Retrieved cost data for {len(costs)} services")
    return costs


def get_total_account_spend(session: boto3.Session) -> float:
    """Get total account spend for the last 30 days."""
    ce = session.client("ce")

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    try:
        response = ce.get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
        )
        total = 0.0
        for result in response.get("ResultsByTime", []):
            total += float(result["Total"]["UnblendedCost"]["Amount"])
        return total
    except ClientError as e:
        logger.warning(f"Total spend query failed: {e}")
        return 0.0
