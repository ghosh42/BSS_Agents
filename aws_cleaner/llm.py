"""Bedrock LLM wrapper for the AWS Cleaner Agent.

Uses Meta Llama 3.3 70B via inference profiles for risk analysis and recommendations.
Falls back to raw invoke_model since langchain-aws may not support inference profiles.
"""
import json
import logging
from typing import Optional

import boto3
from botocore.config import Config

from .config import ScanConfig

logger = logging.getLogger(__name__)


class BedrockLLM:
    """Wrapper around AWS Bedrock for Llama 3.3 70B invocation."""

    def __init__(self, config: ScanConfig):
        self.config = config
        self._client = None

    @property
    def client(self):
        if self._client is None:
            session = boto3.Session(
                profile_name=self.config.aws_profile,
                region_name=self.config.aws_region,
            )
            self._client = session.client(
                "bedrock-runtime",
                config=Config(
                    read_timeout=60,
                    connect_timeout=10,
                    retries={"max_attempts": 2},
                ),
            )
        return self._client

    def invoke(self, prompt: str, max_tokens: Optional[int] = None) -> str:
        """Invoke Llama 3.3 70B with a prompt and return the generated text."""
        max_tokens = max_tokens or self.config.model_max_tokens

        body = json.dumps({
            "prompt": prompt,
            "max_gen_len": max_tokens,
            "temperature": 0,
        })

        logger.info(f"Invoking {self.config.model_id} (max_tokens={max_tokens})")

        response = self.client.invoke_model(
            modelId=self.config.model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )

        response_body = json.loads(response["body"].read())
        generation = response_body.get("generation", "")

        logger.debug(f"LLM response length: {len(generation)} chars")
        return generation.strip()

    def analyze_resources(self, resources: dict, cost_data: dict) -> str:
        """Send discovered resources to LLM for risk analysis and recommendations.

        Batches resources in groups of 25 to avoid token limits, then combines results.
        Prioritizes resources with actual data (size_bytes > 0) and sorts by size desc.
        """
        # Flatten all resources and sort by size descending (largest first)
        all_resources = []
        for service, items in resources.items():
            for item in items:
                all_resources.append(item)

        total_count = len(all_resources)

        # Sort: non-empty first (size_bytes desc), then empty buckets
        all_resources.sort(key=lambda r: r.get("size_bytes", 0), reverse=True)

        # Cap at top 50 to keep LLM prompt manageable
        MAX_RESOURCES = 50
        resources_to_analyze = all_resources[:MAX_RESOURCES]
        skipped = total_count - len(resources_to_analyze)

        logger.info(f"Analyzing {len(resources_to_analyze)}/{total_count} resources with LLM "
                    f"(top by size; {skipped} empty/smaller buckets omitted from AI analysis)")

        # Batch into groups of 25
        BATCH_SIZE = 25
        all_recommendations = []

        for i in range(0, len(resources_to_analyze), BATCH_SIZE):
            batch = resources_to_analyze[i:i + BATCH_SIZE]
            batch_num = (i // BATCH_SIZE) + 1
            total_batches = (len(resources_to_analyze) + BATCH_SIZE - 1) // BATCH_SIZE
            logger.info(f"LLM batch {batch_num}/{total_batches} ({len(batch)} resources)")

            # Build cost subset for this batch
            batch_cost = {
                r["resource_id"]: cost_data.get(r["resource_id"], 0)
                for r in batch
            }

            prompt = f"""You are an AWS cost optimization expert. Analyze these unused/underutilized AWS resources and provide cleanup recommendations.

Total unused resources found: {total_count} (analyzing batch {batch_num}/{total_batches}, top {len(batch)} by storage size)

## Resources to Analyze
{json.dumps(batch, indent=2, default=str)}

## Cost Data (monthly USD, 0 means no billing data available)
{json.dumps(batch_cost, indent=2, default=str)}

## S3 Storage Cost Reference
- Standard tier: ~$0.023 per GB/month
- 1 GB = 1,073,741,824 bytes

## Instructions
For each resource, provide:
1. **Risk Level**: SAFE (can delete immediately), CAUTION (verify first), DANGEROUS (likely needed)
2. **Estimated Monthly Savings** in USD (calculate from size_bytes for S3: size_bytes/1073741824 * 0.023)
3. **Recommended Action**: DELETE, ARCHIVE, RESIZE, INVESTIGATE, or KEEP
4. **Reason**: Brief explanation (1 sentence max)

Risk rules — always base decisions on the data fields, never on bucket name alone:
- `days_since_modified` is the authoritative freshness signal.  Do NOT use bucket name to guess activity.
- days_since_modified < 30, OR last_modified_unreliable=true: DANGEROUS, KEEP — do not flag
- days_since_modified 30–89: CAUTION, INVESTIGATE
- days_since_modified 90–364: CAUTION for any bucket with size_bytes > 0; SAFE only for empty buckets
- days_since_modified >= 365 AND last_modified_unreliable=false: SAFE, DELETE or ARCHIVE
- days_since_modified is null (unknown): CAUTION — cannot confirm inactivity
- size_bytes=0 AND object_count=0 (truly empty): SAFE, DELETE, $0 savings regardless of bucket name

Respond ONLY with a valid JSON array, no preamble, no explanation:
[
  {{
    "resource_id": "...",
    "service": "s3|ecr|ebs|ec2",
    "risk": "SAFE|CAUTION|DANGEROUS",
    "monthly_savings_usd": 0.00,
    "action": "DELETE|ARCHIVE|RESIZE|INVESTIGATE|KEEP",
    "reason": "..."
  }}
]
"""
            try:
                raw = self.invoke(prompt, max_tokens=4096)
                json_start = raw.find("[")
                json_end = raw.rfind("]") + 1
                if json_start >= 0 and json_end > json_start:
                    try:
                        batch_recs = json.loads(raw[json_start:json_end])
                    except json.JSONDecodeError:
                        # LLM may return multiple JSON arrays — try to extract first valid one
                        # by progressively scanning for the first complete JSON array
                        candidate = raw[json_start:json_end]
                        # Try parsing the first complete array by finding balanced brackets
                        depth = 0
                        end_pos = -1
                        for ci, ch in enumerate(candidate):
                            if ch == "[":
                                depth += 1
                            elif ch == "]":
                                depth -= 1
                                if depth == 0:
                                    end_pos = ci + 1
                                    break
                        if end_pos > 0:
                            try:
                                batch_recs = json.loads(candidate[:end_pos])
                            except json.JSONDecodeError as parse_err:
                                logger.warning(f"  Batch {batch_num}: JSON parse failed: {parse_err}")
                                batch_recs = []
                        else:
                            batch_recs = []
                    all_recommendations.extend(batch_recs)
                    logger.info(f"  Batch {batch_num}: got {len(batch_recs)} recommendations")
                else:
                    logger.debug(f"  Batch {batch_num}: LLM returned non-JSON response")
            except Exception as e:
                logger.debug(f"  Batch {batch_num} failed: {e}")

        return json.dumps(all_recommendations)
