# JobBridge 이메일 회원가입 설정

이 문서는 JobBridge의 이메일 회원가입을 Supabase Auth로 연결하는 최소 설정입니다.

## 1. DB 스키마 적용

Supabase SQL editor 또는 `psql`에서 아래 파일을 적용합니다.

```sql
\i supabase/schema_jobbridge_auth.sql
```

생성되는 핵심 객체:

- `public.profiles`: `auth.users`와 1:1로 연결되는 사용자 프로필
- RLS 정책: 로그인한 사용자는 본인 프로필만 `select / insert / update`
- `jobbridge_private.handle_new_auth_user()`: 신규 Auth 사용자 생성 시 프로필 row 자동 생성

## 2. Auth URL 설정

Supabase Dashboard > Authentication > URL Configuration에서 아래를 추가합니다.

- Site URL: 운영 배포 URL
- Redirect URLs:
  - `http://127.0.0.1:8787/JobBridge.dc.html`
  - 운영 배포 URL의 `JobBridge.dc.html`

이메일 링크를 열었을 때 원래 페이지로 돌아와 세션을 복원하기 위한 설정입니다.

## 3. 서버 환경변수

로컬 추론 서버 또는 Lambda에는 공개 설정과 서버 전용 설정을 분리해서 넣습니다.

```powershell
$env:SUPABASE_URL="https://<project-ref>.supabase.co"
$env:SUPABASE_ANON_KEY="<anon-or-publishable-key>"

# 서버 DB 저장/동기화 전용입니다. Site/ 브라우저 코드에 넣지 마세요.
$env:SUPABASE_SERVICE_ROLE_KEY="<service-role-key>"
```

프론트는 `/v1/auth-config`에서 `SUPABASE_URL`과 `SUPABASE_ANON_KEY`만 받아 사용합니다.

## 4. 현재 구현 범위

- 이메일 회원가입/로그인: Supabase `signInWithOtp`
- 인증 후 도전추천 게이트 통과
- 도전추천 API 요청 시 Supabase access token 전달 준비

아직 서버에서 JWT를 검증해 사용자별 추천 결과를 저장하는 단계는 별도 작업입니다.
