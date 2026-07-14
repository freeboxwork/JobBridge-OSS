from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Scripts.prepare_profile_contract_dataset import infer_target_job_class, normalize_job_codes


ENDPOINTS = ("job_list_env", "job_list")
ENV_ENDPOINT = "job_list_env"
PUBLIC_DATASET_ID = "15117692"
DEFAULT_API_BASE_URL = "https://apis.data.go.kr/B552583/job"
DEFAULT_SUPABASE_SCHEMA = "jobbridge_private"
DEFAULT_SUPABASE_TABLE = "job_postings_live"
DEFAULT_JOB_CODES_PATH = PROJECT_ROOT / "Data" / "04_reference_job_codes" / "job_codes_20230825.csv"
DEFAULT_SNAPSHOT_PATH = PROJECT_ROOT / "Data" / "processed" / "live_job_postings" / "job_postings_live.json"

ENV_FIELD_MAP = {
    "envBothHands": "env_both_hands",
    "envEyesight": "env_eyesight",
    "envHandWork": "env_handwork",
    "envLiftPower": "env_lift_power",
    "envLstnTalk": "env_lstn_talk",
    "envStndWalk": "env_stnd_walk",
}

SIDO_ALIASES = {
    "서울특별시": "서울",
    "서울": "서울",
    "부산광역시": "부산",
    "부산": "부산",
    "대구광역시": "대구",
    "대구": "대구",
    "인천광역시": "인천",
    "인천": "인천",
    "광주광역시": "광주",
    "광주": "광주",
    "대전광역시": "대전",
    "대전": "대전",
    "울산광역시": "울산",
    "울산": "울산",
    "세종특별자치시": "세종",
    "세종시": "세종",
    "세종": "세종",
    "경기도": "경기",
    "경기": "경기",
    "강원특별자치도": "강원",
    "강원도": "강원",
    "강원": "강원",
    "충청북도": "충북",
    "충북": "충북",
    "충청남도": "충남",
    "충남": "충남",
    "전북특별자치도": "전북",
    "전라북도": "전북",
    "전북": "전북",
    "전라남도": "전남",
    "전남": "전남",
    "경상북도": "경북",
    "경북": "경북",
    "경상남도": "경남",
    "경남": "경남",
    "제주특별자치도": "제주",
    "제주도": "제주",
    "제주": "제주",
}


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_first(*names: str) -> str | None:
    for name in names:
        value = clean_text(os.getenv(name))
        if value:
            return value
    return None


def int_from_env(name: str, default: int) -> int:
    value = clean_text(os.getenv(name))
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def float_from_env(name: str, default: float) -> float:
    value = clean_text(os.getenv(name))
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def encode_service_key(service_key: str) -> str:
    safe_chars = "%" if "%" in service_key else ""
    return urllib.parse.quote(service_key, safe=safe_chars)


def build_public_data_url(
    api_base_url: str,
    endpoint: str,
    service_key: str,
    page_no: int,
    num_of_rows: int,
) -> str:
    base = api_base_url.rstrip("/")
    query = urllib.parse.urlencode(
        {
            "pageNo": str(page_no),
            "numOfRows": str(num_of_rows),
        }
    )
    return f"{base}/{endpoint}?serviceKey={encode_service_key(service_key)}&{query}"


def request_bytes(url: str, timeout: float, *, user_agent: str = "JobBridgeLiveJobs/0.1") -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def xml_text(root: ET.Element, path: str) -> str | None:
    value = root.findtext(path)
    return clean_text(value)


