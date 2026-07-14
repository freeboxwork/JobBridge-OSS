# JobBridge web client

`JobBridge.dc.html` is the main PWA client. The repository's local Python server
serves this directory and the recommendation API together.

`vercel.json` intentionally contains a non-operational example API Gateway host.
Before a Vercel deployment, replace `example.execute-api...` with an API that you
control, or configure equivalent rewrites in your hosting platform. Never put
server secrets in this directory.

Pinned browser dependencies are listed in `../THIRD_PARTY_NOTICES.md` and the
contest SBOM. The optional Supabase URL and publishable key are returned by a
server function; service-role keys must remain server-side.
