#!/usr/bin/env python3
"""On-demand GitHub licensing & usage reporting.

Pulls licensing and metered-usage information for a GitHub enterprise:
  * GHEC seats (purchased vs. consumed)
  * GHAS seats (purchased vs. active committers)
  * Copilot seats (enterprise, and optionally a business/org)
  * Metered usage summary (from the enterprise billing usage summary)
  * AI credit usage (cloud agents, models, etc. for the current month)

Authentication uses a Personal Access Token supplied via the GITHUB_TOKEN
environment variable. The token must belong to an enterprise admin / billing
manager and carry the ``read:enterprise`` scope (plus billing access for the
usage endpoints).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable

import requests

API_ROOT = "https://api.github.com"
API_VERSION = "2026-03-10"
DEFAULT_ENTERPRISE = "octodemo"

ALL_SECTIONS = ["ghec", "ghas", "copilot", "usage-summary", "ai-usage"]


class GitHubAPIError(Exception):
    """Raised for a non-recoverable API failure for a single section."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class GitHubBillingClient:
    """Thin wrapper around the GitHub REST billing endpoints."""

    # Transient gateway/server errors that are worth retrying.
    RETRY_STATUSES = frozenset({502, 503, 504})

    def __init__(self, token: str, api_root: str = API_ROOT,
                 timeout: int = 30, max_retries: int = 3,
                 retry_backoff: float = 1.0):
        if not token:
            raise ValueError("A GitHub token is required.")
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._api_root = api_root.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "gh-billing-report",
        })

    def _request(self, url: str, path: str,
                 params: dict[str, Any] | None) -> requests.Response:
        """GET ``url`` with retries on transient 5xx/connection errors."""
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._session.get(url, params=params,
                                         timeout=self._timeout)
            except requests.RequestException as exc:
                if attempt < self._max_retries:
                    time.sleep(self._retry_backoff * (2 ** attempt))
                    continue
                raise GitHubAPIError(
                    f"Request to {path} failed: {exc}") from exc
            if (resp.status_code in self.RETRY_STATUSES
                    and attempt < self._max_retries):
                time.sleep(self._retry_backoff * (2 ** attempt))
                continue
            return resp
        # Loop always returns or raises; this satisfies type checkers.
        raise GitHubAPIError(f"Request to {path} failed after retries")

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a single resource, raising GitHubAPIError on failure."""
        url = f"{self._api_root}{path}"
        resp = self._request(url, path, params)
        if resp.status_code == 200:
            return resp.json()

        raise GitHubAPIError(_describe_http_error(resp, path),
                             status=resp.status_code)

    def get_paginated(self, path: str,
                      params: dict[str, Any] | None = None) -> list[Any]:
        """Follow ``Link`` rel="next" headers and concatenate JSON arrays."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        url: str | None = f"{self._api_root}{path}"
        results: list[Any] = []
        while url:
            resp = self._request(url, path, params)
            if resp.status_code != 200:
                raise GitHubAPIError(_describe_http_error(resp, path),
                                     status=resp.status_code)
            payload = resp.json()
            if isinstance(payload, list):
                results.extend(payload)
            else:
                results.append(payload)
            url = resp.links.get("next", {}).get("url")
            params = None  # subsequent URLs already encode the query string
        return results


def _describe_http_error(resp: requests.Response, path: str) -> str:
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("message"):
            detail = f" - {body['message']}"
    except ValueError:
        pass
    hints = {
        401: "check that GITHUB_TOKEN is valid",
        403: "check token scopes/permissions or enhanced billing access",
        404: "endpoint not available for this enterprise/plan, or wrong slug",
    }
    hint = hints.get(resp.status_code)
    hint_text = f" ({hint})" if hint else ""
    return f"GET {path} returned {resp.status_code}{detail}{hint_text}"


# --------------------------------------------------------------------------- #
# Fetch + normalize functions. Each returns a dict; on a handled failure it
# returns {"error": "..."} so one failing section never aborts the whole run.
# --------------------------------------------------------------------------- #

