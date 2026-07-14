from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any


KST = timezone(timedelta(hours=9))
DEFAULT_SCHEMA = "jobbridge_private"
DEFAULT_TABLE = "job_postings_live"
DEFAULT_SYNC_URL = "https://jobbridge-site.vercel.app/api/admin/sync-live-jobs"
MAX_LIMIT = 1000


def env_value(*names: str) -> str:
    for name in names:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def current_kst_date() -> date:
    return datetime.now(KST).date()


def json_response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store, max-age=0",
            "Access-Control-Allow-Origin": os.getenv("JOBBRIDGE_ALLOWED_ORIGIN", "*"),
            "Access-Control-Allow-Methods": "GET,OPTIONS",
            "Access-Control-Allow-Headers": "content-type,authorization",
        },
        "body": json.dumps(payload, ensure_ascii=False, default=str),
    }


def parse_recruit_end(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def is_current_posting(row: dict[str, Any], today: date) -> bool:
    if row.get("is_active") is False:
        return False
    if not str(row.get("target_job_class_candidate") or "").strip():
        return False
    recruit_end = parse_recruit_end(row.get("recruit_end") or row.get("recruit_end_date"))
    return recruit_end is None or recruit_end >= today


def supabase_config() -> tuple[str, str, str, str]:
    url = env_value("SUPABASE_URL", "JOBBRIDGE_SUPABASE_URL").rstrip("/")
    key = env_value(
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_SECRET_KEY",
        "JOBBRIDGE_SUPABASE_SERVICE_ROLE_KEY",
    )
    schema = env_value("SUPABASE_DB_SCHEMA", "JOBBRIDGE_SUPABASE_DB_SCHEMA") or DEFAULT_SCHEMA
    table = env_value("JOBBRIDGE_SUPABASE_LIVE_JOBS_TABLE") or DEFAULT_TABLE
    if not url or not key:
        raise RuntimeError("Supabase server configuration is unavailable")
    return url, key, schema, table


def fetch_live_rows(limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    url, key, schema, table = supabase_config()
    today = current_kst_date()
    safe_limit = max(1, min(int(limit), MAX_LIMIT))
    columns = ",".join(
        [
            "posting_id",
            "source_posting_key",
            "job_title",
            "company_name",
            "employment_type",
            "wage_type",
            "wage_raw",
            "wage_amount",
            "address_raw",
            "sido",
            "sigungu",
            "target_job_class_candidate",
            "agency_name",
            "contact_phone",
            "recruit_period_raw",
            "recruit_start",
            "recruit_end",
            "offer_registered_date",
            "registered_date",
            "env_both_hands",
            "env_eyesight",
            "env_handwork",
            "env_lift_power",
            "env_lstn_talk",
            "env_stnd_walk",
            "has_environment_detail",
            "raw_payload",
            "last_seen_at",
            "is_active",
        ]
    )
    query = urllib.parse.urlencode(
        {
            "select": columns,
            "is_active": "eq.true",
            "target_job_class_candidate": "not.is.null",
            "or": f"(recruit_end.is.null,recruit_end.gte.{today.isoformat()})",
            "order": "recruit_end.asc,offer_registered_date.desc",
            "limit": str(safe_limit),
        }
    )
    request = urllib.request.Request(
        f"{url}/rest/v1/{table}?{query}",
        method="GET",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Accept-Profile": schema,
            "User-Agent": "JobBridgeLiveJobsLambda/1.0",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase live-jobs query failed: HTTP {exc.code} {detail[:240]}") from exc
    rows = json.loads(body or "[]")
    if not isinstance(rows, list):
        rows = []
    current_rows = [row for row in rows if isinstance(row, dict) and is_current_posting(row, today)]
    return current_rows, {
        "dbQueryMs": round((time.perf_counter() - started) * 1000, 2),
        "loadedRows": len(rows),
        "expiredFilteredRows": max(0, len(rows) - len(current_rows)),
        "filterDateKst": today.isoformat(),
    }


def wage_label(row: dict[str, Any]) -> str:
    wage_type = str(row.get("wage_type") or "").strip()
    wage_raw = str(row.get("wage_raw") or "").strip()
    if wage_type and wage_raw:
        return f"{wage_type} {wage_raw}"
    return wage_raw or wage_type


def region_label(row: dict[str, Any]) -> str:
    region = " ".join(
        part for part in [str(row.get("sido") or "").strip(), str(row.get("sigungu") or "").strip()] if part
    )
    return region or str(row.get("address_raw") or "").strip() or "지역 확인"


def row_to_ui_job(row: dict[str, Any]) -> dict[str, Any]:
    posting_id = str(row.get("posting_id") or row.get("source_posting_key") or "").strip()
    raw_payload = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
    return {
        "id": f"api-{posting_id}",
        "postingId": posting_id,
        "title": str(row.get("job_title") or "채용공고").strip(),
        "company": str(row.get("company_name") or "채용공고").strip(),
        "region": region_label(row),
        "location": str(row.get("address_raw") or "").strip(),
        "predictedJobClass": str(row.get("target_job_class_candidate") or "").strip(),
        "employment": str(row.get("employment_type") or "").strip(),
        "wage": wage_label(row),
        "career": str(raw_payload.get("reqCareer") or "").strip(),
        "education": str(raw_payload.get("reqEduc") or "").strip(),
        "recruitPeriod": str(row.get("recruit_period_raw") or "").strip(),
        "recruitStart": row.get("recruit_start"),
        "recruitEnd": row.get("recruit_end"),
        "offerRegisteredDate": row.get("offer_registered_date"),
        "registeredDate": row.get("registered_date"),
        "agency": str(row.get("agency_name") or "").strip(),
        "contact": str(row.get("contact_phone") or "").strip(),
        "env_both_hands": row.get("env_both_hands"),
        "env_eyesight": row.get("env_eyesight"),
        "env_handwork": row.get("env_handwork"),
        "env_lift_power": row.get("env_lift_power"),
        "env_lstn_talk": row.get("env_lstn_talk"),
        "env_stnd_walk": row.get("env_stnd_walk"),
        "hasEnvironmentDetail": bool(row.get("has_environment_detail")),
        "lastSeenAt": row.get("last_seen_at"),
        "is_active": True,
        "statusLabel": "채용중",
        "linked": False,
        "showScore": False,
        "sourceLabel": "KEAD 실시간 구인 API",
    }


def live_jobs_payload(limit: int) -> dict[str, Any]:
    rows, diagnostics = fetch_live_rows(limit)
    jobs = [row_to_ui_job(row) for row in rows]
    latest_last_seen = max((str(row.get("last_seen_at") or "") for row in rows), default="")
    return {
        "ok": True,
        "generatedAt": utc_now_iso(),
        "source": "supabase_live_fresh",
        "jobs": jobs,
        "diagnostics": {
            **diagnostics,
            "postingRows": len(rows),
            "returnedRows": len(jobs),
            "latestLastSeenAt": latest_last_seen or None,
            "cache": "disabled",
        },
    }


def run_remote_sync(dry_run: bool = False) -> dict[str, Any]:
    sync_url = env_value("JOBBRIDGE_SYNC_URL") or DEFAULT_SYNC_URL
    admin_token = env_value("JOBBRIDGE_ADMIN_TOKEN")
    if not admin_token:
        raise RuntimeError("JobBridge sync authentication is unavailable")
    payload = json.dumps(
        {
            "dryRun": bool(dry_run),
            "timeoutSeconds": 25,
            "trigger": "aws_eventbridge",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        sync_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-jobbridge-admin-token": admin_token,
            "User-Agent": "JobBridgeScheduledSync/1.0",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=100) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Scheduled sync failed: HTTP {exc.code} {detail[:320]}") from exc
    result = json.loads(body or "{}")
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Scheduled sync returned an invalid result: {str(result)[:320]}")
    return {
        "ok": True,
        "trigger": "aws_eventbridge",
        "dryRun": bool(dry_run),
        "elapsedMs": round((time.perf_counter() - started) * 1000, 2),
        "fetchedAt": result.get("fetchedAt"),
        "mergedRows": result.get("mergedRows"),
        "normalizedPayloads": result.get("normalizedPayloads"),
        "excludedExpiredRows": result.get("excludedExpiredRows"),
        "mappedJobClassRows": result.get("mappedJobClassRows"),
        "upserted": (result.get("supabase") or {}).get("upserted"),
    }


def request_method_and_path(event: dict[str, Any]) -> tuple[str, str]:
    request_context = event.get("requestContext") if isinstance(event.get("requestContext"), dict) else {}
    http = request_context.get("http") if isinstance(request_context.get("http"), dict) else {}
    method = str(http.get("method") or event.get("httpMethod") or "GET").upper()
    path = str(event.get("rawPath") or event.get("path") or http.get("path") or "/")
    return method, path


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if str(event.get("action") or "").lower() == "sync":
        return run_remote_sync(dry_run=bool(event.get("dryRun")))

    method, path = request_method_and_path(event)
    if method == "OPTIONS":
        return json_response(204, {})
    if method == "GET" and path.endswith("/v1/live-jobs"):
        query = event.get("queryStringParameters") if isinstance(event.get("queryStringParameters"), dict) else {}
        try:
            limit = int((query or {}).get("limit") or 500)
            return json_response(200, live_jobs_payload(limit))
        except Exception as exc:
            return json_response(502, {"ok": False, "error": str(exc), "generatedAt": utc_now_iso()})
    return json_response(404, {"ok": False, "error": "Not found"})
