# JobBridge live-jobs Lambda

This lightweight AWS Lambda owns two production responsibilities:

- `GET /v1/live-jobs`: read current active postings directly from Supabase with a KST deadline filter and no process cache.
- EventBridge `{ "action": "sync" }`: call the protected Vercel collector to refresh Supabase.

The function uses only the Python standard library. Server-only environment variables are copied from the existing inference Lambda; no Supabase secret is exposed to the browser.

Recommended KST schedule:

- 06:00 (`21:00 UTC` on the previous calendar day)
- 12:00 (`03:00 UTC`)
- 18:00 (`09:00 UTC`)
- 23:30 (`14:30 UTC`)

The public route must be attached as the explicit API Gateway route `GET /v1/live-jobs`, which takes precedence over the existing `$default` inference integration.