def parse_public_data_response(body: bytes, endpoint: str) -> tuple[list[dict[str, str | None]], dict[str, Any]]:
    root = ET.fromstring(body)
    result_code = xml_text(root, ".//resultCode")
    result_msg = xml_text(root, ".//resultMsg")
    if result_code and result_code != "0000":
        raise RuntimeError(f"{endpoint} API returned {result_code}: {result_msg or 'no message'}")

    total_count_text = xml_text(root, ".//totalCount")
    page_no_text = xml_text(root, ".//pageNo")
    num_rows_text = xml_text(root, ".//numOfRows")
    total_count = int(total_count_text or 0)
    page_no = int(page_no_text or 0)
    num_rows = int(num_rows_text or 0)

    items: list[dict[str, str | None]] = []
    for item in root.findall(".//items/item"):
        row = {child.tag: clean_text(child.text) for child in list(item)}
        items.append(row)

    meta = {
        "endpoint": endpoint,
        "result_code": result_code,
        "result_msg": result_msg,
        "total_count": total_count,
        "page_no": page_no,
        "num_of_rows": num_rows,
        "items": len(items),
    }
    return items, meta


def fetch_page(
    api_base_url: str,
    endpoint: str,
    service_key: str,
    page_no: int,
    num_of_rows: int,
    timeout: float,
) -> tuple[list[dict[str, str | None]], dict[str, Any]]:
    url = build_public_data_url(api_base_url, endpoint, service_key, page_no, num_of_rows)
    body = request_bytes(url, timeout)
    return parse_public_data_response(body, endpoint)


def fetch_endpoint(
    api_base_url: str,
    endpoint: str,
    service_key: str,
    num_of_rows: int,
    timeout: float,
    max_pages: int | None,
    sleep_seconds: float,
) -> tuple[list[dict[str, str | None]], dict[str, Any]]:
    first_items, first_meta = fetch_page(
        api_base_url,
        endpoint,
        service_key,
        page_no=1,
        num_of_rows=num_of_rows,
        timeout=timeout,
    )
    total_count = int(first_meta["total_count"])
    page_count = max(1, math.ceil(total_count / num_of_rows)) if total_count else 1
    if max_pages is not None:
        page_count = min(page_count, max_pages)

    rows = list(first_items)
    page_metas = [first_meta]
    for page_no in range(2, page_count + 1):
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        items, meta = fetch_page(
            api_base_url,
            endpoint,
            service_key,
            page_no=page_no,
            num_of_rows=num_of_rows,
            timeout=timeout,
        )
        rows.extend(items)
        page_metas.append(meta)

    return rows, {
        "endpoint": endpoint,
        "total_count": total_count,
        "requested_pages": page_count,
        "fetched_rows": len(rows),
        "pages": page_metas,
    }


def fallback_key(row: dict[str, Any]) -> str:
    parts = [
        clean_text(row.get("offerregDt")),
        clean_text(row.get("busplaName")),
        clean_text(row.get("jobNm")),
        clean_text(row.get("termDate")),
        clean_text(row.get("compAddr")),
        clean_text(row.get("salaryType")),
        clean_text(row.get("salary")),
    ]
    digest = hashlib.sha256("|".join(part or "" for part in parts).encode("utf-8")).hexdigest()[:24]
    return f"kead_live:hash:{digest}"


def source_posting_key(row: dict[str, Any]) -> str:
    # rno/rnum are row numbers inside each endpoint response, not stable IDs.
    # The same rno can point to different postings in job_list_env and job_list.
    return fallback_key(row)


def merge_prefer_env(
    current: dict[str, Any] | None,
    incoming: dict[str, Any],
    incoming_endpoint: str,
) -> dict[str, Any]:
    if current is None:
        merged = dict(incoming)
        merged["_source_endpoints"] = [incoming_endpoint]
        merged["_preferred_endpoint"] = incoming_endpoint
        return merged

    endpoints = list(current.get("_source_endpoints", []))
    if incoming_endpoint not in endpoints:
        endpoints.append(incoming_endpoint)

    prefer_incoming = incoming_endpoint == ENV_ENDPOINT
    prefer_current = current.get("_preferred_endpoint") == ENV_ENDPOINT

    merged = dict(current)
    if prefer_incoming or not prefer_current:
        for key, value in incoming.items():
            if value is not None:
                merged[key] = value
        merged["_preferred_endpoint"] = incoming_endpoint
    else:
        for key, value in incoming.items():
            if merged.get(key) is None and value is not None:
                merged[key] = value

    merged["_source_endpoints"] = endpoints
    return merged


