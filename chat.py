#!/usr/bin/env python3
"""AWS Cleaner Agent - Conversational Chat Interface.

Talk to the agent in plain English. It understands what you want,
runs the scan, and explains results in plain language.

Usage:
    python3 chat.py
    python3 chat.py --profile <your-aws-profile>
"""
import argparse
import json
import logging
import re
import sys

import boto3
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

from aws_cleaner.config import ScanConfig
from aws_cleaner.llm import BedrockLLM
from aws_cleaner.agent import run_agent
from aws_cleaner.multi_account import sweep_accounts, render_sweep_summary, aggregate_sweep
from aws_cleaner.tools.deleter import delete_resources, render_audit_log
from aws_cleaner.regions import sweep_regions, render_regions_summary
from aws_cleaner.report import render_csv, render_markdown

console = Console()

SYSTEM_PROMPT = """You are a helpful AWS cloud cost optimization assistant. Your job is to help users find and clean up unused AWS resources.

You can scan these services: S3 buckets, ECR repositories, EBS volumes, EC2 instances.

When the user asks something, extract these settings from their message (use defaults if not mentioned):
- services: which AWS services to scan (default: all)
- s3_days: how many days inactive before flagging S3 (default: 90)
- ecr_days: how many days without push before flagging ECR (default: 180)
- ec2_days: how many days stopped before flagging EC2 (default: 30)
- skip_llm: true if user just wants discovery without AI analysis (default: false)
- delete: true if user explicitly asks to delete/remove/clean up/purge resources (default: false)
- profiles: list of AWS profile names if user mentions multiple accounts/profiles/environments (default: null)
- all_regions: true if user mentions "all regions", "every region", "globally", or "across regions" (default: false)
- output_format: "csv" if user asks for CSV/spreadsheet/export, "markdown" if they want markdown/Jira/Confluence/email format, otherwise null (default: null)

Respond ONLY with valid JSON in this exact format:
{
  "understood": "one sentence describing what you're about to do",
  "services": ["s3", "ecr", "ebs", "ec2"],
  "s3_days": 90,
  "ecr_days": 180,
  "ec2_days": 30,
  "skip_llm": false,
  "delete": false,
  "profiles": null,
  "all_regions": false,
  "output_format": null
}

Examples:
- "scan S3 for buckets unused for 60 days" → services=["s3"], s3_days=60
- "find all unused EBS volumes" → services=["ebs"], skip_llm=false
- "quick check of everything" → all services, skip_llm=true
- "what's wasting money in ECR and S3?" → services=["s3","ecr"]
- "delete unused EBS volumes" → services=["ebs"], delete=true
- "clean up old S3 buckets" → services=["s3"], delete=true
- "scan qa, staging and prod profiles" → profiles=["qa","staging","prod"]
- "scan all regions" → all_regions=true
- "check every region for unused EBS" → services=["ebs"], all_regions=true
- "give me a CSV" → output_format="csv"
- "show as markdown I can paste in Jira" → output_format="markdown"
- "export for email" → output_format="markdown"
"""


def parse_user_intent(llm: BedrockLLM, user_message: str) -> dict:
    """Use LLM to parse natural language into scan config."""
    prompt = f"{SYSTEM_PROMPT}\n\nUser message: {user_message}\n\nJSON response:"
    raw = llm.invoke(prompt, max_tokens=512)

    # Extract JSON from response
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not match:
        raise ValueError(f"Could not parse LLM response: {raw[:200]}")

    return json.loads(match.group())


