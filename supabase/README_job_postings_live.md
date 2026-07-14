# KEAD live job postings collection

This draft collects the KEAD realtime OpenAPI endpoints:

- `job_list_env`
- `job_list`

The script reads `JOBBRIDGE_PUBLIC_DATA_SERVICE_KEY` from `.env` or the current
environment, collects all pages using `pageNo` and `numOfRows`, merges duplicates,
and prepares normalized rows for `jobbridge_private.job_postings_live`.

## 1. Apply schema

Run `supabase/schema_job_postings_live.sql` in the Supabase SQL editor or through
your usual server-side migration path.

If you use the REST API with `Content-Profile: jobbridge_private`, make sure the
`jobbridge_private` schema is exposed to the Supabase Data API in project
settings. The SQL grants access only to `service_role`; it revokes `anon` and
`authenticated` access and enables RLS.

## 2. Environment variables

Required:

```powershell
$env:JOBBRIDGE_PUBLIC_DATA_SERVICE_KEY="<data.go.kr service key>"
```

Required only for Supabase upsert:

```powershell
$env:SUPABASE_URL="https://<project-ref>.supabase.co"
$env:SUPABASE_SERVICE_ROLE_KEY="<service-role-key>"
$env:SUPABASE_DB_SCHEMA="jobbridge_private"
```

The script also accepts the existing JobBridge aliases:

- `JOBBRIDGE_SUPABASE_URL`
- `JOBBRIDGE_SUPABASE_SERVICE_ROLE_KEY`
- `JOBBRIDGE_SUPABASE_DB_SCHEMA`

## 3. Dry run

```powershell
python Scripts\collect_kead_live_jobs.py --dry-run
```

The script also writes a local snapshot to
`Data/processed/live_job_postings/job_postings_live.json` unless `--no-output` is
passed. The local inference server uses this snapshot when Supabase credentials
are not configured, so local QA can still run against live KEAD data.

Use `--out <path>` to choose another output path. A `.jsonl` suffix writes JSONL;
other suffixes write a JSON array.

```powershell
python Scripts\collect_kead_live_jobs.py --dry-run --max-pages 1 --no-output
```

## 4. Supabase upsert

```powershell
python Scripts\collect_kead_live_jobs.py
```

Upsert uses `source_posting_key` as the conflict key. The key is a stable
SHA-256 hash over `offerregDt`, `busplaName`, `jobNm`, `termDate`, `compAddr`,
`salaryType`, and `salary`.

Do not use `rno` or `rnum` as identity keys. They are row numbers inside each API
response, and the same number can refer to different postings across
`job_list_env` and `job_list`.

When the same posting appears in both endpoints, `job_list_env` fields are kept
as the preferred values and `job_list` fills missing values.

After a successful upsert, rows from previous syncs that were not seen in the
current API fetch are marked `is_active = false`. Recommendation APIs should
select only active rows.

## 5. Scheduling

For the MVP, the same collector can be scheduled from the inference host,
GitHub Actions, or Windows Task Scheduler. The lightweight wrapper is:

```powershell
Scripts\sync_kead_live_jobs.ps1
```

Recommended four-times-a-day KST schedule:

```text
06:00, 12:00, 18:00, 23:30 Asia/Seoul
```

If you later move this into Supabase-native scheduling, use a scheduled Edge
Function with `pg_cron` and `pg_net` to call the function. Keep the public data
service key and Supabase service role key server-side only.
