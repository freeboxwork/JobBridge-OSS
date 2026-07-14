from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class SupabaseAuthAdmin:
    def __init__(self, url: str | None, service_role_key: str | None) -> None:
        self.url = (url or "").strip().rstrip("/")
        self.service_role_key = (service_role_key or "").strip()

    @classmethod
    def from_env(cls) -> "SupabaseAuthAdmin":
        return cls(
            os.getenv("SUPABASE_URL") or os.getenv("JOBBRIDGE_SUPABASE_URL"),
            os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            or os.getenv("SUPABASE_SECRET_KEY")
            or os.getenv("JOBBRIDGE_SUPABASE_SERVICE_ROLE_KEY"),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.service_role_key)

    def list_users(self, provider: str = "all", q: str = "", page: int = 1, per_page: int = 1000) -> dict[str, Any]:
        if not self.enabled:
            return {
                "ok": False,
                "enabled": False,
                "message": "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SECRET_KEY must be set on the server.",
                "users": [],
                "total": 0,
                "providerCounts": {},
            }

        page = max(1, int(page or 1))
        per_page = max(1, min(int(per_page or 1000), 1000))
        provider = (provider or "all").strip().lower()
        q = (q or "").strip().lower()

        raw = self._auth_request(
            "GET",
            "/auth/v1/admin/users",
            query={"page": str(page), "per_page": str(per_page)},
        )
        raw_users = raw.get("users") if isinstance(raw, dict) else raw
        if not isinstance(raw_users, list):
            raw_users = []

        profiles, profile_status = self._profile_map()
        users = [normalize_auth_user(user, profiles.get(str(user.get("id") or ""))) for user in raw_users if isinstance(user, dict)]
        if provider and provider != "all":
            users = [user for user in users if provider in user["providers"]]
        if q:
            users = [user for user in users if user_matches_query(user, q)]

        provider_counts: dict[str, int] = {}
        for user in users:
            for item in user["providers"] or ["unknown"]:
                provider_counts[item] = provider_counts.get(item, 0) + 1

        return {
            "ok": True,
            "enabled": True,
            "page": page,
            "perPage": per_page,
            "total": len(users),
            "providerCounts": provider_counts,
            "profileStatus": profile_status,
            "users": users,
        }

    def delete_user(self, user_id: str, should_soft_delete: bool = False) -> dict[str, Any]:
        if not self.enabled:
            return {
                "ok": False,
                "enabled": False,
                "message": "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SECRET_KEY must be set on the server.",
            }
        clean_user_id = (user_id or "").strip()
        if not clean_user_id:
            raise ValueError("userId is required")

        body = {"should_soft_delete": bool(should_soft_delete)}
        data = self._auth_request(
            "DELETE",
            f"/auth/v1/admin/users/{urllib.parse.quote(clean_user_id)}",
            payload=body,
        )
        return {
            "ok": True,
            "enabled": True,
            "deletedUserId": clean_user_id,
            "softDeleted": bool(should_soft_delete),
            "data": data,
        }

    def _auth_request(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        return self._request(method, path, query=query, payload=payload)

    def _profile_map(self) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        if not self.enabled:
            return {}, {"ok": False, "message": "Supabase admin is not configured"}
        try:
            rows = self._request(
                "GET",
                "/rest/v1/profiles",
                query={
                    "select": "id,email,display_name,created_at,updated_at",
                    "limit": "1000",
                },
                profile="public",
            )
            if not isinstance(rows, list):
                return {}, {"ok": False, "message": "profiles response was not a list"}
            return {str(row.get("id")): row for row in rows if isinstance(row, dict) and row.get("id")}, {"ok": True}
        except Exception as exc:
            return {}, {"ok": False, "message": str(exc)}

    def _request(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        profile: str | None = None,
    ) -> Any:
        query_string = f"?{urllib.parse.urlencode(query)}" if query else ""
        endpoint = f"{self.url}{path}{query_string}"
        data = None
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Accept": "application/json",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if profile:
            headers["Accept-Profile"] = profile
            headers["Content-Profile"] = profile

        request = urllib.request.Request(endpoint, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase Auth Admin {method} {path} failed: HTTP {exc.code} {safe_error_detail(detail)}") from exc
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"message": body}


def safe_error_detail(detail: str, limit: int = 400) -> str:
    clean = (detail or "").strip()
    if len(clean) <= limit:
        return clean
    return f"{clean[:limit]}..."


def normalize_auth_user(user: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = user.get("user_metadata") if isinstance(user.get("user_metadata"), dict) else {}
    app_metadata = user.get("app_metadata") if isinstance(user.get("app_metadata"), dict) else {}
    identities = user.get("identities") if isinstance(user.get("identities"), list) else []
    providers = provider_names(user, identities, app_metadata)
    display_name = first_text(
        (profile or {}).get("display_name"),
        metadata.get("name"),
        metadata.get("full_name"),
        metadata.get("display_name"),
        metadata.get("nickname"),
        metadata.get("preferred_username"),
        user.get("email"),
        user.get("phone"),
        user.get("id"),
    )
    avatar_url = first_text(
        metadata.get("avatar_url"),
        metadata.get("picture"),
        metadata.get("profile_image_url"),
        metadata.get("profile_image"),
    )
    return {
        "id": user.get("id"),
        "email": user.get("email") or (profile or {}).get("email") or "",
        "phone": user.get("phone") or "",
        "displayName": display_name,
        "avatarUrl": avatar_url,
        "providers": providers,
        "primaryProvider": providers[0] if providers else "unknown",
        "createdAt": user.get("created_at"),
        "updatedAt": user.get("updated_at"),
        "lastSignInAt": user.get("last_sign_in_at"),
        "confirmedAt": user.get("confirmed_at") or user.get("email_confirmed_at") or user.get("phone_confirmed_at"),
        "bannedUntil": user.get("banned_until"),
        "isAnonymous": bool(user.get("is_anonymous")),
        "profile": sanitize_profile(profile),
        "identities": [
            {
                "id": item.get("id"),
                "provider": item.get("provider") or "unknown",
                "createdAt": item.get("created_at"),
                "lastSignInAt": item.get("last_sign_in_at"),
            }
            for item in identities
            if isinstance(item, dict)
        ],
    }


def provider_names(user: dict[str, Any], identities: list[Any], app_metadata: dict[str, Any]) -> list[str]:
    names: list[str] = []
    provider = app_metadata.get("provider")
    if provider:
        names.append(str(provider).lower())
    for item in identities:
        if isinstance(item, dict) and item.get("provider"):
            names.append(str(item.get("provider")).lower())
    if not names and user.get("email"):
        names.append("email")
    if not names and user.get("phone"):
        names.append("phone")
    return sorted(set(names))


def sanitize_profile(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if not profile:
        return None
    return {
        "email": profile.get("email") or "",
        "displayName": profile.get("display_name") or "",
        "createdAt": profile.get("created_at"),
        "updatedAt": profile.get("updated_at"),
    }


def user_matches_query(user: dict[str, Any], q: str) -> bool:
    haystack = " ".join(
        [
            str(user.get("id") or ""),
            str(user.get("email") or ""),
            str(user.get("phone") or ""),
            str(user.get("displayName") or ""),
            " ".join(user.get("providers") or []),
        ]
    ).lower()
    return q in haystack


def first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""
