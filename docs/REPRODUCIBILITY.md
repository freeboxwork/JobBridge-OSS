# 재현 가이드

## 1. 기본 데모

저장소에 포함된 공개 모델과 합성 공고만 사용합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:PYTHONPATH="$PWD\Services\jobbridge_inference"
python -m jobbridge_inference.http_server --host 127.0.0.1 --port 8787
```

`http://127.0.0.1:8787/health`가 `ok: true`, `modelVersion: lightgbm_jobseeker_preference_v1`, `postingRows: 10`을 반환하면 준비가 끝났습니다.

## 2. 모델 재학습

공공데이터포털에서 다음 두 파일을 직접 내려받아 `Data/raw`에 둡니다.

- 장애인 구직자 현황: https://www.data.go.kr/data/15014774/fileData.do
- 한국고용직업분류 직종코드: https://www.data.go.kr/data/15120487/fileData.do

파일명은 명령의 인수로 지정하므로 공식 배포 파일명과 달라도 됩니다.

```powershell
python Scripts\prepare_oss_training_dataset.py `
  --job-seekers Data\raw\disabled_job_seekers_20251231.csv `
  --job-codes Data\raw\job_codes_20230825.csv `
  --out-dir Data\processed\oss_preference_v1

python Scripts\train_oss_preference_model.py `
  --dataset Data\processed\oss_preference_v1\training_dataset.csv `
  --out-dir Models\lightgbm_jobseeker_preference_v1
```

기준 공개 가중치는 Python 3.10, pandas 2.3.0, NumPy 2.2.6, scikit-learn 1.7.2, LightGBM 4.6.0, joblib 1.5.3에서 random seed 42로 학습했습니다.

## 3. 테스트

```powershell
python -m pip install -r requirements-dev.txt
$env:PYTHONPATH="$PWD\Services\jobbridge_inference"
python -m pytest -q
```

추가 정적 검사:

```powershell
python -m compileall Scripts Services
node --check Site\api\_supabaseAdmin.js
node --check Site\api\admin\sync-live-jobs.js
```

## 4. 결과가 완전히 같지 않을 수 있는 이유

공공 파일이 갱신되거나 LightGBM·운영체제·CPU가 달라지면 마지막 소수점과 최적 반복 횟수가 달라질 수 있습니다. `dataset_metadata.json`의 원본 SHA-256과 `metrics.json`의 버전·seed·행 수를 함께 비교하세요.

## 5. 심사 재현 범위

- 모델 가중치: 저장소에 포함
- 모델·API·웹 소스: 저장소에 포함
- 기본 데이터: 합성 공고 10건 포함
- 제1유형 원본: 공식 사이트에서 재다운로드
- 제2유형 원본·파생 모델: 심사 범위와 저장소에서 제외
- 비밀키: 포함하지 않으며 선택 연동 때만 환경변수로 제공
