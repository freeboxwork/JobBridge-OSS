# JobBridge capability catalog

This schema stores the challenge-recommendation capability master data in the
private Supabase schema used by the inference server.

## Tables

- `jobbridge_private.capability_categories`
- `jobbridge_private.capability_groups`
- `jobbridge_private.capability_items`
- `jobbridge_private.capability_disability_rules`

The browser must not read these tables directly. The inference server reads
them with the Supabase service role key and exposes the filtered result through
`GET /v1/capabilities`.

## Setup

1. Execute `supabase/schema_jobbridge_capabilities.sql`.
   If the inference server reads this schema through Supabase REST, add
   `jobbridge_private` to the project's Data API exposed schemas. Keep the
   `anon` and `authenticated` grants revoked as defined in the schema file; the
   server uses only the service role key.
2. Generate seed SQL:

   ```powershell
   python Scripts\export_capability_catalog_sql.py | Out-File -Encoding utf8 capability_seed.sql
   ```

3. Review and execute `capability_seed.sql`.
4. Set `JOBBRIDGE_CAPABILITY_CATALOG_SOURCE=supabase` in the inference server
   environment after confirming the tables are populated.

If the variable is unset or set to `auto`, the server tries Supabase when the
service role key is configured and falls back to the local catalog when the DB
catalog is unavailable.
