from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "Data" / "processed" / "reference" / "jobbridge_reference.db"

DEFAULT_TRAINING_LIST_URL = "https://www.work24.go.kr/cm/openApi/call/hr/callOpenApiSvcInfo310L01.do"
DEFAULT_TRAINING_DETAIL_URL = "https://www.work24.go.kr/cm/openApi/call/hr/callOpenApiSvcInfo310L02.do"
DEFAULT_TRAINING_SCHEDULE_URL = "https://www.work24.go.kr/cm/openApi/call/hr/callOpenApiSvcInfo310L03.do"
DEFAULT_COMPETENCY_URL = "https://www.work24.go.kr/cm/openApi/call/wk/callOpenApiSvcInfo217L01.do"
DEFAULT_COMMON_CODE_URL = "https://www.work24.go.kr/cm/openApi/call/hr/callOpenApiSvcInfo319L01.do"
DEFAULT_DUTY_URL = "https://www.work24.go.kr/cm/openApi/call/wk/callOpenApiSvcInfo215L11.do"
DEFAULT_OCCUPATION_URL = "https://www.work24.go.kr/cm/openApi/call/wk/callOpenApiSvcInfo212L01.do"
DEFAULT_OCCUPATION_DICTIONARY_URL = "https://www.work24.go.kr/cm/openApi/call/wk/callOpenApiSvcInfo212L50.do"

DEFAULT_KEYWORDS = ("데이터", "사무", "복지", "청소", "제조", "디자인", "정보기술")
DEFAULT_COMMON_CODE_TYPES = tuple(f"{idx:02d}" for idx in range(12))

ONLY_ALIASES = {
    "all": "all",
    "training": "training",
    "training-courses": "training",
    "courses": "training",
    "competency": "competency",
    "competency-programs": "competency",
    "programs": "competency",
    "common": "common-codes",
    "common-code": "common-codes",
    "common-codes": "common-codes",
    "codes": "common-codes",
    "duty": "duty-dictionary",
    "duty-dictionary": "duty-dictionary",
    "duty-info": "duty-dictionary",
    "occupation": "occupation-items",
    "occupation-info": "occupation-items",
    "occupation-items": "occupation-items",
    "occupation-dictionary": "occupation-dictionary",
    "occupation-dictionary-items": "occupation-dictionary",
}

DEFAULT_ONLY = (
    "training",
    "competency",
    "common-codes",
    "duty-dictionary",
    "occupation-items",
    "occupation-dictionary",
)

API_RUN_COLUMNS = (
    "target",
    "started_at",
    "finished_at",
    "status",
    "params_json",
    "pages_requested",
    "rows_fetched",
    "error",
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def text_int(value: Any, default: int = 0) -> int:
    text = clean_text(value)
    if text is None:
        return default
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return default


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def row_hash(*parts: Any) -> str:
    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def require_env(name: str) -> str:
    value = env_first(name)
    if value is None:
        raise RuntimeError(f"{name} is required in .env or environment")
    return value


def env_url(name: str, default: str) -> str:
    return env_first(name) or default


def parse_date_arg(value: str) -> dt.date:
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"Invalid date {value!r}; use YYYY-MM-DD or YYYYMMDD")


def compact_date(value: dt.date) -> str:
    return value.strftime("%Y%m%d")


def iter_dates(start_date: dt.date, end_date: dt.date) -> Any:
    current = start_date
    while current <= end_date:
        yield current
        current += dt.timedelta(days=1)


def split_csv(values: list[str] | None, defaults: tuple[str, ...]) -> list[str]:
    if not values:
        return list(defaults)
    result: list[str] = []
    for value in values:
        for part in value.split(","):
            item = part.strip()
            if item:
                result.append(item)
    return result or list(defaults)


def normalize_only(values: list[str] | None) -> set[str]:
    if not values:
        return set(DEFAULT_ONLY)

    selected: set[str] = set()
    for value in values:
        for part in value.split(","):
            item = part.strip().lower()
            if not item:
                continue
            normalized = ONLY_ALIASES.get(item)
            if normalized is None:
                valid = ", ".join(sorted(ONLY_ALIASES))
                raise argparse.ArgumentTypeError(f"Unknown --only value {item!r}. Valid values: {valid}")
            if normalized == "all":
                return set(DEFAULT_ONLY)
            selected.add(normalized)
    return selected or set(DEFAULT_ONLY)


def safe_params(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if key.lower() not in {"authkey", "servicekey"}}


def build_url(base_url: str, auth_key: str, params: dict[str, Any]) -> str:
    query_params = {"authKey": auth_key}
    query_params.update({key: value for key, value in params.items() if value is not None})
    query = urllib.parse.urlencode(query_params, doseq=True)
    joiner = "&" if "?" in base_url else "?"
    return f"{base_url}{joiner}{query}"


