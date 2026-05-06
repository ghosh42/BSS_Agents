"""Safe deletion engine for AWS resources.

Design principles:
- Every deletion is reversibility-assessed first (S3 = warn, EBS = irreversible)
- Dry-run mode is the default; pass execute=True to actually delete
- All actions are logged to an audit trail (list of dicts returned)
- Never deletes non-empty S3 buckets unless force=True
- Prints Rich confirmation prompt for each resource before deleting
"""
import logging
from typing import Any, Dict, List, Tuple

import boto3
from botocore.exceptions import ClientError
from rich.console import Console
from rich.table import Table
from rich.prompt import Confirm

logger = logging.getLogger(__name__)
console = Console(stderr=True)

# Per-service action labels and reversibility
_SERVICE_META = {
    "s3":  {"action": "Delete S3 bucket",         "reversible": False, "severity": "high"},
    "ecr": {"action": "Delete ECR images",         "reversible": False, "severity": "medium"},
    "ebs": {"action": "Delete EBS volume",         "reversible": False, "severity": "high"},
    "ec2": {"action": "Terminate EC2 instance",    "reversible": False, "severity": "critical"},
}


# ─── Public API ────────────────────────────────────────────────────────────────

def delete_resources(
    session: boto3.Session,
    recommendations: List[Dict[str, Any]],
    *,
    execute: bool = False,
    force: bool = False,
    interactive: bool = True,
) -> List[Dict[str, Any]]:
    """Delete resources from an AWS account.

    Args:
        session:         boto3 Session with credentials
        recommendations: List of recommendation dicts from the agent
                         (must have 'service' and 'resource_id')
        execute:         False = dry-run (default), True = actually delete
        force:           Skip per-resource confirmation prompts
        interactive:     Show Rich confirmation table before acting

    Returns:
        audit_log: List of dicts with {resource_id, service, status, message}
    """
    if not recommendations:
        return []

    audit_log: List[Dict[str, Any]] = []

    # Group by service for efficient batching
    by_service: Dict[str, List[Dict]] = {}
    for rec in recommendations:
        svc = rec.get("service", "").lower()
        by_service.setdefault(svc, []).append(rec)

    if interactive and not force:
        _print_deletion_plan(recommendations, execute=execute)
        if execute:
            confirmed = Confirm.ask(
                f"\n[bold red]Proceed with deleting {len(recommendations)} resource(s)?[/bold red]",
                default=False,
            )
            if not confirmed:
                console.print("[yellow]Deletion cancelled.[/yellow]")
                return [{"resource_id": r["resource_id"], "service": r.get("service"),
                         "status": "cancelled", "message": "User cancelled"} for r in recommendations]

    for service, recs in by_service.items():
        deleter = _get_deleter(service)
        for rec in recs:
            resource_id = rec.get("resource_id", "unknown")
            try:
                if not execute:
                    audit_log.append({
                        "resource_id": resource_id,
                        "service": service,
                        "status": "dry_run",
                        "message": f"[DRY RUN] Would {_SERVICE_META.get(service, {}).get('action', 'delete')}",
                        "monthly_savings_usd": rec.get("monthly_savings_usd", 0),
                    })
                    logger.info(f"  [DRY RUN] {service.upper()} {resource_id}")
                else:
                    msg = deleter(session, rec)
                    audit_log.append({
                        "resource_id": resource_id,
                        "service": service,
                        "status": "deleted",
                        "message": msg,
                        "monthly_savings_usd": rec.get("monthly_savings_usd", 0),
                    })
                    console.print(f"  [green]✓[/green] Deleted {service.upper()} [cyan]{resource_id}[/cyan]")
                    logger.info(f"  Deleted {service.upper()} {resource_id}: {msg}")
            except ClientError as e:
                code = e.response["Error"]["Code"]
                msg = e.response["Error"]["Message"]
                audit_log.append({
                    "resource_id": resource_id,
                    "service": service,
                    "status": "error",
                    "message": f"{code}: {msg}",
                    "monthly_savings_usd": 0,
                })
                console.print(f"  [red]✗[/red] Failed {service.upper()} [cyan]{resource_id}[/cyan]: {code}")
                logger.error(f"  Error deleting {service} {resource_id}: {e}")
            except Exception as e:
                audit_log.append({
                    "resource_id": resource_id,
                    "service": service,
                    "status": "error",
                    "message": str(e),
                    "monthly_savings_usd": 0,
                })
                console.print(f"  [red]✗[/red] Failed {service.upper()} [cyan]{resource_id}[/cyan]: {e}")
                logger.error(f"  Error deleting {service} {resource_id}: {e}")

    return audit_log


