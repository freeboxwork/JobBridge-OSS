from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlencode
from typing import Any

from .auth_admin import SupabaseAuthAdmin
from .core import JobBridgeInferenceService
from .persistence import SupabaseRecorder


SERVICE = JobBridgeInferenceService()
RECORDER = SupabaseRecorder.from_env()
AUTH_ADMIN = SupabaseAuthAdmin.from_env()


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


def vworld_api_key() -> str:
    return (os.getenv("JOBBRIDGE_VWORLD_API_KEY") or os.getenv("VWORLD_API_KEY") or "").strip()


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
        raise RuntimeError(f"VWorld API HTTP {exc.code}: {detail[:240]}") from exc
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
    response_body = data.get("response") if isinstance(data, dict) else {}
    result = response_body.get("result") if isinstance(response_body, dict) else {}
    lat, lng = parse_vworld_point(result.get("point") if isinstance(result, dict) else None)
    if lat is None or lng is None:
        return None
    refined = response_body.get("refined") if isinstance(response_body, dict) else {}
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
    response_body = data.get("response") if isinstance(data, dict) else {}
    result = response_body.get("result") if isinstance(response_body, dict) else {}
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

    attempts: list[dict[str, str]] = []
    for query in queries:
        for address_type in ("ROAD", "PARCEL"):
            attempts.append({"query": query, "kind": "geocoder", "type": address_type})
            point = vworld_address_coord(query, address_type)
            if point:
                return {"ok": True, "query": clean, **point}
        for category in ("road", "parcel"):
            attempts.append({"query": query, "kind": "search", "type": category})
            point = vworld_search_coord(query, category)
            if point:
                return {"ok": True, "query": clean, **point}
    return {"ok": False, "query": clean, "error": "VWorld did not return coordinates", "attempts": attempts}


def cors_headers() -> dict[str, str]:
    origin = os.getenv("JOBBRIDGE_ALLOWED_ORIGIN", "*")
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Headers": "content-type,authorization,x-jobbridge-admin-token",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Cache-Control": "no-store",
        "Content-Type": "application/json; charset=utf-8",
    }


def response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": cors_headers(),
        "body": json.dumps(payload, ensure_ascii=False),
    }


def response_text(status_code: int, body: str, content_type: str) -> dict[str, Any]:
    headers = cors_headers()
    headers["Content-Type"] = content_type
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": body,
    }


