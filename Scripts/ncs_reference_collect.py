from __future__ import annotations

import argparse
import csv
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
DEFAULT_REFERENCE_BASE_URL = "https://apis.data.go.kr/B490007/hrdkapi"
DEFAULT_GW_BASE_URL = "https://apis.data.go.kr/B490007/ncsInfo"
DEFAULT_QUALIFICATION_BASE_URL = "https://apis.data.go.kr/B490007/ncsClCdJm"

HRDK_REFERENCE_ENDPOINTS = ("NCS001", "NCS002", "NCS003", "NCS004", "NCS005", "NCS006")
HRDK_KEYWORD_ENDPOINT = "NCS007"
GW_ENDPOINTS = (
    "ncsCdInfo",
    "ncsDutyInfo",
    "ncsCompeUnitInfo",
    "ncsCompeUnitFactrInfo",
    "ncsKsaInfo",
)
QUALIFICATION_ENDPOINT = "getNcsClCdJmList"
DEFAULT_ONLY = ("local-csv", "hrdk", "gw", "qualification")


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


def default_standard_csv_path() -> Path:
    env_path = clean_text(os.getenv("JOBBRIDGE_NCS_STANDARD_CSV_PATH"))
    if env_path:
        return (PROJECT_ROOT / env_path).resolve() if not Path(env_path).is_absolute() else Path(env_path)

    doc_dir = PROJECT_ROOT / "Doc"
    matches = sorted(doc_dir.glob("*국가직무능력표준 정보_20251231.csv"))
    if matches:
        return matches[0]
    return doc_dir / "한국산업인력공단_국가직무능력표준 정보_20251231.csv"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def pick(row: dict[str, Any], *names: str) -> str | None:
    if not row:
        return None
    for name in names:
        if name in row:
            value = clean_text(row.get(name))
            if value:
                return value
    lookup = {canonical_key(str(key)): value for key, value in row.items()}
    for name in names:
        value = clean_text(lookup.get(canonical_key(name)))
        if value:
            return value
    return None