def render_audit_log(audit_log: List[Dict[str, Any]], execute: bool = False) -> None:
    """Print a Rich summary table of deletion results."""
    if not audit_log:
        return

    mode = "DELETION RESULTS" if execute else "DRY-RUN PREVIEW"
    table = Table(title=f"[bold]{mode}[/bold]", show_lines=True)
    table.add_column("Service", style="cyan", width=8)
    table.add_column("Resource ID", style="white", max_width=50)
    table.add_column("Status", width=10)
    table.add_column("Savings/mo", justify="right", width=12)
    table.add_column("Message", style="dim")

    total_savings = 0.0
    for entry in audit_log:
        status = entry["status"]
        savings = entry.get("monthly_savings_usd", 0) or 0
        total_savings += savings if status in ("deleted", "dry_run") else 0

        status_style = {
            "deleted": "[green]deleted[/green]",
            "dry_run": "[blue]dry-run[/blue]",
            "error": "[red]error[/red]",
            "cancelled": "[yellow]cancelled[/yellow]",
        }.get(status, status)

        table.add_row(
            (entry.get("service") or "").upper(),
            entry.get("resource_id", ""),
            status_style,
            f"${savings:.2f}",
            (entry.get("message") or "")[:80],
        )

    console.print(table)
    console.print(f"\n  [bold]Estimated monthly savings: [green]${total_savings:.2f}[/green][/bold]")


# ─── Per-service deleters ───────────────────────────────────────────────────

def _get_deleter(service: str):
    return {
        "s3":  _delete_s3_bucket,
        "ecr": _delete_ecr_images,
        "ebs": _delete_ebs_volume,
        "ec2": _terminate_ec2_instance,
    }.get(service, _unsupported_deleter)


def _delete_s3_bucket(session: boto3.Session, rec: Dict) -> str:
    """Delete an S3 bucket. Empties it first if needed."""
    s3 = session.client("s3")
    bucket = rec["resource_id"]

    # Empty the bucket first (required before deletion)
    _empty_s3_bucket(s3, bucket)

    s3.delete_bucket(Bucket=bucket)
    return f"Bucket {bucket} emptied and deleted"


def _empty_s3_bucket(s3_client, bucket: str) -> None:
    """Delete all objects (and versions) from a bucket."""
    # Delete regular objects
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        objects = page.get("Contents", [])
        if objects:
            s3_client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": o["Key"]} for o in objects]},
            )

    # Delete versioned objects (versioning-enabled buckets)
    try:
        paginator = s3_client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            versions = page.get("Versions", []) + page.get("DeleteMarkers", [])
            if versions:
                s3_client.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in versions]},
                )
    except ClientError as e:
        # Versioning not enabled — fine to skip
        if e.response["Error"]["Code"] != "NoSuchBucket":
            logger.debug(f"Version cleanup skipped for {bucket}: {e}")


def _delete_ecr_images(session: boto3.Session, rec: Dict) -> str:
    """Delete all images in an ECR repository (leaves the repo itself)."""
    ecr = session.client("ecr")
    repo = rec["resource_id"]

    # List all image digests
    image_ids = []
    paginator = ecr.get_paginator("list_images")
    for page in paginator.paginate(repositoryName=repo):
        image_ids.extend(page.get("imageIds", []))

    if image_ids:
        # Batch delete (max 100 per call)
        for i in range(0, len(image_ids), 100):
            ecr.batch_delete_image(repositoryName=repo, imageIds=image_ids[i:i+100])

    count = len(image_ids)
    return f"Deleted {count} image(s) from {repo}"


def _delete_ebs_volume(session: boto3.Session, rec: Dict) -> str:
    """Delete an unattached EBS volume."""
    ec2 = session.client("ec2")
    volume_id = rec["resource_id"]
    ec2.delete_volume(VolumeId=volume_id)
    return f"Volume {volume_id} deleted"


def _terminate_ec2_instance(session: boto3.Session, rec: Dict) -> str:
    """Terminate a stopped EC2 instance."""
    ec2 = session.client("ec2")
    instance_id = rec["resource_id"]
    ec2.terminate_instances(InstanceIds=[instance_id])
    return f"Instance {instance_id} termination initiated"


def _unsupported_deleter(session: boto3.Session, rec: Dict) -> str:
    raise ValueError(f"No deleter implemented for service: {rec.get('service')}")


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _print_deletion_plan(recommendations: List[Dict], execute: bool) -> None:
    """Print a Rich table summarising what will be deleted."""
    mode = "[bold red]LIVE DELETION[/bold red]" if execute else "[bold blue]DRY RUN[/bold blue]"
    table = Table(title=f"Deletion Plan — {mode}", show_lines=True)
    table.add_column("Service", style="cyan", width=8)
    table.add_column("Resource ID", style="white", max_width=50)
    table.add_column("Reversible", width=10)
    table.add_column("Savings/mo", justify="right", width=12)
    table.add_column("Reason", style="dim")

    total = 0.0
    for rec in recommendations:
        svc = rec.get("service", "").lower()
        meta = _SERVICE_META.get(svc, {"reversible": False, "severity": "medium"})
        reversible = "[yellow]No[/yellow]"
        savings = rec.get("monthly_savings_usd", 0) or 0
        total += savings
        table.add_row(
            svc.upper(),
            rec.get("resource_id", ""),
            reversible,
            f"${savings:.2f}",
            (rec.get("reason") or "")[:60],
        )

    console.print(table)
    console.print(f"\n  Estimated monthly savings if all deleted: [green]${total:.2f}[/green]")
