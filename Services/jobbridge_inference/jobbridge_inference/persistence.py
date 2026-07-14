from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any


KST = timezone(timedelta(hours=9))


def _current_service_date():
    return datetime.now(KST).date()


def _is_current_recruit_end(value: Any, today=None) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    try:
        end_date = datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return True
    return end_date >= (today or _current_service_date())


class SupabaseRecorder:
    def __init__(
        self,
        url: str | None,
        service_role_key: str | None,
        schema: str | None = None,
        anon_key: str | None = None,
    ) -> None:
        self.url = (url or "").rstrip("/")
        self.service_role_key = service_role_key or ""
        self.schema = schema or "jobbridge_private"
        self.anon_key = anon_key or ""

    @classmethod
    def from_env(cls) -> "SupabaseRecorder":
        return cls(
            os.getenv("SUPABASE_URL") or os.getenv("JOBBRIDGE_SUPABASE_URL"),
            os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("JOBBRIDGE_SUPABASE_SERVICE_ROLE_KEY"),
            os.getenv("SUPABASE_DB_SCHEMA") or os.getenv("JOBBRIDGE_SUPABASE_DB_SCHEMA"),
            (
                os.getenv("SUPABASE_ANON_KEY")
                or os.getenv("JOBBRIDGE_SUPABASE_ANON_KEY")
                or os.getenv("SUPABASE_PUBLISHABLE_KEY")
                or os.getenv("JOBBRIDGE_SUPABASE_PUBLISHABLE_KEY")
            ),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.service_role_key)

    @property
    def recommendation_logging_enabled(self) -> bool:
        value = os.getenv("JOBBRIDGE_RECOMMENDATION_LOGGING_ENABLED", "false").strip().lower()
        return value in {"1", "true", "yes", "y", "on"}

    def status(self) -> dict[str, Any]:
        if self.recommendation_logging_enabled and self.enabled:
            logging_status = "enabled"
        elif self.recommendation_logging_enabled:
            logging_status = "configured_but_supabase_missing"
        else:
            logging_status = "disabled"
        return {
            "supabaseConfigured": self.enabled,
            "schema": self.schema,
            "recommendationLoggingEnabled": self.recommendation_logging_enabled,
            "recommendationLoggingStatus": logging_status,
            "recommendationTables": ["recommendation_requests", "recommendation_results"],
            "userRecommendationHistory": "available" if self.recommendation_logging_enabled and self.enabled else "unavailable",
            "secretsExposed": False,
        }

    def record(
        self,
        request_payload: dict[str, Any],
        report: dict[str, Any],
        access_token: str | None = None,
        report_type: str = "matching",
    ) -> dict[str, Any]:
        if not self.recommendation_logging_enabled:
            return {"enabled": self.enabled, "status": "skipped", "reason": "recommendation logging disabled"}
        if not self.enabled:
            return {"enabled": False, "status": "skipped", "reason": "SUPABASE_URL or service role key not set"}
        try:
            user_lookup = self.resolve_user(access_token)
            report_summary = self._report_summary(request_payload, report, report_type)
            client_context = request_payload.get("clientContext") if isinstance(request_payload.get("clientContext"), dict) else {}
            client_context = {
                **client_context,
                "reportType": report_summary["report_type"],
                "authUserStatus": user_lookup["status"],
            }
            request_row = self._insert(
                "recommendation_requests",
                {
                    "user_id": user_lookup.get("userId") or None,
                    "client_session_id": request_payload.get("clientSessionId"),
                    "request_payload": request_payload,
                    "model_features": report_summary["model_features"],
                    "scoring_preferences": report_summary["scoring_preferences"],
                    "model_version": report_summary["model_version"],
                    "client_context": client_context,
                    "fallback_used": report_summary["fallback_used"],
                },
            )
            request_id = request_row.get("id")
            self._insert(
                "recommendation_results",
                {
                    "request_id": request_id,
                    "report_json": report,
                    "top_job_class": report_summary["top_job_class"],
                    "top_score": report_summary["top_score"],
                    "latency_ms": report_summary["latency_ms"],
                    "model_version": report_summary["model_version"],
                },
            )
            return {
                "enabled": True,
                "status": "stored",
                "requestId": request_id,
                "reportType": report_summary["report_type"],
                "userLinked": bool(user_lookup.get("userId")),
                "authUserStatus": user_lookup["status"],
            }
        except Exception as exc:  # Persistence must not break recommendation generation.
            return {"enabled": True, "status": "error", "message": str(exc)}

    def resolve_user(self, access_token: str | None) -> dict[str, Any]:
        token = (access_token or "").strip()
        if not token:
            return {"status": "anonymous", "userId": ""}
        if not self.url:
            return {"status": "unavailable", "userId": ""}
        endpoint = f"{self.url}/auth/v1/user"
        request = urllib.request.Request(
            endpoint,
            method="GET",
            headers={
                "apikey": self.anon_key or self.service_role_key,
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return {"status": "invalid", "userId": ""}
            detail = exc.read().decode("utf-8", errors="replace")
            return {"status": "error", "userId": "", "message": f"HTTP {exc.code} {detail[:160]}"}
        except Exception as exc:
            return {"status": "error", "userId": "", "message": str(exc)}
        data = json.loads(body or "{}")
        user_id = str(data.get("id") or "").strip()
        return {"status": "resolved" if user_id else "missing", "userId": user_id}

    def fetch_user_recommendations(self, access_token: str | None, limit: int = 20) -> dict[str, Any]:
        if not self.recommendation_logging_enabled:
            return {"ok": False, "status": "disabled", "message": "recommendation logging disabled", "items": []}
        if not self.enabled:
            return {"ok": False, "status": "unavailable", "message": "SUPABASE_URL or service role key not set", "items": []}
        user_lookup = self.resolve_user(access_token)
        if not user_lookup.get("userId"):
            return {"ok": False, "status": user_lookup["status"], "message": "valid user session is required", "items": []}
        max_limit = max(1, min(int(limit or 20), 50))
        requests = self._select(
            "recommendation_requests",
            {
                "select": "id,created_at,client_context,scoring_preferences,model_version,fallback_used,status",
                "user_id": f"eq.{user_lookup['userId']}",
                "order": "created_at.desc",
                "limit": str(max_limit),
            },
        )
        request_ids = [str(row.get("id") or "") for row in requests if row.get("id")]
        results_by_request: dict[str, dict[str, Any]] = {}
        if request_ids:
            results = self._select(
                "recommendation_results",
                {
                    "select": "request_id,created_at,top_job_class,top_score,latency_ms,model_version,report_json",
                    "request_id": f"in.({','.join(request_ids)})",
                },
            )
            for row in results:
                request_id = str(row.get("request_id") or "")
                if request_id:
                    results_by_request[request_id] = row
        items = []
        for request_row in requests:
            request_id = str(request_row.get("id") or "")
            result = results_by_request.get(request_id, {})
            context = request_row.get("client_context") if isinstance(request_row.get("client_context"), dict) else {}
            items.append(
                {
                    "id": request_id,
                    "createdAt": request_row.get("created_at"),
                    "reportType": context.get("reportType") or "matching",
                    "topJobClass": result.get("top_job_class"),
                    "topScore": result.get("top_score"),
                    "latencyMs": result.get("latency_ms"),
                    "modelVersion": result.get("model_version") or request_row.get("model_version"),
                    "fallbackUsed": bool(request_row.get("fallback_used")),
                    "status": request_row.get("status"),
                    "report": result.get("report_json") if isinstance(result.get("report_json"), dict) else None,
                }
            )
        return {"ok": True, "status": "ok", "userId": user_lookup["userId"], "items": items}

    def _report_summary(self, request_payload: dict[str, Any], report: dict[str, Any], report_type: str) -> dict[str, Any]:
        report_type = str(report_type or "matching").strip() or "matching"
        diagnostics = report.get("diagnostics") if isinstance(report.get("diagnostics"), dict) else {}
        if report_type == "challenge":
            cards = (
                report.get("challengeRecommendations")
                or report.get("challengeRecs")
                or report.get("cards")
                or []
            )
            top = cards[0] if cards and isinstance(cards[0], dict) else {}
            model_features = request_payload.get("profile") if isinstance(request_payload.get("profile"), dict) else {}
            scoring_preferences = request_payload.get("scoringPreferences") if isinstance(request_payload.get("scoringPreferences"), dict) else {}
            model_version = (
                report.get("modelVersion")
                or diagnostics.get("challengeRecommendationVersion")
                or "challenge_xai_contract_v1"
            )
            top_job_class = top.get("targetJobClass") or top.get("jobClass") or top.get("displayTitle") or top.get("title")
            top_score = top.get("score") if top.get("score") is not None else top.get("challengeScore")
        else:
            model_payload = report.get("modelPayload") if isinstance(report.get("modelPayload"), dict) else {}
            model_features = model_payload.get("modelFeatures") if isinstance(model_payload.get("modelFeatures"), dict) else {}
            scoring_preferences = model_payload.get("scoringPreferences") if isinstance(model_payload.get("scoringPreferences"), dict) else {}
            predicted = report.get("predictedJobClasses") or [{}]
            recs = report.get("recs") or [{}]
            top_predicted = predicted[0] if predicted and isinstance(predicted[0], dict) else {}
            top_rec = recs[0] if recs and isinstance(recs[0], dict) else {}
            model_version = report.get("modelVersion") or "jobbridge_model_v1"
            top_job_class = top_predicted.get("jobClass") or top_rec.get("title")
            top_score = top_rec.get("score")
        fallback = report.get("fallback") if isinstance(report.get("fallback"), dict) else {}
        return {
            "report_type": report_type,
            "model_features": model_features or {},
            "scoring_preferences": scoring_preferences or {},
            "model_version": str(model_version or "jobbridge_model_v1"),
            "top_job_class": str(top_job_class or "").strip() or None,
            "top_score": self._coerce_score(top_score),
            "latency_ms": diagnostics.get("latencyMs"),
            "fallback_used": bool(fallback.get("used")),
        }

    def _coerce_score(self, value: Any) -> int | None:
        try:
            score = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        return max(0, min(score, 100))

    def _insert(self, table: str, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"{self.url}/rest/v1/{table}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=data,
            method="POST",
            headers={
                "apikey": self.service_role_key,
                "Authorization": f"Bearer {self.service_role_key}",
                "Content-Type": "application/json",
                "Content-Profile": self.schema,
                "Accept-Profile": self.schema,
                "Prefer": "return=representation",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase insert failed for {table}: HTTP {exc.code} {detail}") from exc
        rows = json.loads(body or "[]")
        if not rows:
            return {}
        return rows[0]

    def _select(self, table: str, params: dict[str, str], timeout: int = 8) -> list[dict[str, Any]]:
        endpoint = f"{self.url}/rest/v1/{table}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            endpoint,
            method="GET",
            headers={
                "apikey": self.service_role_key,
                "Authorization": f"Bearer {self.service_role_key}",
                "Accept-Profile": self.schema,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase select failed for {table}: HTTP {exc.code} {detail}") from exc
        rows = json.loads(body or "[]")
        return rows if isinstance(rows, list) else []

    def fetch_capability_catalog(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        categories = self._select(
            "capability_categories",
            {
                "select": "id,label,summary,target_job_class,sort_order",
                "is_active": "eq.true",
                "order": "sort_order.asc",
            },
        )
        groups = self._select(
            "capability_groups",
            {
                "select": "id,category_id,label,summary,sort_order",
                "is_active": "eq.true",
                "order": "sort_order.asc",
            },
        )
        items = self._select(
            "capability_items",
            {
                "select": "id,group_id,label,ncs_code,definition,sort_order",
                "is_active": "eq.true",
                "order": "sort_order.asc",
            },
        )
        if not categories or not groups or not items:
            return []

        catalog_by_id: dict[str, dict[str, Any]] = {}
        for category in sorted(categories, key=lambda row: int(row.get("sort_order") or 0)):
            category_id = str(category.get("id") or "").strip()
            if not category_id:
                continue
            catalog_by_id[category_id] = {
                "id": category_id,
                "label": str(category.get("label") or "").strip(),
                "summary": str(category.get("summary") or "").strip(),
                "targetJobClass": str(category.get("target_job_class") or "").strip(),
                "groups": [],
            }

        groups_by_id: dict[str, dict[str, Any]] = {}
        for group in sorted(groups, key=lambda row: int(row.get("sort_order") or 0)):
            category = catalog_by_id.get(str(group.get("category_id") or "").strip())
            group_id = str(group.get("id") or "").strip()
            if not category or not group_id:
                continue
            next_group = {
                "id": group_id,
                "label": str(group.get("label") or "").strip(),
                "summary": str(group.get("summary") or "").strip(),
                "items": [],
            }
            groups_by_id[group_id] = next_group
            category["groups"].append(next_group)

        for item in sorted(items, key=lambda row: int(row.get("sort_order") or 0)):
            group = groups_by_id.get(str(item.get("group_id") or "").strip())
            item_id = str(item.get("id") or "").strip()
            if not group or not item_id:
                continue
            group["items"].append(
                {
                    "id": item_id,
                    "label": str(item.get("label") or "").strip(),
                    "ncsCode": str(item.get("ncs_code") or "").strip(),
                    "definition": str(item.get("definition") or "").strip(),
                }
            )

        return [category for category in catalog_by_id.values() if category["groups"]]

    def fetch_active_live_postings(self, limit: int = 1000) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        today = _current_service_date()
        params = {
            "select": "*",
            "is_active": "eq.true",
            "target_job_class_candidate": "not.is.null",
            "or": f"(recruit_end.is.null,recruit_end.gte.{today.isoformat()})",
            "order": "last_seen_at.desc",
            "limit": str(max(1, min(int(limit), 1000))),
        }
        endpoint = f"{self.url}/rest/v1/job_postings_live?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            endpoint,
            method="GET",
            headers={
                "apikey": self.service_role_key,
                "Authorization": f"Bearer {self.service_role_key}",
                "Accept-Profile": self.schema,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase select failed for job_postings_live: HTTP {exc.code} {detail}") from exc
        rows = json.loads(body or "[]")
        if not isinstance(rows, list):
            return []
        return [
            row
            for row in rows
            if isinstance(row, dict)
            and _is_current_recruit_end(row.get("recruit_end") or row.get("recruit_end_date"), today)
        ]
