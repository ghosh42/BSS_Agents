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
# Max objects to scan per bucket when looking for most-recent modification.
# 50 pages × 1000 objects = 50,000.  Covers most buckets completely.
# For buckets larger than this threshold we set last_modified_unreliable=True.
_MAX_SCAN_OBJECTS = 50_000
# Hard safety buffer: never flag a bucket as unused if an object was modified
# within this many days, regardless of the unused_days parameter.
_SAFETY_BUFFER_DAYS = 30


def scan_s3_buckets(session: boto3.Session, unused_days: int = 90) -> List[Dict[str, Any]]:
    """Scan for S3 buckets that appear unused.

    Three-layer false-positive defence:
    1. CloudWatch growth detection — if NumberOfObjects or BucketSizeBytes grew
       over the past 8 days the bucket is actively written to and is excluded
       entirely before any date logic runs.
    2. Full last-modified scan — up to 50,000 objects (not the first 100
       alphabetically, which for date-organised log buckets would always return
       stale 2020/2021 timestamps even for live buckets).
    3. Hard safety buffer — even if last_modified looks stale, if it falls
       within _SAFETY_BUFFER_DAYS we do NOT flag the bucket.
    """
    s3 = session.client("s3")
    cloudwatch = session.client("cloudwatch")
    cutoff = datetime.now(timezone.utc) - timedelta(days=unused_days)
    safety_cutoff = datetime.now(timezone.utc) - timedelta(days=_SAFETY_BUFFER_DAYS)

    buckets = s3.list_buckets().get("Buckets", [])
    logger.info(f"Found {len(buckets)} S3 buckets — fetching CloudWatch metrics in batch")

    if not buckets:
        return []

    # Step 1: Batch all CW metrics; also detect buckets with active growth.
    size_map, count_map, growing_set = _batch_cloudwatch_metrics(
        cloudwatch, [b["Name"] for b in buckets]
    )

    # Step 2: For buckets that have objects, check last modified in parallel
    def check_bucket(bucket):
        bucket_name = bucket["Name"]
        created = bucket["CreationDate"]
        size_bytes = size_map.get(bucket_name, 0)
        object_count = count_map.get(bucket_name, 0)

        # Layer 1: Skip buckets that CloudWatch confirms are actively growing.
        if bucket_name in growing_set:
            logger.debug(f"Skipping {bucket_name}: CloudWatch shows active growth")
            return None

        is_empty = object_count == 0

        last_modified = None
        last_modified_unreliable = False
        if not is_empty:
            last_modified, scanned_count = _get_last_modified_object(s3, bucket_name)
            # If we hit the scan cap we may have missed the most-recent objects.
            if scanned_count >= _MAX_SCAN_OBJECTS:
                last_modified_unreliable = True
                logger.debug(
                    f"{bucket_name}: scanned {scanned_count} objects (cap reached); "
                    "last_modified may be unreliable"
                )

        # Layer 3: Hard safety buffer — trust recent data over stale estimates.
        if last_modified is not None and last_modified >= safety_cutoff:
            logger.debug(
                f"Skipping {bucket_name}: last_modified {last_modified.date()} "
                f"is within {_SAFETY_BUFFER_DAYS}-day safety buffer"
            )
            return None

        is_stale = last_modified is not None and last_modified < cutoff
        # Unreliable last_modified on a large bucket: treat as stale cautiously.
        if last_modified_unreliable and last_modified is None:
            is_stale = False  # Can't confirm stale; skip.

        is_unused = is_empty or is_stale
        if not is_unused:
            return None

        days_since = (
            (datetime.now(timezone.utc) - last_modified).days
            if last_modified else None
        )

        return {
            "resource_id": bucket_name,
            "service": "s3",
            "bucket_name": bucket_name,
            "created": created.isoformat(),
            "size_bytes": size_bytes,
            "object_count": object_count,
            "last_modified": last_modified.isoformat() if last_modified else None,
            "days_since_modified": days_since,
            "last_modified_unreliable": last_modified_unreliable,
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

    Returns (size_map, count_map, growing_set):
    - size_map  : latest BucketSizeBytes per bucket name
    - count_map : latest NumberOfObjects per bucket name
    - growing_set: bucket names whose object count OR size grew over the last
                  8 days — these are actively-written buckets and must NOT be
                  flagged as unused.
    """
    now = datetime.now(timezone.utc)
    # 8-day window gives ~8 daily datapoints — enough to detect any bucket
    # written to at least once per week.
    start = now - timedelta(days=8)

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
    # Track all datapoints per bucket to detect growth
    size_history: Dict[str, List[float]] = {}
    count_history: Dict[str, List[float]] = {}
    growing_set: set = set()

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
                timestamps = result.get("Timestamps", [])
                if not values:
                    continue
                # Recover bucket name from numeric index in the Id
                if metric_id.startswith("size_"):
                    idx = int(metric_id[5:])
                    name = bucket_names[idx]
                    # Sort by timestamp to get chronological order
                    paired = sorted(zip(timestamps, values), key=lambda x: x[0])
                    size_map[name] = int(paired[-1][1])  # latest
                    size_history[name] = [v for _, v in paired]
                elif metric_id.startswith("count_"):
                    idx = int(metric_id[6:])
                    name = bucket_names[idx]
                    paired = sorted(zip(timestamps, values), key=lambda x: x[0])
                    count_map[name] = int(paired[-1][1])  # latest
                    count_history[name] = [v for _, v in paired]
        except Exception as e:
            logger.warning(f"CloudWatch batch metrics failed: {e} — defaulting to 0")

    # Detect actively-growing buckets: if the newest datapoint is greater than
    # the oldest, the bucket was written to during the observation window.
    for name in bucket_names:
        size_pts = size_history.get(name, [])
        count_pts = count_history.get(name, [])
        size_grew = len(size_pts) >= 2 and size_pts[-1] > size_pts[0]
        count_grew = len(count_pts) >= 2 and count_pts[-1] > count_pts[0]
        if size_grew or count_grew:
            growing_set.add(name)
            logger.debug(f"{name}: detected as actively growing (count_grew={count_grew}, size_grew={size_grew})")

    logger.info(
        f"CloudWatch scan complete: {len(growing_set)} actively-growing buckets "
        "excluded from unused-resource results"
    )
    return size_map, count_map, growing_set


def _get_last_modified_object(s3, bucket_name: str):
    """Find the most recently modified object in a bucket.

    Scans up to _MAX_SCAN_OBJECTS objects (50,000), tracking the global
    maximum LastModified timestamp.  Exits early if an object modified
    within the last 7 days is found — that immediately proves the bucket
    is active.

    Returns (latest_datetime | None, objects_scanned_count).

    IMPORTANT: list_objects_v2 returns keys in alphabetical order, not
    date order.  For date-structured log buckets (AWSLogs/year/month/day/...)
    the most recent objects are at the ALPHABETICAL END.  With the 50k cap
    we cover most real-world buckets completely; the CloudWatch growth-
    detection layer in the caller is the safety net for massive buckets.
    """
    RECENT_THRESHOLD = datetime.now(timezone.utc) - timedelta(days=7)
    try:
        latest = None
        scanned = 0
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            for obj in page.get("Contents", []):
                ts = obj["LastModified"]
                if latest is None or ts > latest:
                    latest = ts
                scanned += 1
                # Early exit: found a very recent object — bucket is active.
                if ts >= RECENT_THRESHOLD:
                    return latest, scanned
            if scanned >= _MAX_SCAN_OBJECTS:
                break
        return latest, scanned
    except ClientError:
        return None, 0