def parse_body(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    if isinstance(body, dict):
        return body
    return json.loads(body)


def admin_token() -> str:
    return (os.getenv("JOBBRIDGE_ADMIN_TOKEN") or "").strip()


def event_header(event: dict[str, Any], name: str) -> str:
    headers = event.get("headers") or {}
    lower_name = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lower_name:
            return str(value or "")
    return ""


def admin_request_allowed(event: dict[str, Any]) -> bool:
    expected = admin_token()
    if not expected:
        return False
    auth_header = event_header(event, "authorization").strip()
    supplied = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else event_header(event, "x-jobbridge-admin-token").strip()
    return supplied == expected


def bearer_access_token(event: dict[str, Any]) -> str:
    auth_header = event_header(event, "authorization").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return ""


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()
    path = (
        event.get("rawPath")
        or event.get("path")
        or event.get("requestContext", {}).get("http", {}).get("path")
        or "/"
    )

    if method == "OPTIONS":
        return response(204, {})
    if method == "GET" and path.endswith("/health"):
        return response(200, SERVICE.health())
    if method == "GET" and path.endswith("/v1/reference/summary"):
        return response(200, SERVICE.reference_summary())
    if method == "GET" and path.endswith("/v1/admin/status"):
        return response(200, SERVICE.admin_status(RECORDER.status()))
    if method == "GET" and path.endswith("/v1/my/recommendations"):
        access_token = bearer_access_token(event)
        if not access_token:
            return response(401, {"ok": False, "status": "anonymous", "message": "login is required"})
        query = event.get("queryStringParameters") or {}
        payload = RECORDER.fetch_user_recommendations(access_token, limit=int(query.get("limit") or 20))
        return response(200 if payload.get("ok") else 401, payload)
    if method == "GET" and path.endswith("/v1/admin/auth-users"):
        if not admin_request_allowed(event):
            return response(403, {"ok": False, "message": "admin token is required for auth user management"})
        query = event.get("queryStringParameters") or {}
        try:
            payload = AUTH_ADMIN.list_users(
                provider=query.get("provider") or "all",
                q=query.get("q") or "",
                page=int(query.get("page") or 1),
                per_page=int(query.get("perPage") or query.get("per_page") or 1000),
            )
            return response(200 if payload.get("enabled") else 503, payload)
        except Exception as exc:
            return response(502, {"ok": False, "enabled": AUTH_ADMIN.enabled, "message": str(exc)})
    if method == "GET" and path.endswith("/v1/auth-config"):
        return response(200, supabase_public_config())
    if method == "GET" and path.endswith("/v1/vworld-loader.js"):
        return response_text(200, vworld_loader_script(), "application/javascript; charset=utf-8")
    if method == "GET" and path.endswith("/v1/map-config"):
        key = vworld_api_key()
        return response(
            200,
            {
                "ok": bool(key),
                "provider": "vworld",
                "hasKey": bool(key),
                "apiKey": key,
                "scriptUrl": vworld_script_url(key) if key else "",
            },
        )
    if method == "GET" and path.endswith("/v1/geocode"):
        query = event.get("queryStringParameters") or {}
        try:
            payload = geocode_vworld_address(query.get("address") or "")
            return response(200 if payload.get("ok") else 404, payload)
        except Exception as exc:
            return response(502, {"ok": False, "error": str(exc)})
    if method == "GET" and path.endswith("/v1/live-jobs"):
        query = event.get("queryStringParameters") or {}
        limit = int(query.get("limit") or 500)
        return response(200, {"ok": True, **SERVICE.live_jobs_for_ui(limit=limit, force_refresh=True)})
    if method == "GET" and path.endswith("/v1/capabilities"):
        query = event.get("queryStringParameters") or {}
        payload = {
            "profile": {
                "disabilityType": query.get("disabilityType") or query.get("disability_type") or "",
                "severity": query.get("severity") or "",
            }
        }
        return response(200, SERVICE.capabilities(payload))
    if method == "GET" and path.endswith("/v1/ncs-capabilities"):
        query = event.get("queryStringParameters") or {}
        payload = {
            "q": query.get("q") or query.get("query") or "",
            "limit": query.get("limit") or 24,
            "profile": {
                "disabilityType": query.get("disabilityType") or query.get("disability_type") or "",
                "severity": query.get("severity") or "",
            },
        }
        return response(200, SERVICE.ncs_capabilities(payload))
    if method == "POST" and path.endswith("/v1/admin/sync-live-jobs"):
        return response(
            501,
            {
                "ok": False,
                "status": "not_supported",
                "message": "live job sync is only supported by the local admin server because Lambda cannot run the local collector script safely",
            },
        )
    if method != "POST":
        return response(404, {"ok": False, "error": "Not found"})

    try:
        request_payload = parse_body(event)
        if path.endswith("/v1/admin/auth-users/delete"):
            if not admin_request_allowed(event):
                return response(403, {"ok": False, "message": "admin token is required for auth user management"})
            payload = AUTH_ADMIN.delete_user(
                str(request_payload.get("userId") or ""),
                should_soft_delete=bool(request_payload.get("shouldSoftDelete")),
            )
            return response(200 if payload.get("enabled") else 503, payload)
        if path.endswith("/v1/challenge-recommendations"):
            report = SERVICE.challenge_recommendations(request_payload)
            persistence = RECORDER.record(
                request_payload,
                report,
                access_token=bearer_access_token(event),
                report_type="challenge",
            )
            report.setdefault("diagnostics", {})["persistence"] = persistence
            return response(200, {"ok": True, **report})
        report = SERVICE.recommend(request_payload)
        persistence = RECORDER.record(
            request_payload,
            report,
            access_token=bearer_access_token(event),
            report_type="matching",
        )
        report.setdefault("diagnostics", {})["persistence"] = persistence
        return response(200, {"ok": True, "report": report})
    except Exception as exc:
        return response(400, {"ok": False, "error": str(exc)})
