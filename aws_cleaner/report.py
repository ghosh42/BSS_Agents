"""Report generation - Rich CLI tables, JSON export, CSV, Markdown summary."""
import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Any

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

logger = logging.getLogger(__name__)
console = Console()


RISK_COLORS = {
    "SAFE": "green",
    "CAUTION": "yellow",
    "DANGEROUS": "red",
}

ACTION_COLORS = {
    "DELETE": "red",
    "ARCHIVE": "cyan",
    "RESIZE": "blue",
    "INVESTIGATE": "yellow",
    "KEEP": "green",
}


def render_table(state: Dict[str, Any]) -> None:
    """Render recommendations as a Rich table."""
    recommendations = state.get("recommendations", [])
    discovered = state.get("discovered_resources", {})
    cost_data = state.get("cost_data", {})

    # Summary panel
    total_resources = sum(len(v) for v in discovered.values())
    total_savings = sum(r.get("monthly_savings_usd", 0) for r in recommendations)

    summary = Text()
    summary.append("AWS Cleaner Agent Results\n", style="bold")
    summary.append(f"Resources scanned: ")
    summary.append(f"{total_resources}", style="bold cyan")
    summary.append(f" | Estimated monthly savings: ")
    summary.append(f"${total_savings:.2f}", style="bold green")
    console.print(Panel(summary, border_style="blue"))

    if not recommendations:
        if total_resources == 0:
            console.print("[green]No unused resources found. Account looks clean![/green]")
        else:
            # skip-llm mode: render raw discovery table without AI recommendations
            _render_raw_discovery_table(discovered, cost_data)
        return

    # Recommendations table
    table = Table(title="Cleanup Recommendations", show_lines=True)
    table.add_column("Resource", style="cyan", max_width=40)
    table.add_column("Service", style="magenta")
    table.add_column("Risk", justify="center")
    table.add_column("Action", justify="center")
    table.add_column("Savings/mo", justify="right", style="green")
    table.add_column("Reason", max_width=50)

    for rec in sorted(recommendations, key=lambda x: x.get("monthly_savings_usd", 0), reverse=True):
        risk = rec.get("risk", "UNKNOWN")
        action = rec.get("action", "INVESTIGATE")
        risk_styled = Text(risk, style=RISK_COLORS.get(risk, "white"))
        action_styled = Text(action, style=ACTION_COLORS.get(action, "white"))

        table.add_row(
            rec.get("resource_id", "?")[:40],
            rec.get("service", "?"),
            risk_styled,
            action_styled,
            f"${rec.get('monthly_savings_usd', 0):.2f}",
            rec.get("reason", "")[:50],
        )

    console.print(table)

    # Errors
    errors = state.get("errors", [])
    if errors:
        console.print(f"\n[yellow]Warnings ({len(errors)}):[/yellow]")
        for err in errors:
            console.print(f"  [dim]• {err}[/dim]")


def _render_raw_discovery_table(discovered: Dict[str, List], cost_data: Dict) -> None:
    """Render raw discovery results (no LLM analysis) sorted by size."""
    table = Table(title="Discovered Unused Resources (Discovery-Only Mode)", show_lines=True)
    table.add_column("Resource", style="cyan", max_width=50)
    table.add_column("Service", style="magenta")
    table.add_column("Size / Info", justify="right")
    table.add_column("Objects", justify="right")
    table.add_column("Last Modified / Reason", max_width=35)

    # Flatten and sort by size desc
    all_resources = []
    for service, items in discovered.items():
        for item in items:
            all_resources.append(item)
    all_resources.sort(key=lambda r: r.get("size_bytes", 0), reverse=True)

    for r in all_resources:
        service = r.get("service", "?")
        size = r.get("size_bytes", 0)
        size_str = _fmt_size(size) if service == "s3" else r.get("image_count", r.get("volume_size_gb", r.get("instance_type", "?")))
        obj_count = str(r.get("object_count", r.get("image_count", "")))
        reason = r.get("reason", r.get("last_modified", ""))
        if reason and len(str(reason)) > 35:
            reason = str(reason)[:35]

        table.add_row(
            r.get("resource_id", "?")[:50],
            service,
            str(size_str),
            obj_count,
            str(reason),
        )

    console.print(table)
    console.print(f"\n[dim]Note: Run without --skip-llm to get AI-powered risk analysis and savings estimates.[/dim]")