def build_plain_english_report(llm: BedrockLLM, result: dict, user_message: str) -> str:
    """Ask the LLM to summarize the scan results in plain conversational English."""
    discovered = result.get("discovered_resources", {})
    recommendations = result.get("recommendations", [])
    errors = result.get("errors", [])

    total = sum(len(v) for v in discovered.values())
    total_savings = sum(r.get("monthly_savings_usd", 0) for r in recommendations)

    # Build compact per-service summary to avoid huge token payloads
    discovery_summary = {}
    for service, resources in discovered.items():
        sorted_res = sorted(resources, key=lambda r: r.get("size_bytes", 0), reverse=True)
        discovery_summary[service] = {
            "total_count": len(resources),
            "top_resources": [
                {
                    "resource_id": r.get("resource_id"),
                    "size_bytes": r.get("size_bytes", 0),
                    "last_modified": r.get("last_modified"),
                    "reason": r.get("reason"),
                }
                for r in sorted_res[:10]
            ],
        }

    # Cap recommendations at top 20 by savings
    top_recs = sorted(recommendations, key=lambda r: r.get("monthly_savings_usd", 0), reverse=True)[:20]

    prompt = f"""You are a helpful AWS cost optimization assistant. The user asked: "{user_message}"

Here are the scan results:

DISCOVERY SUMMARY ({total} total unused resources):
{json.dumps(discovery_summary, indent=2, default=str)}

TOP RECOMMENDATIONS (by estimated savings):
{json.dumps(top_recs, indent=2, default=str)}

ERRORS: {json.dumps(errors, default=str)}

Write a clear, friendly summary in plain English (no JSON, no bullet-point overload). Include:
1. What you found overall (or "nothing unused found" if clean)
2. The top 3-5 most impactful things to clean up, in plain sentences
3. Estimated monthly savings if they follow the recommendations (total: ${total_savings:.2f})
4. Any cautions or things to double-check before deleting

Keep it conversational and under 300 words. Avoid technical jargon where possible."""

    return llm.invoke(prompt, max_tokens=1024)


