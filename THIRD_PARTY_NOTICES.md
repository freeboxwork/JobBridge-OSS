# Third-Party Notices

Copyright 2026 JobBridge contributors.

JobBridge의 원저작 코드와 문서는 별도 표시가 없는 한 [Apache License 2.0](LICENSE)으로 제공됩니다. 아래 구성요소는 각 원저작자의 라이선스를 따르며 JobBridge 라이선스로 재허가되지 않습니다.

이 목록은 저장소의 잠금 파일과 정적 CDN 참조를 기준으로 작성했습니다. 배포 시 실제로 포함한 버전의 라이선스 파일과 저작권 고지를 함께 보존해야 합니다. 의존성의 자체 라이선스 파일이 이 문서보다 우선합니다.

## Python 의존성

| 구성요소 | 버전 | 라이선스 | 원본 및 라이선스 |
| --- | --- | --- | --- |
| NumPy | 2.2.6 | BSD-3-Clause | [numpy/numpy](https://github.com/numpy/numpy/tree/v2.2.6) |
| pandas | 2.3.0 | BSD-3-Clause | [pandas-dev/pandas](https://github.com/pandas-dev/pandas/tree/v2.3.0) |
| scikit-learn | 1.7.2 | BSD-3-Clause | [scikit-learn/scikit-learn](https://github.com/scikit-learn/scikit-learn/tree/1.7.2) |
| LightGBM | 4.6.0 | MIT | [microsoft/LightGBM](https://github.com/microsoft/LightGBM/tree/v4.6.0) |
| joblib | 1.5.3 | BSD-3-Clause | [joblib/joblib](https://github.com/joblib/joblib/tree/1.5.3) |

## JavaScript 및 웹 의존성

| 구성요소 | 버전/선택자 | 라이선스 | 용도 및 원본 |
| --- | --- | --- | --- |
| `@vercel/analytics` | 2.0.1 | MIT | 선택적 웹 분석, [vercel/analytics](https://github.com/vercel/analytics) |
| React | 18.3.1 | MIT | UI 런타임, [facebook/react](https://github.com/facebook/react/tree/v18.3.1) |
| React DOM | 18.3.1 | MIT | 브라우저 렌더링, [facebook/react](https://github.com/facebook/react/tree/v18.3.1) |
| Babel Standalone | 7.26.4 | MIT | 브라우저 JSX 변환, [babel/babel](https://github.com/babel/babel/tree/v7.26.4) |
| Supabase JavaScript | 2.110.4 | MIT | 인증·데이터 클라이언트, [supabase/supabase-js](https://github.com/supabase/supabase-js/tree/v2.110.4) |
| OpenLayers | 10.6.1 | BSD-2-Clause | 지도 UI, [openlayers/openlayers](https://github.com/openlayers/openlayers/tree/v10.6.1) |
| Lucide | 1.24.0 | ISC | 아이콘, [lucide-icons/lucide](https://github.com/lucide-icons/lucide/tree/1.24.0) |
| Pretendard | 1.3.9 | SIL-OFL-1.1 | 웹 글꼴, [orioncactus/pretendard](https://github.com/orioncactus/pretendard/tree/v1.3.9) |

CDN 참조도 정확한 버전으로 고정했습니다. 재현성과 공급망 안전성이 더 엄격한 배포에서는 자체 호스팅 또는 Subresource Integrity 정책을 추가하세요.

## 데이터, 모델 및 외부 서비스

공공데이터, 학습 데이터, 모델 가중치, 지도 타일과 외부 API의 이용조건은 소프트웨어 라이선스와 별개입니다. 저장소의 데이터·모델 라이선스 문서를 확인하고, 배포 지역과 이용 목적에 맞는 원출처 표시 및 약관을 지켜야 합니다. 특정 데이터가 저장소에 있다는 사실만으로 Apache License 2.0이 적용되지는 않습니다.

누락되거나 잘못된 고지를 발견하면 라이선스 이슈로 알려 주세요. 제3자 자료를 추가하는 PR에는 이 문서와 관련 잠금 파일을 함께 갱신해야 합니다.
