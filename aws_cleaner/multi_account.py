"""Multi-account sweep — run scans across multiple AWS profiles in parallel.

Usage:
    from aws_cleaner.multi_account import sweep_accounts, render_sweep_summary

    results = sweep_accounts(
        profiles=["dev", "staging", "prod"],
        base_config=ScanConfig(services=["s3", "ebs"]),
        max_workers=4,
    )
    render_sweep_summary(results)
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from .config import ScanConfig
from .agent import run_agent

logger = logging.getLogger(__name__)
console = Console()


# ─── Core sweep ────────────────────────────────────────────────────────────────

def sweep_accounts(
    profiles: List[str],
    base_config: ScanConfig,
    *,
    region: Optional[str] = None,
    max_workers: int = 4,
) -> List[Dict[str, Any]]:
    """Run a scan against multiple AWS profiles in parallel.

    Args:
        profiles:    List of AWS profile names (from ~/.aws/credentials)
        base_config: ScanConfig template — profile/region will be overridden per account
        region:      Override region for all profiles (uses base_config.aws_region if None)
        max_workers: Parallel scan threads (default 4 — be mindful of AWS API rate limits)

    Returns:
        List of per-account result dicts:
          {profile, region, result (AgentState), error (str|None)}
    """
    sweep_region = region or base_config.aws_region
    results: List[Dict[str, Any]] = []

    console.print(Panel(
        f"[bold blue]Multi-Account Sweep[/bold blue]\n"
        f"Profiles: [cyan]{', '.join(profiles)}[/cyan]\n"
        f"Region: [cyan]{sweep_region}[/cyan] | "
        f"Services: [cyan]{', '.join(base_config.services)}[/cyan]",
        border_style="blue",
    ))

    def _scan_one(profile: str) -> Dict[str, Any]:
        console.print(f"  [dim]→ Starting scan: [cyan]{profile}[/cyan][/dim]")
        config = replace(base_config, aws_profile=profile, aws_region=sweep_region)
        try:
            result = run_agent(config)
            total = sum(len(v) for v in result.get("discovered_resources", {}).values())
            savings = sum(
                r.get("monthly_savings_usd", 0)
                for r in result.get("recommendations", [])
            )
            console.print(
                f"  [green]✓[/green] [cyan]{profile}[/cyan] — "
                f"{total} unused resource(s), [green]${savings:.2f}[/green]/mo potential savings"
            )
            return {"profile": profile, "region": sweep_region, "result": result, "error": None}
        except Exception as e:
            console.print(f"  [red]✗[/red] [cyan]{profile}[/cyan] — failed: {e}")
            logger.error(f"Sweep failed for {profile}: {e}")
            return {"profile": profile, "region": sweep_region, "result": None, "error": str(e)}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_scan_one, p): p for p in profiles}
        for future in as_completed(futures):
            results.append(future.result())

    # Sort by profile name for deterministic output
    results.sort(key=lambda r: r["profile"])
    return results


def aggregate_sweep(sweep_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge per-account results into a single aggregate view.

    Returns a dict with:
      total_resources, total_savings, by_account, by_service, all_recommendations
    """
    by_account = {}
    by_service: Dict[str, List] = {}
    all_recommendations = []
    total_resources = 0
    total_savings = 0.0

    for entry in sweep_results:
        profile = entry["profile"]
        if entry["error"] or not entry["result"]:
            by_account[profile] = {"error": entry["error"], "resources": 0, "savings": 0}
            continue

        result = entry["result"]
        discovered = result.get("discovered_resources", {})
        recs = result.get("recommendations", [])

        account_resources = sum(len(v) for v in discovered.values())
        account_savings = sum(r.get("monthly_savings_usd", 0) for r in recs)

        total_resources += account_resources
        total_savings += account_savings

        by_account[profile] = {
            "resources": account_resources,
            "savings": account_savings,
            "errors": result.get("errors", []),
        }

        # Annotate each resource/recommendation with its source account
        for service, resources in discovered.items():
            tagged = [{**r, "_account": profile} for r in resources]
            by_service.setdefault(service, []).extend(tagged)

        for rec in recs:
            all_recommendations.append({**rec, "_account": profile})

    return {
        "total_resources": total_resources,
        "total_savings_usd": total_savings,
        "accounts_scanned": len(sweep_results),
        "accounts_failed": sum(1 for e in sweep_results if e["error"]),
        "by_account": by_account,
        "by_service": by_service,
        "all_recommendations": all_recommendations,
    }


# ─── Rich rendering ────────────────────────────────────────────────────────────

def render_sweep_summary(sweep_results: List[Dict[str, Any]]) -> None:
    """Print a Rich per-account summary table + aggregate totals."""
    agg = aggregate_sweep(sweep_results)

    # Per-account table
    table = Table(title="[bold]Multi-Account Sweep Results[/bold]", show_lines=True)
    table.add_column("Account (Profile)", style="cyan")
    table.add_column("Unused Resources", justify="right")
    table.add_column("Est. Monthly Savings", justify="right", style="green")
    table.add_column("Status")

    for profile, data in agg["by_account"].items():
        if "error" in data and data["error"]:
            table.add_row(profile, "—", "—", f"[red]Error: {data['error'][:50]}[/red]")
        else:
            savings = data["savings"]
            resources = data["resources"]
            status = "[green]OK[/green]" if not data.get("errors") else f"[yellow]{len(data['errors'])} warning(s)[/yellow]"
            table.add_row(
                profile,
                str(resources),
                f"${savings:.2f}",
                status,
            )

    console.print(table)

    # Aggregate totals
    console.print(
        f"\n[bold]Totals:[/bold] "
        f"[cyan]{agg['accounts_scanned']}[/cyan] accounts scanned, "
        f"[cyan]{agg['total_resources']}[/cyan] unused resources found, "
        f"[green]${agg['total_savings_usd']:.2f}[/green]/month recoverable"
    )

    # Top 10 recommendations across all accounts
    top_recs = sorted(
        agg["all_recommendations"],
        key=lambda r: r.get("monthly_savings_usd", 0),
        reverse=True,
    )[:10]

    if top_recs:
        top_table = Table(title="[bold]Top 10 Recommendations (all accounts)[/bold]", show_lines=True)
        top_table.add_column("Account", style="cyan", width=20)
        top_table.add_column("Service", width=6)
        top_table.add_column("Resource ID", max_width=45)
        top_table.add_column("Savings/mo", justify="right", style="green", width=12)
        top_table.add_column("Reason", style="dim")

        for rec in top_recs:
            top_table.add_row(
                rec.get("_account", ""),
                (rec.get("service") or "").upper(),
                rec.get("resource_id", ""),
                f"${rec.get('monthly_savings_usd', 0):.2f}",
                (rec.get("reason") or "")[:50],
            )
        console.print(top_table)
