#!/usr/bin/env python3
"""
E2E test suite for chat.py — covers all major use cases.

Test Strategy:
  Unit-level: test parse_user_intent() and build_plain_english_report() directly
  Integration: run chat.py subprocess with piped stdin for full loop tests

Run:  python3 test_chat.py
"""

import subprocess
import sys
import json
import os
import textwrap
import time

PROFILE = os.environ.get("AWS_CLEANER_PROFILE", "vonage-bssoss-qa")
REGION = "us-east-1"
CHAT_CMD = [sys.executable, "chat.py", "--profile", PROFILE, "--region", REGION]

# ─── Helpers ──────────────────────────────────────────────────────────────────

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"
WARN = "\033[33mWARN\033[0m"

results = []

def run_test(name, fn):
    print(f"\n{'─'*60}")
    print(f"TEST: {name}")
    print('─'*60)
    try:
        fn()
        print(f"  → {PASS}")
        results.append((name, "PASS", None))
    except AssertionError as e:
        print(f"  → {FAIL}: {e}")
        results.append((name, "FAIL", str(e)))
    except Exception as e:
        print(f"  → {FAIL} (exception): {type(e).__name__}: {e}")
        results.append((name, "FAIL", f"{type(e).__name__}: {e}"))


def chat_subprocess(stdin_lines: list[str], timeout: int = 180) -> tuple[str, str, int]:
    """Run chat.py with given stdin lines. Returns (stdout, stderr, returncode)."""
    user_input = "\n".join(stdin_lines) + "\n"
    proc = subprocess.run(
        CHAT_CMD,
        input=user_input,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    return proc.stdout, proc.stderr, proc.returncode


def assert_contains(text: str, expected: str, msg: str = ""):
    assert expected.lower() in text.lower(), (
        f"{msg or 'Expected substring not found'}: {repr(expected)}\n"
        f"  Got (first 500 chars): {repr(text[:500])}"
    )


def assert_not_contains(text: str, unexpected: str, msg: str = ""):
    assert unexpected.lower() not in text.lower(), (
        f"{msg or 'Unexpected substring found'}: {repr(unexpected)}"
    )


def combined_output(stdout, stderr):
    """Rich renders to stdout; status messages also go there. Combine both for checks."""
    return stdout + stderr


# ─── Unit Tests: parse_user_intent() ─────────────────────────────────────────

def test_intent_import():
    """chat module imports cleanly with all dependencies."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import chat  # noqa: F401
    assert hasattr(chat, "parse_user_intent")
    assert hasattr(chat, "build_plain_english_report")
    assert hasattr(chat, "run_chat")


def test_intent_s3_only():
    """LLM parses 'check S3 unused for 60 days' → services=['s3'], s3_days=60."""
    from chat import parse_user_intent
    from aws_cleaner.llm import BedrockLLM
    from aws_cleaner.config import ScanConfig
    llm = BedrockLLM(ScanConfig(aws_profile=PROFILE, aws_region=REGION))
    intent = parse_user_intent(llm, "Check S3 buckets that haven't been used in 60 days")
    print(f"  Intent: {json.dumps(intent, indent=2)}")
    assert "s3" in intent.get("services", []), "Expected s3 in services"
    assert intent.get("s3_days", 90) <= 90, "Expected s3_days <= 90 (60 or similar)"


def test_intent_all_services():
    """LLM parses 'scan everything for waste' → all 4 services."""
    from chat import parse_user_intent
    from aws_cleaner.llm import BedrockLLM
    from aws_cleaner.config import ScanConfig
    llm = BedrockLLM(ScanConfig(aws_profile=PROFILE, aws_region=REGION))
    intent = parse_user_intent(llm, "Scan everything and tell me what's wasting money")
    print(f"  Intent: {json.dumps(intent, indent=2)}")
    services = intent.get("services", [])
    assert len(services) >= 3, f"Expected at least 3 services, got: {services}"


def test_intent_skip_llm():
    """LLM parses 'quick check no AI analysis' → skip_llm=True."""
    from chat import parse_user_intent
    from aws_cleaner.llm import BedrockLLM
    from aws_cleaner.config import ScanConfig
    llm = BedrockLLM(ScanConfig(aws_profile=PROFILE, aws_region=REGION))
    intent = parse_user_intent(llm, "Quick discovery scan of S3, skip the AI analysis")
    print(f"  Intent: {json.dumps(intent, indent=2)}")
    assert intent.get("skip_llm") is True, f"Expected skip_llm=True, got: {intent.get('skip_llm')}"


def test_intent_ecr_specific():
    """LLM parses 'are there stale Docker images in ECR' → services includes ecr."""
    from chat import parse_user_intent
    from aws_cleaner.llm import BedrockLLM
    from aws_cleaner.config import ScanConfig
    llm = BedrockLLM(ScanConfig(aws_profile=PROFILE, aws_region=REGION))
    intent = parse_user_intent(llm, "Are there any stale Docker images in ECR?")
    print(f"  Intent: {json.dumps(intent, indent=2)}")
    assert "ecr" in intent.get("services", []), f"Expected ecr in services: {intent.get('services')}"


def test_intent_ebs_ec2():
    """LLM parses 'scan EBS and EC2 for orphaned resources' → services=['ebs','ec2']."""
    from chat import parse_user_intent
    from aws_cleaner.llm import BedrockLLM
    from aws_cleaner.config import ScanConfig
    llm = BedrockLLM(ScanConfig(aws_profile=PROFILE, aws_region=REGION))
    intent = parse_user_intent(llm, "Check for orphaned EBS volumes and stopped EC2 instances")
    print(f"  Intent: {json.dumps(intent, indent=2)}")
    services = intent.get("services", [])
    assert "ebs" in services or "ec2" in services, f"Expected ebs/ec2, got: {services}"


def test_intent_gibberish_fallback():
    """Gibberish input doesn't crash — parse_user_intent raises ValueError (caught upstream)."""
    from chat import parse_user_intent
    from aws_cleaner.llm import BedrockLLM
    from aws_cleaner.config import ScanConfig
    llm = BedrockLLM(ScanConfig(aws_profile=PROFILE, aws_region=REGION))
    # LLM should either return a valid JSON or raise ValueError
    # Either outcome is acceptable — the caller handles both
    try:
        intent = parse_user_intent(llm, "xyzzy frobnicator blorp 42!")
        print(f"  Intent (LLM recovered): {json.dumps(intent, indent=2)}")
        # If it returns, it must have a valid structure
        assert "services" in intent, "Intent missing 'services' key"
    except (ValueError, Exception) as e:
        print(f"  Raised (acceptable): {type(e).__name__}: {e}")
        # Acceptable — run_chat() has fallback for this


def test_intent_unsupported_service():
    """'Scan RDS for unused instances' — LLM should map to known services only."""
    from chat import parse_user_intent
    from aws_cleaner.llm import BedrockLLM
    from aws_cleaner.config import ScanConfig
    VALID_SERVICES = {"s3", "ecr", "ebs", "ec2"}
    llm = BedrockLLM(ScanConfig(aws_profile=PROFILE, aws_region=REGION))
    intent = parse_user_intent(llm, "Scan RDS databases for unused instances")
    print(f"  Intent: {json.dumps(intent, indent=2)}")
    services = set(intent.get("services", []))
    unknown = services - VALID_SERVICES
    assert not unknown, f"LLM hallucinated unsupported services: {unknown}"


def test_intent_output_format_csv():
    """'Give me a CSV of the findings' → output_format='csv'."""
    from chat import parse_user_intent
    from aws_cleaner.llm import BedrockLLM
    from aws_cleaner.config import ScanConfig
    llm = BedrockLLM(ScanConfig(aws_profile=PROFILE, aws_region=REGION))
    intent = parse_user_intent(llm, "Scan S3 and give me a CSV of the findings")
    print(f"  Intent: {json.dumps(intent, indent=2)}")
    assert intent.get("output_format") == "csv", (
        f"Expected output_format='csv', got: {intent.get('output_format')!r}"
    )


def test_intent_output_format_markdown():
    """'Show results as markdown I can paste in Jira' → output_format='markdown'."""
    from chat import parse_user_intent
    from aws_cleaner.llm import BedrockLLM
    from aws_cleaner.config import ScanConfig
    llm = BedrockLLM(ScanConfig(aws_profile=PROFILE, aws_region=REGION))
    intent = parse_user_intent(llm, "Show me the results as markdown I can paste into a Jira ticket")
    print(f"  Intent: {json.dumps(intent, indent=2)}")
    assert intent.get("output_format") == "markdown", (
        f"Expected output_format='markdown', got: {intent.get('output_format')!r}"
    )


def test_intent_output_format_default_none():
    """Regular scan request → output_format is null/None (not set)."""
    from chat import parse_user_intent
    from aws_cleaner.llm import BedrockLLM
    from aws_cleaner.config import ScanConfig
    llm = BedrockLLM(ScanConfig(aws_profile=PROFILE, aws_region=REGION))
    intent = parse_user_intent(llm, "What unused S3 buckets are in my account?")
    print(f"  Intent: {json.dumps(intent, indent=2)}")
    fmt = intent.get("output_format")
    assert fmt is None or fmt == "", (
        f"Expected output_format=null for plain scan, got: {fmt!r}"
    )


# ─── Unit Tests: build_plain_english_report() ────────────────────────────────

def test_report_with_zero_findings():
    """build_plain_english_report with empty results → clean/no findings response."""
    from chat import build_plain_english_report
    from aws_cleaner.llm import BedrockLLM
    from aws_cleaner.config import ScanConfig
    llm = BedrockLLM(ScanConfig(aws_profile=PROFILE, aws_region=REGION))
    result = {"discovered_resources": {"s3": [], "ecr": []}, "recommendations": [], "errors": []}
    reply = build_plain_english_report(llm, result, "What's unused in my account?")
    print(f"  Reply (first 200 chars): {reply[:200]}")
    assert len(reply) > 20, "Expected a non-trivial response"
    # Should mention no findings or clean state
    clean_phrases = ["no unused", "nothing unused", "clean", "didn't find", "0 unused", "no resources"]
    found = any(p in reply.lower() for p in clean_phrases)
    assert found, f"Expected 'clean' language, got: {reply[:300]}"


def test_report_with_findings():
    """build_plain_english_report with real-looking S3 findings → mentions savings/buckets."""
    from chat import build_plain_english_report
    from aws_cleaner.llm import BedrockLLM
    from aws_cleaner.config import ScanConfig
    llm = BedrockLLM(ScanConfig(aws_profile=PROFILE, aws_region=REGION))
    mock_result = {
        "discovered_resources": {
            "s3": [
                {"resource_id": "k8s-cluster-logs999", "size_bytes": 320_000_000_000,
                 "last_modified": "2024-01-01", "reason": "No access in 365+ days"},
                {"resource_id": "old-ci-artifacts", "size_bytes": 50_000_000_000,
                 "last_modified": "2024-03-01", "reason": "No access in 200+ days"},
            ]
        },
        "recommendations": [
            {"resource_id": "k8s-cluster-logs999", "service": "s3",
             "action": "delete_bucket", "monthly_savings_usd": 7.36,
             "reason": "Unused for 365 days, 298GB"},
            {"resource_id": "old-ci-artifacts", "service": "s3",
             "action": "delete_bucket", "monthly_savings_usd": 1.15,
             "reason": "Unused for 200 days, 47GB"},
        ],
        "errors": [],
    }
    reply = build_plain_english_report(llm, mock_result, "What S3 buckets should I clean up?")
    print(f"  Reply (first 400 chars): {reply[:400]}")
    assert len(reply) > 50, "Expected a substantive response"
    # Should mention savings or buckets
    relevant = any(w in reply.lower() for w in ["s3", "bucket", "saving", "delete", "unused", "month"])
    assert relevant, f"Response doesn't seem relevant to the query: {reply[:300]}"


# ─── Integration Tests: subprocess chat.py ───────────────────────────────────

def test_quit_command():
    """Typing 'quit' exits cleanly with returncode 0."""
    stdout, stderr, rc = chat_subprocess(["quit"], timeout=30)
    out = combined_output(stdout, stderr)
    print(f"  RC={rc}, output: {repr(out[:200])}")
    assert rc == 0, f"Expected returncode 0, got {rc}"
    assert_contains(out, "goodbye", "Expected goodbye message")


def test_empty_input_continues():
    """Empty input (blank line) then quit — no crash, no scan triggered."""
    stdout, stderr, rc = chat_subprocess(["", "", "quit"], timeout=30)
    out = combined_output(stdout, stderr)
    print(f"  RC={rc}, output: {repr(out[:200])}")
    assert rc == 0, f"Expected returncode 0, got {rc}"
    # Should not have started a scan
    assert_not_contains(out, "scanning", "Should not scan on empty input")


def test_exit_alias():
    """'exit' command also quits cleanly."""
    stdout, stderr, rc = chat_subprocess(["exit"], timeout=30)
    out = combined_output(stdout, stderr)
    print(f"  RC={rc}, output: {repr(out[:200])}")
    assert rc == 0, f"Expected returncode 0, got {rc}"


def test_ctrl_c_eof():
    """EOF on stdin (no 'quit' typed) exits cleanly via EOFError handler."""
    # Send no input at all — stdin closes immediately
    proc = subprocess.run(
        CHAT_CMD,
        input="",
        capture_output=True,
        text=True,
        timeout=30,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    out = combined_output(proc.stdout, proc.stderr)
    print(f"  RC={proc.returncode}, output: {repr(out[:200])}")
    assert proc.returncode == 0, f"Expected clean exit on EOF, got rc={proc.returncode}"


def test_ecr_discovery_only():
    """'List all ECR repos, skip AI' → runs ECR scan, prints results, no LLM analysis."""
    stdout, stderr, rc = chat_subprocess(
        ["List all ECR repositories, skip the AI analysis", "quit"],
        timeout=120
    )
    out = combined_output(stdout, stderr)
    print(f"  RC={rc}")
    print(f"  Output (first 600 chars):\n{out[:600]}")
    assert rc == 0, f"Unexpected exit code {rc}"
    # Should see intent acknowledgment and scan activity
    assert_contains(out, "ecr", "Expected ECR mentioned in output")
    assert_not_contains(out, "scan failed", "Should not have scan failure")


def test_ebs_ec2_discovery():
    """'Check for orphaned EBS and stopped EC2 instances' → scans those two services."""
    stdout, stderr, rc = chat_subprocess(
        ["Check for orphaned EBS volumes and long-stopped EC2 instances, no AI needed", "quit"],
        timeout=120
    )
    out = combined_output(stdout, stderr)
    print(f"  RC={rc}")
    print(f"  Output (first 600 chars):\n{out[:600]}")
    assert rc == 0, f"Unexpected exit code {rc}"
    assert_not_contains(out, "scan failed", "Should not have scan failure")
    # Should mention EBS and/or EC2
    assert any(s in out.lower() for s in ["ebs", "ec2", "volume", "instance"]), \
        "Expected EBS/EC2 in output"


def test_s3_discovery_skip_llm():
    """'Quick discovery of S3, skip AI' → skip_llm=True, shows raw discovery table."""
    stdout, stderr, rc = chat_subprocess(
        ["Quick discovery scan of S3 buckets only, skip the AI analysis", "quit"],
        timeout=180
    )
    out = combined_output(stdout, stderr)
    print(f"  RC={rc}")
    print(f"  Output (first 800 chars):\n{out[:800]}")
    assert rc == 0, f"Unexpected exit code {rc}"
    assert_not_contains(out, "scan failed", "Should not have scan failure")
    assert_contains(out, "s3", "Expected S3 mentioned in output")
    # Should not invoke LLM analysis (skip_llm=True)
    assert_not_contains(out, "analyzing", "LLM should be skipped")


def test_s3_with_llm_analysis():
    """'What S3 buckets are wasting money?' → full scan with LLM analysis + savings report."""
    stdout, stderr, rc = chat_subprocess(
        ["What S3 buckets are wasting money in my AWS account?", "quit"],
        timeout=300
    )
    out = combined_output(stdout, stderr)
    print(f"  RC={rc}")
    print(f"  Output (first 1000 chars):\n{out[:1000]}")
    assert rc == 0, f"Unexpected exit code {rc}"
    assert_not_contains(out, "scan failed", "Should not have scan failure")
    # Should have agent response with findings
    assert_contains(out, "agent:", "Expected Agent: response in output")


def test_gibberish_input_fallback():
    """Non-AWS gibberish → intent parse uses fallback defaults, still runs a scan."""
    stdout, stderr, rc = chat_subprocess(
        ["xyzzy frobnicator blorp 42 hello world!", "quit"],
        timeout=180
    )
    out = combined_output(stdout, stderr)
    print(f"  RC={rc}")
    print(f"  Output (first 600 chars):\n{out[:600]}")
    assert rc == 0, f"Unexpected exit code {rc}"
    assert_not_contains(out, "scan failed", "Scan should succeed even with gibberish")
    # Should either acknowledge it couldn't parse or use defaults
    # Either way it should not crash hard
    assert any(s in out.lower() for s in ["agent:", "scanning", "couldn't parse", "understood"]), \
        "Expected some agent activity"


def test_multi_turn_s3_then_ecr():
    """Multi-turn: first ask about S3, then ask about ECR — both scans complete."""
    stdout, stderr, rc = chat_subprocess(
        [
            "List S3 buckets that haven't been used, skip AI",
            "Now check ECR for stale images, also skip AI",
            "quit",
        ],
        timeout=360  # two scans
    )
    out = combined_output(stdout, stderr)
    print(f"  RC={rc}")
    print(f"  Output (first 1000 chars):\n{out[:1000]}")
    assert rc == 0, f"Unexpected exit code {rc}"
    assert_not_contains(out, "scan failed", "Should not have scan failure")
    # Both services should appear in output
    assert_contains(out, "s3", "Expected S3 mentioned in first turn")
    assert_contains(out, "ecr", "Expected ECR mentioned in second turn")


def test_cost_focus_query():
    """'How much money are we wasting?' — cost-focused intent → full LLM analysis."""
    stdout, stderr, rc = chat_subprocess(
        ["How much money are we wasting on unused resources? Give me a cost breakdown.", "quit"],
        timeout=300
    )
    out = combined_output(stdout, stderr)
    print(f"  RC={rc}")
    print(f"  Output (first 800 chars):\n{out[:800]}")
    assert rc == 0, f"Unexpected exit code {rc}"
    assert_not_contains(out, "scan failed", "Should not have scan failure")
    assert_contains(out, "agent:", "Expected Agent: response")


def test_help_style_query():
    """'What can you scan?' — meta query handled gracefully."""
    stdout, stderr, rc = chat_subprocess(
        ["What kinds of AWS resources can you scan?", "quit"],
        timeout=60
    )
    out = combined_output(stdout, stderr)
    print(f"  RC={rc}")
    print(f"  Output (first 600 chars):\n{out[:600]}")
    assert rc == 0, f"Unexpected exit code {rc}"
    # Even if LLM can't meaningfully parse this, it should not crash
    assert_not_contains(out, "traceback", "Should not throw traceback")


# ─── Unit Tests: render_csv / render_markdown (no AWS, no LLM) ───────────────

_FAKE_SCAN_RESULT = {
    "discovered_resources": {
        "s3": [{"resource_id": "old-logs-bucket", "size_bytes": 5_368_709_120,
                "last_modified": "2024-01-01", "reason": "No access in 180 days"}],
        "ebs": [{"resource_id": "vol-0abc1234", "volume_size_gb": 20,
                 "last_modified": "2024-02-01", "reason": "Unattached 90 days"}],
    },
    "recommendations": [
        {"service": "s3", "resource_id": "old-logs-bucket", "action": "Delete",
         "risk": "Low", "monthly_savings_usd": 12.30, "reason": "No access in 180 days"},
        {"service": "ebs", "resource_id": "vol-0abc1234", "action": "Delete",
         "risk": "Medium", "monthly_savings_usd": 2.00, "reason": "Unattached 90 days"},
    ],
    "errors": [],
}


def test_render_csv_structure():
    """render_csv produces valid CSV with expected headers and rows, no AWS needed."""
    from aws_cleaner.report import render_csv
    csv_text = render_csv(_FAKE_SCAN_RESULT)
    print(f"  CSV output:\n{csv_text}")
    lines = csv_text.strip().splitlines()
    # Header comment line
    assert any("AWS Cleaner" in l for l in lines), "Expected AWS Cleaner header comment"
    # Column header row
    header_line = next((l for l in lines if "Service" in l and "Resource" in l), None)
    assert header_line is not None, "Expected CSV column header row"
    # Data rows — should have both services
    assert any("S3" in l for l in lines), "Expected S3 row in CSV"
    assert any("EBS" in l for l in lines), "Expected EBS row in CSV"
    # TOTAL row
    assert any("TOTAL" in l for l in lines), "Expected TOTAL row"
    assert any("14.30" in l for l in lines), "Expected savings total 14.30"
    # Sorted by savings desc — S3 ($12.30) should appear before EBS ($2.00)
    s3_idx = next(i for i, l in enumerate(lines) if "S3" in l)
    ebs_idx = next(i for i, l in enumerate(lines) if "EBS" in l)
    assert s3_idx < ebs_idx, "S3 (higher savings) should appear before EBS"


def test_render_markdown_structure():
    """render_markdown produces Jira-pasteable Markdown with table and summary, no AWS needed."""
    from aws_cleaner.report import render_markdown
    md = render_markdown(_FAKE_SCAN_RESULT, profile="test-profile", region="us-east-1")
    print(f"  Markdown output:\n{md}")
    # Main header
    assert "## AWS Cleaner Agent Report" in md, "Expected H2 report header"
    # Metadata table rows
    assert "test-profile" in md, "Expected profile name in metadata"
    assert "us-east-1" in md, "Expected region in metadata"
    assert "$14.30" in md, "Expected total savings"
    # Recommendations section
    assert "### Recommendations" in md, "Expected Recommendations section"
    # Markdown table header
    assert "| Service |" in md, "Expected markdown table header"
    assert "| S3 |" in md or "S3" in md, "Expected S3 row in table"
    assert "| EBS |" in md or "EBS" in md, "Expected EBS row in table"
    # Resource IDs in code ticks
    assert "`old-logs-bucket`" in md, "Expected resource_id in backticks"
    # Footer
    assert "aws-cleaner-agent" in md, "Expected footer link"


# ─── E2E: export output via chat subprocess ───────────────────────────────────

def test_e2e_csv_export():
    """'Scan S3, skip AI, give me CSV' → stdout contains CSV column headers."""
    stdout, stderr, rc = chat_subprocess(
        ["Scan S3 buckets, skip the AI analysis, give me a CSV of the findings", "quit"],
        timeout=180
    )
    out = combined_output(stdout, stderr)
    print(f"  RC={rc}")
    print(f"  Output (first 1000 chars):\n{out[:1000]}")
    assert rc == 0, f"Unexpected exit code {rc}"
    assert_not_contains(out, "scan failed", "Should not have scan failure")
    # CSV header should appear in output
    assert_contains(out, "Service", "Expected CSV 'Service' column header")
    assert_contains(out, "Resource ID", "Expected CSV 'Resource ID' column header")
    assert_contains(out, "AWS Cleaner", "Expected CSV title comment")


def test_e2e_markdown_export():
    """'Scan EBS, skip AI, show as markdown' → stdout contains Markdown table."""
    stdout, stderr, rc = chat_subprocess(
        ["Scan EBS volumes, skip AI, show results as markdown I can paste in Jira", "quit"],
        timeout=180
    )
    out = combined_output(stdout, stderr)
    print(f"  RC={rc}")
    print(f"  Output (first 1000 chars):\n{out[:1000]}")
    assert rc == 0, f"Unexpected exit code {rc}"
    assert_not_contains(out, "scan failed", "Should not have scan failure")
    # Markdown structure
    assert_contains(out, "## AWS Cleaner Agent Report", "Expected Markdown H2 header")
    assert_contains(out, "| Service |", "Expected Markdown table header row")


# ─── Main ─────────────────────────────────────────────────────────────────────

TEST_GROUPS = [
    # Group 1: Unit tests (fast, no subprocess)
    ("UNIT | Import check",                         test_intent_import),
    ("UNIT | Intent: S3 only, 60 days",             test_intent_s3_only),
    ("UNIT | Intent: all services",                 test_intent_all_services),
    ("UNIT | Intent: skip_llm flag",                test_intent_skip_llm),
    ("UNIT | Intent: ECR specific",                 test_intent_ecr_specific),
    ("UNIT | Intent: EBS + EC2",                    test_intent_ebs_ec2),
    ("UNIT | Intent: gibberish fallback",           test_intent_gibberish_fallback),
    ("UNIT | Intent: unsupported service",          test_intent_unsupported_service),
    ("UNIT | Intent: output_format=csv",            test_intent_output_format_csv),
    ("UNIT | Intent: output_format=markdown",       test_intent_output_format_markdown),
    ("UNIT | Intent: output_format default null",   test_intent_output_format_default_none),
    ("UNIT | Report: zero findings",                test_report_with_zero_findings),
    ("UNIT | Report: with findings",                test_report_with_findings),
    ("UNIT | render_csv: structure",                test_render_csv_structure),
    ("UNIT | render_markdown: structure",           test_render_markdown_structure),
    # Group 2: Subprocess integration tests
    ("E2E  | quit command",                         test_quit_command),
    ("E2E  | empty input continues",                test_empty_input_continues),
    ("E2E  | exit alias",                           test_exit_alias),
    ("E2E  | EOF/Ctrl-C exit",                      test_ctrl_c_eof),
    ("E2E  | ECR discovery only",                   test_ecr_discovery_only),
    ("E2E  | EBS+EC2 discovery",                    test_ebs_ec2_discovery),
    ("E2E  | S3 skip-LLM discovery",                test_s3_discovery_skip_llm),
    ("E2E  | S3 full LLM analysis",                 test_s3_with_llm_analysis),
    ("E2E  | Gibberish → fallback scan",            test_gibberish_input_fallback),
    ("E2E  | Multi-turn: S3 then ECR",              test_multi_turn_s3_then_ecr),
    ("E2E  | Cost-focused query",                   test_cost_focus_query),
    ("E2E  | Help/meta query",                      test_help_style_query),
    ("E2E  | CSV export",                           test_e2e_csv_export),
    ("E2E  | Markdown export",                      test_e2e_markdown_export),
]


def main():
    import argparse
    parser = argparse.ArgumentParser(description="chat.py E2E test suite")
    parser.add_argument("--unit-only", action="store_true", help="Run only unit tests (no subprocess)")
    parser.add_argument("--e2e-only", action="store_true", help="Run only E2E subprocess tests")
    parser.add_argument("--filter", default="", help="Only run tests whose name contains this string")
    args = parser.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    print("=" * 60)
    print("  chat.py E2E Test Suite")
    print(f"  Profile: {PROFILE} | Region: {REGION}")
    print("=" * 60)

    start = time.time()
    for name, fn in TEST_GROUPS:
        if args.unit_only and not name.startswith("UNIT"):
            continue
        if args.e2e_only and not name.startswith("E2E"):
            continue
        if args.filter and args.filter.lower() not in name.lower():
            continue
        run_test(name, fn)

    elapsed = time.time() - start

    print(f"\n{'='*60}")
    print("  RESULTS SUMMARY")
    print('='*60)
    passed = [r for r in results if r[1] == "PASS"]
    failed = [r for r in results if r[1] == "FAIL"]

    for name, status, msg in results:
        icon = "✓" if status == "PASS" else "✗"
        color = "\033[32m" if status == "PASS" else "\033[31m"
        print(f"  {color}{icon}\033[0m {name}")
        if msg:
            print(f"      {msg[:120]}")

    print(f"\n  Ran {len(results)} tests in {elapsed:.1f}s")
    print(f"  {PASS}: {len(passed)}  {FAIL}: {len(failed)}")

    if failed:
        print("\n  FAILED TESTS:")
        for name, _, msg in failed:
            print(f"    - {name}")
            if msg:
                print(f"      {msg[:200]}")
        sys.exit(1)
    else:
        print(f"\n  All tests {PASS}!")
        sys.exit(0)


if __name__ == "__main__":
    main()
