"""LangGraph-based AWS Cleaner Agent.

Orchestrates: discover → enrich_cost → analyze (LLM) → report
"""
import json
import logging
from typing import Any, Dict, List, Optional

import boto3
from langgraph.graph import StateGraph, START, END
from typing_extensions import TypedDict

from .config import ScanConfig
from .llm import BedrockLLM
from .tools.s3_scanner import scan_s3_buckets
from .tools.ecr_scanner import scan_ecr_repos
from .tools.ebs_scanner import scan_ebs_volumes
from .tools.ec2_scanner import scan_ec2_stopped
from .tools.cost_explorer import get_resource_costs

logger = logging.getLogger(__name__)


class AgentState(TypedDict):
    """State passed between graph nodes."""
    config: Dict[str, Any]
    session: Any  # boto3.Session (not serializable in TypedDict but works at runtime)
    discovered_resources: Dict[str, List[Dict]]
    cost_data: Dict[str, float]
    llm_analysis: str
    recommendations: List[Dict]
    errors: List[str]


def discover_node(state: AgentState) -> dict:
    """Run all scanner tools to discover unused resources."""
    config = ScanConfig(**state["config"])
    session = boto3.Session(profile_name=config.aws_profile, region_name=config.aws_region)

    discovered = {}
    errors = []

    scanners = {
        "s3": lambda: scan_s3_buckets(session, unused_days=config.s3_unused_days),
        "ecr": lambda: scan_ecr_repos(session, unused_days=config.ecr_unused_days),
        "ebs": lambda: scan_ebs_volumes(session),
        "ec2": lambda: scan_ec2_stopped(session, stopped_days=config.ec2_stopped_days),
    }

    for service in config.services:
        if service in scanners:
            try:
                logger.info(f"Scanning {service}...")
                results = scanners[service]()
                discovered[service] = results
                logger.info(f"  → {len(results)} unused resources found")
            except Exception as e:
                errors.append(f"{service}: {str(e)}")
                logger.error(f"Error scanning {service}: {e}")

    return {
        "discovered_resources": discovered,
        "errors": errors,
    }


def enrich_cost_node(state: AgentState) -> dict:
    """Attach cost data to discovered resources."""
    config = ScanConfig(**state["config"])
    session = boto3.Session(profile_name=config.aws_profile, region_name=config.aws_region)

    # Gather all resource IDs
    all_resource_ids = []
    for service_resources in state["discovered_resources"].values():
        for resource in service_resources:
            all_resource_ids.append(resource["resource_id"])

    if not all_resource_ids:
        return {"cost_data": {}}

    try:
        cost_data = get_resource_costs(session, resource_ids=all_resource_ids)
    except Exception as e:
        logger.warning(f"Cost enrichment failed: {e}")
        cost_data = {}

    return {"cost_data": cost_data}


def analyze_node(state: AgentState) -> dict:
    """Send discoveries to LLM for risk analysis and recommendations."""
    config = ScanConfig(**state["config"])
    discovered = state["discovered_resources"]

    # If nothing found, skip LLM
    total_resources = sum(len(v) for v in discovered.values())
    if total_resources == 0:
        return {
            "llm_analysis": "No unused resources found.",
            "recommendations": [],
        }

    llm = BedrockLLM(config)

    try:
        raw_response = llm.analyze_resources(discovered, state["cost_data"])

        # Parse LLM JSON response
        # Find JSON array in response (LLM may include preamble text)
        json_start = raw_response.find("[")
        json_end = raw_response.rfind("]") + 1
        if json_start >= 0 and json_end > json_start:
            recommendations = json.loads(raw_response[json_start:json_end])
        else:
            logger.warning("LLM did not return valid JSON, using raw response")
            recommendations = []

    except Exception as e:
        logger.error(f"LLM analysis failed: {e}")
        raw_response = f"LLM analysis failed: {e}"
        recommendations = []

    return {
        "llm_analysis": raw_response,
        "recommendations": recommendations,
    }


def should_analyze(state: AgentState) -> str:
    """Conditional edge: skip analysis if nothing discovered or skip_llm=True."""
    if state["config"].get("skip_llm", False):
        return "skip"
    total = sum(len(v) for v in state["discovered_resources"].values())
    if total == 0:
        return "skip"
    return "analyze"


def build_graph() -> StateGraph:
    """Build and compile the LangGraph agent."""
    builder = StateGraph(AgentState)

    # Add nodes
    builder.add_node("discover", discover_node)
    builder.add_node("enrich_cost", enrich_cost_node)
    builder.add_node("analyze", analyze_node)

    # Add edges
    builder.add_edge(START, "discover")
    builder.add_conditional_edges(
        "discover",
        should_analyze,
        {"analyze": "enrich_cost", "skip": END},
    )
    builder.add_edge("enrich_cost", "analyze")
    builder.add_edge("analyze", END)

    return builder.compile()


def run_agent(config: ScanConfig) -> AgentState:
    """Execute the full cleaner agent workflow."""
    graph = build_graph()

    initial_state: AgentState = {
        "config": {
            "aws_profile": config.aws_profile,
            "aws_region": config.aws_region,
            "s3_unused_days": config.s3_unused_days,
            "ecr_unused_days": config.ecr_unused_days,
            "ec2_stopped_days": config.ec2_stopped_days,
            "ebs_unattached": config.ebs_unattached,
            "services": config.services,
            "model_id": config.model_id,
            "model_max_tokens": config.model_max_tokens,
            "output_format": config.output_format,
            "skip_llm": config.skip_llm,
            "all_regions": config.all_regions,
        },
        "session": None,
        "discovered_resources": {},
        "cost_data": {},
        "llm_analysis": "",
        "recommendations": [],
        "errors": [],
    }

    result = graph.invoke(initial_state)
    return result
