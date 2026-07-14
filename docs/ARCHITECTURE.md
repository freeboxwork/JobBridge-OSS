# 아키텍처

## 실행 경로

```mermaid
sequenceDiagram
    participant U as 구직자
    participant UI as Site
    participant API as Inference service
    participant M as Preference model
    participant J as Job source
    participant DB as Optional Supabase
    U->>UI: 프로필·희망조건 입력
    UI->>API: POST /v1/recommendations
    API->>M: 6개 피처로 직무 선호 prior 계산
    API->>J: 현재 공고 또는 합성 공고 조회
    API->>API: 사용자 선호·임금·지역·접근성 규칙 결합
    API-->>UI: 추천·근거·주의조건 반환
    API-->>DB: 명시적으로 켠 경우에만 서버 기록
    UI-->>U: 비교 가능한 카드로 표시
```

## 신뢰 경계

- 브라우저에는 publishable/anon 키만 둘 수 있습니다.
- Supabase service-role 키와 공공 API 키는 서버 환경변수로만 전달합니다.
- `JOBBRIDGE_RECOMMENDATION_LOGGING_ENABLED` 기본값은 `false`입니다.
- 공개 데모는 제3자 데이터 없이 `Data/demo`로 실행됩니다.
- 실제 공고는 만료일·활성 상태를 서버에서 다시 검사합니다.

## 추천 점수의 우선순위

1. 사용자가 명시한 희망 직무·임금
2. 지역과 현재 공고 조건
3. 접근성·작업환경의 주의/지원 단서
4. 공개 선호 모델의 확률과 시장 prior

장애유형 규칙은 특정 직업을 차단하는 자동 의사결정이 아니라 설명용 주의 신호입니다. 사용자는 결과를 변경하고 다른 직무를 탐색할 수 있어야 합니다.

## 배포 단위

- `Site`: 정적 웹과 최소 Vercel 서버 함수
- `jobbridge_inference`: 표준 라이브러리 HTTP 서버 또는 Lambda handler
- `jobbridge_live_jobs`: 선택형 공고 읽기·동기화 Lambda
- `supabase`: 사용자 프로필·추천·공고·역량 카탈로그 SQL