def run_chat(profile: str, region: str):
    """Main interactive chat loop."""
    config = ScanConfig(aws_profile=profile, aws_region=region)
    llm = BedrockLLM(config)

    console.print(Panel(
        f"[bold blue]AWS Cleaner Agent[/bold blue] — Chat Mode\n"
        f"Profile: [cyan]{profile}[/cyan] | Region: [cyan]{region}[/cyan]\n\n"
        f"[dim]Tell me what to scan in plain English. Type [bold]quit[/bold] to exit.[/dim]",
        border_style="blue"
    ))
    console.print()

    while True:
        try:
            user_input = console.input("[bold green]You:[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "bye", "q"):
            console.print("[dim]Goodbye![/dim]")
            break

        console.print()

        # Step 1: Parse user intent
        console.print("[dim]Understanding your request...[/dim]")
        try:
            intent = parse_user_intent(llm, user_input)
        except Exception as e:
            console.print(f"[yellow]Couldn't parse intent ({e}), using defaults.[/yellow]")
            intent = {
                "understood": "Scanning all services with default settings.",
                "services": ["s3", "ecr", "ebs", "ec2"],
                "s3_days": 90, "ecr_days": 180, "ec2_days": 30,
                "skip_llm": False
            }

        console.print(f"[bold cyan]Agent:[/bold cyan] {intent.get('understood', 'Got it, scanning now...')}")
        console.print()

        # Step 2: Build scan config from intent (needed for both single and multi-account)
        scan_config = ScanConfig(
            aws_profile=profile,
            aws_region=region,
            services=intent.get("services", config.services),
            s3_unused_days=intent.get("s3_days", config.s3_unused_days),
            ecr_unused_days=intent.get("ecr_days", config.ecr_unused_days),
            ec2_stopped_days=intent.get("ec2_days", config.ec2_stopped_days),
            skip_llm=intent.get("skip_llm", False),
            all_regions=intent.get("all_regions", False),
        )

        # Multi-account sweep path
        profiles_intent = intent.get("profiles")
        if profiles_intent and isinstance(profiles_intent, list) and len(profiles_intent) > 1:
            _handle_multi_account(llm, intent, profiles_intent, region, scan_config)
            console.print()
            continue

        # Step 3: Run the scan
        console.print(f"[dim]Scanning {', '.join(scan_config.services)}...[/dim]")
        try:
            if scan_config.all_regions:
                console.print("[dim]Running all-regions sweep (this takes a few minutes)...[/dim]")
                result = sweep_regions(scan_config)
                render_regions_summary(result)
            else:
                result = run_agent(scan_config)
        except Exception as e:
            console.print(f"[red]Scan failed: {e}[/red]\n")
            continue

        total = sum(len(v) for v in result.get("discovered_resources", {}).values())
        console.print(f"[dim]Found {total} unused resource(s). Generating summary...[/dim]\n")

        # Step 4: Structured export OR plain English response
        export_format = intent.get("output_format")
        if export_format == "csv":
            console.print(f"[bold cyan]Agent:[/bold cyan] Here's your CSV export:\n")
            print(render_csv(result))
            console.print()
        elif export_format == "markdown":
            console.print(f"[bold cyan]Agent:[/bold cyan] Here's your Markdown report (paste into Jira / email):\n")
            md_text = render_markdown(result, profile=scan_config.aws_profile, region=scan_config.aws_region)
            print(md_text)
            console.print()
        elif total == 0:
            reply = "Great news — I didn't find any unused resources matching your criteria. Your account looks clean for those services."
        elif intent.get("skip_llm"):
            # Just list what was found, no LLM analysis
            lines = [f"Here's what I found ({total} unused resources):\n"]
            for service, resources in result.get("discovered_resources", {}).items():
                if resources:
                    lines.append(f"**{service.upper()}** ({len(resources)} resources):")
                    for r in resources[:5]:  # cap at 5 per service
                        lines.append(f"  - {r.get('resource_id', 'unknown')} — {r.get('reason', '')}")
                    if len(resources) > 5:
                        lines.append(f"  - ...and {len(resources) - 5} more")
            reply = "\n".join(lines)
        else:
            try:
                reply = build_plain_english_report(llm, result, user_input)
            except Exception as e:
                reply = f"Scan completed but couldn't generate summary ({e}). Found {total} unused resources."

        if not export_format:
            console.print(f"[bold cyan]Agent:[/bold cyan]")
            console.print(Markdown(reply))
            console.print()

        # Step 5: Deletion flow (if requested)
        if intent.get("delete") and total > 0:
            _handle_deletion_flow(result, scan_config)


def _handle_deletion_flow(result: dict, scan_config: ScanConfig) -> None:
    """Interactive deletion flow after a scan — always dry-run first, then confirm."""
    recommendations = result.get("recommendations", [])
    if not recommendations:
        console.print("[yellow]No actionable recommendations from LLM to delete.[/yellow]\n")
        return

    session = boto3.Session(
        profile_name=scan_config.aws_profile,
        region_name=scan_config.aws_region,
    )

    # Show dry-run preview first
    console.print("[bold yellow]\nDeletion requested. Showing dry-run preview first:[/bold yellow]")
    dry_audit = delete_resources(
        session, recommendations, execute=False, interactive=True, force=True
    )
    render_audit_log(dry_audit, execute=False)

    # Confirm before live execution
    try:
        answer = console.input(
            "\n[bold red]Execute actual deletion? Type 'yes' to confirm, anything else to cancel:[/bold red] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""

    if answer == "yes":
        console.print("[bold red]Executing deletion...[/bold red]")
        audit = delete_resources(
            session, recommendations, execute=True, interactive=False, force=True
        )
        render_audit_log(audit, execute=True)
    else:
        console.print("[yellow]Deletion cancelled. No resources were deleted.[/yellow]")
    console.print()


def _handle_multi_account(
    llm: BedrockLLM,
    intent: dict,
    profiles: list,
    region: str,
    base_config: ScanConfig,
) -> None:
    """Run a multi-account sweep from chat and summarise results."""
    sweep_results = sweep_accounts(
        profiles=profiles,
        base_config=base_config,
        region=region,
    )
    render_sweep_summary(sweep_results)

    if intent.get("delete"):
        agg = aggregate_sweep(sweep_results)
        recs = agg["all_recommendations"]
        if recs:
            console.print("[yellow]\nNote: live multi-account deletion must be confirmed per profile.[/yellow]")
            console.print(f"Found [bold]{len(recs)}[/bold] recommendations across all accounts.")
            console.print("[dim]Re-run per profile with delete intent to actually delete resources.[/dim]")
        else:
            console.print("[green]No recommendations — nothing to delete across all accounts.[/green]")


def main():
    parser = argparse.ArgumentParser(
        description="AWS Cleaner Agent - Chat with your AWS account in plain English"
    )
    parser.add_argument("--profile", default=os.getenv("AWS_CLEANER_PROFILE", "default"), help="AWS profile name (default: $AWS_CLEANER_PROFILE or 'default')")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

    run_chat(profile=args.profile, region=args.region)


if __name__ == "__main__":
    main()
