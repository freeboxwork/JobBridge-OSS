from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "Data" / "processed" / "reference" / "jobbridge_reference.db"
DEFAULT_NCS_CSV_GLOB = "한국산업인력공단_국가직무능력표준 정보_20251231.csv"
DEFAULT_WORK24_KEYWORDS = [
    "데이터",
    "사무",
    "복지",
    "청소",
    "제조",
    "디자인",
    "정보기술",
    "고객상담",
    "경비",
    "회계",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_yyyymmdd(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def load_env(path: Path = PROJECT_ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required in .env or environment")
    return value


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def text_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def int_value(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def request_text(url: str, timeout: int = 30) -> tuple[str, str | None]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "JobBridgeReferenceCollector/1.0",
            "Accept": "application/json, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
        content_type = response.headers.get("Content-Type")
    return body.decode("utf-8", errors="replace"), content_type


def build_url(base: str, params: dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    return f"{base}?{urllib.parse.urlencode(clean)}"


def xml_child_text(node: ET.Element, name: str) -> str | None:
    child = node.find(name)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text if text else None


def xml_to_dict(node: ET.Element) -> dict[str, Any]:
    children = list(node)
    if not children:
        return {node.tag: node.text.strip() if node.text else ""}
    data: dict[str, Any] = {}
    for child in children:
        value = xml_to_dict(child)
        child_value = value.get(child.tag)
        if child.tag in data:
            if not isinstance(data[child.tag], list):
                data[child.tag] = [data[child.tag]]
            data[child.tag].append(child_value)
        else:
            data[child.tag] = child_value
    return {node.tag: data}


def xml_children(root: ET.Element, path: str) -> list[ET.Element]:
    found = root.findall(path)
    return found if found else []


@contextmanager
def sync_run(conn: sqlite3.Connection, source: str, mode: str) -> Iterable[int]:
    started = utc_now()
    cur = conn.execute(
        """
        insert into api_sync_runs(source, mode, started_at, status, rows_inserted)
        values (?, ?, ?, 'running', 0)
        """,
        (source, mode, started),
    )
    run_id = int(cur.lastrowid)
    try:
        yield run_id
    except Exception as exc:
        conn.execute(
            """
            update api_sync_runs
            set finished_at = ?, status = 'failed', error = ?
            where id = ?
            """,
            (utc_now(), str(exc), run_id),
        )
        conn.commit()
        raise
    else:
        conn.execute(
            """
            update api_sync_runs
            set finished_at = ?, status = 'succeeded'
            where id = ?
            """,
            (utc_now(), run_id),
        )
        conn.commit()


def add_rows(conn: sqlite3.Connection, run_id: int, count: int) -> None:
    conn.execute(
        "update api_sync_runs set rows_inserted = rows_inserted + ? where id = ?",
        (count, run_id),
    )


def init_db(db_path: Path) -> sqlite3.Connection:
    ensure_parent(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma synchronous = normal")
    conn.executescript(
        """
        create table if not exists api_sync_runs (
          id integer primary key autoincrement,
          source text not null,
          mode text not null,
          started_at text not null,
          finished_at text,
          status text not null,
          rows_inserted integer not null default 0,
          error text
        );

        create table if not exists raw_api_items (
          source text not null,
          endpoint text not null,
          item_key text not null,
          fetched_at text not null,
          payload text not null,
          primary key (source, endpoint, item_key)
        );

        create table if not exists ncs_classifications (
          source text not null,
          ncs_degr integer,
          lclas_cd text,
          lclas_name text,
          mclas_cd text,
          mclas_name text,
          sclas_cd text,
          sclas_name text,
          subd_cd text,
          subd_name text,
          duty_def text,
          usage_yn text,
          raw_json text not null,
          synced_at text not null,
          primary key (source, ncs_degr, lclas_cd, mclas_cd, sclas_cd, subd_cd)
        );

        create table if not exists ncs_competency_units (
          ncs_cl_cd text primary key,
          compe_unit_cd text,
          name text,
          definition text,
          level integer,
          training_hours integer,
          ncs_degr integer,
          source text not null,
          raw_json text not null,
          synced_at text not null
        );

        create table if not exists ncs_competency_factors (
          ncs_cl_cd text not null,
          factor_no integer not null,
          factor_code text,
          factor_name text,
          factor_level integer,
          source text not null,
          raw_json text not null,
          synced_at text not null,
          primary key (ncs_cl_cd, factor_no, source)
        );

        create table if not exists ncs_ksa_items (
          ncs_cl_cd text not null,
          item_no integer,
          gbn_cd text,
          gbn_name text,
          gbn_val text,
          source text not null,
          raw_json text not null,
          synced_at text not null,
          primary key (ncs_cl_cd, item_no, gbn_cd, gbn_val, source)
        );

        create table if not exists ncs_qualification_items (
          ncs_cl_cd text not null,
          jm_cd text not null,
          jm_nm text,
          organ_std_ver_cd text,
          edu_training_hours integer,
          ablt_unit_type_cd text,
          ablt_unit_type_name text,
          min_training_time integer,
          source text not null,
          raw_json text not null,
          synced_at text not null,
          primary key (ncs_cl_cd, jm_cd, organ_std_ver_cd, ablt_unit_type_cd)
        );

        create table if not exists work24_training_courses (
          trpr_id text not null,
          trpr_degr text not null,
          trainst_cst_id text not null default '',
          title text,
          provider text,
          address text,
          ncs_cd text,
          train_target text,
          start_date text,
          end_date text,
          course_cost integer,
          real_cost integer,
          title_link text,
          raw_xml text not null,
          synced_at text not null,
          primary key (trpr_id, trpr_degr, trainst_cst_id)
        );

        create table if not exists work24_training_details (
          trpr_id text not null,
          trpr_degr text not null,
          trainst_cst_id text not null default '',
          title text,
          ncs_name text,
          total_training_time integer,
          raw_xml text not null,
          synced_at text not null,
          primary key (trpr_id, trpr_degr, trainst_cst_id)
        );

        create table if not exists work24_training_schedules (
          trpr_id text not null,
          trpr_degr text not null,
          start_date text,
          end_date text,
          title text,
          total_cost integer,
          capacity integer,
          raw_xml text not null,
          synced_at text not null,
          primary key (trpr_id, trpr_degr, start_date, end_date)
        );

        create table if not exists work24_competency_programs (
          org_nm text not null,
          pgm_nm text not null,
          pgm_sub_nm text not null default '',
          pgm_target text,
          start_date text not null,
          end_date text,
          open_time text not null default '',
          operation_time text,
          place text,
          raw_xml text not null,
          synced_at text not null,
          primary key (org_nm, pgm_nm, pgm_sub_nm, start_date, open_time)
        );

        create table if not exists work24_common_codes (
          srch_type text not null,
          code_name text,
          result_code text not null,
          result_name text,
          use_yn text,
          raw_xml text not null,
          synced_at text not null,
          primary key (srch_type, result_code)
        );

        create table if not exists work24_duty_dictionary (
          keyword text not null,
          center_label text not null,
          ablt_unit text,
          ablt_def text,
          job_lcfn text,
          job_mcn text,
          job_scfn text,
          job_sdvn text,
          raw_json text not null,
          synced_at text not null,
          primary key (keyword, center_label)
        );

        create table if not exists work24_occupation_items (
          keyword text not null,
          job_cd text not null,
          job_nm text,
          job_class_cd text,
          job_class_name text,
          raw_xml text not null,
          synced_at text not null,
          primary key (keyword, job_cd)
        );

        create table if not exists work24_occupation_dictionary_items (
          keyword text not null,
          d_job_cd text not null,
          d_job_cd_seq text not null default '',
          d_job_nm text,
          raw_xml text not null,
          synced_at text not null,
          primary key (keyword, d_job_cd, d_job_cd_seq)
        );

        create table if not exists jobbridge_job_to_ncs_map (
          target_job_class text not null,
          job_title text not null default '',
          reference_large text,
          reference_mid text,
          reference_small text,
          ncs_cl_cd text not null default '',
          ncs_cd_prefix text,
          mapping_method text not null,
          confidence real not null default 0,
          review_status text not null default 'candidate',
          raw_json text,
          synced_at text not null,
          primary key (target_job_class, job_title, ncs_cl_cd, mapping_method)
        );

        create index if not exists ncs_units_name_idx on ncs_competency_units(name);
        create index if not exists ncs_units_prefix_idx on ncs_competency_units(substr(ncs_cl_cd, 1, 8));
        create index if not exists training_ncs_idx on work24_training_courses(ncs_cd);
        create index if not exists duty_keyword_idx on work24_duty_dictionary(keyword);
        """
    )
    conn.commit()
    return conn


def upsert_raw(conn: sqlite3.Connection, source: str, endpoint: str, item_key: str, payload: Any) -> None:
    conn.execute(
        """
        insert into raw_api_items(source, endpoint, item_key, fetched_at, payload)
        values (?, ?, ?, ?, ?)
        on conflict(source, endpoint, item_key) do update set
          fetched_at = excluded.fetched_at,
          payload = excluded.payload
        """,
        (source, endpoint, item_key, utc_now(), json_dumps(payload)),
    )


def get_json_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = payload.get("response") or {}
    header = response.get("header") or payload.get("header") or {}
    result_code = str(header.get("resultCode") or "")
    if result_code and result_code not in {"00", "0"}:
        return []
    body = response.get("body") or payload.get("body") or {}
    items = body.get("items")
    if isinstance(items, dict):
        items = items.get("item")
    if isinstance(items, list):
        return [x for x in items if isinstance(x, dict)]
    if isinstance(items, dict):
        return [items]
    return []


def paged_hrdk_json(
    base_url: str,
    endpoint: str,
    service_key: str,
    extra: dict[str, Any] | None = None,
    page_size: int = 100,
    limit_pages: int | None = None,
    sleep_seconds: float = 0.0,
) -> Iterable[tuple[int, dict[str, Any], list[dict[str, Any]]]]:
    page = 1
    extra = extra or {}
    while True:
        params = {
            "serviceKey": service_key,
            "pageNo": page,
            "numOfRows": page_size,
            "type": "json",
            **extra,
        }
        url = build_url(f"{base_url.rstrip('/')}/{endpoint}", params)
        text, _ = request_text(url)
        payload = json.loads(text)
        items = get_json_items(payload)
        yield page, payload, items
        body = (payload.get("response") or {}).get("body") or payload.get("body") or {}
        total = int_value(body.get("totalCount")) or len(items)
        if page * page_size >= total or not items:
            break
        page += 1
        if limit_pages and page > limit_pages:
            break
        if sleep_seconds:
            time.sleep(sleep_seconds)


def upsert_ncs_classification(conn: sqlite3.Connection, source: str, row: dict[str, Any]) -> None:
    conn.execute(
        """
        insert into ncs_classifications(
          source, ncs_degr, lclas_cd, lclas_name, mclas_cd, mclas_name,
          sclas_cd, sclas_name, subd_cd, subd_name, duty_def, usage_yn, raw_json, synced_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(source, ncs_degr, lclas_cd, mclas_cd, sclas_cd, subd_cd) do update set
          lclas_name = excluded.lclas_name,
          mclas_name = excluded.mclas_name,
          sclas_name = excluded.sclas_name,
          subd_name = excluded.subd_name,
          duty_def = excluded.duty_def,
          usage_yn = excluded.usage_yn,
          raw_json = excluded.raw_json,
          synced_at = excluded.synced_at
        """,
        (
            source,
            int_value(row.get("NCS_DEGR")),
            text_value(row.get("NCS_LCLAS_CD")),
            text_value(row.get("NCS_LCLAS_CDNM")),
            text_value(row.get("NCS_MCLAS_CD")),
            text_value(row.get("NCS_MCLAS_CDNM")),
            text_value(row.get("NCS_SCLAS_CD")),
            text_value(row.get("NCS_SCLAS_CDNM")),
            text_value(row.get("NCS_SUBD_CD")),
            text_value(row.get("NCS_SUBD_CDNM")),
            text_value(row.get("DUTY_DEF")),
            text_value(row.get("USG_YN")),
            json_dumps(row),
            utc_now(),
        ),
    )


def upsert_ncs_unit(conn: sqlite3.Connection, source: str, row: dict[str, Any]) -> None:
    ncs_cl_cd = text_value(row.get("NCS_CL_CD") or row.get("분류번호"))
    if not ncs_cl_cd:
        return
    conn.execute(
        """
        insert into ncs_competency_units(
          ncs_cl_cd, compe_unit_cd, name, definition, level, training_hours,
          ncs_degr, source, raw_json, synced_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(ncs_cl_cd) do update set
          compe_unit_cd = coalesce(excluded.compe_unit_cd, ncs_competency_units.compe_unit_cd),
          name = coalesce(excluded.name, ncs_competency_units.name),
          definition = coalesce(excluded.definition, ncs_competency_units.definition),
          level = coalesce(excluded.level, ncs_competency_units.level),
          training_hours = coalesce(excluded.training_hours, ncs_competency_units.training_hours),
          ncs_degr = coalesce(excluded.ncs_degr, ncs_competency_units.ncs_degr),
          source = excluded.source,
          raw_json = excluded.raw_json,
          synced_at = excluded.synced_at
        """,
        (
            ncs_cl_cd,
            text_value(row.get("NCS_COMPE_UNIT_CD") or row.get("compUnitCd")),
            text_value(row.get("COMPE_UNIT_NAME") or row.get("compUnitName") or row.get("명칭")),
            text_value(row.get("COMPE_UNIT_DEF") or row.get("compUnitDef")),
            int_value(row.get("COMPE_UNIT_LEVEL") or row.get("수준")),
            int_value(row.get("훈련시간")),
            int_value(row.get("NCS_DEGR")),
            source,
            json_dumps(row),
            utc_now(),
        ),
    )


def upsert_ncs_factor(conn: sqlite3.Connection, source: str, row: dict[str, Any]) -> None:
    ncs_cl_cd = text_value(row.get("NCS_CL_CD") or row.get("ncsClCd"))
    factor_no = int_value(row.get("COMPE_UNIT_FACTR_NO") or row.get("compUnitFactrNo"))
    if not ncs_cl_cd or factor_no is None:
        return
    conn.execute(
        """
        insert into ncs_competency_factors(
          ncs_cl_cd, factor_no, factor_code, factor_name, factor_level,
          source, raw_json, synced_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(ncs_cl_cd, factor_no, source) do update set
          factor_code = excluded.factor_code,
          factor_name = excluded.factor_name,
          factor_level = excluded.factor_level,
          raw_json = excluded.raw_json,
          synced_at = excluded.synced_at
        """,
        (
            ncs_cl_cd,
            factor_no,
            text_value(row.get("COMPE_UNIT_FACTR_NO_CD") or row.get("compUnitFactrCd")),
            text_value(row.get("COMPE_UNIT_FACTR_NAME") or row.get("compUnitFactrName")),
            int_value(row.get("COMPE_UNIT_FACTR_LEVEL") or row.get("compUnitFactrLevel")),
            source,
            json_dumps(row),
            utc_now(),
        ),
    )


def upsert_ncs_qualification(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    ncs_cl_cd = text_value(row.get("ncsClCd"))
    jm_cd = text_value(row.get("jmCd"))
    if not ncs_cl_cd or not jm_cd:
        return
    conn.execute(
        """
        insert into ncs_qualification_items(
          ncs_cl_cd, jm_cd, jm_nm, organ_std_ver_cd, edu_training_hours,
          ablt_unit_type_cd, ablt_unit_type_name, min_training_time, source, raw_json, synced_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, 'hrdk_ncs_qualification', ?, ?)
        on conflict(ncs_cl_cd, jm_cd, organ_std_ver_cd, ablt_unit_type_cd) do update set
          jm_nm = excluded.jm_nm,
          edu_training_hours = excluded.edu_training_hours,
          ablt_unit_type_name = excluded.ablt_unit_type_name,
          min_training_time = excluded.min_training_time,
          raw_json = excluded.raw_json,
          synced_at = excluded.synced_at
        """,
        (
            ncs_cl_cd,
            jm_cd,
            text_value(row.get("jmNm")),
            text_value(row.get("organStdVerCd")),
            int_value(row.get("eduTrngStdTmSum")),
            text_value(row.get("abltUnitTypCd")),
            text_value(row.get("abltUnitTypNm")),
            int_value(row.get("minEduTrngTm")),
            json_dumps(row),
            utc_now(),
        ),
    )


def find_ncs_csv() -> Path | None:
    env_path = os.getenv("JOBBRIDGE_NCS_STANDARD_CSV_PATH")
    candidates: list[Path] = []
    if env_path:
        candidates.append(PROJECT_ROOT / env_path)
    candidates.extend((PROJECT_ROOT / "Doc").glob(DEFAULT_NCS_CSV_GLOB))
    candidates.extend((PROJECT_ROOT / "Doc").glob("*국가직무능력표준*20251231*.csv"))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_ncs_csv(conn: sqlite3.Connection, run_id: int) -> int:
    csv_path = find_ncs_csv()
    if not csv_path:
        return 0
    count = 0
    for encoding in ("cp949", "utf-8-sig", "utf-8"):
        try:
            with csv_path.open("r", encoding=encoding, newline="") as handle:
                rows = list(csv.DictReader(handle))
            break
        except UnicodeDecodeError:
            rows = []
            continue
    for row in rows:
        upsert_ncs_unit(conn, "ncs_standard_csv", row)
        upsert_raw(conn, "ncs_standard_csv", "local_csv", text_value(row.get("분류번호")) or str(count), row)
        count += 1
    add_rows(conn, run_id, count)
    conn.commit()
    return count


def collect_ncs(args: argparse.Namespace) -> None:
    service_key = env_required("JOBBRIDGE_HRDK_SERVICE_KEY")
    reference_base = os.getenv("JOBBRIDGE_NCS_REFERENCE_BASE_URL", "https://apis.data.go.kr/B490007/hrdkapi")
    qualification_url = os.getenv(
        "JOBBRIDGE_NCS_QUALIFICATION_LIST_URL",
        "https://apis.data.go.kr/B490007/ncsClCdJm/getNcsClCdJmList",
    )
    conn = init_db(args.db)
    with sync_run(conn, "ncs", args.mode) as run_id:
        load_ncs_csv(conn, run_id)
        if args.only and "csv" in args.only and len(args.only) == 1:
            return

        rows_by_endpoint: dict[str, list[dict[str, Any]]] = {}
        for endpoint in ["NCS001"]:
            rows: list[dict[str, Any]] = []
            for page, payload, items in paged_hrdk_json(
                reference_base,
                endpoint,
                service_key,
                page_size=args.page_size,
                limit_pages=args.limit_pages,
                sleep_seconds=args.sleep,
            ):
                upsert_raw(conn, "hrdkapi", endpoint, f"page:{page}", payload)
                for item in items:
                    upsert_ncs_classification(conn, "hrdkapi", item)
                rows.extend(items)
            rows_by_endpoint[endpoint] = rows
            add_rows(conn, run_id, len(rows))
            conn.commit()

        lclas_degrees: list[tuple[str, int]] = []
        latest_by_lclas: dict[str, int] = {}
        for row in rows_by_endpoint.get("NCS001", []):
            lclas = text_value(row.get("NCS_LCLAS_CD"))
            degr = int_value(row.get("NCS_DEGR"))
            if not lclas or degr is None:
                continue
            latest_by_lclas[lclas] = max(latest_by_lclas.get(lclas, 0), degr)
        for row in rows_by_endpoint.get("NCS001", []):
            lclas = text_value(row.get("NCS_LCLAS_CD"))
            degr = int_value(row.get("NCS_DEGR"))
            if not lclas or degr is None:
                continue
            if args.all_degrees or latest_by_lclas.get(lclas) == degr:
                lclas_degrees.append((lclas, degr))

        ncs002_rows: list[dict[str, Any]] = []
        if not args.only or "classifications" in args.only:
            for lclas, degr in lclas_degrees:
                extra = {"NCS_LCLAS_CD": lclas, "NCS_DEGR": degr}
                for page, payload, items in paged_hrdk_json(
                    reference_base,
                    "NCS002",
                    service_key,
                    extra,
                    args.page_size,
                    args.limit_pages,
                    args.sleep,
                ):
                    upsert_raw(conn, "hrdkapi", "NCS002", f"{lclas}:{degr}:page:{page}", payload)
                    for item in items:
                        upsert_ncs_classification(conn, "hrdkapi", item)
                    ncs002_rows.extend(items)
            add_rows(conn, run_id, len(ncs002_rows))
            conn.commit()

        ncs003_rows: list[dict[str, Any]] = []
        for row in ncs002_rows:
            extra = {
                "NCS_LCLAS_CD": row.get("NCS_LCLAS_CD"),
                "NCS_MCLAS_CD": row.get("NCS_MCLAS_CD"),
                "NCS_DEGR": row.get("NCS_DEGR"),
            }
            for page, payload, items in paged_hrdk_json(
                reference_base,
                "NCS003",
                service_key,
                extra,
                args.page_size,
                args.limit_pages,
                args.sleep,
            ):
                upsert_raw(conn, "hrdkapi", "NCS003", f"{json_dumps(extra)}:page:{page}", payload)
                for item in items:
                    upsert_ncs_classification(conn, "hrdkapi", item)
                ncs003_rows.extend(items)
        add_rows(conn, run_id, len(ncs003_rows))
        conn.commit()

        ncs004_rows: list[dict[str, Any]] = []
        for row in ncs003_rows:
            extra = {
                "NCS_LCLAS_CD": row.get("NCS_LCLAS_CD"),
                "NCS_MCLAS_CD": row.get("NCS_MCLAS_CD"),
                "NCS_SCLAS_CD": row.get("NCS_SCLAS_CD"),
                "NCS_DEGR": row.get("NCS_DEGR"),
            }
            for page, payload, items in paged_hrdk_json(
                reference_base,
                "NCS004",
                service_key,
                extra,
                args.page_size,
                args.limit_pages,
                args.sleep,
            ):
                upsert_raw(conn, "hrdkapi", "NCS004", f"{json_dumps(extra)}:page:{page}", payload)
                for item in items:
                    upsert_ncs_classification(conn, "hrdkapi", item)
                ncs004_rows.extend(items)
        add_rows(conn, run_id, len(ncs004_rows))
        conn.commit()

        ncs005_rows: list[dict[str, Any]] = []
        if not args.only or "units" in args.only:
            for row in ncs004_rows:
                extra = {
                    "NCS_LCLAS_CD": row.get("NCS_LCLAS_CD"),
                    "NCS_MCLAS_CD": row.get("NCS_MCLAS_CD"),
                    "NCS_SCLAS_CD": row.get("NCS_SCLAS_CD"),
                    "NCS_SUBD_CD": row.get("NCS_SUBD_CD"),
                    "NCS_DEGR": row.get("NCS_DEGR"),
                }
                for page, payload, items in paged_hrdk_json(
                    reference_base,
                    "NCS005",
                    service_key,
                    extra,
                    args.page_size,
                    args.limit_pages,
                    args.sleep,
                ):
                    upsert_raw(conn, "hrdkapi", "NCS005", f"{json_dumps(extra)}:page:{page}", payload)
                    for item in items:
                        upsert_ncs_unit(conn, "hrdkapi", item)
                    ncs005_rows.extend(items)
                    if args.limit_units and len(ncs005_rows) >= args.limit_units:
                        break
                if args.limit_units and len(ncs005_rows) >= args.limit_units:
                    break
            add_rows(conn, run_id, len(ncs005_rows))
            conn.commit()

        unit_codes = [
            row[0]
            for row in conn.execute(
                "select ncs_cl_cd from ncs_competency_units where ncs_cl_cd is not null order by ncs_cl_cd"
            ).fetchall()
        ]
        if args.limit_units:
            unit_codes = unit_codes[: args.limit_units]

        if not args.only or "factors" in args.only:
            factor_count = 0
            for code in unit_codes:
                for page, payload, items in paged_hrdk_json(
                    reference_base,
                    "NCS006",
                    service_key,
                    {"NCS_CL_CD": code},
                    args.page_size,
                    args.limit_pages,
                    args.sleep,
                ):
                    upsert_raw(conn, "hrdkapi", "NCS006", f"{code}:page:{page}", payload)
                    for item in items:
                        upsert_ncs_factor(conn, "hrdkapi", item)
                    factor_count += len(items)
                conn.commit()
            add_rows(conn, run_id, factor_count)

        if args.include_qualifications:
            qualification_count = 0
            for code in unit_codes:
                params = {
                    "serviceKey": service_key,
                    "numOfRows": args.page_size,
                    "pageNo": 1,
                    "dataFormat": "json",
                    "ncsClCd": code,
                }
                while True:
                    text, _ = request_text(build_url(qualification_url, params))
                    payload = json.loads(text)
                    upsert_raw(conn, "hrdk_ncs_qualification", "getNcsClCdJmList", f"{code}:page:{params['pageNo']}", payload)
                    items = payload.get("body", {}).get("items") or []
                    if isinstance(items, dict):
                        items = [items]
                    for item in items:
                        upsert_ncs_qualification(conn, item)
                    qualification_count += len(items)
                    total = int_value(payload.get("body", {}).get("totalCount")) or len(items)
                    if params["pageNo"] * args.page_size >= total or not items:
                        break
                    params["pageNo"] += 1
                    if args.limit_pages and params["pageNo"] > args.limit_pages:
                        break
                    if args.sleep:
                        time.sleep(args.sleep)
                conn.commit()
            add_rows(conn, run_id, qualification_count)


def work24_xml_request(url: str) -> ET.Element:
    text, _ = request_text(url)
    return ET.fromstring(text.strip())


def upsert_training_course(conn: sqlite3.Connection, item: ET.Element) -> None:
    trpr_id = xml_child_text(item, "trprId")
    trpr_degr = xml_child_text(item, "trprDegr")
    if not trpr_id or not trpr_degr:
        return
    raw = ET.tostring(item, encoding="unicode")
    conn.execute(
        """
        insert into work24_training_courses(
          trpr_id, trpr_degr, trainst_cst_id, title, provider, address, ncs_cd,
          train_target, start_date, end_date, course_cost, real_cost, title_link, raw_xml, synced_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(trpr_id, trpr_degr, trainst_cst_id) do update set
          title = excluded.title,
          provider = excluded.provider,
          address = excluded.address,
          ncs_cd = excluded.ncs_cd,
          train_target = excluded.train_target,
          start_date = excluded.start_date,
          end_date = excluded.end_date,
          course_cost = excluded.course_cost,
          real_cost = excluded.real_cost,
          title_link = excluded.title_link,
          raw_xml = excluded.raw_xml,
          synced_at = excluded.synced_at
        """,
        (
            trpr_id,
            trpr_degr,
            xml_child_text(item, "trainstCstId") or "",
            xml_child_text(item, "title"),
            xml_child_text(item, "subTitle"),
            xml_child_text(item, "address"),
            xml_child_text(item, "ncsCd"),
            xml_child_text(item, "trainTarget"),
            xml_child_text(item, "traStartDate"),
            xml_child_text(item, "traEndDate"),
            int_value(xml_child_text(item, "courseMan")),
            int_value(xml_child_text(item, "realMan")),
            xml_child_text(item, "titleLink"),
            raw,
            utc_now(),
        ),
    )


def collect_work24_training(conn: sqlite3.Connection, run_id: int, args: argparse.Namespace) -> int:
    key = env_required("JOBBRIDGE_WORK24_TRAINING_COURSE_AUTH_KEY")
    base = os.getenv(
        "JOBBRIDGE_WORK24_TRAINING_COURSE_LIST_URL",
        "https://www.work24.go.kr/cm/openApi/call/hr/callOpenApiSvcInfo310L01.do",
    )
    page = 1
    count = 0
    while True:
        params = {
            "authKey": key,
            "returnType": "XML",
            "outType": "1",
            "pageNum": page,
            "pageSize": args.page_size,
            "srchTraStDt": args.start_date,
            "srchTraEndDt": args.end_date,
            "sort": "ASC",
            "sortCol": "2",
        }
        root = work24_xml_request(build_url(base, params))
        items = xml_children(root, "./srchList/scn_list")
        for item in items:
            upsert_training_course(conn, item)
        count += len(items)
        total = int_value(xml_child_text(root, "scn_cnt")) or count
        conn.commit()
        if page * args.page_size >= total or not items:
            break
        page += 1
        if args.limit_pages and page > args.limit_pages:
            break
        if args.sleep:
            time.sleep(args.sleep)
    add_rows(conn, run_id, count)
    return count


def collect_work24_common_codes(conn: sqlite3.Connection, run_id: int) -> int:
    key = env_required("JOBBRIDGE_WORK24_COMMON_CODE_AUTH_KEY")
    base = os.getenv(
        "JOBBRIDGE_WORK24_COMMON_CODE_URL",
        "https://www.work24.go.kr/cm/openApi/call/hr/callOpenApiSvcInfo319L01.do",
    )
    count = 0
    for srch_type in [f"{idx:02d}" for idx in range(12)]:
        root = work24_xml_request(
            build_url(
                base,
                {
                    "authKey": key,
                    "returnType": "XML",
                    "outType": "1",
                    "srchType": srch_type,
                },
            )
        )
        code_name = xml_child_text(root, "codeName")
        for item in xml_children(root, "./srchList/scn_list"):
            result_code = xml_child_text(item, "rsltCode")
            if not result_code:
                continue
            raw = ET.tostring(item, encoding="unicode")
            conn.execute(
                """
                insert into work24_common_codes(
                  srch_type, code_name, result_code, result_name, use_yn, raw_xml, synced_at
                )
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(srch_type, result_code) do update set
                  code_name = excluded.code_name,
                  result_name = excluded.result_name,
                  use_yn = excluded.use_yn,
                  raw_xml = excluded.raw_xml,
                  synced_at = excluded.synced_at
                """,
                (
                    srch_type,
                    code_name,
                    result_code,
                    xml_child_text(item, "rsltName"),
                    xml_child_text(item, "useYn"),
                    raw,
                    utc_now(),
                ),
            )
            count += 1
        conn.commit()
    add_rows(conn, run_id, count)
    return count


def collect_work24_competency_programs(conn: sqlite3.Connection, run_id: int, args: argparse.Namespace) -> int:
    key = env_required("JOBBRIDGE_WORK24_JOBSEEKER_COMPETENCY_AUTH_KEY")
    base = os.getenv(
        "JOBBRIDGE_WORK24_JOBSEEKER_COMPETENCY_URL",
        "https://www.work24.go.kr/cm/openApi/call/wk/callOpenApiSvcInfo217L01.do",
    )
    start = parse_yyyymmdd(args.start_date)
    end = parse_yyyymmdd(args.end_date)
    current = start
    count = 0
    while current <= end:
        page = 1
        while True:
            root = work24_xml_request(
                build_url(
                    base,
                    {
                        "authKey": key,
                        "returnType": "XML",
                        "startPage": page,
                        "display": min(args.page_size, 100),
                        "pgmStdt": current.strftime("%Y%m%d"),
                    },
                )
            )
            message_code = xml_child_text(root, "messageCd")
            if message_code == "006":
                break
            items = xml_children(root, "./empPgmSchdInvite")
            for item in items:
                org_nm = xml_child_text(item, "orgNm")
                pgm_nm = xml_child_text(item, "pgmNm")
                start_date = xml_child_text(item, "pgmStdt")
                if not org_nm or not pgm_nm or not start_date:
                    continue
                raw = ET.tostring(item, encoding="unicode")
                conn.execute(
                    """
                    insert into work24_competency_programs(
                      org_nm, pgm_nm, pgm_sub_nm, pgm_target, start_date, end_date,
                      open_time, operation_time, place, raw_xml, synced_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(org_nm, pgm_nm, pgm_sub_nm, start_date, open_time) do update set
                      pgm_target = excluded.pgm_target,
                      end_date = excluded.end_date,
                      operation_time = excluded.operation_time,
                      place = excluded.place,
                      raw_xml = excluded.raw_xml,
                      synced_at = excluded.synced_at
                    """,
                    (
                        org_nm,
                        pgm_nm,
                        xml_child_text(item, "pgmSubNm") or "",
                        xml_child_text(item, "pgmTarget"),
                        start_date,
                        xml_child_text(item, "pgmEndt"),
                        xml_child_text(item, "openTime") or "",
                        xml_child_text(item, "operationTime"),
                        xml_child_text(item, "openPlcCont"),
                        raw,
                        utc_now(),
                    ),
                )
                count += 1
            total = int_value(xml_child_text(root, "total")) or count
            conn.commit()
            if page * min(args.page_size, 100) >= total or not items:
                break
            page += 1
            if args.limit_pages and page > args.limit_pages:
                break
            if args.sleep:
                time.sleep(args.sleep)
        current += timedelta(days=1)
    add_rows(conn, run_id, count)
    return count


def collect_work24_duty_dictionary(conn: sqlite3.Connection, run_id: int, keywords: list[str], limit: int) -> int:
    key = env_required("JOBBRIDGE_WORK24_DUTY_INFO_AUTH_KEY")
    base = os.getenv(
        "JOBBRIDGE_WORK24_DUTY_INFO_URL",
        "https://www.work24.go.kr/cm/openApi/call/wk/callOpenApiSvcInfo215L11.do",
    )
    count = 0
    for keyword in keywords:
        text, _ = request_text(
            build_url(
                base,
                {
                    "authKey": key,
                    "word": keyword,
                    "limit": limit,
                    "returnType": "JSON",
                },
            )
        )
        payload = json.loads(text)
        result = payload.get("result") or {}
        for center_label, item in result.items():
            if not isinstance(item, dict):
                continue
            conn.execute(
                """
                insert into work24_duty_dictionary(
                  keyword, center_label, ablt_unit, ablt_def, job_lcfn, job_mcn,
                  job_scfn, job_sdvn, raw_json, synced_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(keyword, center_label) do update set
                  ablt_unit = excluded.ablt_unit,
                  ablt_def = excluded.ablt_def,
                  job_lcfn = excluded.job_lcfn,
                  job_mcn = excluded.job_mcn,
                  job_scfn = excluded.job_scfn,
                  job_sdvn = excluded.job_sdvn,
                  raw_json = excluded.raw_json,
                  synced_at = excluded.synced_at
                """,
                (
                    keyword,
                    center_label,
                    text_value(item.get("ablt_unit")),
                    text_value(item.get("ablt_def")),
                    text_value(item.get("job_lcfn")),
                    text_value(item.get("job_mcn")),
                    text_value(item.get("job_scfn")),
                    text_value(item.get("job_sdvn")),
                    json_dumps(item),
                    utc_now(),
                ),
            )
            count += 1
        conn.commit()
    add_rows(conn, run_id, count)
    return count


def collect_work24_occupations(conn: sqlite3.Connection, run_id: int, keywords: list[str], dictionary: bool = False) -> int:
    key = env_required("JOBBRIDGE_WORK24_OCCUPATION_INFO_AUTH_KEY")
    if dictionary:
        base = os.getenv(
            "JOBBRIDGE_WORK24_OCCUPATION_DICTIONARY_URL",
            "https://www.work24.go.kr/cm/openApi/call/wk/callOpenApiSvcInfo212L50.do",
        )
    else:
        base = os.getenv(
            "JOBBRIDGE_WORK24_OCCUPATION_INFO_URL",
            "https://www.work24.go.kr/cm/openApi/call/wk/callOpenApiSvcInfo212L01.do",
        )
    count = 0
    for keyword in keywords:
        if dictionary:
            params = {
                "authKey": key,
                "returnType": "XML",
                "target": "dJobCD",
                "startPage": 1,
                "display": 100,
                "srchType": "K",
                "keyword": keyword,
            }
            root = work24_xml_request(build_url(base, params))
            for item in xml_children(root, "./dJobList"):
                d_job_cd = xml_child_text(item, "dJobCd")
                if not d_job_cd:
                    continue
                raw = ET.tostring(item, encoding="unicode")
                conn.execute(
                    """
                    insert into work24_occupation_dictionary_items(
                      keyword, d_job_cd, d_job_cd_seq, d_job_nm, raw_xml, synced_at
                    )
                    values (?, ?, ?, ?, ?, ?)
                    on conflict(keyword, d_job_cd, d_job_cd_seq) do update set
                      d_job_nm = excluded.d_job_nm,
                      raw_xml = excluded.raw_xml,
                      synced_at = excluded.synced_at
                    """,
                    (
                        keyword,
                        d_job_cd,
                        xml_child_text(item, "dJobCdSeq") or "",
                        xml_child_text(item, "dJobNm"),
                        raw,
                        utc_now(),
                    ),
                )
                count += 1
        else:
            root = work24_xml_request(
                build_url(
                    base,
                    {
                        "authKey": key,
                        "returnType": "XML",
                        "target": "JOBCD",
                        "srchType": "K",
                        "keyword": keyword,
                    },
                )
            )
            for item in xml_children(root, "./jobList"):
                job_cd = xml_child_text(item, "jobCd")
                if not job_cd:
                    continue
                raw = ET.tostring(item, encoding="unicode")
                conn.execute(
                    """
                    insert into work24_occupation_items(
                      keyword, job_cd, job_nm, job_class_cd, job_class_name, raw_xml, synced_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?)
                    on conflict(keyword, job_cd) do update set
                      job_nm = excluded.job_nm,
                      job_class_cd = excluded.job_class_cd,
                      job_class_name = excluded.job_class_name,
                      raw_xml = excluded.raw_xml,
                      synced_at = excluded.synced_at
                    """,
                    (
                        keyword,
                        job_cd,
                        xml_child_text(item, "jobNm"),
                        xml_child_text(item, "jobClcd"),
                        xml_child_text(item, "jobClcdNM"),
                        raw,
                        utc_now(),
                    ),
                )
                count += 1
        conn.commit()
    add_rows(conn, run_id, count)
    return count


def collect_work24(args: argparse.Namespace) -> None:
    conn = init_db(args.db)
    keywords = args.keywords or DEFAULT_WORK24_KEYWORDS
    with sync_run(conn, "work24", args.mode) as run_id:
        only = set(args.only or [])
        if not only or "training" in only:
            collect_work24_training(conn, run_id, args)
        if not only or "common" in only:
            collect_work24_common_codes(conn, run_id)
        if not only or "programs" in only:
            collect_work24_competency_programs(conn, run_id, args)
        if not only or "duty" in only:
            collect_work24_duty_dictionary(conn, run_id, keywords, args.keyword_limit)
        if not only or "occupation" in only:
            collect_work24_occupations(conn, run_id, keywords, dictionary=False)
        if not only or "occupation_dictionary" in only:
            collect_work24_occupations(conn, run_id, keywords, dictionary=True)


def print_summary(db_path: Path) -> None:
    conn = init_db(db_path)
    tables = [
        "ncs_classifications",
        "ncs_competency_units",
        "ncs_competency_factors",
        "ncs_qualification_items",
        "work24_training_courses",
        "work24_training_details",
        "work24_training_schedules",
        "work24_competency_programs",
        "work24_common_codes",
        "work24_duty_dictionary",
        "work24_occupation_items",
        "work24_occupation_dictionary_items",
        "jobbridge_job_to_ncs_map",
        "raw_api_items",
        "api_sync_runs",
    ]
    for table in tables:
        count = conn.execute(f"select count(*) from {table}").fetchone()[0]
        print(f"{table}: {count}")


def build_parser() -> argparse.ArgumentParser:
    today = date.today()
    parser = argparse.ArgumentParser(description="Collect JobBridge reference API data into SQLite.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    ncs = sub.add_parser("ncs")
    ncs.add_argument("--mode", default="ncs")
    ncs.add_argument("--page-size", type=int, default=100)
    ncs.add_argument("--limit-pages", type=int)
    ncs.add_argument("--limit-units", type=int)
    ncs.add_argument("--sleep", type=float, default=0.0)
    ncs.add_argument("--all-degrees", action="store_true")
    ncs.add_argument("--include-qualifications", action="store_true")
    ncs.add_argument("--only", nargs="+", choices=["csv", "classifications", "units", "factors"])

    work24 = sub.add_parser("work24")
    work24.add_argument("--mode", default="work24")
    work24.add_argument("--page-size", type=int, default=100)
    work24.add_argument("--limit-pages", type=int)
    work24.add_argument("--sleep", type=float, default=0.0)
    work24.add_argument("--start-date", default=today.strftime("%Y%m%d"))
    work24.add_argument("--end-date", default=(today + timedelta(days=365)).strftime("%Y%m%d"))
    work24.add_argument("--keywords", nargs="+")
    work24.add_argument("--keyword-limit", type=int, default=20)
    work24.add_argument(
        "--only",
        nargs="+",
        choices=["training", "common", "programs", "duty", "occupation", "occupation_dictionary"],
    )

    all_cmd = sub.add_parser("all")
    all_cmd.add_argument("--mode", default="all")
    all_cmd.add_argument("--page-size", type=int, default=100)
    all_cmd.add_argument("--limit-pages", type=int)
    all_cmd.add_argument("--limit-units", type=int)
    all_cmd.add_argument("--sleep", type=float, default=0.0)
    all_cmd.add_argument("--start-date", default=today.strftime("%Y%m%d"))
    all_cmd.add_argument("--end-date", default=(today + timedelta(days=365)).strftime("%Y%m%d"))
    all_cmd.add_argument("--keywords", nargs="+")
    all_cmd.add_argument("--keyword-limit", type=int, default=20)
    all_cmd.add_argument("--include-qualifications", action="store_true")

    sub.add_parser("summary")
    return parser


def main() -> None:
    load_env()
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "ncs":
        collect_ncs(args)
    elif args.command == "work24":
        collect_work24(args)
    elif args.command == "all":
        collect_ncs(
            argparse.Namespace(
                db=args.db,
                mode=args.mode,
                page_size=args.page_size,
                limit_pages=args.limit_pages,
                limit_units=args.limit_units,
                sleep=args.sleep,
                all_degrees=False,
                include_qualifications=args.include_qualifications,
                only=None,
            )
        )
        collect_work24(
            argparse.Namespace(
                db=args.db,
                mode=args.mode,
                page_size=args.page_size,
                limit_pages=args.limit_pages,
                sleep=args.sleep,
                start_date=args.start_date,
                end_date=args.end_date,
                keywords=args.keywords,
                keyword_limit=args.keyword_limit,
                only=None,
            )
        )
    elif args.command == "summary":
        print_summary(args.db)


if __name__ == "__main__":
    main()