def raw_json(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def row_item_key(row: dict[str, Any], endpoint: str) -> str:
    parts = [
        endpoint,
        pick(row, "NCS_DEGR", "ncsDegr"),
        pick(row, "NCS_LCLAS_CD", "lclasCd", "lclas_cd"),
        pick(row, "NCS_MCLAS_CD", "mclasCd", "mclas_cd"),
        pick(row, "NCS_SCLAS_CD", "sclasCd", "sclas_cd"),
        pick(row, "NCS_SUBD_CD", "ncsSubdCd", "subdCd", "subd_cd", "dutyCd"),
        pick(row, "NCS_CL_CD", "ncsClCd", "ncs_cl_cd", "compUnitCd", "NCS_COMPE_UNIT_CD"),
        pick(row, "COMPE_UNIT_FACTR_NO", "compUnitFactrNo", "factorNo"),
        pick(row, "gbnCd", "gbnName", "gbnVal"),
        pick(row, "jmCd", "JM_CD"),
        pick(row, "분류번호"),
    ]
    filtered = [part for part in parts if part]
    if len(filtered) > 1:
        return "|".join(filtered)
    return f"{endpoint}|{stable_hash(raw_json(row))}"


def encode_service_key(service_key: str) -> str:
    safe_chars = "%" if "%" in service_key else ""
    return urllib.parse.quote(service_key, safe=safe_chars)


def join_endpoint(base_url: str, endpoint: str | None) -> str:
    if not endpoint:
        return base_url
    base = base_url.rstrip("/")
    if base.lower().endswith(f"/{endpoint.lower()}"):
        return base
    return f"{base}/{endpoint.lstrip('/')}"


def parse_format_value(format_value: str | None) -> dict[str, str]:
    value = clean_text(format_value)
    if not value:
        return {}
    if "=" in value:
        key, param_value = value.split("=", 1)
        return {key.strip(): normalize_format_token(param_value.strip(), "json")}
    return {"type": normalize_format_token(value, "json")}


def parse_return_type_value(format_value: str | None) -> dict[str, str]:
    value = clean_text(format_value)
    if not value:
        return {}
    if "=" in value:
        key, param_value = value.split("=", 1)
        return {key.strip(): normalize_format_token(param_value.strip(), "xml")}
    return {"returnType": normalize_format_token(value, "xml")}


def parse_data_format_value(format_value: str | None) -> dict[str, str]:
    value = clean_text(format_value)
    if not value:
        return {}
    if "=" in value:
        key, param_value = value.split("=", 1)
        return {"dataFormat": normalize_format_token(param_value.strip(), "json")}
    return {"dataFormat": normalize_format_token(value, "json")}


def normalize_format_token(value: str, default: str) -> str:
    token = value.strip()
    token_upper = token.upper().replace("-", "_")
    if token_upper in {"JSON_XML", "XML_JSON", "JSON/XML", "XML/JSON", "JSON+XML", "XML+JSON"}:
        return default
    if token_upper in {"JSON", "XML"}:
        return token.lower()
    return token


def build_api_url(
    base_url: str,
    endpoint: str | None,
    service_key: str,
    params: dict[str, Any] | None = None,
    format_value: str | None = None,
) -> str:
    url = join_endpoint(base_url, endpoint)
    parsed = urllib.parse.urlparse(url)
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query: dict[str, str] = {key: value for key, value in query_pairs if key.lower() != "servicekey"}
    for key, value in parse_format_value(format_value).items():
        query.setdefault(key, value)
    for key, value in (params or {}).items():
        if value is not None:
            query[key] = str(value)

    encoded_rest = urllib.parse.urlencode(query)
    service_key_part = f"serviceKey={encode_service_key(service_key)}"
    query_string = f"{service_key_part}&{encoded_rest}" if encoded_rest else service_key_part
    return urllib.parse.urlunparse(parsed._replace(query=query_string))


def request_bytes(url: str, timeout: float, user_agent: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def decode_body(body: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def case_get(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    lookup = {canonical_key(str(key)): value for key, value in mapping.items()}
    for name in names:
        key = canonical_key(name)
        if key in lookup:
            return lookup[key]
    return None


def find_number(node: Any, *names: str) -> int | None:
    if isinstance(node, dict):
        direct = case_get(node, *names)
        if direct is not None:
            try:
                return int(str(direct).strip())
            except ValueError:
                return None
        for value in node.values():
            found = find_number(value, *names)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_number(value, *names)
            if found is not None:
                return found
    return None


def find_string(node: Any, *names: str) -> str | None:
    if isinstance(node, dict):
        direct = case_get(node, *names)
        value = clean_text(direct)
        if value:
            return value
        for child in node.values():
            found = find_string(child, *names)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = find_string(child, *names)
            if found:
                return found
    return None


def looks_like_data_row(row: dict[str, Any]) -> bool:
    if not row:
        return False
    meta_keys = {
        "header",
        "body",
        "items",
        "item",
        "data",
        "dataInfo",
        "resultcode",
        "resultmsg",
        "code",
        "message",
        "pageNo",
        "numOfRows",
        "totalCount",
        "totalPage",
        "totCnt",
    }
    canonical_meta = {canonical_key(key) for key in meta_keys}
    keys = {canonical_key(str(key)) for key in row}
    if keys and keys <= canonical_meta:
        return False
    hints = {
        "ncsclcd",
        "ncsdegr",
        "ncslclascd",
        "ncsmclascd",
        "ncssclascd",
        "ncssubdcd",
        "compeunitname",
        "compunitname",
        "jmcd",
        "dutynm",
        "gbnval",
        "분류번호",
    }
    return bool(keys & hints) or all(not isinstance(value, (dict, list)) for value in row.values())


def normalize_json_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        rows = [row for row in value if isinstance(row, dict) and looks_like_data_row(row)]
        if rows:
            return rows
        nested: list[dict[str, Any]] = []
        for item in value:
            nested.extend(normalize_json_rows(item))
        return nested
    if isinstance(value, dict):
        for key in ("item", "items", "data", "list", "rows", "result", "resultList"):
            child = case_get(value, key)
            rows = normalize_json_rows(child)
            if rows:
                return rows
        if looks_like_data_row(value):
            return [value]
        for child in value.values():
            rows = normalize_json_rows(child)
            if rows:
                return rows
    return []


def inspect_json_error(data: Any, endpoint: str) -> None:
    result_code = find_string(data, "resultCode", "code", "messageCd", "returnCode")
    result_msg = find_string(data, "resultMsg", "message", "messageText", "returnAuthMsg")
    if not result_code:
        return
    ok_codes = {"0", "00", "000", "0000", "002", "03", "003", "normal_code", "success", "ok"}
    if result_code.strip().lower() not in ok_codes:
        message = result_msg or "no message"
        raise RuntimeError(f"{endpoint} API returned {result_code}: {message}")


def parse_json_response(text: str, endpoint: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = json.loads(text)
    inspect_json_error(data, endpoint)
    rows = normalize_json_rows(data)
    meta = {
        "endpoint": endpoint,
        "format": "json",
        "total_count": find_number(data, "totalCount", "totalCnt", "totCnt", "total"),
        "page_no": find_number(data, "pageNo", "page", "pageIndex"),
        "num_of_rows": find_number(data, "numOfRows", "numOfRow", "perPage", "count"),
        "items": len(rows),
    }
    return rows, meta


def xml_find_text(root: ET.Element, *names: str) -> str | None:
    wanted = {canonical_key(name) for name in names}
    for element in root.iter():
        if canonical_key(element.tag) in wanted:
            value = clean_text(element.text)
            if value:
                return value
    return None


def xml_find_int(root: ET.Element, *names: str) -> int | None:
    value = xml_find_text(root, *names)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def xml_element_to_dict(element: ET.Element) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for child in list(element):
        if len(list(child)) == 0:
            row[child.tag] = clean_text(child.text)
        else:
            row[child.tag] = xml_element_to_dict(child)
    return row


def candidate_xml_rows(root: ET.Element) -> list[ET.Element]:
    items = root.findall(".//item")
    if items:
        return items

    candidates: list[ET.Element] = []
    ignored = {"response", "header", "body", "items", "item", "data", "datainfo"}
    for element in root.iter():
        children = list(element)
        if not children or canonical_key(element.tag) in ignored:
            continue
        leaf_children = [child for child in children if len(list(child)) == 0 and clean_text(child.text)]
        if len(leaf_children) >= 2:
            candidates.append(element)
    if not candidates:
        return []

    by_tag: dict[str, list[ET.Element]] = {}
    for element in candidates:
        by_tag.setdefault(element.tag, []).append(element)
    return max(by_tag.values(), key=len)


def inspect_xml_error(root: ET.Element, endpoint: str) -> None:
    result_code = xml_find_text(root, "resultCode", "code", "messageCd", "returnCode")
    result_msg = xml_find_text(root, "resultMsg", "message", "messageText", "returnAuthMsg")
    if not result_code:
        return
    ok_codes = {"0", "00", "000", "0000", "002", "03", "003", "normal_code", "success", "ok"}
    if result_code.strip().lower() not in ok_codes:
        message = result_msg or "no message"
        raise RuntimeError(f"{endpoint} API returned {result_code}: {message}")


def parse_xml_response(text: str, endpoint: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = ET.fromstring(text)
    inspect_xml_error(root, endpoint)
    rows = [xml_element_to_dict(element) for element in candidate_xml_rows(root)]
    meta = {
        "endpoint": endpoint,
        "format": "xml",
        "total_count": xml_find_int(root, "totalCount", "totalCnt", "totCnt", "total"),
        "page_no": xml_find_int(root, "pageNo", "page", "pageIndex"),
        "num_of_rows": xml_find_int(root, "numOfRows", "numOfRow", "perPage", "count"),
        "items": len(rows),
    }
    return rows, meta


def parse_api_response(body: bytes, endpoint: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    text = decode_body(body).strip()
    if not text:
        return [], {"endpoint": endpoint, "format": "empty", "items": 0}
    if text.startswith("{") or text.startswith("["):
        return parse_json_response(text, endpoint)
    return parse_xml_response(text, endpoint)


def fetch_page(
    base_url: str,
    endpoint: str | None,
    service_key: str,
    page_no: int,
    num_of_rows: int,
    timeout: float,
    user_agent: str,
    format_value: str | None,
    extra_params: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params: dict[str, Any] = {"pageNo": page_no, "numOfRows": num_of_rows}
    if extra_params:
        params.update(extra_params)
    label = endpoint or Path(urllib.parse.urlparse(base_url).path).name or "endpoint"
    url = build_api_url(base_url, endpoint, service_key, params, format_value)
    try:
        body = request_bytes(url, timeout, user_agent)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{label} API HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{label} API request failed: {exc.reason}") from exc
    return parse_api_response(body, label)


def setup_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;

        CREATE TABLE IF NOT EXISTS ncs_reference_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_key TEXT NOT NULL,
            source TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            ncs_degr TEXT,
            ncs_lclas_cd TEXT,
            ncs_lclas_nm TEXT,
            ncs_mclas_cd TEXT,
            ncs_mclas_nm TEXT,
            ncs_sclas_cd TEXT,
            ncs_sclas_nm TEXT,
            ncs_subd_cd TEXT,
            ncs_subd_nm TEXT,
            ncs_cl_cd TEXT,
            code TEXT,
            name TEXT,
            raw_json TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            UNIQUE(source, endpoint, item_key)
        );

        CREATE TABLE IF NOT EXISTS ncs_competency_units (
            ncs_cl_cd TEXT NOT NULL,
            name TEXT,
            definition TEXT,
            level TEXT,
            training_hours TEXT,
            source TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            PRIMARY KEY (ncs_cl_cd, source)
        );

        CREATE TABLE IF NOT EXISTS ncs_competency_factors (
            ncs_cl_cd TEXT NOT NULL,
            factor_no TEXT NOT NULL,
            factor_name TEXT,
            factor_level TEXT,
            source TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            PRIMARY KEY (ncs_cl_cd, factor_no, source)
        );

        CREATE TABLE IF NOT EXISTS ncs_qualification_items (
            ncs_cl_cd TEXT NOT NULL,
            jm_cd TEXT NOT NULL,
            jm_nm TEXT,
            ablt_unit_type TEXT NOT NULL,
            min_training_time TEXT,
            raw_json TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            PRIMARY KEY (ncs_cl_cd, jm_cd, ablt_unit_type)
        );

        CREATE TABLE IF NOT EXISTS ncs_ksa_items (
            ncs_cl_cd TEXT NOT NULL,
            item_no TEXT NOT NULL,
            gbn_cd TEXT NOT NULL,
            gbn_name TEXT,
            gbn_val TEXT NOT NULL,
            source TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            PRIMARY KEY (ncs_cl_cd, item_no, gbn_cd, gbn_val, source)
        );

        CREATE TABLE IF NOT EXISTS api_sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            rows_inserted INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_ncs_reference_items_ncs_cl_cd
            ON ncs_reference_items(ncs_cl_cd);
        CREATE INDEX IF NOT EXISTS idx_ncs_competency_units_name
            ON ncs_competency_units(name);
        CREATE INDEX IF NOT EXISTS idx_ncs_qualification_items_jm_nm
            ON ncs_qualification_items(jm_nm);
        """
    )


def start_sync_run(conn: sqlite3.Connection, source: str) -> int:
    started_at = utc_now()
    cursor = conn.execute(
        "INSERT INTO api_sync_runs(source, started_at, status) VALUES (?, ?, ?)",
        (source, started_at, "running"),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_sync_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    rows_inserted: int,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE api_sync_runs
           SET finished_at = ?, status = ?, rows_inserted = ?, error = ?
         WHERE id = ?
        """,
        (utc_now(), status, rows_inserted, error, run_id),
    )
    conn.commit()


def insert_reference_rows(
    conn: sqlite3.Connection,
    source: str,
    endpoint: str,
    rows: list[dict[str, Any]],
    synced_at: str,
) -> int:
    inserted = 0
    for row in rows:
        ncs_cl_cd = pick(row, "NCS_CL_CD", "ncsClCd", "ncs_cl_cd")
        code = (
            ncs_cl_cd
            or pick(row, "NCS_SUBD_CD", "ncsSubdCd", "subdCd", "dutyCd")
            or pick(row, "NCS_SCLAS_CD", "ncsSclasCd", "sclasCd")
            or pick(row, "NCS_MCLAS_CD", "ncsMclasCd", "mclasCd")
            or pick(row, "NCS_LCLAS_CD", "ncsLclasCd", "lclasCd")
            or pick(row, "jmCd", "JM_CD")
        )
        name = (
            pick(row, "COMPE_UNIT_NAME", "compUnitName", "compeUnitName")
            or pick(row, "NCS_SUBD_CDNM", "ncsSubdCdNm", "subdNm", "dutyNm")
            or pick(row, "NCS_SCLAS_CDNM", "ncsSclasCdNm", "sclasNm")
            or pick(row, "NCS_MCLAS_CDNM", "ncsMclasCdNm", "mclasNm")
            or pick(row, "NCS_LCLAS_CDNM", "ncsLclasCdNm", "lclasNm")
            or pick(row, "jmNm", "JM_NM")
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO ncs_reference_items(
                item_key, source, endpoint, ncs_degr,
                ncs_lclas_cd, ncs_lclas_nm, ncs_mclas_cd, ncs_mclas_nm,
                ncs_sclas_cd, ncs_sclas_nm, ncs_subd_cd, ncs_subd_nm,
                ncs_cl_cd, code, name, raw_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_item_key(row, endpoint),
                source,
                endpoint,
                pick(row, "NCS_DEGR", "ncsDegr"),
                pick(row, "NCS_LCLAS_CD", "ncsLclasCd", "lclasCd"),
                pick(row, "NCS_LCLAS_CDNM", "ncsLclasCdNm", "lclasNm", "lclasName"),
                pick(row, "NCS_MCLAS_CD", "ncsMclasCd", "mclasCd"),
                pick(row, "NCS_MCLAS_CDNM", "ncsMclasCdNm", "mclasNm", "mclasName"),
                pick(row, "NCS_SCLAS_CD", "ncsSclasCd", "sclasCd"),
                pick(row, "NCS_SCLAS_CDNM", "ncsSclasCdNm", "sclasNm", "sclasName"),
                pick(row, "NCS_SUBD_CD", "ncsSubdCd", "subdCd", "dutyCd"),
                pick(row, "NCS_SUBD_CDNM", "ncsSubdCdNm", "subdNm", "dutyNm"),
                ncs_cl_cd,
                code,
                name,
                raw_json(row),
                synced_at,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def insert_competency_units(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    source: str,
    synced_at: str,
) -> int:
    inserted = 0
    for row in rows:
        ncs_cl_cd = pick(row, "NCS_CL_CD", "ncsClCd", "ncs_cl_cd", "compUnitCd", "NCS_COMPE_UNIT_CD", "분류번호")
        if not ncs_cl_cd:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO ncs_competency_units(
                ncs_cl_cd, name, definition, level, training_hours, source, raw_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ncs_cl_cd,
                pick(row, "COMPE_UNIT_NAME", "compUnitName", "compeUnitName", "명칭"),
                pick(row, "COMPE_UNIT_DEF", "compUnitDef", "compeUnitDef", "definition"),
                pick(row, "COMPE_UNIT_LEVEL", "compUnitLevel", "compeUnitLevel", "수준"),
                pick(row, "COMPE_UNIT_TRAIN_TIME", "trainingHours", "trainingTime", "훈련시간"),
                source,
                raw_json(row),
                synced_at,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def insert_competency_factors(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    source: str,
    synced_at: str,
) -> int:
    inserted = 0
    for row in rows:
        ncs_cl_cd = pick(row, "NCS_CL_CD", "ncsClCd", "ncs_cl_cd", "compUnitCd", "NCS_COMPE_UNIT_CD")
        factor_no = pick(row, "COMPE_UNIT_FACTR_NO", "compUnitFactrNo", "factorNo")
        if not ncs_cl_cd or not factor_no:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO ncs_competency_factors(
                ncs_cl_cd, factor_no, factor_name, factor_level, source, raw_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ncs_cl_cd,
                factor_no,
                pick(row, "COMPE_UNIT_FACTR_NAME", "compUnitFactrName", "factorName"),
                pick(row, "COMPE_UNIT_FACTR_LEVEL", "compUnitFactrLevel", "factorLevel"),
                source,
                raw_json(row),
                synced_at,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def insert_ksa_items(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    source: str,
    synced_at: str,
) -> int:
    inserted = 0
    for row in rows:
        ncs_cl_cd = pick(row, "NCS_CL_CD", "ncsClCd", "ncs_cl_cd", "compUnitCd", "NCS_COMPE_UNIT_CD")
        gbn_val = pick(row, "gbnVal", "GBN_VAL", "ksaVal", "content")
        if not ncs_cl_cd or not gbn_val:
            continue
        item_no = pick(row, "gbnNo", "itemNo", "seq", "no") or stable_hash(raw_json(row))[:16]
        gbn_cd = pick(row, "gbnCd", "GBN_CD", "ksaCd") or "-"
        conn.execute(
            """
            INSERT OR REPLACE INTO ncs_ksa_items(
                ncs_cl_cd, item_no, gbn_cd, gbn_name, gbn_val, source, raw_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ncs_cl_cd,
                item_no,
                gbn_cd,
                pick(row, "gbnName", "GBN_NAME", "ksaName"),
                gbn_val,
                source,
                raw_json(row),
                synced_at,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def insert_qualification_items(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    synced_at: str,
) -> int:
    inserted = 0
    for row in rows:
        ncs_cl_cd = pick(row, "ncsClCd", "NCS_CL_CD", "ncs_cl_cd")
        jm_cd = pick(row, "jmCd", "JM_CD")
        if not ncs_cl_cd or not jm_cd:
            continue
        ablt_unit_type = pick(row, "abltUnitTypNm", "abltUnitType", "ABLT_UNIT_TYPE") or "-"
        conn.execute(
            """
            INSERT OR REPLACE INTO ncs_qualification_items(
                ncs_cl_cd, jm_cd, jm_nm, ablt_unit_type, min_training_time, raw_json, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ncs_cl_cd,
                jm_cd,
                pick(row, "jmNm", "JM_NM"),
                ablt_unit_type,
                pick(row, "minEduTrngTm", "minTrainingTime", "MIN_TRAINING_TIME"),
                raw_json(row),
                synced_at,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def insert_rows_by_endpoint(
    conn: sqlite3.Connection,
    source: str,
    endpoint: str,
    rows: list[dict[str, Any]],
    synced_at: str,
) -> int:
    inserted = insert_reference_rows(conn, source, endpoint, rows, synced_at)
    if endpoint in {"NCS005", "ncsCompeUnitInfo"}:
        inserted += insert_competency_units(conn, rows, source, synced_at)
    elif endpoint in {"NCS006", "ncsCompeUnitFactrInfo"}:
        inserted += insert_competency_factors(conn, rows, source, synced_at)
    elif endpoint == "ncsKsaInfo":
        inserted += insert_ksa_items(conn, rows, source, synced_at)
    return inserted


def collect_paged_endpoint(
    conn: sqlite3.Connection,
    source: str,
    endpoint: str,
    base_url: str,
    service_key: str,
    num_of_rows: int,
    limit_pages: int | None,
    sleep_seconds: float,
    timeout: float,
    user_agent: str,
    format_value: str | None,
    extra_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_id = start_sync_run(conn, f"{source}:{endpoint}")
    rows_inserted = 0
    pages_fetched = 0
    rows_fetched = 0
    synced_at = utc_now()
    try:
        page_no = 1
        while True:
            if limit_pages is not None and page_no > limit_pages:
                break
            rows, meta = fetch_page(
                base_url,
                endpoint,
                service_key,
                page_no,
                num_of_rows,
                timeout,
                user_agent,
                format_value,
                extra_params,
            )
            pages_fetched += 1
            rows_fetched += len(rows)
            rows_inserted += insert_rows_by_endpoint(conn, source, endpoint, rows, synced_at)

            total_count = meta.get("total_count")
            page_size = meta.get("num_of_rows") or num_of_rows
            if not rows:
                break
            if total_count and page_size and page_no >= math.ceil(int(total_count) / int(page_size)):
                break
            page_no += 1
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        finish_sync_run(conn, run_id, "success", rows_inserted)
        return {
            "source": source,
            "endpoint": endpoint,
            "status": "success",
            "pages_fetched": pages_fetched,
            "rows_fetched": rows_fetched,
            "rows_inserted": rows_inserted,
        }
    except Exception as exc:
        finish_sync_run(conn, run_id, "failed", rows_inserted, str(exc))
        raise


def collect_paged_endpoint_for_param_sets(
    conn: sqlite3.Connection,
    source: str,
    endpoint: str,
    base_url: str,
    service_key: str,
    num_of_rows: int,
    limit_pages: int | None,
    sleep_seconds: float,
    timeout: float,
    user_agent: str,
    format_value: str | None,
    param_sets: list[dict[str, Any]],
) -> dict[str, Any]:
    run_id = start_sync_run(conn, f"{source}:{endpoint}")
    rows_inserted = 0
    pages_fetched = 0
    rows_fetched = 0
    synced_at = utc_now()
    try:
        for extra_params in param_sets:
            page_no = 1
            while True:
                if limit_pages is not None and page_no > limit_pages:
                    break
                rows, meta = fetch_page(
                    base_url,
                    endpoint,
                    service_key,
                    page_no,
                    num_of_rows,
                    timeout,
                    user_agent,
                    format_value,
                    extra_params,
                )
                pages_fetched += 1
                rows_fetched += len(rows)
                rows_inserted += insert_rows_by_endpoint(conn, source, endpoint, rows, synced_at)

                total_count = meta.get("total_count")
                page_size = meta.get("num_of_rows") or num_of_rows
                if not rows:
                    break
                if total_count and page_size and page_no >= math.ceil(int(total_count) / int(page_size)):
                    break
                page_no += 1
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
        finish_sync_run(conn, run_id, "success", rows_inserted)
        return {
            "source": source,
            "endpoint": endpoint,
            "status": "success",
            "param_sets": len(param_sets),
            "pages_fetched": pages_fetched,
            "rows_fetched": rows_fetched,
            "rows_inserted": rows_inserted,
        }
    except Exception as exc:
        finish_sync_run(conn, run_id, "failed", rows_inserted, str(exc))
        raise


def limit_param_sets(param_sets: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    if limit is None:
        return param_sets
    return param_sets[:limit]


def selected_values(only: list[str] | None) -> set[str]:
    if not only:
        return set(DEFAULT_ONLY)
    selected: set[str] = set()
    for value in only:
        for part in value.split(","):
            normalized = part.strip()
            if normalized:
                selected.add(normalized)
    if "all" in {value.lower() for value in selected}:
        return set(DEFAULT_ONLY)
    return selected


def includes_family(selected: set[str], family: str, endpoints: tuple[str, ...]) -> bool:
    lowered = {value.lower() for value in selected}
    return family.lower() in lowered or any(endpoint in selected for endpoint in endpoints)


def selected_endpoints(
    selected: set[str],
    family: str,
    endpoints: tuple[str, ...],
    include_all_when_family: bool = True,
) -> list[str]:
    if family.lower() in {value.lower() for value in selected} and include_all_when_family:
        return list(endpoints)
    explicit = [endpoint for endpoint in endpoints if endpoint in selected]
    return explicit


def distinct_tuples(conn: sqlite3.Connection, sql: str, limit: int | None = None) -> list[tuple[Any, ...]]:
    if limit is not None:
        sql = f"{sql} LIMIT ?"
        return [tuple(row) for row in conn.execute(sql, (limit,)).fetchall()]
    return [tuple(row) for row in conn.execute(sql).fetchall()]


def hrdk_param_sets(conn: sqlite3.Connection, endpoint: str, limit: int | None) -> list[dict[str, Any]]:
    if endpoint == "NCS001":
        return [{}]
    if endpoint == "NCS002":
        rows = distinct_tuples(
            conn,
            """
            SELECT DISTINCT ncs_lclas_cd
              FROM ncs_reference_items
             WHERE source = 'hrdk_reference'
               AND ncs_lclas_cd IS NOT NULL
               AND TRIM(ncs_lclas_cd) <> ''
             ORDER BY ncs_lclas_cd
            """,
            limit,
        )
        return [{"NCS_LCLAS_CD": row[0]} for row in rows]
    if endpoint == "NCS003":
        rows = distinct_tuples(
            conn,
            """
            SELECT DISTINCT ncs_lclas_cd, ncs_mclas_cd
              FROM ncs_reference_items
             WHERE source = 'hrdk_reference'
               AND ncs_lclas_cd IS NOT NULL
               AND ncs_mclas_cd IS NOT NULL
               AND TRIM(ncs_lclas_cd) <> ''
               AND TRIM(ncs_mclas_cd) <> ''
             ORDER BY ncs_lclas_cd, ncs_mclas_cd
            """,
            limit,
        )
        return [{"NCS_LCLAS_CD": row[0], "NCS_MCLAS_CD": row[1]} for row in rows]
    if endpoint == "NCS004":
        rows = distinct_tuples(
            conn,
            """
            SELECT DISTINCT ncs_lclas_cd, ncs_mclas_cd, ncs_sclas_cd
              FROM ncs_reference_items
             WHERE source = 'hrdk_reference'
               AND ncs_lclas_cd IS NOT NULL
               AND ncs_mclas_cd IS NOT NULL
               AND ncs_sclas_cd IS NOT NULL
               AND TRIM(ncs_lclas_cd) <> ''
               AND TRIM(ncs_mclas_cd) <> ''
               AND TRIM(ncs_sclas_cd) <> ''
             ORDER BY ncs_lclas_cd, ncs_mclas_cd, ncs_sclas_cd
            """,
            limit,
        )
        return [{"NCS_LCLAS_CD": row[0], "NCS_MCLAS_CD": row[1], "NCS_SCLAS_CD": row[2]} for row in rows]
    if endpoint == "NCS005":
        rows = distinct_tuples(
            conn,
            """
            SELECT DISTINCT ncs_lclas_cd, ncs_mclas_cd, ncs_sclas_cd, ncs_subd_cd
              FROM ncs_reference_items
             WHERE source = 'hrdk_reference'
               AND ncs_lclas_cd IS NOT NULL
               AND ncs_mclas_cd IS NOT NULL
               AND ncs_sclas_cd IS NOT NULL
               AND ncs_subd_cd IS NOT NULL
               AND TRIM(ncs_lclas_cd) <> ''
               AND TRIM(ncs_mclas_cd) <> ''
               AND TRIM(ncs_sclas_cd) <> ''
               AND TRIM(ncs_subd_cd) <> ''
             ORDER BY ncs_lclas_cd, ncs_mclas_cd, ncs_sclas_cd, ncs_subd_cd
            """,
            limit,
        )
        return [
            {
                "NCS_LCLAS_CD": row[0],
                "NCS_MCLAS_CD": row[1],
                "NCS_SCLAS_CD": row[2],
                "NCS_SUBD_CD": row[3],
            }
            for row in rows
        ]
    return [{}]


def collect_hrdk_reference(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    service_key: str,
    selected: set[str],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    endpoints = selected_endpoints(selected, "hrdk", HRDK_REFERENCE_ENDPOINTS)
    endpoints_without_factors = [endpoint for endpoint in endpoints if endpoint != "NCS006"]
    for endpoint in endpoints_without_factors:
        param_sets = hrdk_param_sets(conn, endpoint, args.limit_codes)
        if not param_sets:
            raise RuntimeError(f"{endpoint} requires parent NCS codes. Collect previous HRDK levels first.")
        summaries.append(
            collect_paged_endpoint_for_param_sets(
                conn,
                "hrdk_reference",
                endpoint,
                args.reference_base_url,
                service_key,
                args.num_of_rows,
                args.limit_pages,
                args.sleep,
                args.timeout_seconds,
                args.user_agent,
                args.reference_format,
                param_sets,
            )
        )

    if "NCS006" in endpoints:
        summaries.extend(collect_ncs006(conn, args, service_key))

    if args.include_keyword_search or HRDK_KEYWORD_ENDPOINT in selected:
        keywords = args.keyword or []
        if not keywords:
            raise RuntimeError("--keyword is required when collecting NCS007 keyword search")
        for keyword in keywords:
            summaries.append(
                collect_paged_endpoint(
                    conn,
                    "hrdk_reference",
                    HRDK_KEYWORD_ENDPOINT,
                    args.reference_base_url,
                    service_key,
                    args.num_of_rows,
                    args.limit_pages,
                    args.sleep,
                    args.timeout_seconds,
                    args.user_agent,
                    args.reference_format,
                    {
                        "LVL": args.keyword_level,
                        "SWRD": keyword,
                        "SNUM": args.keyword_start_record,
                        "ENUM": args.keyword_end_record,
                    },
                )
            )
    return summaries


def current_ncs_cl_codes(conn: sqlite3.Connection, limit: int | None = None) -> list[str]:
    sql = """
        SELECT DISTINCT ncs_cl_cd
          FROM ncs_competency_units
         WHERE ncs_cl_cd IS NOT NULL AND TRIM(ncs_cl_cd) <> ''
         ORDER BY ncs_cl_cd
    """
    if limit is not None:
        sql += " LIMIT ?"
        rows = conn.execute(sql, (limit,)).fetchall()
    else:
        rows = conn.execute(sql).fetchall()
    return [str(row[0]) for row in rows]


def current_gw_duty_codes(conn: sqlite3.Connection, limit: int | None = None) -> list[str]:
    sql = """
        SELECT DISTINCT code
          FROM ncs_reference_items
         WHERE source = 'ncs_gw'
           AND endpoint IN ('ncsCdInfo', 'ncsDutyInfo')
           AND code IS NOT NULL
           AND TRIM(code) <> ''
         ORDER BY code
    """
    if limit is not None:
        sql += " LIMIT ?"
        rows = conn.execute(sql, (limit,)).fetchall()
    else:
        rows = conn.execute(sql).fetchall()
    return [str(row[0]) for row in rows]


def collect_ncs006_by_unit(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    service_key: str,
    codes: list[str],
) -> dict[str, Any]:
    run_id = start_sync_run(conn, "hrdk_reference:NCS006:by-unit")
    rows_inserted = 0
    calls = 0
    rows_fetched = 0
    synced_at = utc_now()
    try:
        for ncs_cl_cd in codes:
            page_no = 1
            while True:
                if args.limit_pages is not None and page_no > args.limit_pages:
                    break
                rows, meta = fetch_page(
                    args.reference_base_url,
                    "NCS006",
                    service_key,
                    page_no,
                    args.num_of_rows,
                    args.timeout_seconds,
                    args.user_agent,
                    args.reference_format,
                    {"NCS_CL_CD": ncs_cl_cd},
                )
                calls += 1
                rows_fetched += len(rows)
                rows_inserted += insert_rows_by_endpoint(conn, "hrdk_reference", "NCS006", rows, synced_at)
                total_count = meta.get("total_count")
                page_size = meta.get("num_of_rows") or args.num_of_rows
                if not rows:
                    break
                if total_count and page_size and page_no >= math.ceil(int(total_count) / int(page_size)):
                    break
                page_no += 1
                if args.sleep > 0:
                    time.sleep(args.sleep)
        finish_sync_run(conn, run_id, "success", rows_inserted)
        return {
            "source": "hrdk_reference",
            "endpoint": "NCS006",
            "mode": "by-unit",
            "status": "success",
            "codes_called": calls,
            "rows_fetched": rows_fetched,
            "rows_inserted": rows_inserted,
        }
    except Exception as exc:
        finish_sync_run(conn, run_id, "failed", rows_inserted, str(exc))
        raise


def collect_ncs006(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    service_key: str,
) -> list[dict[str, Any]]:
    if args.factor_mode == "paged":
        return [
            collect_paged_endpoint(
                conn,
                "hrdk_reference",
                "NCS006",
                args.reference_base_url,
                service_key,
                args.num_of_rows,
                args.limit_pages,
                args.sleep,
                args.timeout_seconds,
                args.user_agent,
                args.reference_format,
            )
        ]

    codes = current_ncs_cl_codes(conn, args.limit_codes)
    if args.factor_mode == "by-unit":
        if not codes:
            raise RuntimeError("No ncs_cl_cd values found. Run NCS005 or local-csv first.")
        return [collect_ncs006_by_unit(conn, args, service_key, codes)]

    try:
        summary = collect_paged_endpoint(
            conn,
            "hrdk_reference",
            "NCS006",
            args.reference_base_url,
            service_key,
            args.num_of_rows,
            args.limit_pages,
            args.sleep,
            args.timeout_seconds,
            args.user_agent,
            args.reference_format,
        )
        if summary["rows_fetched"] > 0:
            return [summary]
    except Exception as exc:
        if not codes:
            raise RuntimeError(
                f"NCS006 paged collection failed and no ncs_cl_cd values are available: {exc}"
            ) from exc

    if not codes:
        return [
            {
                "source": "hrdk_reference",
                "endpoint": "NCS006",
                "mode": "auto",
                "status": "skipped",
                "reason": "paged collection returned no rows and no ncs_cl_cd values are available",
            }
        ]
    return [collect_ncs006_by_unit(conn, args, service_key, codes)]


def collect_gw(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    service_key: str,
    selected: set[str],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    endpoints = selected_endpoints(selected, "gw", GW_ENDPOINTS)
    base_params = parse_return_type_value(args.gw_format)
    for endpoint in [value for value in endpoints if value != "ncsKsaInfo"]:
        summaries.append(
            collect_paged_endpoint_for_param_sets(
                conn,
                "ncs_gw",
                endpoint,
                args.gw_base_url,
                service_key,
                args.num_of_rows,
                args.limit_pages,
                args.sleep,
                args.timeout_seconds,
                args.user_agent,
                None,
                [base_params],
            )
        )
    if "ncsKsaInfo" in endpoints:
        duty_codes = args.gw_duty_cd or current_gw_duty_codes(conn, args.limit_codes)
        if not duty_codes:
            raise RuntimeError("ncsKsaInfo requires dutyCd. Collect ncsCdInfo/ncsDutyInfo first or pass --gw-duty-cd.")
        param_sets = [dict(base_params, dutyCd=duty_cd) for duty_cd in duty_codes]
        summaries.append(
            collect_paged_endpoint_for_param_sets(
                conn,
                "ncs_gw",
                "ncsKsaInfo",
                args.gw_base_url,
                service_key,
                args.num_of_rows,
                args.limit_pages,
                args.sleep,
                args.timeout_seconds,
                args.user_agent,
                None,
                param_sets,
            )
        )
    return summaries


def collect_local_csv(conn: sqlite3.Connection, csv_path: Path) -> dict[str, Any]:
    run_id = start_sync_run(conn, "local_csv:ncs_standard")
    rows_inserted = 0
    synced_at = utc_now()
    try:
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV not found: {csv_path}")
        rows: list[dict[str, Any]] = []
        with csv_path.open("r", encoding="cp949", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                rows.append(dict(row))
        rows_inserted += insert_reference_rows(conn, "local_csv", "ncs_standard_csv", rows, synced_at)
        rows_inserted += insert_competency_units(conn, rows, "local_csv", synced_at)
        finish_sync_run(conn, run_id, "success", rows_inserted)
        return {
            "source": "local_csv",
            "endpoint": "ncs_standard_csv",
            "status": "success",
            "path": str(csv_path),
            "rows_fetched": len(rows),
            "rows_inserted": rows_inserted,
        }
    except Exception as exc:
        finish_sync_run(conn, run_id, "failed", rows_inserted, str(exc))
        raise


def qualification_list_url(args: argparse.Namespace) -> tuple[str, str | None]:
    if args.qualification_list_url:
        return args.qualification_list_url, None
    return args.qualification_base_url, QUALIFICATION_ENDPOINT


def collect_qualifications(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    service_key: str,
) -> dict[str, Any]:
    codes = current_ncs_cl_codes(conn, args.limit_codes)
    run_id = start_sync_run(conn, "qualification:ncsClCdJm")
    rows_inserted = 0
    rows_fetched = 0
    calls = 0
    synced_at = utc_now()
    try:
        if not codes:
            raise RuntimeError("No ncs_cl_cd values found. Run local-csv or NCS005 before qualification collection.")
        base_url, endpoint = qualification_list_url(args)
        format_params = parse_data_format_value(args.qualification_format)
        qualification_num_rows = min(args.num_of_rows, 50)
        for ncs_cl_cd in codes:
            page_no = 1
            while True:
                if args.limit_pages is not None and page_no > args.limit_pages:
                    break
                rows, meta = fetch_page(
                    base_url,
                    endpoint,
                    service_key,
                    page_no,
                    qualification_num_rows,
                    args.timeout_seconds,
                    args.user_agent,
                    None,
                    dict(format_params, ncsClCd=ncs_cl_cd),
                )
                calls += 1
                rows_fetched += len(rows)
                rows_inserted += insert_qualification_items(conn, rows, synced_at)
                total_count = meta.get("total_count")
                page_size = meta.get("num_of_rows") or qualification_num_rows
                if not rows:
                    break
                if total_count and page_size and page_no >= math.ceil(int(total_count) / int(page_size)):
                    break
                page_no += 1
                if args.sleep > 0:
                    time.sleep(args.sleep)
        finish_sync_run(conn, run_id, "success", rows_inserted)
        return {
            "source": "qualification",
            "endpoint": QUALIFICATION_ENDPOINT,
            "status": "success",
            "codes_called": len(codes),
            "requests": calls,
            "rows_fetched": rows_fetched,
            "rows_inserted": rows_inserted,
        }
    except Exception as exc:
        finish_sync_run(conn, run_id, "failed", rows_inserted, str(exc))
        raise


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "ncs_reference_items",
        "ncs_competency_units",
        "ncs_competency_factors",
        "ncs_ksa_items",
        "ncs_qualification_items",
        "api_sync_runs",
    )
    counts: dict[str, int] = {}
    for table in tables:
        counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return counts


def run_step(
    summary: list[dict[str, Any]],
    continue_on_error: bool,
    step_name: str,
    callback: Any,
) -> None:
    try:
        result = callback()
        if isinstance(result, list):
            summary.extend(result)
        else:
            summary.append(result)
    except Exception as exc:
        if not continue_on_error:
            raise
        summary.append({"step": step_name, "status": "failed", "error": str(exc)})


def run(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    selected = selected_values(args.only)
    summaries: list[dict[str, Any]] = []
    api_required = (
        includes_family(selected, "hrdk", HRDK_REFERENCE_ENDPOINTS)
        or HRDK_KEYWORD_ENDPOINT in selected
        or includes_family(selected, "gw", GW_ENDPOINTS)
        or "qualification" in {value.lower() for value in selected}
    )
    service_key = env_first("JOBBRIDGE_HRDK_SERVICE_KEY", "JOBBRIDGE_PUBLIC_DATA_SERVICE_KEY")
    if api_required and not service_key:
        raise RuntimeError("JOBBRIDGE_HRDK_SERVICE_KEY or JOBBRIDGE_PUBLIC_DATA_SERVICE_KEY is required")

    with sqlite3.connect(db_path) as conn:
        setup_db(conn)

        if "local-csv" in {value.lower() for value in selected}:
            run_step(
                summaries,
                args.continue_on_error,
                "local-csv",
                lambda: collect_local_csv(conn, Path(args.standard_csv)),
            )

        if service_key and (
            includes_family(selected, "hrdk", HRDK_REFERENCE_ENDPOINTS) or HRDK_KEYWORD_ENDPOINT in selected
        ):
            run_step(
                summaries,
                args.continue_on_error,
                "hrdk",
                lambda: collect_hrdk_reference(conn, args, service_key or "", selected),
            )

        if service_key and includes_family(selected, "gw", GW_ENDPOINTS):
            run_step(
                summaries,
                args.continue_on_error,
                "gw",
                lambda: collect_gw(conn, args, service_key or "", selected),
            )

        if service_key and "qualification" in {value.lower() for value in selected}:
            run_step(
                summaries,
                args.continue_on_error,
                "qualification",
                lambda: collect_qualifications(conn, args, service_key or ""),
            )

        counts = table_counts(conn)

    return {
        "status": "success",
        "db_path": str(db_path),
        "selected": sorted(selected),
        "limit_pages": args.limit_pages,
        "sleep": args.sleep,
        "steps": summaries,
        "table_counts": counts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect HRDK/NCS reference APIs and local NCS CSV into a local SQLite DB."
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--standard-csv", default=str(default_standard_csv_path()))
    parser.add_argument("--only", action="append", help="Limit collection. Examples: hrdk, gw, qualification, local-csv, NCS005, ncsKsaInfo.")
    parser.add_argument("--limit-pages", type=int, default=None, help="Limit pages per endpoint/code for smoke tests.")
    parser.add_argument("--sleep", type=float, default=0.0, help="Delay seconds between API calls.")
    parser.add_argument("--num-of-rows", type=int, default=int_from_env("JOBBRIDGE_NCS_NUM_OF_ROWS", 100))
    parser.add_argument("--limit-codes", type=int, default=None, help="Limit ncs_cl_cd loop count for NCS006 by-unit or qualification smoke tests.")
    parser.add_argument("--timeout-seconds", type=float, default=float_from_env("JOBBRIDGE_NCS_TIMEOUT_SECONDS", 30.0))
    parser.add_argument("--continue-on-error", action="store_true", help="Record failed endpoint runs and keep collecting remaining sources.")

    parser.add_argument("--reference-base-url", default=env_first("JOBBRIDGE_NCS_REFERENCE_BASE_URL") or DEFAULT_REFERENCE_BASE_URL)
    parser.add_argument("--reference-format", default=env_first("JOBBRIDGE_NCS_REFERENCE_FORMAT") or "json")
    parser.add_argument("--gw-base-url", default=env_first("JOBBRIDGE_NCS_GW_BASE_URL") or DEFAULT_GW_BASE_URL)
    parser.add_argument("--gw-format", default=env_first("JOBBRIDGE_NCS_GW_FORMAT") or "xml")
    parser.add_argument("--qualification-base-url", default=env_first("JOBBRIDGE_NCS_QUALIFICATION_BASE_URL") or DEFAULT_QUALIFICATION_BASE_URL)
    parser.add_argument("--qualification-list-url", default=env_first("JOBBRIDGE_NCS_QUALIFICATION_LIST_URL"))
    parser.add_argument("--qualification-format", default=env_first("JOBBRIDGE_NCS_QUALIFICATION_FORMAT") or "dataFormat=json")

    parser.add_argument("--factor-mode", choices=("auto", "paged", "by-unit"), default="auto")
    parser.add_argument("--include-keyword-search", action="store_true", help="Also call NCS007 keyword search. Requires --keyword.")
    parser.add_argument("--keyword", action="append", help="Keyword for NCS007. Repeat for multiple keywords.")
    parser.add_argument("--keyword-level", default="5", help="NCS007 LVL search target. Default 5 searches competency-unit area.")
    parser.add_argument("--keyword-start-record", type=int, default=1, help="NCS007 SNUM.")
    parser.add_argument("--keyword-end-record", type=int, default=100, help="NCS007 ENUM.")
    parser.add_argument("--gw-duty-cd", action="append", help="Limit GW ncsKsaInfo to one dutyCd. Repeat for multiple codes.")
    parser.add_argument("--user-agent", default="JobBridgeNcsReferenceCollector/0.1")
    return parser


def main() -> None:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--env-file", default=".env")
    pre_args, _remaining = pre_parser.parse_known_args()
    load_env_file(Path(pre_args.env_file))

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