def merge_rows(endpoint_rows: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    merged_by_key: dict[str, dict[str, Any]] = {}
    for endpoint in (ENV_ENDPOINT, "job_list"):
        for row in endpoint_rows.get(endpoint, []):
            key = source_posting_key(row)
            row_with_meta = dict(row)
            row_with_meta["_source_posting_key"] = key
            merged_by_key[key] = merge_prefer_env(merged_by_key.get(key), row_with_meta, endpoint)
    return list(merged_by_key.values())


def parse_date(value: Any) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    text = text.replace(".", "-").replace("/", "-")
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def parse_recruit_period(value: Any) -> tuple[str | None, str | None]:
    text = clean_text(value)
    if not text or "~" not in text:
        return None, None
    start, end = text.split("~", 1)
    return parse_date(start), parse_date(end)


def parse_number(value: Any) -> float | None:
    text = clean_text(value)
    if not text:
        return None
    normalized = text.replace(",", "")
    try:
        return float(normalized)
    except ValueError:
        return None


def split_region(address: Any) -> tuple[str | None, str | None]:
    text = clean_text(address)
    if not text:
        return None, None
    parts = text.split()
    if not parts:
        return None, None
    sido = SIDO_ALIASES.get(parts[0], parts[0])
    rest = parts[1:]
    if not rest:
        return sido, None
    if len(rest) >= 2 and rest[0].endswith("시") and rest[1].endswith("구"):
        return sido, f"{rest[0]} {rest[1]}"
    if rest[0].endswith(("시", "군", "구")):
        return sido, rest[0]
    return sido, None


def payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_current_payload(row: dict[str, Any], today: dt.date) -> bool:
    end_text = clean_text(row.get("recruit_end"))
    if not end_text:
        return True
    try:
        return dt.date.fromisoformat(end_text) >= today
    except ValueError:
        return True


def normalize_payload(row: dict[str, Any], fetched_at: str, job_code_lookup: dict[str, dict[str, str | None]]) -> dict[str, Any]:
    source_key = clean_text(row.get("_source_posting_key")) or source_posting_key(row)
    recruit_start_date, recruit_end_date = parse_recruit_period(row.get("termDate"))
    sido, sigungu = split_region(row.get("compAddr"))
    target_job_class, mapping_method, reference_large, reference_mid, reference_small = infer_target_job_class(
        row.get("jobNm"),
        job_code_lookup,
    )
    raw_payload = {
        key: value
        for key, value in row.items()
        if not key.startswith("_")
    }

    normalized = {
        "posting_id": source_key,
        "source_system": "kead",
        "source_dataset_id": PUBLIC_DATASET_ID,
        "source_endpoint": clean_text(row.get("_preferred_endpoint")) or ENV_ENDPOINT,
        "source_endpoints": row.get("_source_endpoints", []),
        "source_posting_key": source_key,
        "rno": clean_text(row.get("rno")),
        "posting_date": parse_date(row.get("offerregDt")),
        "offer_registered_date": parse_date(row.get("offerregDt")),
        "registered_date": parse_date(row.get("regDt")),
        "recruit_period_raw": clean_text(row.get("termDate")),
        "recruit_start": recruit_start_date,
        "recruit_end": recruit_end_date,
        "company_name": clean_text(row.get("busplaName")),
        "job_title": clean_text(row.get("jobNm")),
        "employment_type": clean_text(row.get("empType")),
        "entry_type": clean_text(row.get("enterType")),
        "wage_type": clean_text(row.get("salaryType")),
        "wage_raw": clean_text(row.get("salary")),
        "wage_amount": parse_number(row.get("salary")),
        "required_career": clean_text(row.get("reqCareer")),
        "required_education": clean_text(row.get("reqEduc")),
        "address_raw": clean_text(row.get("compAddr")),
        "sido": sido,
        "sigungu": sigungu,
        "target_job_class_candidate": target_job_class,
        "job_class_mapping_method": mapping_method,
        "reference_large": reference_large,
        "reference_mid": reference_mid,
        "reference_small": reference_small,
        "agency_name": clean_text(row.get("regagnName")),
        "contact_phone": clean_text(row.get("cntctNo")),
        "has_environment_detail": any(clean_text(row.get(field)) for field in ENV_FIELD_MAP),
        "raw_payload": raw_payload,
        "fetched_at": fetched_at,
        "last_seen_at": fetched_at,
        "is_active": True,
    }
    for source_field, target_field in ENV_FIELD_MAP.items():
        normalized[target_field] = clean_text(row.get(source_field))

    normalized["payload_hash"] = payload_hash(
        {key: value for key, value in normalized.items() if key not in {"fetched_at", "last_seen_at"}}
    )
    return normalized


def write_jsonl(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            file.write("\n")


def write_json(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


class SupabaseRestClient:
    def __init__(self, url: str, service_role_key: str, schema: str, table: str, timeout: float) -> None:
        self.url = url.rstrip("/")
        self.service_role_key = service_role_key
        self.schema = schema
        self.table = table
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        return f"{self.url}/rest/v1/{self.table}?on_conflict=source_posting_key"

    def upsert_batch(self, rows: list[dict[str, Any]]) -> None:
        data = json.dumps(rows, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=data,
            method="POST",
            headers={
                "apikey": self.service_role_key,
                "Authorization": f"Bearer {self.service_role_key}",
                "Content-Type": "application/json",
                "Content-Profile": self.schema,
                "Accept-Profile": self.schema,
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase upsert failed: HTTP {exc.code} {detail}") from exc

    def deactivate_stale(self, fetched_at: str) -> None:
        query = urllib.parse.urlencode(
            {
                "source_system": "eq.kead",
                "last_seen_at": f"lt.{fetched_at}",
                "is_active": "eq.true",
            }
        )
        data = json.dumps({"is_active": False}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}/rest/v1/{self.table}?{query}",
            data=data,
            method="PATCH",
            headers={
                "apikey": self.service_role_key,
                "Authorization": f"Bearer {self.service_role_key}",
                "Content-Type": "application/json",
                "Content-Profile": self.schema,
                "Accept-Profile": self.schema,
                "Prefer": "return=minimal",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase stale deactivate failed: HTTP {exc.code} {detail}") from exc


def chunked(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


def run(args: argparse.Namespace) -> dict[str, Any]:
    env_path = Path(args.env_file)
    load_env_file(env_path)

    service_key = env_first("JOBBRIDGE_PUBLIC_DATA_SERVICE_KEY")
    if not service_key:
        raise RuntimeError("JOBBRIDGE_PUBLIC_DATA_SERVICE_KEY is required in .env or environment")
    _, job_code_lookup = normalize_job_codes(Path(args.job_codes))

    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    endpoints = tuple(args.endpoint or ENDPOINTS)
    endpoint_rows: dict[str, list[dict[str, Any]]] = {}
    endpoint_summaries: list[dict[str, Any]] = []
    for endpoint in endpoints:
        rows, summary = fetch_endpoint(
            args.api_base_url,
            endpoint,
            service_key,
            args.num_of_rows,
            args.timeout_seconds,
            args.max_pages,
            args.sleep_seconds,
        )
        endpoint_rows[endpoint] = rows
        endpoint_summaries.append(summary)

    merged_rows = merge_rows(endpoint_rows)
    payloads = [normalize_payload(row, fetched_at, job_code_lookup) for row in merged_rows]
    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now().date()
    expired_count = sum(1 for row in payloads if not is_current_payload(row, today))
    payloads = [row for row in payloads if is_current_payload(row, today)]
    payloads.sort(key=lambda row: row["source_posting_key"])

    supabase_url = env_first("SUPABASE_URL", "JOBBRIDGE_SUPABASE_URL")
    supabase_key = env_first("SUPABASE_SERVICE_ROLE_KEY", "JOBBRIDGE_SUPABASE_SERVICE_ROLE_KEY")
    can_upsert = bool(supabase_url and supabase_key and not args.dry_run)

    output_path: Path | None = None
    if args.out:
        output_path = Path(args.out)
    elif not args.no_output:
        output_path = Path(os.getenv("JOBBRIDGE_LIVE_JOBS_SNAPSHOT", str(DEFAULT_SNAPSHOT_PATH)))

    if output_path is not None and not args.no_output:
        if output_path.suffix.lower() == ".jsonl":
            write_jsonl(payloads, output_path)
        else:
            write_json(payloads, output_path)

    upserted = 0
    if can_upsert:
        client = SupabaseRestClient(
            supabase_url or "",
            supabase_key or "",
            args.supabase_schema,
            args.supabase_table,
            args.supabase_timeout_seconds,
        )
        for batch in chunked(payloads, args.supabase_batch_size):
            client.upsert_batch(batch)
            upserted += len(batch)
        client.deactivate_stale(fetched_at)

    return {
        "fetched_at": fetched_at,
        "api_base_url": args.api_base_url,
        "endpoints": endpoint_summaries,
        "merged_rows": len(merged_rows),
        "normalized_payloads": len(payloads),
        "excluded_expired_rows": expired_count,
        "current_filter_date": today.isoformat(),
        "dedupe_key": "source_posting_key = sha256(offerregDt|busplaName|jobNm|termDate|compAddr|salaryType|salary); rno is not used because it is only an endpoint row number",
        "merge_policy": "job_list_env values override job_list values for the same source_posting_key; job_list fills missing fields.",
        "supabase": {
            "enabled": can_upsert,
            "schema": args.supabase_schema,
            "table": args.supabase_table,
            "upserted": upserted,
            "reason": None if can_upsert else "dry-run or SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY missing",
        },
        "snapshot_output": str(output_path) if output_path is not None and not args.no_output else None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect KEAD realtime job_list_env/job_list postings and upsert normalized rows to Supabase."
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--job-codes", default=str(DEFAULT_JOB_CODES_PATH))
    parser.add_argument("--api-base-url", default=os.getenv("JOBBRIDGE_LIVE_JOBS_API_BASE_URL", DEFAULT_API_BASE_URL))
    parser.add_argument("--endpoint", choices=ENDPOINTS, action="append", help="Fetch only one endpoint; repeat to select both.")
    parser.add_argument("--num-of-rows", type=int, default=int_from_env("JOBBRIDGE_LIVE_JOBS_NUM_OF_ROWS", 100))
    parser.add_argument("--max-pages", type=int, default=None, help="Limit pages per endpoint for smoke tests.")
    parser.add_argument("--today", default=None, help="YYYY-MM-DD date used to filter expired recruit_end rows. Defaults to local today.")
    parser.add_argument("--timeout-seconds", type=float, default=float_from_env("JOBBRIDGE_LIVE_JOBS_TIMEOUT_SECONDS", 20.0))
    parser.add_argument("--sleep-seconds", type=float, default=0.0, help="Optional delay between public API pages.")
    parser.add_argument("--dry-run", action="store_true", help="Skip Supabase upsert even when credentials are set.")
    parser.add_argument("--out", default=None, help="Write normalized payloads as JSONL.")
    parser.add_argument("--no-output", action="store_true", help="Do not write JSONL during dry-run/smoke tests.")
    parser.add_argument("--supabase-schema", default=env_first("SUPABASE_DB_SCHEMA", "JOBBRIDGE_SUPABASE_DB_SCHEMA") or DEFAULT_SUPABASE_SCHEMA)
    parser.add_argument("--supabase-table", default=DEFAULT_SUPABASE_TABLE)
    parser.add_argument("--supabase-batch-size", type=int, default=500)
    parser.add_argument("--supabase-timeout-seconds", type=float, default=30.0)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        summary = run(args)
    except Exception as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
