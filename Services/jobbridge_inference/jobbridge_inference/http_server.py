from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from .auth_admin import SupabaseAuthAdmin
from .core import PROJECT_ROOT, JobBridgeInferenceService
from .persistence import SupabaseRecorder


SITE_DIR = PROJECT_ROOT / "Site"


def load_project_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


load_project_env()
SERVICE = JobBridgeInferenceService()
RECORDER = SupabaseRecorder.from_env()
AUTH_ADMIN = SupabaseAuthAdmin.from_env()
SYNC_LOCK = Lock()


def vworld_api_key() -> str:
    return (os.getenv("JOBBRIDGE_VWORLD_API_KEY") or os.getenv("VWORLD_API_KEY") or "").strip()


def supabase_public_config() -> dict[str, Any]:
    url = (os.getenv("SUPABASE_URL") or os.getenv("JOBBRIDGE_SUPABASE_URL") or "").strip().rstrip("/")
    anon_key = (
        os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("JOBBRIDGE_SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or os.getenv("JOBBRIDGE_SUPABASE_PUBLISHABLE_KEY")
        or ""
    ).strip()
    return {
        "ok": bool(url and anon_key),
        "hasUrl": bool(url),
        "hasAnonKey": bool(anon_key),
        "supabaseUrl": url,
        "supabaseAnonKey": anon_key,
        "secretsExposed": False,
    }


def admin_token() -> str:
    return (os.getenv("JOBBRIDGE_ADMIN_TOKEN") or "").strip()


def header_admin_token(headers: Any) -> str:
    auth_header = (headers.get("Authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return (headers.get("x-jobbridge-admin-token") or "").strip()


def is_local_client(client_address: tuple[str, int] | Any) -> bool:
    host = client_address[0] if client_address else ""
    return host in {"127.0.0.1", "::1", "localhost"}


def vworld_script_url(key: str) -> str:
    return f"https://map.vworld.kr/js/vworldMapInit.js.do?version=2.0&apiKey={key}"


def vworld_loader_script() -> str:
    key = vworld_api_key()
    if not key:
        return 'window.JOBBRIDGE_VWORLD_READY=false;console.warn("JOBBRIDGE_VWORLD_API_KEY is not configured");'
    script_url = vworld_script_url(key).replace("\\", "\\\\").replace('"', '\\"')
    return (
        'window.JOBBRIDGE_VWORLD_READY=false;'
        'window.JOBBRIDGE_MAP_PROVIDER="vworld";'
        f'document.write("<script src=\\"{script_url}\\"></scr" + "ipt>");'
    )


def fetch_vworld_json(path: str, params: dict[str, Any], timeout_seconds: int = 8) -> dict[str, Any]:
    key = vworld_api_key()
    if not key:
        raise RuntimeError("JOBBRIDGE_VWORLD_API_KEY is not configured")
    request_params = {**params, "key": key}
    url = f"https://api.vworld.kr{path}?{urlencode(request_params)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"VWorld API HTTP {exc.code}: {limited_preview(detail, 240)}") from exc
    return json.loads(body or "{}")


def parse_vworld_point(point: dict[str, Any] | None) -> tuple[float | None, float | None]:
    if not isinstance(point, dict):
        return None, None
    try:
        lng = float(point.get("x"))
        lat = float(point.get("y"))
    except (TypeError, ValueError):
        return None, None
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        return None, None
    return lat, lng


def vworld_address_coord(address: str, address_type: str) -> dict[str, Any] | None:
    data = fetch_vworld_json(
        "/req/address",
        {
            "service": "address",
            "request": "getCoord",
            "version": "2.0",
            "crs": "EPSG:4326",
            "address": address,
            "refine": "true",
            "simple": "false",
            "format": "json",
            "type": address_type,
        },
    )
    response = data.get("response") if isinstance(data, dict) else {}
    result = response.get("result") if isinstance(response, dict) else {}
    lat, lng = parse_vworld_point(result.get("point") if isinstance(result, dict) else None)
    if lat is None or lng is None:
        return None
    refined = response.get("refined") if isinstance(response, dict) else {}
    return {
        "lat": lat,
        "lng": lng,
        "label": (refined or {}).get("text") or address,
        "source": "vworld_geocoder",
        "matchType": address_type.lower(),
    }


def vworld_search_coord(address: str, category: str) -> dict[str, Any] | None:
    data = fetch_vworld_json(
        "/req/search",
        {
            "service": "search",
            "request": "search",
            "version": "2.0",
            "crs": "EPSG:4326",
            "size": "1",
            "page": "1",
            "query": address,
            "type": "address",
            "category": category,
            "format": "json",
            "errorformat": "json",
        },
    )
    response = data.get("response") if isinstance(data, dict) else {}
    result = response.get("result") if isinstance(response, dict) else {}
    items = result.get("items") if isinstance(result, dict) else []
    item = items[0] if isinstance(items, list) and items else None
    if not isinstance(item, dict):
        return None
    lat, lng = parse_vworld_point(item.get("point"))
    if lat is None or lng is None:
        return None
    item_address = item.get("address") if isinstance(item.get("address"), dict) else {}
    return {
        "lat": lat,
        "lng": lng,
        "label": item_address.get("road") or item_address.get("parcel") or item.get("title") or address,
        "source": "vworld_search",
        "matchType": category,
    }


def geocode_vworld_address(address: str) -> dict[str, Any]:
    clean = re.sub(r"\s+", " ", address or "").strip()
    if not clean:
        raise ValueError("address is required")
    without_parens = re.sub(r"\s*\([^)]*\)\s*", " ", clean).strip()
    queries = [clean]
    if without_parens and without_parens != clean:
        queries.append(without_parens)

    road_like = bool(re.search(r"(로|길)\s*\d", clean))
    address_types = ["ROAD", "PARCEL"] if road_like else ["PARCEL", "ROAD"]
    search_categories = ["road", "parcel"] if road_like else ["parcel", "road"]

    attempts: list[dict[str, str]] = []
    for query in queries:
        for address_type in address_types:
            attempts.append({"query": query, "kind": "geocoder", "type": address_type})
            point = vworld_address_coord(query, address_type)
            if point:
                return {"ok": True, "query": clean, **point}
        for category in search_categories:
            attempts.append({"query": query, "kind": "search", "type": category})
            point = vworld_search_coord(query, category)
            if point:
                return {"ok": True, "query": clean, **point}
    return {"ok": False, "query": clean, "error": "VWorld did not return coordinates", "attempts": attempts}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def redact_secrets(text: str) -> str:
    redacted = text or ""
    for key, value in os.environ.items():
        key_upper = key.upper()
        if not value or len(value) < 8:
            continue
        if any(token in key_upper for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            redacted = redacted.replace(value, "[redacted]")
    return redacted


def limited_preview(text: str, limit: int = 700) -> str:
    clean = redact_secrets(text).strip()
    if len(clean) <= limit:
        return clean
    return f"{clean[:limit]}..."


def sync_live_jobs(timeout_seconds: int = 240) -> tuple[HTTPStatus, dict[str, Any]]:
    if not SYNC_LOCK.acquire(blocking=False):
        return HTTPStatus.CONFLICT, {
            "ok": False,
            "status": "busy",
            "message": "live job sync is already running",
        }

    started = time.perf_counter()
    started_at = utc_now_iso()
    script_path = PROJECT_ROOT / "Scripts" / "collect_kead_live_jobs.py"
    command = [sys.executable, str(script_path)]
    try:
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(30, min(int(timeout_seconds), 600)),
        )
        summary = parse_sync_summary(result.stdout)
        ok = result.returncode == 0
        if ok:
            SERVICE.reset_loaded_state()
        payload = {
            "ok": ok,
            "status": "completed" if ok else "failed",
            "startedAt": started_at,
            "finishedAt": utc_now_iso(),
            "durationMs": round((time.perf_counter() - started) * 1000, 2),
            "exitCode": result.returncode,
            "summary": summary,
            "message": sync_message(ok, summary),
        }
        if not ok:
            payload["errorPreview"] = limited_preview(result.stderr or result.stdout)
        return (HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY), payload
    except subprocess.TimeoutExpired as exc:
        return HTTPStatus.GATEWAY_TIMEOUT, {
            "ok": False,
            "status": "timeout",
            "startedAt": started_at,
            "finishedAt": utc_now_iso(),
            "durationMs": round((time.perf_counter() - started) * 1000, 2),
            "message": f"live job sync exceeded {timeout_seconds} seconds",
            "errorPreview": limited_preview((exc.stderr or exc.stdout or "") if isinstance(exc.stderr or exc.stdout, str) else ""),
        }
    except Exception as exc:
        return HTTPStatus.INTERNAL_SERVER_ERROR, {
            "ok": False,
            "status": "error",
            "startedAt": started_at,
            "finishedAt": utc_now_iso(),
            "durationMs": round((time.perf_counter() - started) * 1000, 2),
            "message": limited_preview(str(exc), 300),
        }
    finally:
        SYNC_LOCK.release()


def parse_sync_summary(stdout: str) -> dict[str, Any]:
    try:
        raw = json.loads(stdout.strip() or "{}")
    except json.JSONDecodeError:
        raw = {}
    supabase = raw.get("supabase") if isinstance(raw.get("supabase"), dict) else {}
    return {
        "fetchedAt": raw.get("fetched_at"),
        "apiBaseUrl": raw.get("api_base_url"),
        "endpointCount": len(raw.get("endpoints") or []),
        "endpoints": [
            {
                "endpoint": item.get("endpoint"),
                "totalCount": item.get("total_count"),
                "fetchedRows": item.get("fetched_rows"),
                "requestedPages": item.get("requested_pages"),
            }
            for item in (raw.get("endpoints") or [])
            if isinstance(item, dict)
        ],
        "mergedRows": raw.get("merged_rows"),
        "normalizedPayloads": raw.get("normalized_payloads"),
        "excludedExpiredRows": raw.get("excluded_expired_rows"),
        "currentFilterDate": raw.get("current_filter_date"),
        "snapshotOutput": raw.get("snapshot_output"),
        "supabase": {
            "enabled": bool(supabase.get("enabled")),
            "schema": supabase.get("schema"),
            "table": supabase.get("table"),
            "upserted": supabase.get("upserted"),
            "reason": summarize_supabase_reason(supabase.get("reason")),
        },
    }


def summarize_supabase_reason(reason: Any) -> str | None:
    if not reason:
        return None
    text = str(reason)
    if "SUPABASE" in text.upper() or "SERVICE_ROLE" in text.upper():
        return "Supabase credentials unavailable or dry-run mode"
    return limited_preview(text, 160)


def sync_message(ok: bool, summary: dict[str, Any]) -> str:
    if not ok:
        return "live job sync failed"
    normalized = summary.get("normalizedPayloads")
    excluded = summary.get("excludedExpiredRows")
    upserted = (summary.get("supabase") or {}).get("upserted")
    return f"synced {normalized or 0} active rows, excluded {excluded or 0} expired rows, upserted {upserted or 0} rows"


class JobBridgeHandler(BaseHTTPRequestHandler):
    server_version = "JobBridgeInference/0.1"

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type,authorization,x-jobbridge-admin-token")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(HTTPStatus.OK, SERVICE.health())
            return
        if path == "/v1/reference/summary":
            self._send_json(HTTPStatus.OK, SERVICE.reference_summary())
            return
        if path == "/v1/capabilities":
            query = parse_qs(urlparse(self.path).query)
            payload = {
                "profile": {
                    "disabilityType": (query.get("disabilityType") or query.get("disability_type") or [""])[0],
                    "severity": (query.get("severity") or [""])[0],
                }
            }
            self._send_json(HTTPStatus.OK, SERVICE.capabilities(payload))
            return
        if path == "/v1/ncs-capabilities":
            query = parse_qs(urlparse(self.path).query)
            payload = {
                "q": (query.get("q") or query.get("query") or [""])[0],
                "limit": (query.get("limit") or ["24"])[0],
                "profile": {
                    "disabilityType": (query.get("disabilityType") or query.get("disability_type") or [""])[0],
                    "severity": (query.get("severity") or [""])[0],
                },
            }
            self._send_json(HTTPStatus.OK, SERVICE.ncs_capabilities(payload))
            return
        if path == "/v1/admin/status":
            self._send_json(HTTPStatus.OK, SERVICE.admin_status(RECORDER.status()))
            return
        if path == "/v1/my/recommendations":
            access_token = self._bearer_access_token()
            if not access_token:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "status": "anonymous", "message": "login is required"})
                return
            query = parse_qs(urlparse(self.path).query)
            limit = int((query.get("limit") or ["20"])[0])
            payload = RECORDER.fetch_user_recommendations(access_token, limit=limit)
            status = HTTPStatus.OK if payload.get("ok") else HTTPStatus.UNAUTHORIZED
            self._send_json(status, payload)
            return
        if path == "/v1/admin/auth-users":
            if not self._admin_request_allowed():
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "admin token is required for auth user management"})
                return
            query = parse_qs(urlparse(self.path).query)
            try:
                payload = AUTH_ADMIN.list_users(
                    provider=(query.get("provider") or ["all"])[0],
                    q=(query.get("q") or [""])[0],
                    page=int((query.get("page") or ["1"])[0]),
                    per_page=int((query.get("perPage") or query.get("per_page") or ["1000"])[0]),
                )
                self._send_json(HTTPStatus.OK if payload.get("enabled") else HTTPStatus.SERVICE_UNAVAILABLE, payload)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "enabled": AUTH_ADMIN.enabled, "message": str(exc)})
            return
        if path == "/v1/auth-config":
            self._send_json(HTTPStatus.OK, supabase_public_config())
            return
        if path == "/v1/vworld-loader.js":
            self._send_js(HTTPStatus.OK, vworld_loader_script())
            return
        if path == "/v1/map-config":
            key = vworld_api_key()
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": bool(key),
                    "provider": "vworld",
                    "hasKey": bool(key),
                    "apiKey": key,
                    "scriptUrl": vworld_script_url(key) if key else "",
                },
            )
            return
        if path == "/v1/geocode":
            query = parse_qs(urlparse(self.path).query)
            address = (query.get("address") or [""])[0]
            try:
                payload = geocode_vworld_address(address)
                status = HTTPStatus.OK if payload.get("ok") else HTTPStatus.NOT_FOUND
                self._send_json(status, payload)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})
            return
        if path == "/v1/live-jobs":
            query = parse_qs(urlparse(self.path).query)
            limit = int((query.get("limit") or ["500"])[0])
            self._send_json(HTTPStatus.OK, {"ok": True, **SERVICE.live_jobs_for_ui(limit=limit)})
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/challenge-recommendations":
            try:
                payload = self._read_json_body()
                report = SERVICE.challenge_recommendations(payload)
                persistence = RECORDER.record(
                    payload,
                    report,
                    access_token=self._bearer_access_token(),
                    report_type="challenge",
                )
                report.setdefault("diagnostics", {})["persistence"] = persistence
                self._send_json(HTTPStatus.OK, {"ok": True, **report})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        if path == "/v1/admin/sync-live-jobs":
            try:
                payload = self._read_json_body()
                timeout_seconds = int(payload.get("timeoutSeconds") or os.getenv("JOBBRIDGE_ADMIN_SYNC_TIMEOUT_SECONDS", "240"))
                status, response_payload = sync_live_jobs(timeout_seconds=timeout_seconds)
                self._send_json(status, response_payload)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "status": "error", "message": str(exc)})
            return
        if path == "/v1/admin/auth-users/delete":
            if not self._admin_request_allowed():
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "admin token is required for auth user management"})
                return
            try:
                payload = self._read_json_body()
                response_payload = AUTH_ADMIN.delete_user(
                    str(payload.get("userId") or ""),
                    should_soft_delete=bool(payload.get("shouldSoftDelete")),
                )
                self._send_json(HTTPStatus.OK if response_payload.get("enabled") else HTTPStatus.SERVICE_UNAVAILABLE, response_payload)
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "enabled": AUTH_ADMIN.enabled, "message": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "enabled": AUTH_ADMIN.enabled, "message": str(exc)})
            return
        if path != "/v1/recommendations":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return
        try:
            payload = self._read_json_body()
            report = SERVICE.recommend(payload)
            persistence = RECORDER.record(
                payload,
                report,
                access_token=self._bearer_access_token(),
                report_type="matching",
            )
            report.setdefault("diagnostics", {})["persistence"] = persistence
            self._send_json(HTTPStatus.OK, {"ok": True, "report": report})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})

    def _admin_request_allowed(self) -> bool:
        if is_local_client(self.client_address):
            return True
        expected = admin_token()
        if expected:
            return header_admin_token(self.headers) == expected
        return False

    def _bearer_access_token(self) -> str:
        value = str(self.headers.get("Authorization") or "").strip()
        if value.lower().startswith("bearer "):
            return value[7:].strip()
        return ""

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        for encoding in ("utf-8", "utf-8-sig", "cp949"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")
        return json.loads(text or "{}")

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_js(self, status: HTTPStatus, source: str) -> None:
        data = source.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path: str) -> None:
        relative = "JobBridge.dc.html" if path in ("", "/") else unquote(path).lstrip("/")
        candidate = (SITE_DIR / relative).resolve()
        if not str(candidate).startswith(str(SITE_DIR.resolve())) or not candidate.exists() or candidate.is_dir():
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        data = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the JobBridge inference HTTP server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), JobBridgeHandler)
    print(f"JobBridge inference server running at http://{args.host}:{args.port}")
    print("Health check: /health, recommendation API: POST /v1/recommendations")
    server.serve_forever()


if __name__ == "__main__":
    main()