def render_json(state: Dict[str, Any]) -> str:
    """Render results as JSON string."""
    output = {
        "discovered_resources": state.get("discovered_resources", {}),
        "cost_data": state.get("cost_data", {}),
        "recommendations": state.get("recommendations", []),
        "summary": {
            "total_resources": sum(len(v) for v in state.get("discovered_resources", {}).values()),
            "total_monthly_savings": sum(
                r.get("monthly_savings_usd", 0) for r in state.get("recommendations", [])
            ),
        },
        "errors": state.get("errors", []),
    }
    return json.dumps(output, indent=2, default=str)


def render_discovery_summary(discovered: Dict[str, List], to_stderr: bool = False) -> None:
    """Print a quick summary of what was discovered (before LLM analysis)."""
    out = Console(stderr=True) if to_stderr else console
    out.print("\n[bold]Discovery Results:[/bold]")
    for service, resources in discovered.items():
        count = len(resources)
        icon = "🪣" if service == "s3" else "🐳" if service == "ecr" else "💾" if service == "ebs" else "🖥️"
        out.print(f"  {icon} {service.upper()}: [cyan]{count}[/cyan] unused resources")
    out.print()


def render_csv(state: Dict[str, Any]) -> str:
    """Render recommendations (and raw discoveries if no LLM) as CSV.

    Suitable for pasting into Excel, Jira attachments, or email tables.
    """
    recommendations = state.get("recommendations", [])
    discovered = state.get("discovered_resources", {})
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    buf = io.StringIO()
    writer = csv.writer(buf)

    if recommendations:
        writer.writerow([f"# AWS Cleaner Agent — Recommendations — {generated_at}"])
        writer.writerow([])
        writer.writerow(["Service", "Resource ID", "Action", "Risk", "Estimated Savings/mo (USD)", "Reason"])
        for rec in sorted(recommendations, key=lambda r: r.get("monthly_savings_usd", 0), reverse=True):
            writer.writerow([
                (rec.get("service") or "").upper(),
                rec.get("resource_id", ""),
                rec.get("action", ""),
                rec.get("risk", ""),
                f"{rec.get('monthly_savings_usd', 0):.2f}",
                rec.get("reason", ""),
            ])
        total = sum(r.get("monthly_savings_usd", 0) for r in recommendations)
        writer.writerow([])
        writer.writerow(["", "", "", "TOTAL", f"{total:.2f}", ""])
    else:
        # Discovery-only mode
        writer.writerow([f"# AWS Cleaner Agent — Discovery Results — {generated_at}"])
        writer.writerow([])
        writer.writerow(["Service", "Resource ID", "Size / Info", "Last Modified", "Reason"])
        for service, resources in discovered.items():
            for r in sorted(resources, key=lambda x: x.get("size_bytes", 0), reverse=True):
                writer.writerow([
                    service.upper(),
                    r.get("resource_id", ""),
                    _fmt_size(r.get("size_bytes", 0)) if service == "s3" else str(r.get("volume_size_gb", r.get("instance_type", ""))),
                    str(r.get("last_modified", ""))[:19],
                    r.get("reason", ""),
                ])

    return buf.getvalue()


