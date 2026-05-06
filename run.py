#!/usr/bin/env python3
"""AWS Cleaner Agent - CLI Entry Point.

Scans AWS accounts for unused resources and provides AI-powered cleanup recommendations.

Usage:
    python run.py --profile <your-aws-profile> --region us-east-1
    python run.py --services s3,ecr --days-unused 60
    python run.py --output json > report.json
"""
import argparse
import logging
import os
import sys

from rich.console import Console

from aws_cleaner.config import ScanConfig
from aws_cleaner.agent import run_agent
from aws_cleaner.report import render_table, render_json, render_discovery_summary, render_csv, render_markdown
from aws_cleaner.multi_account import sweep_accounts, render_sweep_summary, aggregate_sweep
from aws_cleaner.tools.deleter import delete_resources, render_audit_log
from aws_cleaner.regions import sweep_regions, render_regions_summary

console = Console()


def parse_args():
    parser = argparse.ArgumentParser(
        description="AWS Cleaner Agent - Find and recommend cleanup of unused AWS resources",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--profile", default=os.getenv("AWS_CLEANER_PROFILE", "default"),
        help="AWS profile name (default: $AWS_CLEANER_PROFILE or 'default')",
    )
    parser.add_argument(
        "--region", default="us-east-1",
        help="AWS region (default: us-east-1)",
    )
    parser.add_argument(
        "--all-regions", action="store_true",
        help="Scan all enabled AWS regions (ECR/EBS/EC2 per region; S3 is global)",
    )
    parser.add_argument(
        "--services", default="s3,ecr,ebs,ec2",
        help="Comma-separated services to scan (default: s3,ecr,ebs,ec2)",
    )
    parser.add_argument(
        "--s3-days", type=int, default=90,
        help="Days without activity to flag S3 bucket (default: 90)",
    )
    parser.add_argument(
        "--ecr-days", type=int, default=180,
        help="Days without push to flag ECR repo (default: 180)",
    )
    parser.add_argument(
        "--ec2-days", type=int, default=30,
        help="Days stopped to flag EC2 instance (default: 30)",
    )
    parser.add_argument(
        "--model", default="us.meta.llama3-3-70b-instruct-v1:0",
        help="Bedrock model ID for analysis (default: Llama 3.3 70B)",
    )
    parser.add_argument(
        "--output", choices=["table", "json", "csv", "markdown"], default="table",
        help="Output format: table (default), json, csv, markdown",
    )
    parser.add_argument(
        "--skip-llm", action="store_true",
        help="Skip LLM analysis (discovery only)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable verbose logging",
    )

    # Multi-account sweep
    parser.add_argument(
        "--profiles",
        help="Comma-separated AWS profile names for multi-account sweep (overrides --profile)",
    )
    parser.add_argument(
        "--sweep-workers", type=int, default=4,
        help="Parallel workers for multi-account sweep (default: 4)",
    )

    # Deletion
    parser.add_argument(
        "--delete", action="store_true",
        help="Enable deletion mode (dry-run by default, combine with --confirm for real deletion)",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="Confirm real deletion (requires --delete; skipping this runs a dry-run)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Skip per-resource confirmation prompts (requires --delete --confirm)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Build base config from CLI args
    profiles = [p.strip() for p in args.profiles.split(",")] if args.profiles else None
    primary_profile = (profiles[0] if profiles else None) or args.profile

    config = ScanConfig(
        aws_profile=primary_profile,
        aws_region=args.region,
        services=args.services.split(","),
        s3_unused_days=args.s3_days,
        ecr_unused_days=args.ecr_days,
        ec2_stopped_days=args.ec2_days,
        model_id=args.model,
        output_format=args.output,
        skip_llm=args.skip_llm,
        all_regions=args.all_regions,
    )

    # In non-table mode, use stderr console for status messages
    stderr_console = Console(stderr=True) if args.output != "table" else console

    try:
        # ── Multi-account sweep ──────────────────────────────────────────────
        if profiles and len(profiles) > 1:
            sweep_results = sweep_accounts(
                profiles=profiles,
                base_config=config,
                region=args.region,
                max_workers=args.sweep_workers,
            )
            render_sweep_summary(sweep_results)

            if args.delete:
                agg = aggregate_sweep(sweep_results)
                all_recs = agg["all_recommendations"]
                execute = args.confirm
                if execute:
                    stderr_console.print(
                        f"\n[bold red]LIVE DELETION across {len(profiles)} account(s)[/bold red]"
                    )
                else:
                    stderr_console.print(
                        "\n[bold blue]DRY-RUN deletion preview (add --confirm to execute)[/bold blue]"
                    )
                audit = delete_resources(
                    session=None,  # multi-account: deletions handled per-account below
                    recommendations=all_recs,
                    execute=False,  # preview only in multi-account mode
                    interactive=True,
                )
                render_audit_log(audit, execute=False)
                if execute:
                    stderr_console.print(
                        "[yellow]Note: live multi-account deletion must be run per-profile.[/yellow]"
                    )
            return

        # ── Single-account scan ──────────────────────────────────────────────
        stderr_console.print(f"\n[bold blue]AWS Cleaner Agent[/bold blue]")
        stderr_console.print(f"Profile: [cyan]{config.aws_profile}[/cyan] | Region: [cyan]{config.aws_region}[/cyan]")
        stderr_console.print(f"Services: [cyan]{', '.join(config.services)}[/cyan]")
        stderr_console.print(f"Model: [cyan]{config.model_id}[/cyan]\n")

        result = run_agent(config)

        # Show discovery summary
        render_discovery_summary(result.get("discovered_resources", {}), to_stderr=(args.output != "table"))

        # All-regions: replace single-region result with aggregated sweep
        if args.all_regions:
            stderr_console.print("[bold]\nStarting all-regions sweep...[/bold]")
            result = sweep_regions(config)
            render_regions_summary(result)
        else:
            render_discovery_summary(result.get("discovered_resources", {}), to_stderr=(args.output != "table"))

        # Render output
        if args.output == "json":
            print(render_json(result))
        elif args.output == "csv":
            print(render_csv(result))
        elif args.output == "markdown":
            print(render_markdown(result, profile=config.aws_profile, region=config.aws_region))
        else:
            render_table(result)

        # ── Deletion ─────────────────────────────────────────────────────────
        if args.delete:
            import boto3
            session = boto3.Session(
                profile_name=config.aws_profile,
                region_name=config.aws_region,
            )
            recommendations = result.get("recommendations", [])
            if not recommendations:
                stderr_console.print("\n[yellow]No recommendations to delete.[/yellow]")
            else:
                execute = args.confirm
                if execute:
                    stderr_console.print(
                        f"\n[bold red]LIVE DELETION — {len(recommendations)} resource(s)[/bold red]"
                    )
                else:
                    stderr_console.print(
                        "\n[bold blue]Deletion dry-run (add --confirm to execute for real)[/bold blue]"
                    )
                audit = delete_resources(
                    session,
                    recommendations,
                    execute=execute,
                    force=args.force,
                    interactive=True,
                )
                render_audit_log(audit, execute=execute)

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[red]Error: {e}[/red]")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
