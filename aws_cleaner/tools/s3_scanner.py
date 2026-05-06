"""S3 bucket scanner - finds unused/empty buckets."""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Max parallel workers for per-bucket S3 calls
_MAX_WORKERS = 20
# CloudWatch get_metric_data supports up to 500 queries per call
_CW_BATCH_SIZE = 500


def scan_s3_buckets(session: boto3.Session, unused_days: int = 90) -> List[Dict[str, Any]]:
    """Scan for S3 buckets that appear unused.

    Fast path: batch all CloudWatch metrics in a single API call, then
    parallelize per-bucket last-modified checks.

    Criteria for "unused":
    - Empty buckets (0 objects)
    - Buckets with no recent object modifications (> unused_days)
    """
    s3 = session.client("s3")
    cloudwatch = session.client("cloudwatch")
    cutoff = datetime.now(timezone.utc) - timedelta(days=unused_days)

    buckets = s3.list_buckets().get("Buckets", [])
    logger.info(f"Found {len(buckets)} S3 buckets — fetching CloudWatch metrics in batch")

    if not buckets:
        return []

    # Step 1: Batch all CW metrics in one call (size + object count for all buckets)
    size_map, count_map = _batch_cloudwatch_metrics(cloudwatch, [b["Name"] for b in buckets])

    # Step 2: For buckets that have objects, check last modified in parallel
    def check_bucket(bucket):
        bucket_name = bucket["Name"]
        created = bucket["CreationDate"]
        size_bytes = size_map.get(bucket_name, 0)
        object_count = count_map.get(bucket_name, 0)

        is_empty = object_count == 0

        last_modified = None
        if not is_empty:
            last_modified = _get_last_modified_object(s3, bucket_name)

        is_stale = last_modified is not None and last_modified < cutoff
        is_unused = is_empty or is_stale

        if not is_unused:
            return None

        return {
            "resource_id": bucket_name,
            "service": "s3",
            "bucket_name": bucket_name,
            "created": created.isoformat(),
            "size_bytes": size_bytes,
            "object_count": object_count,
            "last_modified": last_modified.isoformat() if last_modified else None,
            "reason": "empty" if is_empty else f"no activity in {unused_days} days",
        }

    results = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(check_bucket, b): b["Name"] for b in buckets}
        for future in as_completed(futures):
            bucket_name = futures[future]
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as e:
                logger.warning(f"Error scanning bucket {bucket_name}: {e}")

    logger.info(f"Found {len(results)} potentially unused S3 buckets")
    return results


def _batch_cloudwatch_metrics(cloudwatch, bucket_names: List[str]):
    """Fetch BucketSizeBytes + NumberOfObjects for all buckets in one API call.

    Returns (size_map, count_map) both keyed by bucket name.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=3)

    # Build metric queries — 2 per bucket (size + count).
    # Use a numeric index as the Id to guarantee uniqueness regardless of bucket name.
    queries = []
    for idx, name in enumerate(bucket_names):
        queries.append({
            "Id": f"size_{idx}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/S3",
                    "MetricName": "BucketSizeBytes",
                    "Dimensions": [
                        {"Name": "BucketName", "Value": name},
                        {"Name": "StorageType", "Value": "StandardStorage"},
                    ],
                },
                "Period": 86400,
                "Stat": "Average",
            },
            "ReturnData": True,
        })
        queries.append({
            "Id": f"count_{idx}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/S3",
                    "MetricName": "NumberOfObjects",
                    "Dimensions": [
                        {"Name": "BucketName", "Value": name},
                        {"Name": "StorageType", "Value": "AllStorageTypes"},
                    ],
                },
                "Period": 86400,
                "Stat": "Average",
            },
            "ReturnData": True,
        })

    size_map: Dict[str, int] = {}
    count_map: Dict[str, int] = {}

    # CloudWatch allows max 500 queries per call — chunk if needed
    for i in range(0, len(queries), _CW_BATCH_SIZE):
        chunk = queries[i:i + _CW_BATCH_SIZE]
        try:
            response = cloudwatch.get_metric_data(
                MetricDataQueries=chunk,
                StartTime=start,
                EndTime=now,
            )
            for result in response.get("MetricDataResults", []):
                metric_id = result["Id"]
                values = result.get("Values", [])
                val = int(values[0]) if values else 0
                # Recover bucket name from numeric index in the Id
                if metric_id.startswith("size_"):
                    idx = int(metric_id[5:])
                    size_map[bucket_names[idx]] = val
                elif metric_id.startswith("count_"):
                    idx = int(metric_id[6:])
                    count_map[bucket_names[idx]] = val
        except Exception as e:
            logger.warning(f"CloudWatch batch metrics failed: {e} — defaulting to 0")

    return size_map, count_map


def _get_last_modified_object(s3, bucket_name: str) -> Optional[datetime]:
    """Get the most recently modified object timestamp via list_objects_v2."""
    try:
        # list_objects_v2 returns objects sorted by key name, not date.
        # We fetch a small page and take the max LastModified seen.
        latest = None
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name, PaginationConfig={"MaxItems": 100}):
            for obj in page.get("Contents", []):
                ts = obj["LastModified"]
                if latest is None or ts > latest:
                    latest = ts
        return latest
    except ClientError:
        return None