def render_multi_account_csv(agg: Dict[str, Any], limit: int = 1000) -> str:
    """Render all recommendations from a multi-account sweep as CSV.

    Columns include Account so rows from different profiles are identifiable.
    Results are sorted by estimated monthly savings descending and capped at
    ``limit`` rows (default 1000).
    """
    all_recs = agg.get("all_recommendations", [])
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sorted_recs = sorted(
        all_recs,
        key=lambda r: r.get("monthly_savings_usd", 0),
        reverse=True,
    )[:limit]

    buf = io.StringIO()
    writer = csv.writer(buf)

    accounts = agg.get("accounts_scanned", len(agg.get("by_account", {})))
    writer.writerow([
        f"# AWS Cleaner Agent — Top {limit} Savings ({accounts} accounts) — {generated_at}"
    ])
    writer.writerow([])
    writer.writerow(["Account", "Service", "Resource ID", "Action", "Risk",
                     "Estimated Savings/mo (USD)", "Reason"])

    for rec in sorted_recs:
        writer.writerow([
            rec.get("_account", ""),
            (rec.get("service") or "").upper(),
            rec.get("resource_id", ""),
            rec.get("action", ""),
            rec.get("risk", ""),
            f"{rec.get('monthly_savings_usd', 0):.2f}",
            rec.get("reason", ""),
        ])

    if sorted_recs:
        total = sum(r.get("monthly_savings_usd", 0) for r in sorted_recs)
        writer.writerow([])
        writer.writerow(["", "", "", "", "TOTAL", f"{total:.2f}", ""])

    return buf.getvalue()


def render_markdown(state: Dict[str, Any], profile: str = "", region: str = "") -> str:
    """Render a Jira/email-friendly Markdown report.

    Produces clean Markdown: a summary header, a savings table, and
    a per-service breakdown.  Paste directly into Jira, Confluence,
    GitHub issues, or email.
    """
    recommendations = state.get("recommendations", [])
    discovered = state.get("discovered_resources", {})
    errors = state.get("errors", [])
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    total_resources = sum(len(v) for v in discovered.values())
    total_savings = sum(r.get("monthly_savings_usd", 0) for r in recommendations)

    lines = []
    lines.append("## AWS Cleaner Agent Report")
    lines.append("")
    lines.append(f"| | |")
    lines.append(f"|---|---|")
    lines.append(f"| **Generated** | {generated_at} |")
    if profile:
        lines.append(f"| **Account** | {profile} |")
    if region:
        lines.append(f"| **Region** | {region} |")
    lines.append(f"| **Unused resources found** | {total_resources} |")
    lines.append(f"| **Estimated monthly savings** | ${total_savings:.2f} |")
    lines.append("")

    if recommendations:
        lines.append("### Recommendations")
        lines.append("")
        lines.append("| Service | Resource ID | Action | Risk | Savings/mo | Reason |")
        lines.append("|---------|-------------|--------|------|------------|--------|")
        for rec in sorted(recommendations, key=lambda r: r.get("monthly_savings_usd", 0), reverse=True):
            svc = (rec.get("service") or "").upper()
            rid = rec.get("resource_id", "")
            action = rec.get("action", "")
            risk = rec.get("risk", "")
            savings = f"${rec.get('monthly_savings_usd', 0):.2f}"
            reason = (rec.get("reason") or "").replace("|", "/")
            lines.append(f"| {svc} | `{rid}` | {action} | {risk} | {savings} | {reason} |")
        lines.append("")
        lines.append(f"**Total estimated monthly savings: ${total_savings:.2f}**")
    else:
        lines.append("### Discovered Unused Resources (Discovery Mode)")
        lines.append("")
        lines.append("| Service | Resource ID | Size / Info | Reason |")
        lines.append("|---------|-------------|-------------|--------|")
        for service, resources in discovered.items():
            for r in sorted(resources, key=lambda x: x.get("size_bytes", 0), reverse=True):
                svc = service.upper()
                rid = r.get("resource_id", "")
                info = _fmt_size(r.get("size_bytes", 0)) if service == "s3" else str(r.get("volume_size_gb", r.get("instance_type", "")))
                reason = (r.get("reason") or "").replace("|", "/")
                lines.append(f"| {svc} | `{rid}` | {info} | {reason} |")
        lines.append("")
        lines.append(f"_Total: {total_resources} unused resources found._")

    if errors:
        lines.append("")
        lines.append("### Warnings")
        lines.append("")
        for err in errors:
            lines.append(f"- {err}")

    lines.append("")
    lines.append("---")
    lines.append("_Generated by [aws-cleaner-agent](https://github.com/Vonage/aws-cleaner-agent)_")

    return "\n".join(lines)


def _fmt_size(size_bytes: int) -> str:
    if size_bytes >= 1_073_741_824:
        return f"{size_bytes / 1_073_741_824:.1f} GB"
    elif size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"
