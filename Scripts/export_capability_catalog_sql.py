from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INFERENCE_ROOT = PROJECT_ROOT / "Services" / "jobbridge_inference"
sys.path.insert(0, str(INFERENCE_ROOT))

from jobbridge_inference.reference import CAPABILITY_CATALOG  # noqa: E402


def sql_literal(value: Any) -> str:
    if value is None:
        return "null"
    text = str(value).replace("'", "''")
    return f"'{text}'"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("-- Generated from jobbridge_inference.reference.CAPABILITY_CATALOG")
    print("-- Usage: run supabase/schema_jobbridge_capabilities.sql first, then execute this output.")
    print("begin;")

    for category_index, category in enumerate(CAPABILITY_CATALOG, start=1):
        print(
            "insert into jobbridge_private.capability_categories "
            "(id, label, summary, target_job_class, sort_order, is_active) values "
            f"({sql_literal(category['id'])}, {sql_literal(category['label'])}, "
            f"{sql_literal(category.get('summary', ''))}, {sql_literal(category.get('targetJobClass', ''))}, "
            f"{category_index}, true) "
            "on conflict (id) do update set "
            "label = excluded.label, summary = excluded.summary, "
            "target_job_class = excluded.target_job_class, sort_order = excluded.sort_order, "
            "is_active = excluded.is_active, updated_at = now();"
        )

        for group_index, group in enumerate(category.get("groups", []), start=1):
            print(
                "insert into jobbridge_private.capability_groups "
                "(id, category_id, label, summary, sort_order, is_active) values "
                f"({sql_literal(group['id'])}, {sql_literal(category['id'])}, "
                f"{sql_literal(group['label'])}, {sql_literal(group.get('summary', ''))}, "
                f"{group_index}, true) "
                "on conflict (id) do update set "
                "category_id = excluded.category_id, label = excluded.label, "
                "summary = excluded.summary, sort_order = excluded.sort_order, "
                "is_active = excluded.is_active, updated_at = now();"
            )

            for item_index, item in enumerate(group.get("items", []), start=1):
                print(
                    "insert into jobbridge_private.capability_items "
                    "(id, group_id, label, ncs_code, definition, sort_order, is_active) values "
                    f"({sql_literal(item['id'])}, {sql_literal(group['id'])}, "
                    f"{sql_literal(item['label'])}, {sql_literal(item.get('ncsCode', ''))}, "
                    f"{sql_literal(item.get('definition', ''))}, {item_index}, true) "
                    "on conflict (id) do update set "
                    "group_id = excluded.group_id, label = excluded.label, "
                    "ncs_code = excluded.ncs_code, definition = excluded.definition, "
                    "sort_order = excluded.sort_order, is_active = excluded.is_active, updated_at = now();"
                )

    print("commit;")
    print("notify pgrst, 'reload schema';")


if __name__ == "__main__":
    main()
