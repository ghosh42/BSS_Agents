"""Agent configuration - thresholds, region, profile settings."""
import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class ScanConfig:
    """Configuration for the AWS cleaner scan."""
    aws_profile: str = os.getenv("AWS_CLEANER_PROFILE", "default")
    aws_region: str = os.getenv("AWS_CLEANER_REGION", "us-east-1")
    
    # Thresholds for "unused" classification
    s3_unused_days: int = 90
    ecr_unused_days: int = 180
    ec2_stopped_days: int = 30
    ebs_unattached: bool = True
    
    # Which services to scan
    services: List[str] = field(default_factory=lambda: ["s3", "ecr", "ebs", "ec2"])
    
    # LLM settings
    model_id: str = "us.meta.llama3-3-70b-instruct-v1:0"
    model_max_tokens: int = 4096
    
    # Output
    output_format: str = "table"  # table, json, slack

    # Execution control
    skip_llm: bool = False  # Skip LLM analysis, just report raw discoveries
    all_regions: bool = False  # Scan all enabled AWS regions instead of just aws_region


# Default thresholds for cost significance (USD/month)
COST_THRESHOLD_LOW = 1.0
COST_THRESHOLD_MEDIUM = 10.0
COST_THRESHOLD_HIGH = 50.0
