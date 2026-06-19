# GitHub Licensing & Usage Report

`gh_billing_report.py` pulls GitHub **licensing and metered-usage** information
for an enterprise on demand and prints it as a human-readable table or JSON.

## What it reports

| Section | Description | Endpoint |
|---------|-------------|----------|
| `ghec` | GHEC seats — purchased vs. consumed | `GET /enterprises/{ent}/consumed-licenses` |
| `ghas` | GHAS seats — purchased vs. active committers | `GET /enterprises/{ent}/settings/billing/advanced-security` |
| `copilot` | Copilot seats (enterprise; optional org) | `GET /enterprises/{ent}/copilot/billing/seats`, `GET /orgs/{org}/copilot/billing` |
| `usage-summary` | Metered usage summary (defaults to current month) | `GET /enterprises/{ent}/settings/billing/usage/summary` |
| `ai-usage` | AI credit usage — cloud agents, models, etc. (defaults to current month) | `GET /enterprises/{ent}/settings/billing/ai_credit/usage` |

> **Note on "purchased" Copilot seats:** the API does not expose a single
> "purchased" number for Copilot. The script reports the seat breakdown
> (total assigned, active/inactive this cycle, pending) instead.

> **Note on AI credit usage:** results are aggregated **per model** (one row
> each) to match the enterprise billing UI. Columns map to the API as follows:
>
> | Report column | UI column | API field |
> |---------------|-----------|-----------|
> | Included | Included credits | `discountQuantity` |
> | Additional | Additional credits | `netQuantity` |
> | Gross $ | Gross amount | `grossAmount` |
> | Add. usage $ | Additional usage | `netAmount` |
>
> "Additional usage" is what is actually billed beyond the plan's included
> credits.

## Requirements

- Python 3.8+
- `requests` (see `requirements.txt`)

## Setup

```bash
# from this directory
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Authentication

Set a Personal Access Token in the `GITHUB_TOKEN` environment variable, and the
target enterprise slug in `GITHUB_ENTERPRISE`:

```bash
export GITHUB_TOKEN="<your-token>"
export GITHUB_ENTERPRISE="<your-enterprise-slug>"
```

The token must belong to an **enterprise administrator or billing manager**.
Required scopes:

- `read:enterprise` — licensing/consumed-licenses and Copilot billing
- Billing access (e.g. `manage_billing:enterprise` / billing-manager role) for
  the usage and AI-credit endpoints

The billing **usage** and **AI credit** endpoints require the enterprise to be
on the **enhanced billing platform** and use API version `2026-03-10`. If an
endpoint is unavailable, that section reports an error but the rest of the run
continues.

## Usage

```bash
# Everything (table), enterprise from $GITHUB_ENTERPRISE
.venv/bin/python gh_billing_report.py

# Specific enterprise, JSON output
.venv/bin/python gh_billing_report.py --enterprise my-enterprise --format json

# Only some sections (repeatable --section)
.venv/bin/python gh_billing_report.py --section ghec --section copilot

# Include a Copilot Business org's seat data
.venv/bin/python gh_billing_report.py --org my-org

# Scope usage/AI-credit reports to a period
.venv/bin/python gh_billing_report.py --year 2026 --month 5
```

### Options

| Flag | Description |
|------|-------------|
| `--enterprise, -e` | Enterprise slug (default: `$GITHUB_ENTERPRISE`; required) |
| `--org, -o` | Org login for Copilot Business seat data |
| `--section, -s` | `ghec`, `ghas`, `copilot`, `usage-summary`, `ai-usage`, or `all` (repeatable) |
| `--format, -f` | `table` (default) or `json` |
| `--year` / `--month` / `--day` | Period filter for usage/AI-credit reports (default: current month) |

## Exit codes

- `0` — completed (at least one section succeeded)
- `1` — every requested section failed
- `2` — configuration error (e.g. `GITHUB_TOKEN` not set)
