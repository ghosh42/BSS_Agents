# AWS Cleaner Agent

An AI-powered CLI tool that scans your AWS account(s) for unused resources, estimates monthly savings, and can optionally delete them — with safety confirmations at every step.

Powered by **LangGraph** orchestration + **Meta Llama 3.3 70B** via Amazon Bedrock.

---

## Tech Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| **Language** | Python | 3.12 |
| **AI Orchestration** | LangGraph (StateGraph) | 1.1.10 |
| **LLM** | Meta Llama 3.3 70B Instruct | via Amazon Bedrock |
| **AWS SDK** | boto3 | 1.42.59 |
| **LangChain AWS** | langchain-aws | 1.4.6 |
| **LangChain Core** | langchain-core | 1.3.3 |
| **CLI / Formatting** | Rich | 14.3.3 |
| **AWS Services used** | S3, ECR, EBS, EC2, CloudWatch, Cost Explorer, Bedrock | — |

---

## What it scans

| Service | What it looks for | Default threshold |
|---------|-------------------|-------------------|
| **S3** | Buckets with no object activity | 90 days |
| **ECR** | Repos with no image push | 180 days |
| **EBS** | Unattached volumes | Any |
| **EC2** | Long-stopped instances | 30 days |

---

## Prerequisites

| Requirement | Version |
|-------------|---------|
| Python | 3.10+ |
| AWS CLI | v2 |
| AWS profile | Credentials with the [required IAM permissions](#iam-permissions) |
| Bedrock access | `us.meta.llama3-3-70b-instruct-v1:0` enabled in your account |

---

## Setup

**1. Clone and install dependencies**
```bash
git clone <repo-url>
cd aws-cleaner-agent
pip install -r requirements.txt
```

**2. Ensure your AWS credentials are valid**
```bash
aws sts get-caller-identity --profile vonage-bssoss-qa
```
If you see `ExpiredToken`, refresh your credentials using your team's method (e.g. `gimme-aws-creds`, `saml2aws`, Okta).

**3. Enable Bedrock model access**

In the AWS Console → Amazon Bedrock → Model access → enable **Meta Llama 3.3 70B Instruct**.

---

## Usage

### Chat mode (recommended)

Talk to the agent in plain English:

```bash
python3 chat.py --profile vonage-bssoss-qa
```

**Example prompts:**
```
You: scan S3 for buckets unused for 60 days
You: what's wasting money across ECR and EBS?
You: check every region for unused EBS volumes
You: scan qa, staging and prod profiles
You: delete unused EBS volumes
You: quick scan of everything, no AI
```

Type `quit` or `exit` to stop.

---

### CLI mode (one-shot, scriptable)

**Basic scan:**
```bash
python3 run.py --profile vonage-bssoss-qa
```

**Specific services:**
```bash
python3 run.py --profile vonage-bssoss-qa --services s3,ebs
```

**All regions** (scans every enabled region for ECR/EBS/EC2; S3 is global):
```bash
python3 run.py --profile vonage-bssoss-qa --all-regions
```

**Multi-account sweep:**
```bash
python3 run.py --profiles vonage-bssoss-qa,vonage-bssoss-prod --services s3,ebs
```

**JSON output** (pipe to file or jq):
```bash
python3 run.py --profile vonage-bssoss-qa --output json > report.json
```

**Skip LLM** (discovery only, faster):
```bash
python3 run.py --profile vonage-bssoss-qa --skip-llm
```

---

### Deletion

> **Deletion is always dry-run by default.** You must explicitly confirm to delete anything.

**Step 1 — Preview what would be deleted (safe, no changes):**
```bash
python3 run.py --profile vonage-bssoss-qa --delete
```

**Step 2 — Execute real deletion (irreversible):**
```bash
python3 run.py --profile vonage-bssoss-qa --delete --confirm
```

**Skip per-resource prompts** (batch confirm):
```bash
python3 run.py --profile vonage-bssoss-qa --delete --confirm --force
```

Deletion behaviour per service:
- **S3**: empties all objects (including versioned) then deletes the bucket
- **ECR**: deletes all images in the repo (repo itself is kept)
- **EBS**: deletes the unattached volume
- **EC2**: terminates the stopped instance

---

### All CLI flags

```
--profile          AWS profile name (default: $AWS_CLEANER_PROFILE or 'default')
--region           AWS region (default: us-east-1)
--all-regions      Scan all enabled regions
--services         Comma-separated: s3,ecr,ebs,ec2 (default: all)
--s3-days          Days inactive before flagging S3 bucket (default: 90)
--ecr-days         Days without push before flagging ECR repo (default: 180)
--ec2-days         Days stopped before flagging EC2 instance (default: 30)
--profiles         Comma-separated profiles for multi-account sweep
--sweep-workers    Parallel threads for multi-account sweep (default: 4)
--delete           Enable deletion mode (dry-run unless --confirm added)
--confirm          Execute real deletion (requires --delete)
--force            Skip per-resource confirmation prompts
--skip-llm         Discovery only, skip AI analysis
--output           table (default) or json
-v / --verbose     Debug logging
```

---

## Environment variables

You can set defaults via environment variables instead of flags:

```bash
export AWS_CLEANER_PROFILE=vonage-bssoss-qa
export AWS_CLEANER_REGION=us-east-1
```

---

## IAM permissions

See [docs/iam-policy.json](docs/iam-policy.json) for the least-privilege policy required.

**Read-only scan** needs: S3 list/get, ECR describe/list, EC2 describe, CloudWatch GetMetricData, Cost Explorer GetCostAndUsage, Bedrock InvokeModel.

**Deletion** additionally needs: S3 DeleteObject/DeleteBucket, ECR BatchDeleteImage, EC2 DeleteVolume/TerminateInstances.

> Recommendation: give most users the **read-only policy only**. Reserve the deletion policy for a dedicated `aws-cleaner-delete` IAM role that requires MFA or explicit assumption.

---

## Running tests

All tests except E2E run without live AWS credentials:

```bash
python3 test_deletion.py       # 19 tests — deletion engine (no AWS needed)
python3 test_multi_account.py  # 18 tests — multi-account sweep (no AWS needed)
python3 test_chat.py --unit-only   # requires live AWS credentials
python3 test_chat.py --e2e-only    # requires live AWS credentials (~3 min)
```

---

## Architecture

```
chat.py / run.py
      │
      ▼
aws_cleaner/agent.py        ← LangGraph StateGraph
      │
      ├── discover_node      ← runs scanners in parallel
      │     ├── s3_scanner
      │     ├── ecr_scanner
      │     ├── ebs_scanner
      │     └── ec2_scanner
      │
      ├── enrich_cost_node   ← AWS Cost Explorer
      │
      └── analyze_node       ← Bedrock LLM (Llama 3.3 70B)

aws_cleaner/regions.py      ← multi-region sweep
aws_cleaner/multi_account.py← multi-profile sweep
aws_cleaner/tools/deleter.py← safe deletion engine
```