def request_bytes(base_url: str, auth_key: str, params: dict[str, Any], timeout: float) -> bytes:
    url = build_url(base_url, auth_key, params)
    request = urllib.request.Request(url, headers={"User-Agent": "JobBridgeWork24ReferenceCollector/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from Work24 endpoint") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error from Work24 endpoint: {exc.reason}") from exc


def parse_xml_root(body: bytes) -> ET.Element:
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        text = body.decode("utf-8", errors="replace").strip()
        if not text:
            raise RuntimeError("Empty response from Work24 endpoint")
        raise


def xml_text(root: ET.Element, path: str) -> str | None:
    return clean_text(root.findtext(path))


def check_xml_message(root: ET.Element) -> bool:
    message_cd = xml_text(root, ".//messageCd") or xml_text(root, ".//message_cd")
    if message_cd == "006":
        return False
    if message_cd and message_cd not in {"0", "00", "0000"}:
        message = xml_text(root, ".//message") or xml_text(root, ".//messageMsg") or "no message"
        raise RuntimeError(f"Work24 API returned {message_cd}: {message}")
    result_code = xml_text(root, ".//resultCode")
    if result_code and result_code not in {"0", "00", "0000"}:
        result_msg = xml_text(root, ".//resultMsg") or "no message"
        raise RuntimeError(f"Work24 API returned {result_code}: {result_msg}")
    return True


def element_to_dict(element: ET.Element) -> dict[str, Any]:
    children = list(element)
    if not children:
        return {element.tag: clean_text(element.text)}
    record: dict[str, Any] = {}
    for child in children:
        child_children = list(child)
        if child_children:
            existing = record.get(child.tag)
            child_value = element_to_dict(child)
            if existing is None:
                record[child.tag] = child_value
            elif isinstance(existing, list):
                existing.append(child_value)
            else:
                record[child.tag] = [existing, child_value]
        else:
            record[child.tag] = clean_text(child.text)
    return record


def elements_to_records(elements: list[ET.Element]) -> list[dict[str, Any]]:
    return [element_to_dict(element) for element in elements]


def generic_xml_records(root: ET.Element) -> list[dict[str, Any]]:
    meta_tags = {
        "HRDNet",
        "jobsList",
        "dJobsList",
        "empPgmSchdInviteList",
        "total",
        "display",
        "startPage",
        "pageNum",
        "pageSize",
        "scn_cnt",
        "messageCd",
        "message",
        "resultCode",
        "resultMsg",
    }
    records: list[dict[str, Any]] = []
    for element in root.iter():
        if element.tag in meta_tags:
            continue
        children = list(element)
        if not children:
            continue
        leaf_children = [child for child in children if not list(child)]
        if len(leaf_children) >= 2:
            record = element_to_dict(element)
            record["_record_tag"] = element.tag
            records.append(record)
    return records


def xml_records_by_tag(root: ET.Element, tag: str) -> list[dict[str, Any]]:
    records = elements_to_records(root.findall(f".//{tag}"))
    return records or generic_xml_records(root)


def parse_json_body(body: bytes) -> Any:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        raise RuntimeError("Empty response from Work24 endpoint")
    data = json.loads(text)
    if isinstance(data, dict):
        message_cd = clean_text(data.get("message_cd") or data.get("messageCd"))
        if message_cd == "006":
            return {}
        if message_cd and message_cd not in {"0", "00", "0000"}:
            raise RuntimeError(f"Work24 API returned {message_cd}: {data.get('message') or 'no message'}")
    return data


def flatten_duty_dictionary(data: Any, keyword: str) -> list[dict[str, Any]]:
    result = data.get("result") if isinstance(data, dict) else data
    records: list[dict[str, Any]] = []

    if isinstance(result, dict):
        for ability_name, value in result.items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, dict):
                    row = dict(item)
                else:
                    row = {"value": item}
                row["keyword"] = keyword
                row["ability_name"] = ability_name
                records.append(row)
    elif isinstance(result, list):
        for item in result:
            row = dict(item) if isinstance(item, dict) else {"value": item}
            row["keyword"] = keyword
            records.append(row)
    elif isinstance(data, dict):
        row = dict(data)
        row["keyword"] = keyword
        records.append(row)

    return records


def find_first(record: dict[str, Any], *names: str) -> str | None:
    lowered = {key.lower(): value for key, value in record.items()}
    for name in names:
        direct = record.get(name)
        if direct is not None:
            return clean_text(direct)
        lower = lowered.get(name.lower())
        if lower is not None:
            return clean_text(lower)
    return None


def code_from_record(record: dict[str, Any]) -> str | None:
    for key, value in record.items():
        key_lower = key.lower()
        if key_lower.endswith("cd") or key_lower.endswith("code") or key_lower in {"code", "rsltcode"}:
            return clean_text(value)
    return None


def name_from_record(record: dict[str, Any]) -> str | None:
    for key, value in record.items():
        key_lower = key.lower()
        if key_lower.endswith("nm") or key_lower.endswith("name") or key_lower in {"name", "rsltname"}:
            return clean_text(value)
    return None


