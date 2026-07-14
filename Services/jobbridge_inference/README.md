# JobBridge inference service

This service keeps the model logic shared between Lambda and a small always-on
server such as Lightsail.

## Local run

```powershell
$env:PYTHONPATH="$PWD\Services\jobbridge_inference"
python -m jobbridge_inference.http_server --host 127.0.0.1 --port 8787
```

Open `http://127.0.0.1:8787/JobBridge.dc.html`.

The public contest build loads `Models/lightgbm_jobseeker_preference_v1` and
the fictional rows in `Data/demo` by default. It does not need external data or
credentials for this local path. The model predicts a desired-job-class prior,
not employment success or individual aptitude.

## API

`POST /v1/recommendations`

```json
{
  "modelFeatures": {
    "sido": "경기",
    "sigungu": "수원시",
    "age": 32,
    "age_group": "30s",
    "disability_type": "청각장애",
    "severity": "경증"
  },
  "scoringPreferences": {
    "desired_job_class": "정보통신 연구개발직 및 공학기술직",
    "desired_wage": "월 220~260만원"
  }
}
```

`sigungu` is optional. Send an empty value, omit it, or send `unknown` when the
user only chooses a `sido`; the service treats that as the full selected
province/city.

`desired_job_class` and `desired_wage` are post-processing inputs only. They are
not passed to the LightGBM feature frame.

## Supabase persistence

Set these variables only on the server or Lambda runtime:

```powershell
$env:SUPABASE_URL="https://<project-ref>.supabase.co"
$env:SUPABASE_SERVICE_ROLE_KEY="<service-role-key>"
$env:SUPABASE_DB_SCHEMA="jobbridge_private"
```

If either value is missing, recommendation generation still works and persistence
is reported as skipped in `report.diagnostics.persistence`.

Recommendation request/result logging is off by default so the profile input
notice remains true. Enable it only after the privacy policy is updated:

```powershell
$env:JOBBRIDGE_RECOMMENDATION_LOGGING_ENABLED="true"
```

Never expose `SUPABASE_SERVICE_ROLE_KEY` in `Site/` or browser code.
If you use the REST API recorder with `jobbridge_private`, expose that schema to
the Supabase Data API for server-side service role access only, and keep browser
grants revoked.
