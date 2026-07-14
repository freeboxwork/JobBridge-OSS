from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INFERENCE_ROOT = PROJECT_ROOT / "Services" / "jobbridge_inference"
sys.path.insert(0, str(INFERENCE_ROOT))

from jobbridge_inference.reference import CAPABILITY_CATALOG  # noqa: E402


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
        if key and key not in os.environ:
            os.environ[key] = value


def required_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.rstrip("/") if name.endswith("URL") else value
    raise RuntimeError(f"Missing environment variable: {' or '.join(names)}")


def rows_from_catalog() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    categories: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for category_index, category in enumerate(CAPABILITY_CATALOG, start=1):
        categories.append(
            {
                "id": category["id"],
                "label": category["label"],
                "summary": category.get("summary", ""),
                "target_job_class": category.get("targetJobClass", ""),
                "sort_order": category_index,
                "is_active": True,
            }
        )
        for group_index, group in enumerate(category.get("groups", []), start=1):
            groups.append(
                {
                    "id": group["id"],
                    "category_id": category["id"],
                    "label": group["label"],
                    "summary": group.get("summary", ""),
                    "sort_order": group_index,
                    "is_active": True,
                }
            )
            for item_index, item in enumerate(group.get("items", []), start=1):
                items.append(
                    {
                        "id": item["id"],
                        "group_id": group["id"],
                        "label": item["label"],
                        "ncs_code": item.get("ncsCode", ""),
                        "definition": item.get("definition", ""),
                        "sort_order": item_index,
                        "is_active": True,
                    }
                )
    return categories, groups, items


def upsert_rows(url: str, key: str, schema: str, table: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    endpoint = f"{url}/rest/v1/{table}?{urllib.parse.urlencode({'on_conflict': 'id'})}"
    data = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Content-Profile": schema,
            "Accept-Profile": schema,
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase upsert failed for {table}: HTTP {exc.code} {detail}") from exc
    return len(rows)


def main() -> None:
    load_env()
    url = required_env("SUPABASE_URL", "JOBBRIDGE_SUPABASE_URL")
    key = required_env("SUPABASE_SERVICE_ROLE_KEY", "JOBBRIDGE_SUPABASE_SERVICE_ROLE_KEY")
    schema = os.getenv("SUPABASE_DB_SCHEMA") or os.getenv("JOBBRIDGE_SUPABASE_DB_SCHEMA") or "jobbridge_private"

    categories, groups, items = rows_from_catalog()
    counts = {
        "categories": upsert_rows(url, key, schema, "capability_categories", categories),
        "groups": upsert_rows(url, key, schema, "capability_groups", groups),
        "items": upsert_rows(url, key, schema, "capability_items", items),
    }
    print(json.dumps({"ok": True, "schema": schema, "counts": counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