def flatten_common_codes(root: ET.Element, srch_type: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def visit(element: ET.Element, parent_code: str | None, depth: int, code_name: str | None) -> None:
        children = list(element)
        leaf_record: dict[str, Any] = {}
        nested_children: list[ET.Element] = []
        for child in children:
            if list(child):
                nested_children.append(child)
            else:
                leaf_record[child.tag] = clean_text(child.text)

        if leaf_record:
            code = code_from_record(leaf_record)
            name = name_from_record(leaf_record)
            if code or name or len(leaf_record) >= 2:
                row = dict(leaf_record)
                row["_record_tag"] = element.tag
                row["_code"] = code
                row["_name"] = name
                row["_parent_code"] = parent_code
                row["_depth"] = depth
                row["_code_name"] = code_name
                records.append(row)
                if code:
                    parent_code = code
                if name and not code_name:
                    code_name = name

        for child in nested_children:
            visit(child, parent_code, depth + 1, code_name)

    visit(root, None, 0, None)

    filtered = [
        row
        for row in records
        if row.get("_record_tag")
        not in {"HRDNet", "cmcdRegion", "cmcdList", "messageCd", "resultCode", "resultMsg"}
    ]
    for row in filtered:
        row["srch_type"] = srch_type
    return filtered


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS api_sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            params_json TEXT,
            pages_requested INTEGER DEFAULT 0,
            rows_fetched INTEGER DEFAULT 0,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS work24_training_courses (
            id TEXT PRIMARY KEY,
            sync_run_id INTEGER,
            fetched_at TEXT NOT NULL,
            source_endpoint TEXT NOT NULL,
            source_params_json TEXT NOT NULL,
            trpr_id TEXT,
            trpr_degr TEXT,
            trainst_cst_id TEXT,
            inst_cd TEXT,
            title TEXT,
            sub_title TEXT,
            ncs_cd TEXT,
            address TEXT,
            tel_no TEXT,
            train_target TEXT,
            train_target_cd TEXT,
            tra_start_date TEXT,
            tra_end_date TEXT,
            course_man TEXT,
            real_man TEXT,
            yard_man TEXT,
            reg_course_man TEXT,
            title_link TEXT,
            raw_json TEXT NOT NULL,
            FOREIGN KEY(sync_run_id) REFERENCES api_sync_runs(id)
        );

        CREATE TABLE IF NOT EXISTS work24_training_details (
            id TEXT PRIMARY KEY,
            sync_run_id INTEGER,
            fetched_at TEXT NOT NULL,
            source_endpoint TEXT NOT NULL,
            source_params_json TEXT NOT NULL,
            trpr_id TEXT,
            trpr_degr TEXT,
            trainst_cst_id TEXT,
            trpr_nm TEXT,
            ncs_nm TEXT,
            inst_cd TEXT,
            inst_nm TEXT,
            tot_traing_time TEXT,
            raw_json TEXT NOT NULL,
            FOREIGN KEY(sync_run_id) REFERENCES api_sync_runs(id)
        );

        CREATE TABLE IF NOT EXISTS work24_training_schedules (
            id TEXT PRIMARY KEY,
            sync_run_id INTEGER,
            fetched_at TEXT NOT NULL,
            source_endpoint TEXT NOT NULL,
            source_params_json TEXT NOT NULL,
            trpr_id TEXT,
            trpr_degr TEXT,
            trainst_cst_id TEXT,
            tr_sta_dt TEXT,
            tr_end_dt TEXT,
            tot_trco TEXT,
            tot_fxnum TEXT,
            raw_json TEXT NOT NULL,
            FOREIGN KEY(sync_run_id) REFERENCES api_sync_runs(id)
        );

        CREATE TABLE IF NOT EXISTS work24_competency_programs (
            id TEXT PRIMARY KEY,
            sync_run_id INTEGER,
            fetched_at TEXT NOT NULL,
            source_endpoint TEXT NOT NULL,
            source_params_json TEXT NOT NULL,
            query_date TEXT,
            org_nm TEXT,
            pgm_nm TEXT,
            pgm_sub_nm TEXT,
            pgm_target TEXT,
            pgm_stdt TEXT,
            pgm_endt TEXT,
            open_time_clcd TEXT,
            open_time TEXT,
            operation_time TEXT,
            open_plc_cont TEXT,
            raw_json TEXT NOT NULL,
            FOREIGN KEY(sync_run_id) REFERENCES api_sync_runs(id)
        );

        CREATE TABLE IF NOT EXISTS work24_common_codes (
            id TEXT PRIMARY KEY,
            sync_run_id INTEGER,
            fetched_at TEXT NOT NULL,
            source_endpoint TEXT NOT NULL,
            source_params_json TEXT NOT NULL,
            srch_type TEXT NOT NULL,
            record_tag TEXT,
            code TEXT,
            name TEXT,
            parent_code TEXT,
            code_name TEXT,
            depth INTEGER,
            raw_json TEXT NOT NULL,
            FOREIGN KEY(sync_run_id) REFERENCES api_sync_runs(id)
        );

        CREATE TABLE IF NOT EXISTS work24_duty_dictionary (
            id TEXT PRIMARY KEY,
            sync_run_id INTEGER,
            fetched_at TEXT NOT NULL,
            source_endpoint TEXT NOT NULL,
            source_params_json TEXT NOT NULL,
            keyword TEXT NOT NULL,
            ability_name TEXT,
            job_lcfn TEXT,
            job_mcn TEXT,
            job_scfn TEXT,
            job_sdvn TEXT,
            job_lrcl_cd TEXT,
            job_mlsf_cd TEXT,
            job_scla_cd TEXT,
            job_sdvn_cd TEXT,
            ablt_unit TEXT,
            ablt_def TEXT,
            raw_json TEXT NOT NULL,
            FOREIGN KEY(sync_run_id) REFERENCES api_sync_runs(id)
        );

        CREATE TABLE IF NOT EXISTS work24_occupation_items (
            id TEXT PRIMARY KEY,
            sync_run_id INTEGER,
            fetched_at TEXT NOT NULL,
            source_endpoint TEXT NOT NULL,
            source_params_json TEXT NOT NULL,
            keyword TEXT,
            job_clcd TEXT,
            job_clcd_nm TEXT,
            job_cd TEXT,
            job_nm TEXT,
            raw_json TEXT NOT NULL,
            FOREIGN KEY(sync_run_id) REFERENCES api_sync_runs(id)
        );

        CREATE TABLE IF NOT EXISTS work24_occupation_dictionary_items (
            id TEXT PRIMARY KEY,
            sync_run_id INTEGER,
            fetched_at TEXT NOT NULL,
            source_endpoint TEXT NOT NULL,
            source_params_json TEXT NOT NULL,
            keyword TEXT,
            d_job_cd TEXT,
            d_job_cd_seq TEXT,
            d_job_nm TEXT,
            raw_json TEXT NOT NULL,
            FOREIGN KEY(sync_run_id) REFERENCES api_sync_runs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_work24_training_courses_keys
            ON work24_training_courses(trpr_id, trpr_degr, trainst_cst_id);
        CREATE INDEX IF NOT EXISTS idx_work24_training_courses_dates
            ON work24_training_courses(tra_start_date, tra_end_date);
        CREATE INDEX IF NOT EXISTS idx_work24_competency_programs_date
            ON work24_competency_programs(pgm_stdt);
        CREATE INDEX IF NOT EXISTS idx_work24_common_codes_type
            ON work24_common_codes(srch_type, code);
        CREATE INDEX IF NOT EXISTS idx_work24_duty_dictionary_keyword
            ON work24_duty_dictionary(keyword);
        CREATE INDEX IF NOT EXISTS idx_work24_occupation_items_keyword
            ON work24_occupation_items(keyword);
        CREATE INDEX IF NOT EXISTS idx_work24_occupation_dictionary_keyword
            ON work24_occupation_dictionary_items(keyword);
        """
    )
    ensure_api_sync_runs_columns(conn)
    conn.commit()


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def ensure_api_sync_runs_columns(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "api_sync_runs", "target", "TEXT")
    ensure_column(conn, "api_sync_runs", "params_json", "TEXT")
    ensure_column(conn, "api_sync_runs", "pages_requested", "INTEGER DEFAULT 0")
    ensure_column(conn, "api_sync_runs", "rows_fetched", "INTEGER DEFAULT 0")


def start_run(conn: sqlite3.Connection, target: str, params: dict[str, Any]) -> int:
    columns = table_columns(conn, "api_sync_runs")
    row: dict[str, Any] = {
        "started_at": utc_now(),
        "status": "running",
    }
    if "target" in columns:
        row["target"] = target
    if "source" in columns:
        row["source"] = target
    if "params_json" in columns:
        row["params_json"] = json_dumps(params)

    names = list(row)
    placeholders = ", ".join("?" for _ in names)
    cursor = conn.execute(
        f"INSERT INTO api_sync_runs ({', '.join(names)}) VALUES ({placeholders})",
        [row[name] for name in names],
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    *,
    pages_requested: int = 0,
    rows_fetched: int = 0,
    error: str | None = None,
) -> None:
    columns = table_columns(conn, "api_sync_runs")
    updates: dict[str, Any] = {
        "finished_at": utc_now(),
        "status": status,
        "error": error,
    }
    if "pages_requested" in columns:
        updates["pages_requested"] = pages_requested
    if "rows_fetched" in columns:
        updates["rows_fetched"] = rows_fetched
    if "rows_inserted" in columns:
        updates["rows_inserted"] = rows_fetched

    assignments = ", ".join(f"{name} = ?" for name in updates)
    conn.execute(
        f"UPDATE api_sync_runs SET {assignments} WHERE id = ?",
        [*updates.values(), run_id],
    )
    conn.commit()


def insert_row(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    quoted_columns = ", ".join(columns)
    updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "id")
    sql = (
        f"INSERT INTO {table} ({quoted_columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [row[column] for column in columns])


class Work24Collector:
    def __init__(self, conn: sqlite3.Connection, args: argparse.Namespace) -> None:
        self.conn = conn
        self.args = args
        self.timeout = float(args.timeout)
        self.sleep_seconds = float(args.sleep)

    def maybe_sleep(self) -> None:
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)

    def get_xml(self, base_url: str, auth_key: str, params: dict[str, Any]) -> ET.Element:
        body = request_bytes(base_url, auth_key, params, self.timeout)
        self.maybe_sleep()
        root = parse_xml_root(body)
        check_xml_message(root)
        return root

    def get_json(self, base_url: str, auth_key: str, params: dict[str, Any]) -> Any:
        body = request_bytes(base_url, auth_key, params, self.timeout)
        self.maybe_sleep()
        return parse_json_body(body)

    def collect_training_courses(self) -> list[dict[str, Any]]:
        auth_key = require_env("JOBBRIDGE_WORK24_TRAINING_COURSE_AUTH_KEY")
        list_url = env_url("JOBBRIDGE_WORK24_TRAINING_COURSE_LIST_URL", DEFAULT_TRAINING_LIST_URL)
        run_params = {
            "endpoint": "training_courses",
            "start_date": compact_date(self.args.start_date),
            "end_date": compact_date(self.args.end_date),
            "page_size": self.args.page_size,
            "limit_pages": self.args.limit_pages,
        }
        run_id = start_run(self.conn, "work24_training_courses", run_params)
        fetched_at = utc_now()
        all_records: list[dict[str, Any]] = []
        pages_requested = 0

        try:
            page_size = min(max(int(self.args.page_size), 1), 100)
            base_params = {
                "returnType": "XML",
                "outType": "1",
                "pageSize": page_size,
                "srchTraStDt": compact_date(self.args.start_date),
                "srchTraEndDt": compact_date(self.args.end_date),
                "sort": "ASC",
                "sortCol": "2",
            }
            first_params = dict(base_params, pageNum=1)
            first_root = self.get_xml(list_url, auth_key, first_params)
            pages_requested += 1
            total = text_int(xml_text(first_root, ".//scn_cnt"))
            page_count = max(1, math.ceil(total / page_size)) if total else 1
            if self.args.limit_pages is not None:
                page_count = min(page_count, int(self.args.limit_pages))

            page_records = xml_records_by_tag(first_root, "scn_list")
            all_records.extend(page_records)
            self.insert_training_course_records(run_id, list_url, first_params, page_records, fetched_at)

            for page_num in range(2, page_count + 1):
                params = dict(base_params, pageNum=page_num)
                root = self.get_xml(list_url, auth_key, params)
                pages_requested += 1
                page_records = xml_records_by_tag(root, "scn_list")
                all_records.extend(page_records)
                self.insert_training_course_records(run_id, list_url, params, page_records, utc_now())

            self.conn.commit()
            finish_run(
                self.conn,
                run_id,
                "success",
                pages_requested=pages_requested,
                rows_fetched=len(all_records),
            )
            print(f"work24_training_courses: rows={len(all_records)} pages={pages_requested}")
            return all_records
        except Exception as exc:
            self.conn.rollback()
            finish_run(
                self.conn,
                run_id,
                "failed",
                pages_requested=pages_requested,
                rows_fetched=len(all_records),
                error=str(exc),
            )
            raise

    def insert_training_course_records(
        self,
        run_id: int,
        source_endpoint: str,
        params: dict[str, Any],
        records: list[dict[str, Any]],
        fetched_at: str,
    ) -> None:
        for record in records:
            trpr_id = find_first(record, "trprId", "TRPR_ID")
            trpr_degr = find_first(record, "trprDegr", "TRPR_DEGR")
            trainst_cst_id = find_first(record, "trainstCstId", "TRAINST_CST_ID")
            title = find_first(record, "title", "TITLE")
            tra_start_date = find_first(record, "traStartDate", "TRA_START_DATE")
            row = {
                "id": row_hash("training", trpr_id, trpr_degr, trainst_cst_id, tra_start_date, title),
                "sync_run_id": run_id,
                "fetched_at": fetched_at,
                "source_endpoint": source_endpoint,
                "source_params_json": json_dumps(safe_params(params)),
                "trpr_id": trpr_id,
                "trpr_degr": trpr_degr,
                "trainst_cst_id": trainst_cst_id,
                "inst_cd": find_first(record, "instCd", "INST_CD"),
                "title": title,
                "sub_title": find_first(record, "subTitle", "SUB_TITLE"),
                "ncs_cd": find_first(record, "ncsCd", "NCS_CD"),
                "address": find_first(record, "address", "ADDRESS"),
                "tel_no": find_first(record, "telNo", "TEL_NO"),
                "train_target": find_first(record, "trainTarget", "TRAIN_TARGET"),
                "train_target_cd": find_first(record, "trainTargetCd", "TRAIN_TARGET_CD"),
                "tra_start_date": tra_start_date,
                "tra_end_date": find_first(record, "traEndDate", "TRA_END_DATE"),
                "course_man": find_first(record, "courseMan", "COURSE_MAN"),
                "real_man": find_first(record, "realMan", "REAL_MAN"),
                "yard_man": find_first(record, "yardMan", "YARD_MAN"),
                "reg_course_man": find_first(record, "regCourseMan", "REG_COURSE_MAN"),
                "title_link": find_first(record, "titleLink", "TITLE_LINK"),
                "raw_json": json_dumps(record),
            }
            insert_row(self.conn, "work24_training_courses", row)

    def collect_training_details_and_schedules(self, course_records: list[dict[str, Any]]) -> None:
        if not course_records:
            print("work24_training_details: skipped rows=0")
            print("work24_training_schedules: skipped rows=0")
            return

        auth_key = require_env("JOBBRIDGE_WORK24_TRAINING_COURSE_AUTH_KEY")
        detail_url = env_url("JOBBRIDGE_WORK24_TRAINING_COURSE_DETAIL_URL", DEFAULT_TRAINING_DETAIL_URL)
        schedule_url = env_url("JOBBRIDGE_WORK24_TRAINING_COURSE_SCHEDULE_URL", DEFAULT_TRAINING_SCHEDULE_URL)

        unique_courses: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for record in course_records:
            key = (
                find_first(record, "trprId") or "",
                find_first(record, "trprDegr") or "",
                find_first(record, "trainstCstId") or "",
            )
            if not all(key) or key in seen:
                continue
            seen.add(key)
            unique_courses.append(key)

        if self.args.limit_details is not None:
            unique_courses = unique_courses[: int(self.args.limit_details)]

        detail_run_id = start_run(
            self.conn,
            "work24_training_details",
            {"course_count": len(unique_courses), "limit_details": self.args.limit_details},
        )
        schedule_run_id = start_run(
            self.conn,
            "work24_training_schedules",
            {"course_count": len(unique_courses), "limit_details": self.args.limit_details},
        )
        detail_rows = 0
        schedule_rows = 0
        detail_pages = 0
        schedule_pages = 0

        try:
            for trpr_id, trpr_degr, trainst_cst_id in unique_courses:
                base_detail_params = {
                    "returnType": "XML",
                    "outType": "2",
                    "srchTrprId": trpr_id,
                    "srchTrprDegr": trpr_degr,
                    "srchTorgId": trainst_cst_id,
                    "trainstCstId": trainst_cst_id,
                }
                detail_root = self.get_xml(detail_url, auth_key, base_detail_params)
                detail_pages += 1
                detail_records = generic_xml_records(detail_root)
                if not detail_records:
                    detail_records = [element_to_dict(detail_root)]
                detail_rows += len(detail_records)
                self.insert_training_detail_records(
                    detail_run_id,
                    detail_url,
                    base_detail_params,
                    detail_records,
                    trpr_id,
                    trpr_degr,
                    trainst_cst_id,
                    utc_now(),
                )

                base_schedule_params = {
                    "returnType": "XML",
                    "outType": "3",
                    "srchTrprId": trpr_id,
                    "srchTrprDegr": trpr_degr,
                    "srchTorgId": trainst_cst_id,
                    "trainstCstId": trainst_cst_id,
                }
                schedule_root = self.get_xml(schedule_url, auth_key, base_schedule_params)
                schedule_pages += 1
                schedule_records = generic_xml_records(schedule_root)
                if not schedule_records:
                    schedule_records = [element_to_dict(schedule_root)]
                schedule_rows += len(schedule_records)
                self.insert_training_schedule_records(
                    schedule_run_id,
                    schedule_url,
                    base_schedule_params,
                    schedule_records,
                    trpr_id,
                    trpr_degr,
                    trainst_cst_id,
                    utc_now(),
                )

            self.conn.commit()
            finish_run(
                self.conn,
                detail_run_id,
                "success",
                pages_requested=detail_pages,
                rows_fetched=detail_rows,
            )
            finish_run(
                self.conn,
                schedule_run_id,
                "success",
                pages_requested=schedule_pages,
                rows_fetched=schedule_rows,
            )
            print(f"work24_training_details: rows={detail_rows} calls={detail_pages}")
            print(f"work24_training_schedules: rows={schedule_rows} calls={schedule_pages}")
        except Exception as exc:
            self.conn.rollback()
            finish_run(
                self.conn,
                detail_run_id,
                "failed",
                pages_requested=detail_pages,
                rows_fetched=detail_rows,
                error=str(exc),
            )
            finish_run(
                self.conn,
                schedule_run_id,
                "failed",
                pages_requested=schedule_pages,
                rows_fetched=schedule_rows,
                error=str(exc),
            )
            raise

    def insert_training_detail_records(
        self,
        run_id: int,
        source_endpoint: str,
        params: dict[str, Any],
        records: list[dict[str, Any]],
        trpr_id: str,
        trpr_degr: str,
        trainst_cst_id: str,
        fetched_at: str,
    ) -> None:
        for index, record in enumerate(records):
            row = {
                "id": row_hash("training-detail", trpr_id, trpr_degr, trainst_cst_id, index, json_dumps(record)),
                "sync_run_id": run_id,
                "fetched_at": fetched_at,
                "source_endpoint": source_endpoint,
                "source_params_json": json_dumps(safe_params(params)),
                "trpr_id": trpr_id,
                "trpr_degr": trpr_degr,
                "trainst_cst_id": trainst_cst_id,
                "trpr_nm": find_first(record, "trprNm", "trpr_Nm", "title"),
                "ncs_nm": find_first(record, "ncsNm", "ncs_Nm"),
                "inst_cd": find_first(record, "instCd", "inst_cd"),
                "inst_nm": find_first(record, "instNm", "inoNm", "inst_name"),
                "tot_traing_time": find_first(record, "totTraingTime", "totTraningTime", "totTraingTime"),
                "raw_json": json_dumps(record),
            }
            insert_row(self.conn, "work24_training_details", row)

    def insert_training_schedule_records(
        self,
        run_id: int,
        source_endpoint: str,
        params: dict[str, Any],
        records: list[dict[str, Any]],
        trpr_id: str,
        trpr_degr: str,
        trainst_cst_id: str,
        fetched_at: str,
    ) -> None:
        for index, record in enumerate(records):
            tr_sta_dt = find_first(record, "trStaDt", "traStartDate")
            tr_end_dt = find_first(record, "trEndDt", "traEndDate")
            row = {
                "id": row_hash("training-schedule", trpr_id, trpr_degr, trainst_cst_id, tr_sta_dt, tr_end_dt, index),
                "sync_run_id": run_id,
                "fetched_at": fetched_at,
                "source_endpoint": source_endpoint,
                "source_params_json": json_dumps(safe_params(params)),
                "trpr_id": trpr_id,
                "trpr_degr": trpr_degr,
                "trainst_cst_id": trainst_cst_id,
                "tr_sta_dt": tr_sta_dt,
                "tr_end_dt": tr_end_dt,
                "tot_trco": find_first(record, "totTrco"),
                "tot_fxnum": find_first(record, "totFxnum"),
                "raw_json": json_dumps(record),
            }
            insert_row(self.conn, "work24_training_schedules", row)

    def collect_competency_programs(self) -> None:
        auth_key = require_env("JOBBRIDGE_WORK24_JOBSEEKER_COMPETENCY_AUTH_KEY")
        url = env_url("JOBBRIDGE_WORK24_JOBSEEKER_COMPETENCY_URL", DEFAULT_COMPETENCY_URL)
        run_params = {
            "endpoint": "competency_programs",
            "start_date": compact_date(self.args.start_date),
            "end_date": compact_date(self.args.end_date),
            "page_size": self.args.page_size,
            "limit_pages": self.args.limit_pages,
            "mode": "daily pgmStdt iteration",
        }
        run_id = start_run(self.conn, "work24_competency_programs", run_params)
        rows = 0
        pages = 0

        try:
            page_size = min(max(int(self.args.page_size), 1), 100)
            for query_date in iter_dates(self.args.start_date, self.args.end_date):
                date_text = compact_date(query_date)
                base_params = {
                    "returnType": "XML",
                    "display": page_size,
                    "pgmStdt": date_text,
                }
                first_params = dict(base_params, startPage=1)
                first_root = self.get_xml(url, auth_key, first_params)
                pages += 1
                total = text_int(xml_text(first_root, ".//total"))
                page_count = max(1, math.ceil(total / page_size)) if total else 1
                if self.args.limit_pages is not None:
                    page_count = min(page_count, int(self.args.limit_pages))
                page_records = xml_records_by_tag(first_root, "empPgmSchdInvite")
                rows += len(page_records)
                self.insert_competency_records(run_id, url, first_params, page_records, date_text, utc_now())

                for page_num in range(2, page_count + 1):
                    params = dict(base_params, startPage=page_num)
                    root = self.get_xml(url, auth_key, params)
                    pages += 1
                    page_records = xml_records_by_tag(root, "empPgmSchdInvite")
                    rows += len(page_records)
                    self.insert_competency_records(run_id, url, params, page_records, date_text, utc_now())

            self.conn.commit()
            finish_run(self.conn, run_id, "success", pages_requested=pages, rows_fetched=rows)
            print(f"work24_competency_programs: rows={rows} calls={pages}")
        except Exception as exc:
            self.conn.rollback()
            finish_run(self.conn, run_id, "failed", pages_requested=pages, rows_fetched=rows, error=str(exc))
            raise

    def insert_competency_records(
        self,
        run_id: int,
        source_endpoint: str,
        params: dict[str, Any],
        records: list[dict[str, Any]],
        query_date: str,
        fetched_at: str,
    ) -> None:
        for record in records:
            org_nm = find_first(record, "orgNm")
            pgm_nm = find_first(record, "pgmNm")
            pgm_sub_nm = find_first(record, "pgmSubNm")
            pgm_stdt = find_first(record, "pgmStdt")
            open_time = find_first(record, "openTime")
            open_plc_cont = find_first(record, "openPlcCont")
            row = {
                "id": row_hash("competency", org_nm, pgm_nm, pgm_sub_nm, pgm_stdt, open_time, open_plc_cont),
                "sync_run_id": run_id,
                "fetched_at": fetched_at,
                "source_endpoint": source_endpoint,
                "source_params_json": json_dumps(safe_params(params)),
                "query_date": query_date,
                "org_nm": org_nm,
                "pgm_nm": pgm_nm,
                "pgm_sub_nm": pgm_sub_nm,
                "pgm_target": find_first(record, "pgmTarget"),
                "pgm_stdt": pgm_stdt,
                "pgm_endt": find_first(record, "pgmEndt"),
                "open_time_clcd": find_first(record, "openTimeClcd"),
                "open_time": open_time,
                "operation_time": find_first(record, "operationTime"),
                "open_plc_cont": open_plc_cont,
                "raw_json": json_dumps(record),
            }
            insert_row(self.conn, "work24_competency_programs", row)

    def collect_common_codes(self) -> None:
        auth_key = require_env("JOBBRIDGE_WORK24_COMMON_CODE_AUTH_KEY")
        url = env_url("JOBBRIDGE_WORK24_COMMON_CODE_URL", DEFAULT_COMMON_CODE_URL)
        srch_types = split_csv(self.args.common_code_types, DEFAULT_COMMON_CODE_TYPES)
        run_id = start_run(self.conn, "work24_common_codes", {"srch_types": srch_types})
        rows = 0
        calls = 0

        try:
            for srch_type in srch_types:
                params = {"returnType": "XML", "srchType": srch_type}
                root = self.get_xml(url, auth_key, params)
                calls += 1
                records = flatten_common_codes(root, srch_type)
                if not records:
                    records = generic_xml_records(root)
                    for record in records:
                        record["srch_type"] = srch_type
                rows += len(records)
                self.insert_common_code_records(run_id, url, params, records, srch_type, utc_now())
            self.conn.commit()
            finish_run(self.conn, run_id, "success", pages_requested=calls, rows_fetched=rows)
            print(f"work24_common_codes: rows={rows} calls={calls}")
        except Exception as exc:
            self.conn.rollback()
            finish_run(self.conn, run_id, "failed", pages_requested=calls, rows_fetched=rows, error=str(exc))
            raise

    def insert_common_code_records(
        self,
        run_id: int,
        source_endpoint: str,
        params: dict[str, Any],
        records: list[dict[str, Any]],
        srch_type: str,
        fetched_at: str,
    ) -> None:
        for index, record in enumerate(records):
            code = clean_text(record.get("_code")) or code_from_record(record)
            name = clean_text(record.get("_name")) or name_from_record(record)
            row = {
                "id": row_hash("common-code", srch_type, code, name, record.get("_parent_code"), index),
                "sync_run_id": run_id,
                "fetched_at": fetched_at,
                "source_endpoint": source_endpoint,
                "source_params_json": json_dumps(safe_params(params)),
                "srch_type": srch_type,
                "record_tag": clean_text(record.get("_record_tag")),
                "code": code,
                "name": name,
                "parent_code": clean_text(record.get("_parent_code")),
                "code_name": clean_text(record.get("_code_name")),
                "depth": text_int(record.get("_depth"), 0),
                "raw_json": json_dumps(record),
            }
            insert_row(self.conn, "work24_common_codes", row)

    def collect_duty_dictionary(self) -> None:
        auth_key = require_env("JOBBRIDGE_WORK24_DUTY_INFO_AUTH_KEY")
        url = env_url("JOBBRIDGE_WORK24_DUTY_INFO_URL", DEFAULT_DUTY_URL)
        keywords = split_csv(self.args.duty_keywords or self.args.keywords, DEFAULT_KEYWORDS)
        run_id = start_run(
            self.conn,
            "work24_duty_dictionary",
            {"keywords": keywords, "limit": self.args.duty_limit},
        )
        rows = 0
        calls = 0

        try:
            for keyword in keywords:
                params = {"returnType": "JSON", "word": keyword, "limit": self.args.duty_limit}
                data = self.get_json(url, auth_key, params)
                calls += 1
                records = flatten_duty_dictionary(data, keyword)
                rows += len(records)
                self.insert_duty_records(run_id, url, params, records, keyword, utc_now())
            self.conn.commit()
            finish_run(self.conn, run_id, "success", pages_requested=calls, rows_fetched=rows)
            print(f"work24_duty_dictionary: rows={rows} calls={calls}")
        except Exception as exc:
            self.conn.rollback()
            finish_run(self.conn, run_id, "failed", pages_requested=calls, rows_fetched=rows, error=str(exc))
            raise

    def insert_duty_records(
        self,
        run_id: int,
        source_endpoint: str,
        params: dict[str, Any],
        records: list[dict[str, Any]],
        keyword: str,
        fetched_at: str,
    ) -> None:
        for index, record in enumerate(records):
            ability_name = find_first(record, "ability_name", "job_sdvn")
            ablt_unit = find_first(record, "ablt_unit")
            row = {
                "id": row_hash("duty", keyword, ability_name, ablt_unit, index),
                "sync_run_id": run_id,
                "fetched_at": fetched_at,
                "source_endpoint": source_endpoint,
                "source_params_json": json_dumps(safe_params(params)),
                "keyword": keyword,
                "ability_name": ability_name,
                "job_lcfn": find_first(record, "job_lcfn"),
                "job_mcn": find_first(record, "job_mcn"),
                "job_scfn": find_first(record, "job_scfn"),
                "job_sdvn": find_first(record, "job_sdvn"),
                "job_lrcl_cd": find_first(record, "job_lrcl_cd"),
                "job_mlsf_cd": find_first(record, "job_mlsf_cd"),
                "job_scla_cd": find_first(record, "job_scla_cd"),
                "job_sdvn_cd": find_first(record, "job_sdvn_cd"),
                "ablt_unit": ablt_unit,
                "ablt_def": find_first(record, "ablt_def"),
                "raw_json": json_dumps(record),
            }
            insert_row(self.conn, "work24_duty_dictionary", row)

    def collect_occupation_items(self) -> None:
        auth_key = require_env("JOBBRIDGE_WORK24_OCCUPATION_INFO_AUTH_KEY")
        url = env_url("JOBBRIDGE_WORK24_OCCUPATION_INFO_URL", DEFAULT_OCCUPATION_URL)
        keywords = split_csv(self.args.occupation_keywords or self.args.keywords, DEFAULT_KEYWORDS)
        run_id = start_run(self.conn, "work24_occupation_items", {"keywords": keywords})
        rows = 0
        calls = 0

        try:
            for keyword in keywords:
                params = {"returnType": "XML", "target": "JOBCD", "srchType": "K", "keyword": keyword}
                root = self.get_xml(url, auth_key, params)
                calls += 1
                records = xml_records_by_tag(root, "jobList")
                rows += len(records)
                self.insert_occupation_records(run_id, url, params, records, keyword, utc_now())
            self.conn.commit()
            finish_run(self.conn, run_id, "success", pages_requested=calls, rows_fetched=rows)
            print(f"work24_occupation_items: rows={rows} calls={calls}")
        except Exception as exc:
            self.conn.rollback()
            finish_run(self.conn, run_id, "failed", pages_requested=calls, rows_fetched=rows, error=str(exc))
            raise

    def insert_occupation_records(
        self,
        run_id: int,
        source_endpoint: str,
        params: dict[str, Any],
        records: list[dict[str, Any]],
        keyword: str,
        fetched_at: str,
    ) -> None:
        for index, record in enumerate(records):
            job_cd = find_first(record, "jobCd")
            row = {
                "id": row_hash("occupation", keyword, job_cd, find_first(record, "jobNm"), index),
                "sync_run_id": run_id,
                "fetched_at": fetched_at,
                "source_endpoint": source_endpoint,
                "source_params_json": json_dumps(safe_params(params)),
                "keyword": keyword,
                "job_clcd": find_first(record, "jobClcd"),
                "job_clcd_nm": find_first(record, "jobClcdNM", "jobClcdNm"),
                "job_cd": job_cd,
                "job_nm": find_first(record, "jobNm"),
                "raw_json": json_dumps(record),
            }
            insert_row(self.conn, "work24_occupation_items", row)

    def collect_occupation_dictionary(self) -> None:
        auth_key = require_env("JOBBRIDGE_WORK24_OCCUPATION_INFO_AUTH_KEY")
        url = env_url("JOBBRIDGE_WORK24_OCCUPATION_DICTIONARY_URL", DEFAULT_OCCUPATION_DICTIONARY_URL)
        keywords = split_csv(self.args.occupation_keywords or self.args.keywords, DEFAULT_KEYWORDS)
        run_id = start_run(
            self.conn,
            "work24_occupation_dictionary_items",
            {"keywords": keywords, "page_size": self.args.page_size, "limit_pages": self.args.limit_pages},
        )
        rows = 0
        pages = 0

        try:
            page_size = min(max(int(self.args.page_size), 1), 100)
            for keyword in keywords:
                base_params = {
                    "returnType": "XML",
                    "target": "dJobCD",
                    "display": page_size,
                    "srchType": "K",
                    "keyword": keyword,
                }
                first_params = dict(base_params, startPage=1)
                first_root = self.get_xml(url, auth_key, first_params)
                pages += 1
                total = text_int(xml_text(first_root, ".//total"))
                page_count = max(1, math.ceil(total / page_size)) if total else 1
                if self.args.limit_pages is not None:
                    page_count = min(page_count, int(self.args.limit_pages))
                records = xml_records_by_tag(first_root, "dJobList")
                rows += len(records)
                self.insert_occupation_dictionary_records(run_id, url, first_params, records, keyword, utc_now())

                for page_num in range(2, page_count + 1):
                    params = dict(base_params, startPage=page_num)
                    root = self.get_xml(url, auth_key, params)
                    pages += 1
                    records = xml_records_by_tag(root, "dJobList")
                    rows += len(records)
                    self.insert_occupation_dictionary_records(run_id, url, params, records, keyword, utc_now())
            self.conn.commit()
            finish_run(self.conn, run_id, "success", pages_requested=pages, rows_fetched=rows)
            print(f"work24_occupation_dictionary_items: rows={rows} calls={pages}")
        except Exception as exc:
            self.conn.rollback()
            finish_run(self.conn, run_id, "failed", pages_requested=pages, rows_fetched=rows, error=str(exc))
            raise

    def insert_occupation_dictionary_records(
        self,
        run_id: int,
        source_endpoint: str,
        params: dict[str, Any],
        records: list[dict[str, Any]],
        keyword: str,
        fetched_at: str,
    ) -> None:
        for index, record in enumerate(records):
            d_job_cd = find_first(record, "dJobCd")
            d_job_cd_seq = find_first(record, "dJobCdSeq")
            d_job_nm = find_first(record, "dJobNm")
            row = {
                "id": row_hash("occupation-dictionary", keyword, d_job_cd, d_job_cd_seq, d_job_nm, index),
                "sync_run_id": run_id,
                "fetched_at": fetched_at,
                "source_endpoint": source_endpoint,
                "source_params_json": json_dumps(safe_params(params)),
                "keyword": keyword,
                "d_job_cd": d_job_cd,
                "d_job_cd_seq": d_job_cd_seq,
                "d_job_nm": d_job_nm,
                "raw_json": json_dumps(record),
            }
            insert_row(self.conn, "work24_occupation_dictionary_items", row)


def parse_args(argv: list[str]) -> argparse.Namespace:
    today = dt.date.today()
    parser = argparse.ArgumentParser(
        description="Collect Work24 reference APIs into the JobBridge SQLite reference database."
    )
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--start-date", type=parse_date_arg, default=today)
    parser.add_argument("--end-date", type=parse_date_arg, default=today + dt.timedelta(days=365))
    parser.add_argument("--limit-pages", type=int, default=None)
    parser.add_argument("--limit-details", type=int, default=None)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--include-details", action="store_true")
    parser.add_argument("--only", action="append", help="Comma-separated targets or repeatable target values.")
    parser.add_argument("--keywords", action="append", help="Default keyword list for duty and occupation APIs.")
    parser.add_argument("--duty-keywords", action="append", help="Keyword list for duty dictionary API.")
    parser.add_argument("--occupation-keywords", action="append", help="Keyword list for occupation APIs.")
    parser.add_argument("--duty-limit", type=int, default=20)
    parser.add_argument("--common-code-types", action="append", help="Common-code srchType values. Default: 00..11.")
    args = parser.parse_args(argv)

    if args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date")
    if args.limit_pages is not None and args.limit_pages < 1:
        parser.error("--limit-pages must be >= 1")
    if args.limit_details is not None and args.limit_details < 1:
        parser.error("--limit-details must be >= 1")
    if args.page_size < 1:
        parser.error("--page-size must be >= 1")
    args.only_set = normalize_only(args.only)
    return args


def main(argv: list[str]) -> int:
    load_env_file(PROJECT_ROOT / ".env")
    args = parse_args(argv)

    conn = connect_db(args.db_path)
    collector = Work24Collector(conn, args)
    print(f"db_path={args.db_path}")

    course_records: list[dict[str, Any]] = []
    if "training" in args.only_set:
        course_records = collector.collect_training_courses()
        if args.include_details:
            collector.collect_training_details_and_schedules(course_records)
    if "competency" in args.only_set:
        collector.collect_competency_programs()
    if "common-codes" in args.only_set:
        collector.collect_common_codes()
    if "duty-dictionary" in args.only_set:
        collector.collect_duty_dictionary()
    if "occupation-items" in args.only_set:
        collector.collect_occupation_items()
    if "occupation-dictionary" in args.only_set:
        collector.collect_occupation_dictionary()

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