def _safe(fetch: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return fetch()
    except GitHubAPIError as exc:
        return {"error": str(exc), "status": exc.status}


def get_ghec_seats(client: GitHubBillingClient, enterprise: str) -> dict:
    def _fetch() -> dict:
        data = client.get(f"/enterprises/{enterprise}/consumed-licenses")
        purchased = data.get("total_seats_purchased")
        consumed = data.get("total_seats_consumed")
        return {
            "purchased": purchased,
            "consumed": consumed,
            "available": _diff(purchased, consumed),
        }
    return _safe(_fetch)


def get_ghas_seats(client: GitHubBillingClient, enterprise: str) -> dict:
    def _fetch() -> dict:
        data = client.get(
            f"/enterprises/{enterprise}/settings/billing/advanced-security")
        purchased = data.get("purchased_advanced_security_committers")
        if purchased is None:
            purchased = data.get("maximum_advanced_security_committers")
        consumed = data.get("total_advanced_security_committers")
        return {
            "purchased": purchased,
            "consumed": consumed,
            "available": _diff(purchased, consumed),
            "repositories_with_committers": data.get("total_count"),
        }
    return _safe(_fetch)


def get_copilot_seats(client: GitHubBillingClient, enterprise: str,
                      org: str | None) -> dict:
    def _breakdown(data: dict) -> dict:
        sb = data.get("seat_breakdown", {}) or {}
        return {
            "total_assigned": sb.get("total"),
            "active_this_cycle": sb.get("active_this_cycle"),
            "inactive_this_cycle": sb.get("inactive_this_cycle"),
            "added_this_cycle": sb.get("added_this_cycle"),
            "pending_invitation": sb.get("pending_invitation"),
            "pending_cancellation": sb.get("pending_cancellation"),
            "seat_management_setting": data.get("seat_management_setting"),
        }

    def _fetch() -> dict:
        # NOTE: There is no enterprise-level Copilot "seat_breakdown" summary
        # endpoint (GET /enterprises/{ent}/copilot/billing returns 404). The
        # enterprise API only exposes the seat *listing* endpoint, so the
        # breakdown is derived from those seats. The org endpoint
        # (/orgs/{org}/copilot/billing) does provide a native seat_breakdown.
        result: dict[str, Any] = {
            "enterprise": _safe(
                lambda: _breakdown_from_seats(
                    *_collect_enterprise_seats(client, enterprise)))
        }
        if org:
            result["org"] = {
                "login": org,
                **_safe(lambda: _breakdown(
                    client.get(f"/orgs/{org}/copilot/billing"))),
            }
        return result
    return _fetch()


def _collect_enterprise_seats(
        client: GitHubBillingClient,
        enterprise: str) -> tuple[int | None, list[dict]]:
    """Return (total_seats, seats[]) from the enterprise seats endpoint."""
    pages = client.get_paginated(
        f"/enterprises/{enterprise}/copilot/billing/seats")
    total: int | None = None
    seats: list[dict] = []
    for page in pages:
        if isinstance(page, dict):
            if total is None and isinstance(page.get("total_seats"), int):
                total = page["total_seats"]
            seats.extend(page.get("seats", []) or [])
    return total, seats


def _breakdown_from_seats(total: int | None, seats: list[dict]) -> dict:
    """Approximate an org-style seat_breakdown from a list of seat objects.

    The enterprise seats endpoint returns one seat object per org/team that
    grants a user access, so the same user can appear multiple times. To match
    the enterprise licensing page (which counts each user once), seats are
    de-duplicated by user before counting. Seats are also split by ``plan_type``
    (business vs. enterprise) to mirror the "Consumed licenses" breakdown.

    'active this cycle' is approximated as users with activity in the last 30
    days, and 'pending invitation' is unavailable at the enterprise level.
    """
    now = datetime.now(timezone.utc)
    users: dict[Any, dict[str, Any]] = {}
    for seat in seats:
        assignee = seat.get("assignee") or {}
        key = assignee.get("id") or assignee.get("login") or id(seat)
        rec = users.setdefault(
            key, {"plan_type": None, "last_activity": None,
                  "pending_cancellation": False})
        plan = seat.get("plan_type")
        if plan:
            rec["plan_type"] = str(plan).lower()
        last_active = _parse_dt(seat.get("last_activity_at"))
        if last_active and (rec["last_activity"] is None
                            or last_active > rec["last_activity"]):
            rec["last_activity"] = last_active
        if seat.get("pending_cancellation_date"):
            rec["pending_cancellation"] = True

    business = sum(1 for r in users.values() if r["plan_type"] == "business")
    enterprise = sum(
        1 for r in users.values() if r["plan_type"] == "enterprise")
    active = sum(
        1 for r in users.values()
        if r["last_activity"] and (now - r["last_activity"]).days <= 30)
    pending_cancellation = sum(
        1 for r in users.values() if r["pending_cancellation"])

    total_assigned = total if total is not None else len(users)
    inactive = (total_assigned - active
                if isinstance(total_assigned, int) else None)
    return {
        "total_assigned": total_assigned,
        "business_seats": business,
        "enterprise_seats": enterprise,
        "active_this_cycle": active,
        "inactive_this_cycle": inactive,
        "added_this_cycle": None,
        "pending_invitation": None,
        "pending_cancellation": pending_cancellation,
        "seat_management_setting": None,
    }


def get_metered_usage_summary(client: GitHubBillingClient, enterprise: str,
                              period: dict[str, int]) -> dict:
    def _fetch() -> dict:
        # Default the reporting window to the current month when the caller
        # has not narrowed it. The summary endpoint also defaults to the
        # current year/month server-side, but we set it explicitly so the
        # rendered period label is accurate.
        now = datetime.now(timezone.utc)
        params: dict[str, int] = dict(period or {})
        params.setdefault("year", now.year)
        params.setdefault("month", now.month)
        data = client.get(
            f"/enterprises/{enterprise}/settings/billing/usage/summary",
            params=params)
        items = data.get("usageItems", []) or []
        total_net = sum(_num(it.get("netAmount")) for it in items)
        total_gross = sum(_num(it.get("grossAmount")) for it in items)
        total_discount = sum(
            _num(it.get("discountAmount")) for it in items)
        return {
            "time_period": data.get("timePeriod"),
            "total_net_amount": round(total_net, 2),
            "total_gross_amount": round(total_gross, 2),
            "total_discount_amount": round(total_discount, 2),
            "line_items": [
                {
                    "product": it.get("product"),
                    "sku": it.get("sku"),
                    "quantity": it.get("netQuantity"),
                    "unitType": it.get("unitType"),
                    "netAmount": it.get("netAmount"),
                }
                for it in items
            ],
        }
    return _safe(_fetch)


def get_ai_credit_usage(client: GitHubBillingClient, enterprise: str,
                        period: dict[str, int]) -> dict:
    def _fetch() -> dict:
        # Default the reporting window to the current month. The endpoint also
        # defaults to the current year/month server-side, but we set it
        # explicitly so the rendered period label is accurate.
        now = datetime.now(timezone.utc)
        params: dict[str, int] = dict(period or {})
        params.setdefault("year", now.year)
        params.setdefault("month", now.month)
        data = client.get(
            f"/enterprises/{enterprise}/settings/billing/ai_credit/usage",
            params=params)
        items = data.get("usageItems", []) or []
        # The endpoint can return several SKU rows per model; the billing UI
        # shows one row per model, so aggregate accordingly. Field mapping to
        # the UI columns:
        #   Included credits  -> discountQuantity (credits covered by the plan)
        #   Additional credits-> netQuantity      (credits billed on top)
        #   Gross amount      -> grossAmount       (full value of usage)
        #   Additional usage  -> netAmount         (amount actually billed)
        models: dict[str, dict[str, Any]] = {}
        for it in items:
            model = it.get("model") or "(unknown)"
            row = models.setdefault(model, {
                "model": model,
                "product": it.get("product"),
                "unitType": it.get("unitType"),
                "included_credits": 0.0,
                "additional_credits": 0.0,
                "gross_amount": 0.0,
                "additional_usage": 0.0,
            })
            row["included_credits"] += _num(it.get("discountQuantity"))
            row["additional_credits"] += _num(it.get("netQuantity"))
            row["gross_amount"] += _num(it.get("grossAmount"))
            row["additional_usage"] += _num(it.get("netAmount"))

        line_items = [
            {
                "model": r["model"],
                "product": r["product"],
                "unitType": r["unitType"],
                "included_credits": round(r["included_credits"], 2),
                "additional_credits": round(r["additional_credits"], 2),
                "gross_amount": round(r["gross_amount"], 2),
                "additional_usage": round(r["additional_usage"], 2),
            }
            for r in sorted(models.values(),
                            key=lambda r: r["gross_amount"], reverse=True)
        ]
        return {
            "time_period": data.get("timePeriod"),
            "total_gross_amount": round(
                sum(r["gross_amount"] for r in line_items), 2),
            "total_additional_usage": round(
                sum(r["additional_usage"] for r in line_items), 2),
            "line_items": line_items,
        }
    return _safe(_fetch)


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _diff(a: Any, b: Any) -> Any:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a - b
    return None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _fmt(value: Any) -> str:
    return "n/a" if value is None else str(value)


def render_table(report: dict[str, Any]) -> str:
    lines: list[str] = []
    meta = report["meta"]
    lines.append("=" * 60)
    lines.append(f"GitHub Billing & Usage Report - {meta['enterprise']}")
    lines.append(f"Generated: {meta['generated_at']}  "
                 f"Period: {meta['period_label']}")
    lines.append("=" * 60)
    data = report["data"]

    if "ghec" in data:
        lines += _render_seat_block("GHEC Seats", data["ghec"])
    if "ghas" in data:
        lines += _render_seat_block("GHAS Seats", data["ghas"])
    if "copilot" in data:
        lines += _render_copilot_block(data["copilot"])
    if "usage-summary" in data:
        lines += _render_cost_block(
            "Metered Usage Summary", data["usage-summary"])
    if "ai-usage" in data:
        lines += _render_ai_block(data["ai-usage"])

    return "\n".join(lines)


def _section_header(title: str) -> list[str]:
    return ["", f"## {title}", "-" * 60]


def _render_seat_block(title: str, block: dict) -> list[str]:
    out = _section_header(title)
    if "error" in block:
        out.append(f"  ! {block['error']}")
        return out
    out.append(f"  {'Purchased':<20}{_fmt(block.get('purchased'))}")
    out.append(f"  {'Consumed':<20}{_fmt(block.get('consumed'))}")
    out.append(f"  {'Available':<20}{_fmt(block.get('available'))}")
    if block.get("repositories_with_committers") is not None:
        out.append(f"  {'Repos w/ committers':<20}"
                   f"{block['repositories_with_committers']}")
    return out


def _render_copilot_block(block: dict) -> list[str]:
    out = _section_header("Copilot Seats")
    for scope_key, label in (("enterprise", "Enterprise"), ("org", "Org")):
        scope = block.get(scope_key)
        if not scope:
            continue
        if scope_key == "org":
            out.append(f"  [Org: {scope.get('login')}]")
        else:
            out.append("  [Enterprise]")
        if "error" in scope:
            out.append(f"    ! {scope['error']}")
            continue
        out.append(f"    {'Total assigned':<22}"
                   f"{_fmt(scope.get('total_assigned'))}")
        if scope.get("business_seats") is not None:
            out.append(f"    {'Business licenses':<22}"
                       f"{_fmt(scope.get('business_seats'))}")
        if scope.get("enterprise_seats") is not None:
            out.append(f"    {'Enterprise licenses':<22}"
                       f"{_fmt(scope.get('enterprise_seats'))}")
        out.append(f"    {'Active this cycle':<22}"
                   f"{_fmt(scope.get('active_this_cycle'))}")
        out.append(f"    {'Inactive this cycle':<22}"
                   f"{_fmt(scope.get('inactive_this_cycle'))}")
        out.append(f"    {'Pending invitation':<22}"
                   f"{_fmt(scope.get('pending_invitation'))}")
    return out


def _render_cost_block(title: str, block: dict) -> list[str]:
    out = _section_header(title)
    if "error" in block:
        out.append(f"  ! {block['error']}")
        return out
    out.append(f"  {'Net amount':<20}${_fmt(block.get('total_net_amount'))}")
    out.append(f"  {'Gross amount':<20}"
               f"${_fmt(block.get('total_gross_amount'))}")
    out.append(f"  {'Discount':<20}"
               f"${_fmt(block.get('total_discount_amount'))}")
    items = block.get("line_items", [])
    if items:
        out.append(f"  {'Line items:':<20}")
        for it in items:
            out.append(f"    - {_fmt(it.get('product'))}/{_fmt(it.get('sku'))} "
                       f"({_fmt(it.get('quantity'))} {_fmt(it.get('unitType'))})"
                       f": ${_fmt(it.get('netAmount'))}")
    else:
        out.append("  (no usage line items for this period)")
    return out


def _render_ai_block(block: dict) -> list[str]:
    out = _section_header("AI Credit Usage")
    if "error" in block:
        out.append(f"  ! {block['error']}")
        return out
    out.append(f"  {'Gross amount':<20}"
               f"${_fmt(block.get('total_gross_amount'))}")
    out.append(f"  {'Additional usage':<20}"
               f"${_fmt(block.get('total_additional_usage'))}")
    items = block.get("line_items", [])
    if not items:
        out.append("  (no AI credit usage for this period)")
        return out
    out.append("")
    out.append(f"  {'Model':<28}{'Included':>14}{'Additional':>14}"
               f"{'Gross $':>12}{'Add. usage $':>14}")
    for it in items:
        out.append(
            f"  {_fmt(it.get('model')):<28}"
            f"{_fmt(it.get('included_credits')):>14}"
            f"{_fmt(it.get('additional_credits')):>14}"
            f"{_fmt(it.get('gross_amount')):>12}"
            f"{_fmt(it.get('additional_usage')):>14}")
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_period(args: argparse.Namespace) -> dict[str, int]:
    period: dict[str, int] = {}
    if args.year:
        period["year"] = args.year
    if args.month:
        period["month"] = args.month
    if args.day:
        period["day"] = args.day
    return period


def period_label(period: dict[str, int]) -> str:
    if not period:
        return "current month (default)"
    parts = [f"{k}={v}" for k, v in period.items()]
    return ", ".join(parts)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pull GitHub licensing & usage information on demand.")
    parser.add_argument(
        "--enterprise", "-e",
        default=os.environ.get("GITHUB_ENTERPRISE", DEFAULT_ENTERPRISE),
        help="Enterprise slug (default: env GITHUB_ENTERPRISE or "
             f"'{DEFAULT_ENTERPRISE}').")
    parser.add_argument(
        "--org", "-o", default=None,
        help="Optional organization login for Copilot Business seat data.")
    parser.add_argument(
        "--section", "-s", action="append", choices=ALL_SECTIONS + ["all"],
        help="Section(s) to report. Repeatable. Default: all.")
    parser.add_argument(
        "--format", "-f", choices=["table", "json"], default="table",
        help="Output format (default: table).")
    parser.add_argument("--year", type=int, help="Usage period year (YYYY).")
    parser.add_argument("--month", type=int, choices=range(1, 13),
                        metavar="1-12", help="Usage period month.")
    parser.add_argument("--day", type=int, choices=range(1, 32),
                        metavar="1-31", help="Usage period day.")
    return parser.parse_args(argv)


def resolve_sections(selected: list[str] | None) -> list[str]:
    if not selected or "all" in selected:
        return list(ALL_SECTIONS)
    # de-duplicate while preserving order
    seen: list[str] = []
    for s in selected:
        if s not in seen:
            seen.append(s)
    return seen


def collect(client: GitHubBillingClient, args: argparse.Namespace,
            sections: list[str], period: dict[str, int]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if "ghec" in sections:
        data["ghec"] = get_ghec_seats(client, args.enterprise)
    if "ghas" in sections:
        data["ghas"] = get_ghas_seats(client, args.enterprise)
    if "copilot" in sections:
        data["copilot"] = get_copilot_seats(client, args.enterprise, args.org)
    if "usage-summary" in sections:
        data["usage-summary"] = get_metered_usage_summary(
            client, args.enterprise, period)
    if "ai-usage" in sections:
        data["ai-usage"] = get_ai_credit_usage(
            client, args.enterprise, period)
    return data


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("error: GITHUB_TOKEN environment variable is not set.",
              file=sys.stderr)
        return 2

    try:
        client = GitHubBillingClient(token)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    sections = resolve_sections(args.section)
    period = build_period(args)
    data = collect(client, args, sections, period)

    report = {
        "meta": {
            "enterprise": args.enterprise,
            "generated_at": datetime.now(timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S UTC"),
            "period_label": period_label(period),
            "sections": sections,
        },
        "data": data,
    }

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(render_table(report))

    # Exit non-zero if every requested section errored out.
    section_results = [v for k, v in data.items()]
    errored = sum(1 for v in section_results if _section_failed(v))
    if section_results and errored == len(section_results):
        return 1
    return 0


def _section_failed(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if "error" in value:
        return True
    # copilot block nests scopes; failed only if all present scopes errored
    if "enterprise" in value or "org" in value:
        scopes = [value.get(k) for k in ("enterprise", "org")
                  if value.get(k) is not None]
        return bool(scopes) and all(
            isinstance(s, dict) and "error" in s for s in scopes)
    return False


if __name__ == "__main__":
    sys.exit(main())
