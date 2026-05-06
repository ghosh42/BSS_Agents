"""ECR repository scanner - finds unused/stale container repos."""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_MAX_WORKERS = 20


def scan_ecr_repos(session: boto3.Session, unused_days: int = 180) -> List[Dict[str, Any]]:
    """Scan for ECR repositories with no recent push/pull activity.

    Uses parallel describe_images calls to avoid serial slowdown across many repos.
    """
    ecr = session.client("ecr")
    cutoff = datetime.now(timezone.utc) - timedelta(days=unused_days)

    # Collect all repos first (paginated)
    repos = []
    paginator = ecr.get_paginator("describe_repositories")
    for page in paginator.paginate():
        repos.extend(page.get("repositories", []))

    logger.info(f"Found {len(repos)} ECR repositories — checking images in parallel")

    def check_repo(repo) -> Optional[Dict]:
        repo_name = repo["repositoryName"]
        repo_uri = repo["repositoryUri"]
        try:
            images = []
            img_paginator = ecr.get_paginator("describe_images")
            for page in img_paginator.paginate(repositoryName=repo_name, filter={"tagStatus": "ANY"}):
                images.extend(page.get("imageDetails", []))

            if not images:
                return {
                    "resource_id": repo_uri,
                    "service": "ecr",
                    "repo_name": repo_name,
                    "image_count": 0,
                    "last_push": None,
                    "last_pull": None,
                    "size_bytes": 0,
                    "reason": "empty repository",
                }

            last_push = max(
                (img["imagePushedAt"] for img in images if "imagePushedAt" in img),
                default=None,
            )
            last_pull = max(
                (img.get("lastRecordedPullTime") for img in images if img.get("lastRecordedPullTime")),
                default=None,
            )
            total_size = sum(img.get("imageSizeInBytes", 0) for img in images)

            if last_push and last_push < cutoff:
                return {
                    "resource_id": repo_uri,
                    "service": "ecr",
                    "repo_name": repo_name,
                    "image_count": len(images),
                    "last_push": last_push.isoformat(),
                    "last_pull": last_pull.isoformat() if last_pull else None,
                    "size_bytes": total_size,
                    "reason": f"no push in {unused_days} days (last: {last_push.date()})",
                }
        except ClientError as e:
            logger.warning(f"Error scanning ECR repo {repo_name}: {e}")
        return None

    results = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(check_repo, r): r["repositoryName"] for r in repos}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as e:
                logger.warning(f"ECR repo check failed: {e}")

    logger.info(f"Found {len(results)} potentially unused ECR repos")
    return results
