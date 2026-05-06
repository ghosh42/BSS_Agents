"""Multi-region sweep — discover enabled regions and aggregate scan results.

S3 is global (buckets exist independent of region) so it is scanned once.
ECR, EBS, EC2 are regional — scanned once per enabled region.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError
from rich.console import Console
from rich.table import Table

from .config import ScanConfig
from .agent import run_agent

logger = logging.getLogger(__name__)
console = Console()

# Services that are truly regional (need per-region scan)
_REGIONAL_SERVICES = {"ecr", "ebs", "ec2"}
# Services that are global (scan once regardless of --all-regions)
_GLOBAL_SERVICES = {"s3"}


def get_enabled_regions(session: boto3.Session) -> List[str]:
    """Return all regions that are enabled for this account."""
    ec2 = session.client("ec2", region_name="us-east-1")
    try:
        response = ec2.describe_regions(Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}])
        regions = sorted(r["RegionName"] for r in response["Regions"])
        logger.info(f"Found {len(regions)} enabled regions")
        return regions
    except ClientError as e:
        logger.warning(f"Could not list regions: {e} — falling back to us-east-1")
        return ["us-east-1"]


def sweep_regions(
    config: ScanConfig,
    *,
    max_workers: int = 6,
) -> Dict[str, Any]:
    """Run a scan across all enabled regions for this profile.

    S3 is scanned once (global). Regional services (ECR/EBS/EC2) are
    scanned per region. Results are merged into a single result dict
    matching the shape returned by run_agent().

    Returns:
        Merged AgentState-like dict with:
          discovered_resources, recommendations, errors,
          regions_scanned, regions_failed
    """
    session = boto3.Session(
        profile_name=config.aws_profile,
        region_name=config.aws_region,
    )

    requested_services = set(config.services)
    global_services = list(requested_services & _GLOBAL_SERVICES)
    regional_services = list(requested_services & _REGIONAL_SERVICES)

    all_regions = get_enabled_regions(session)

    console.print(
        f"  Scanning [bold]{len(all_regions)}[/bold] regions "
        f"for [cyan]{', '.join(regional_services) or 'no regional services'}[/cyan] "
        f"(+ global: [cyan]{', '.join(global_services) or 'none'}[/cyan])"
    )

    merged_discovered: Dict[str, List] = {}
    merged_recs: List[Dict] = []
    merged_errors: List[str] = []
    regions_scanned: List[str] = []
    regions_failed: List[str] = []

    # ── Global services: scan once in base region ───────────────────────────
    if global_services:
        global_config = replace(config, services=global_services, all_regions=False)
        try:
            result = run_agent(global_config)
            for svc, resources in result.get("discovered_resources", {}).items():
                merged_discovered.setdefault(svc, []).extend(resources)
            merged_recs.extend(result.get("recommendations", []))
            merged_errors.extend(result.get("errors", []))
        except Exception as e:
            logger.error(f"Global services scan failed: {e}")
            merged_errors.append(f"global: {e}")

    # ── Regional services: scan each region in parallel ─────────────────────
    if not regional_services:
        return _build_result(merged_discovered, merged_recs, merged_errors, all_regions, [])

    def _scan_region(region: str) -> Dict[str, Any]:
        regional_config = replace(config, aws_region=region, services=regional_services, all_regions=False)
        try:
            result = run_agent(regional_config)
            result["_region"] = region
            return result
        except Exception as e:
            logger.warning(f"  Region {region} failed: {e}")
            return {"_region": region, "_error": str(e)}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_scan_region, r): r for r in all_regions}
        for future in as_completed(futures):
            res = future.result()
            region = res.get("_region", "unknown")
            if "_error" in res:
                regions_failed.append(region)
                merged_errors.append(f"{region}: {res['_error']}")
            else:
                regions_scanned.append(region)
                for svc, resources in res.get("discovered_resources", {}).items():
                    # Tag each resource with its region
                    tagged = [{**r, "_region": region} for r in resources]
                    merged_discovered.setdefault(svc, []).extend(tagged)
                merged_recs.extend(
                    [{**r, "_region": region} for r in res.get("recommendations", [])]
                )
                merged_errors.extend(res.get("errors", []))

    return _build_result(merged_discovered, merged_recs, merged_errors, regions_scanned, regions_failed)


def _build_result(discovered, recs, errors, scanned, failed):
    return {
        "discovered_resources": discovered,
        "recommendations": recs,
        "errors": errors,
        "cost_data": {},
        "llm_analysis": "",
        "regions_scanned": sorted(scanned),
        "regions_failed": sorted(failed),
    }


def render_regions_summary(result: Dict[str, Any]) -> None:
    """Print a compact per-region breakdown table."""
    scanned = result.get("regions_scanned", [])
    failed = result.get("regions_failed", [])
    discovered = result.get("discovered_resources", {})

    # Build per-region resource counts
    region_counts: Dict[str, int] = {}
    for resources in discovered.values():
        for r in resources:
            reg = r.get("_region", "global")
            region_counts[reg] = region_counts.get(reg, 0) + 1

    active_regions = {r for r in region_counts if region_counts[r] > 0}

    if not active_regions and not failed:
        console.print("  [green]All regions clean — no unused resources found.[/green]")
        return

    table = Table(title="[bold]Per-Region Breakdown[/bold]", show_lines=False)
    table.add_column("Region", style="cyan")
    table.add_column("Unused Resources", justify="right")
    table.add_column("Status")

    for region in sorted(scanned):
        count = region_counts.get(region, 0)
        if count > 0:
            table.add_row(region, str(count), "[yellow]findings[/yellow]")

    for region in sorted(failed):
        table.add_row(region, "—", "[red]error[/red]")

    console.print(table)
    console.print(
        f"  Scanned [cyan]{len(scanned)}[/cyan] regions, "
        f"[red]{len(failed)}[/red] failed, "
        f"[yellow]{sum(region_counts.values())}[/yellow] total unused resources"
    )
