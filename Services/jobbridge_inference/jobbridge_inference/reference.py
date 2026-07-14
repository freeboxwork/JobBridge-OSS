from __future__ import annotations

import csv
import os
import re
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parents[2]
DEFAULT_REFERENCE_DB_PATH = PROJECT_ROOT / "Data" / "processed" / "reference" / "jobbridge_reference.db"
DEFAULT_NCS_STANDARD_CSV_PATH = PROJECT_ROOT / "Doc" / "한국산업인력공단_국가직무능력표준 정보_20251231.csv"
_NCS_STANDARD_CACHE: list[dict[str, str]] | None = None

REFERENCE_TABLES = (
    "ncs_competency_units",
    "work24_training_courses",
    "ncs_qualification_items",
    "work24_duty_dictionary",
    "api_sync_runs",
)

NCS_PREFIX_LABELS = {
    "01": "사업관리",
    "02": "경영·회계·사무",
    "03": "금융·보험",
    "04": "교육·자연·사회과학",
    "05": "법률·경찰·소방·교도·국방",
    "06": "보건·의료",
    "07": "사회복지·종교",
    "08": "문화·예술·디자인·방송",
    "09": "운전·운송",
    "10": "영업판매",
    "11": "경비·청소",
    "12": "이용·숙박·여행·오락·스포츠",
    "13": "음식서비스",
    "14": "건설",
    "15": "기계",
    "16": "재료",
    "17": "화학·바이오",
    "18": "섬유·의복",
    "19": "전기·전자",
    "20": "정보통신",
    "21": "식품가공",
    "22": "인쇄·목재·가구·공예",
    "23": "환경·에너지·안전",
    "24": "농림어업",
}

EXECUTIVE_SUPPORT_TERMS = ("경영진", "임원", "대표이사", "의전", "비서", "수행비서")
EXECUTIVE_DESIRED_TERMS = ("관리직", "임원", "부서장", "경영진")
ADVANCED_ROLE_TERMS = (
    *EXECUTIVE_SUPPORT_TERMS,
    "계획 수립",
    "성과 평가",
    "전략 수립",
    "관리 계획",
    "교육평가",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def compact_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_search_text(value: Any) -> str:
    return re.sub(r"\s+", "", safe_text(value).lower())


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def first_text(*values: Any) -> str:
    for value in values:
        text = safe_text(value)
        if text:
            return text
    return ""


def pick_key(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in source and source.get(key) not in (None, ""):
            return source.get(key)
    return None


def tokenize(*values: Any) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []
    for value in values:
        for item in as_list(value):
            text = safe_text(item)
            if not text:
                continue
            for token in re.findall(r"[0-9A-Za-z가-힣+#.]+", text):
                token = token.strip()
                if len(token) < 2:
                    continue
                lowered = token.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                tokens.append(token)
    return tokens[:12]


def ncs_prefix_for_text(text: str) -> str:
    lowered = text.lower()
    keyword_map = (
        ("20", ("정보", "it", "ict", "데이터", "개발", "소프트웨어", "sw", "ai", "인공지능", "디지털", "프로그래밍", "sql")),
        ("02", ("사무", "행정", "경영", "회계", "재무", "마케팅", "인사", "총무", "기획", "상담", "통계", "문서", "자료", "서식", "파일", "회의록")),
        ("08", ("디자인", "콘텐츠", "영상", "방송", "문화", "예술", "웹디자인")),
        ("07", ("사회복지", "복지", "돌봄", "상담복지")),
        ("06", ("보건", "의료", "간호", "요양", "재활")),
        ("10", ("영업", "판매", "고객", "cs", "매장")),
        ("11", ("경비", "청소", "환경미화", "시설관리")),
        ("13", ("음식", "조리", "제과", "카페", "외식")),
        ("14", ("건설", "토목", "건축", "교통")),
        ("15", ("기계", "생산", "제조", "정비", "가공")),
        ("19", ("전기", "전자", "로봇", "반도체")),
        ("03", ("금융", "보험", "자산")),
        ("09", ("운전", "운송", "배송", "물류")),
        ("23", ("환경", "에너지", "안전")),
        ("24", ("농업", "농림", "어업", "축산")),
    )
    for prefix, words in keyword_map:
        if any(word in lowered for word in words):
            return prefix
    return ""


def level_label(level: Any) -> str:
    try:
        numeric = int(float(str(level).strip()))
    except (TypeError, ValueError):
        return "탐색"
    if numeric <= 3:
        return "입문"
    if numeric <= 5:
        return "도전"
    return "심화"


def numeric_level(level: Any) -> int | None:
    try:
        return int(float(str(level).strip()))
    except (TypeError, ValueError):
        return None


DISABILITY_FIT_RULES = {
    "시각장애": {
        "positive": ("안마", "마사지", "헬스키퍼", "음성", "전화", "콜", "상담", "문서", "텍스트", "데이터", "입력", "정제", "사무", "행정", "컴퓨터", "접수"),
        "caution": (
            "색상",
            "시각",
            "도면",
            "cad",
            "장비",
            "수리",
            "정비",
            "계량",
            "청소",
            "경영진",
            "임원",
            "비서",
            "회의",
            "의전",
            "문서 작성관리",
        ),
        "blocked": (
            "방문자 안내",
            "방문고객",
            "고객 방문",
            "대면 안내",
            "현장 안내",
            "육안",
            "품질검사",
            "검사",
            "운전",
            "배송",
            "배달",
            "감시",
            "경비",
            "순찰",
            "조리",
            "미용",
            "위생",
            "건설",
            "현장",
            "기계",
        ),
        "caution_prefixes": {"09", "14", "15", "19", "21", "22", "23"},
        "blocked_prefixes": {"09"},
        "assistive": ("화면낭독 SW", "점자 디스플레이", "접근성 문서 템플릿", "헤드셋"),
    },
    "청각장애": {
        "positive": ("문서", "데이터", "사무", "행정", "제조", "생산", "검사", "디자인", "개발"),
        "caution": ("전화", "콜센터", "상담", "고객응대", "안내", "음성", "통역"),
        "caution_prefixes": {"10", "13"},
    },
    "지체장애": {
        "positive": ("문서", "데이터", "사무", "행정", "컴퓨터", "상담", "접수"),
        "caution": ("운반", "배송", "배달", "상하차", "현장", "건설", "청소", "조리", "생산", "포장", "장시간", "서서", "이동", "수리", "정비"),
        "caution_prefixes": {"09", "11", "13", "14", "15", "21", "22", "24"},
    },
    "뇌병변장애": {
        "positive": ("문서", "데이터", "사무", "행정", "컴퓨터", "접수"),
        "caution": ("운반", "배송", "배달", "상하차", "현장", "건설", "청소", "조리", "생산", "포장", "장시간", "서서", "이동", "수리", "정비"),
        "caution_prefixes": {"09", "11", "13", "14", "15", "21", "22", "24"},
    },
    "발달": {
        "positive": ("반복", "분류", "정리", "포장", "보조", "청소", "문서", "데이터"),
        "caution": ("고객응대", "상담", "전화", "관리자", "기획", "고난도", "위험", "복잡"),
        "caution_prefixes": {"10"},
    },
    "내부기관": {
        "positive": ("문서", "데이터", "사무", "행정", "컴퓨터", "접수"),
        "caution": ("운반", "배송", "배달", "상하차", "현장", "건설", "청소", "조리", "생산", "포장", "장시간", "서서", "이동", "야간"),
        "caution_prefixes": {"09", "11", "13", "14", "15", "21", "22", "24"},
    },
}

CAPABILITY_CATALOG = [
    {
        "id": "office",
        "label": "사무·행정",
        "summary": "문서·자료 · 일정·지원",
        "targetJobClass": "경영·회계·사무",
        "groups": [
            {
                "id": "office-docs",
                "label": "문서·자료",
                "summary": "문서 작성 · 자료 정리 등",
                "items": [
                    {"id": "office-doc-write", "label": "문서 작성", "ncsCode": "02", "definition": "문서 작성과 서식 정리"},
                    {"id": "office-data-sort", "label": "자료 정리", "ncsCode": "02", "definition": "자료 분류와 문서 정리"},
                    {"id": "office-meeting-minutes", "label": "회의록 정리", "ncsCode": "02", "definition": "회의 내용을 문서로 정리"},
                    {"id": "office-form-write", "label": "서식 작성", "ncsCode": "02", "definition": "정해진 양식에 맞춘 문서 입력"},
                    {"id": "office-file-sort", "label": "파일 분류", "ncsCode": "02", "definition": "전자 문서와 파일 분류"},
                ],
            },
            {
                "id": "office-support",
                "label": "일정·지원",
                "summary": "일정 관리 · 예약 확인 등",
                "items": [
                    {"id": "office-schedule", "label": "일정 관리", "ncsCode": "02", "definition": "일정 확인과 사무 지원"},
                    {"id": "office-reservation", "label": "예약 확인", "ncsCode": "02", "definition": "전화나 시스템 기반 예약 확인"},
                    {"id": "office-phone", "label": "전화 응대", "ncsCode": "02", "definition": "음성 기반 전화 접수와 응대"},
                    {"id": "office-visitor", "label": "방문자 안내", "ncsCode": "02", "definition": "방문자 대면 안내와 현장 응대"},
                    {"id": "office-mail", "label": "우편물 정리", "ncsCode": "02", "definition": "우편물 분류와 사무 지원"},
                ],
            },
            {
                "id": "office-general",
                "label": "총무 보조",
                "summary": "영수증 정리 · 비품 확인 등",
                "items": [
                    {"id": "office-receipts", "label": "영수증 정리", "ncsCode": "02", "definition": "영수증과 지출 내역 정리"},
                    {"id": "office-inventory", "label": "비품 재고 확인", "ncsCode": "02", "definition": "비품 재고 수량 확인"},
                    {"id": "office-expense", "label": "지출 내역 입력", "ncsCode": "02", "definition": "지출 자료 데이터 입력"},
                    {"id": "office-approval", "label": "결재 서류 준비", "ncsCode": "02", "definition": "결재 문서 준비와 자료 정리"},
                    {"id": "office-clean", "label": "사무실 정리", "ncsCode": "02", "definition": "사무공간 정리와 현장 보조"},
                ],
            },
        ],
    },
    {
        "id": "digital",
        "label": "디지털·데이터",
        "summary": "데이터 처리 · 온라인 도구",
        "targetJobClass": "정보통신",
        "groups": [
            {
                "id": "digital-data",
                "label": "데이터 처리",
                "summary": "데이터 입력 · 표 정리 등",
                "items": [
                    {"id": "digital-entry", "label": "데이터 입력", "ncsCode": "20", "definition": "정형 데이터 입력과 정제"},
                    {"id": "digital-table", "label": "표 정리", "ncsCode": "20", "definition": "스프레드시트 표 정리"},
                    {"id": "digital-sheet", "label": "스프레드시트 활용", "ncsCode": "20", "definition": "스프레드시트 기본 함수와 데이터 정리"},
                    {"id": "digital-error", "label": "오류 확인", "ncsCode": "20", "definition": "데이터 오류 점검과 수정"},
                    {"id": "digital-filename", "label": "파일 이름 정리", "ncsCode": "20", "definition": "파일명 규칙 정리와 분류"},
                ],
            },
            {
                "id": "digital-tools",
                "label": "온라인 도구",
                "summary": "이메일 · 클라우드 · 웹 도구",
                "items": [
                    {"id": "digital-email", "label": "이메일 작성", "ncsCode": "20", "definition": "업무 이메일 작성과 발송"},
                    {"id": "digital-cloud", "label": "클라우드 파일 공유", "ncsCode": "20", "definition": "온라인 파일 공유와 권한 확인"},
                    {"id": "digital-video-meeting", "label": "화상회의 준비", "ncsCode": "20", "definition": "화상회의 링크와 장비 준비"},
                    {"id": "digital-web-tool", "label": "웹 도구 사용", "ncsCode": "20", "definition": "웹 기반 업무 도구 사용"},
                    {"id": "digital-capture", "label": "화면 캡처", "ncsCode": "20", "definition": "시각 화면 캡처와 이미지 확인"},
                ],
            },
            {
                "id": "digital-check",
                "label": "기본 점검",
                "summary": "계정 · 장비 · 시스템 점검",
                "items": [
                    {"id": "digital-login", "label": "계정 로그인 확인", "ncsCode": "20", "definition": "계정 로그인과 접근 확인"},
                    {"id": "digital-device", "label": "장비 연결 확인", "ncsCode": "20", "definition": "컴퓨터 주변 장비 연결 확인"},
                    {"id": "digital-printer", "label": "프린터 사용", "ncsCode": "20", "definition": "프린터 출력과 장비 사용"},
                    {"id": "digital-system-log", "label": "시스템 점검 기록", "ncsCode": "20", "definition": "시스템 상태 기록"},
                    {"id": "digital-report", "label": "간단한 문제 보고", "ncsCode": "20", "definition": "문제 상황을 문서로 보고"},
                ],
            },
        ],
    },
    {
        "id": "service",
        "label": "고객응대·서비스",
        "summary": "문의 응대 · 매장·현장 서비스",
        "targetJobClass": "영업판매",
        "groups": [
            {
                "id": "service-contact",
                "label": "문의 응대",
                "summary": "문의 기록 · 결과 공유 등",
                "items": [
                    {"id": "service-inquiry", "label": "고객 문의 응대", "ncsCode": "10", "definition": "전화나 문서 기반 고객 문의 응대"},
                    {"id": "service-script", "label": "안내 문구 전달", "ncsCode": "10", "definition": "정해진 안내 문구 전달"},
                    {"id": "service-complaint-record", "label": "민원 내용 기록", "ncsCode": "10", "definition": "고객 민원 내용을 문서로 기록"},
                    {"id": "service-result-share", "label": "응대 결과 공유", "ncsCode": "10", "definition": "응대 결과 문서 공유"},
                    {"id": "service-wait-time", "label": "대기 시간 안내", "ncsCode": "10", "definition": "고객 대기 시간 대면 안내"},
                ],
            },
            {
                "id": "service-store",
                "label": "매장·현장 서비스",
                "summary": "진열 · 예약 · 현장 안내",
                "items": [
                    {"id": "service-store-clean", "label": "매장 정리", "ncsCode": "10", "definition": "매장 현장 정리"},
                    {"id": "service-display", "label": "상품 진열 보조", "ncsCode": "10", "definition": "상품 위치와 진열 상태 확인"},
                    {"id": "service-booking", "label": "예약 확인", "ncsCode": "10", "definition": "예약 내역 확인"},
                    {"id": "service-waiting", "label": "대기 고객 안내", "ncsCode": "10", "definition": "대기 고객 대면 안내"},
                    {"id": "service-payment", "label": "결제 보조", "ncsCode": "10", "definition": "결제 과정 현장 보조"},
                ],
            },
            {
                "id": "service-ops",
                "label": "운영 지원",
                "summary": "청결 · 분실물 · 안전 안내",
                "items": [
                    {"id": "service-clean-check", "label": "청결 상태 확인", "ncsCode": "10", "definition": "현장 청결 상태 시각 확인"},
                    {"id": "service-lost-item", "label": "분실물 접수", "ncsCode": "10", "definition": "분실물 접수와 기록"},
                    {"id": "service-safety", "label": "안전 수칙 안내", "ncsCode": "10", "definition": "현장 안전 수칙 안내"},
                    {"id": "service-repeat", "label": "반복 작업 수행", "ncsCode": "10", "definition": "정해진 절차의 반복 업무"},
                    {"id": "service-communication", "label": "동료와 소통", "ncsCode": "10", "definition": "업무 진행을 위한 동료 소통"},
                ],
            },
        ],
    },
    {
        "id": "content",
        "label": "콘텐츠·디자인",
        "summary": "콘텐츠 준비 · 이미지·편집",
        "targetJobClass": "문화·예술·디자인·방송",
        "groups": [
            {
                "id": "content-plan",
                "label": "콘텐츠 준비",
                "summary": "자료 수집 · 원고 정리",
                "items": [
                    {"id": "content-research", "label": "자료 수집", "ncsCode": "08", "definition": "콘텐츠 자료 수집"},
                    {"id": "content-script", "label": "원고 정리", "ncsCode": "08", "definition": "텍스트 원고 정리"},
                    {"id": "content-title", "label": "제목 정리", "ncsCode": "08", "definition": "콘텐츠 제목 정리"},
                    {"id": "content-schedule", "label": "배포 일정 확인", "ncsCode": "08", "definition": "콘텐츠 배포 일정 확인"},
                    {"id": "content-plan-support", "label": "콘텐츠 기획 보조", "ncsCode": "08", "definition": "콘텐츠 기획 자료 보조"},
                ],
            },
            {
                "id": "content-edit",
                "label": "이미지·편집",
                "summary": "이미지 · 색상 · 교정",
                "items": [
                    {"id": "content-image-edit", "label": "이미지 편집", "ncsCode": "08", "definition": "이미지 시각 편집"},
                    {"id": "content-photo-sort", "label": "사진 정리", "ncsCode": "08", "definition": "사진 파일 분류와 정리"},
                    {"id": "content-color", "label": "색상 맞춤", "ncsCode": "08", "definition": "색상과 시각 요소 조정"},
                    {"id": "content-thumbnail", "label": "간단한 썸네일 제작", "ncsCode": "08", "definition": "썸네일 이미지 제작"},
                    {"id": "content-typo", "label": "오탈자 확인", "ncsCode": "08", "definition": "텍스트 오탈자 확인과 교정"},
                ],
            },
            {
                "id": "content-publish",
                "label": "파일·게시",
                "summary": "파일 관리 · 게시 · 검수",
                "items": [
                    {"id": "content-file", "label": "파일 관리", "ncsCode": "08", "definition": "콘텐츠 파일 관리"},
                    {"id": "content-post", "label": "게시물 등록", "ncsCode": "08", "definition": "게시물 등록과 확인"},
                    {"id": "content-portfolio", "label": "포트폴리오 정리", "ncsCode": "08", "definition": "포트폴리오 파일 정리"},
                    {"id": "content-link", "label": "링크 확인", "ncsCode": "08", "definition": "게시 링크 확인"},
                    {"id": "content-proofread", "label": "교정 검수", "ncsCode": "08", "definition": "텍스트 교정 검수"},
                ],
            },
        ],
    },
    {
        "id": "field",
        "label": "제조·현장",
        "summary": "생산 보조 · 품질·안전",
        "targetJobClass": "생산·제조",
        "groups": [
            {
                "id": "field-production",
                "label": "생산 보조",
                "summary": "작업 순서 · 조립 · 기록",
                "items": [
                    {"id": "field-sequence", "label": "작업 순서 준수", "ncsCode": "15", "definition": "현장 작업 순서 준수"},
                    {"id": "field-part-sort", "label": "부품 정리", "ncsCode": "15", "definition": "부품 분류와 정리"},
                    {"id": "field-assembly", "label": "조립 보조", "ncsCode": "15", "definition": "기계·부품 조립 보조"},
                    {"id": "field-repeat", "label": "반복 작업 수행", "ncsCode": "15", "definition": "반복 생산 업무"},
                    {"id": "field-record", "label": "작업 기록", "ncsCode": "15", "definition": "작업 결과 기록"},
                ],
            },
            {
                "id": "field-quality",
                "label": "품질·안전",
                "summary": "품질 확인 · 보호구 · 현장 안전",
                "items": [
                    {"id": "field-quality-check", "label": "품질 확인", "ncsCode": "15", "definition": "육안 품질검사와 불량 확인"},
                    {"id": "field-defect", "label": "불량품 분류", "ncsCode": "15", "definition": "불량품 시각 분류"},
                    {"id": "field-tool", "label": "도구 사용", "ncsCode": "15", "definition": "현장 도구 사용"},
                    {"id": "field-protection", "label": "보호구 착용", "ncsCode": "15", "definition": "현장 보호구 착용"},
                    {"id": "field-safety", "label": "현장 안전 확인", "ncsCode": "15", "definition": "현장 안전 상태 확인"},
                ],
            },
            {
                "id": "field-logistics",
                "label": "물류·재고",
                "summary": "입출고 · 포장 · 운반",
                "items": [
                    {"id": "field-stock", "label": "재고 정리", "ncsCode": "15", "definition": "재고 물품 정리"},
                    {"id": "field-inout", "label": "입출고 확인", "ncsCode": "15", "definition": "입출고 수량 확인"},
                    {"id": "field-pack", "label": "포장 보조", "ncsCode": "15", "definition": "상품 포장 보조"},
                    {"id": "field-label", "label": "라벨 부착", "ncsCode": "15", "definition": "상품 라벨 부착"},
                    {"id": "field-carry", "label": "운반 보조", "ncsCode": "15", "definition": "물품 운반 보조"},
                ],
            },
        ],
    },
    {
        "id": "care",
        "label": "돌봄·보건",
        "summary": "생활 지원 · 보건 행정",
        "targetJobClass": "보건·의료",
        "groups": [
            {
                "id": "care-life",
                "label": "생활 지원",
                "summary": "식사 · 이동 · 활동 기록",
                "items": [
                    {"id": "care-meal", "label": "식사 보조", "ncsCode": "06", "definition": "식사와 생활 지원"},
                    {"id": "care-move", "label": "이동 보조", "ncsCode": "06", "definition": "대상자 이동 보조"},
                    {"id": "care-record", "label": "활동 기록", "ncsCode": "06", "definition": "활동 내용 문서 기록"},
                    {"id": "care-item", "label": "물품 정리", "ncsCode": "06", "definition": "생활 물품 정리"},
                    {"id": "care-talk", "label": "말벗 지원", "ncsCode": "06", "definition": "음성 대화와 정서 지원"},
                ],
            },
            {
                "id": "care-admin",
                "label": "보건 행정",
                "summary": "접수 · 검진 일정 · 의료 서류",
                "items": [
                    {"id": "care-reception", "label": "접수 안내", "ncsCode": "06", "definition": "대면 접수 안내"},
                    {"id": "care-schedule", "label": "검진 일정 확인", "ncsCode": "06", "definition": "검진 일정 확인과 전화 안내"},
                    {"id": "care-questionnaire", "label": "문진표 정리", "ncsCode": "06", "definition": "문진표 문서 정리"},
                    {"id": "care-medical-doc", "label": "의료 서류 정리", "ncsCode": "06", "definition": "의료 서류 분류와 정리"},
                    {"id": "care-waiting-room", "label": "대기실 안내", "ncsCode": "06", "definition": "대기실 현장 안내"},
                ],
            },
            {
                "id": "care-safety",
                "label": "위생·안전",
                "summary": "위생 · 소독 · 보호자 안내",
                "items": [
                    {"id": "care-hygiene", "label": "위생 상태 확인", "ncsCode": "06", "definition": "위생 상태 시각 확인"},
                    {"id": "care-disinfect", "label": "소독 물품 준비", "ncsCode": "06", "definition": "소독 물품 준비"},
                    {"id": "care-path", "label": "안전 동선 확인", "ncsCode": "06", "definition": "현장 안전 동선 확인"},
                    {"id": "care-emergency", "label": "응급 연락 전달", "ncsCode": "06", "definition": "응급 연락 전달"},
                    {"id": "care-guardian", "label": "보호자 안내", "ncsCode": "06", "definition": "보호자 대면 안내"},
                ],
            },
        ],
    },
]


def disability_rule_key(disability_type: str) -> str:
    disability = safe_text(disability_type)
    if disability in ("지적장애", "자폐성장애"):
        return "발달"
    if disability in ("신장장애", "심장장애", "간장애", "호흡기장애", "장루요루장애", "뇌전증장애"):
        return "내부기관"
    return disability


class ReferenceRepository:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or Path(os.getenv("JOBBRIDGE_REFERENCE_DB_PATH", str(DEFAULT_REFERENCE_DB_PATH)))
        self._runtime_db_path: Path | None = None

    @classmethod
    def from_env(cls) -> "ReferenceRepository":
        return cls()

    def capabilities(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        normalized = self._normalize_request(request or {})
        categories: list[dict[str, Any]] = []
        totals = {"items": 0, "suitable": 0, "caution": 0, "blocked": 0, "review": 0}
        capability_catalog, capability_source, fallback_reason = self._load_capability_catalog()

        for category in capability_catalog:
            next_category = {
                "id": category["id"],
                "label": category["label"],
                "summary": category.get("summary", ""),
                "targetJobClass": category.get("targetJobClass", ""),
                "groups": [],
            }
            category_counts = {"items": 0, "suitable": 0, "caution": 0, "blocked": 0, "review": 0}
            for group in category.get("groups", []):
                next_group = {
                    "id": group["id"],
                    "label": group["label"],
                    "summary": group.get("summary", ""),
                    "items": [],
                }
                group_counts = {"items": 0, "suitable": 0, "caution": 0, "blocked": 0, "review": 0}
                for item in group.get("items", []):
                    candidate = {
                        "title": item.get("label"),
                        "targetJobClass": category.get("targetJobClass"),
                        "definition": item.get("definition"),
                        "keyword": " ".join([safe_text(group.get("label")), safe_text(category.get("label"))]),
                        "ncsCode": item.get("ncsCode"),
                    }
                    fit = self._disability_fit(candidate, normalized)
                    fit_level = safe_text(fit.get("level"), "review")
                    bucket = "blocked" if fit_level == "blocked" else ("caution" if fit_level in ("caution", "review") else "suitable")
                    group_counts["items"] += 1
                    group_counts[bucket] += 1
                    category_counts["items"] += 1
                    category_counts[bucket] += 1
                    totals["items"] += 1
                    totals[bucket] += 1
                    next_group["items"].append(
                        {
                            "id": item["id"],
                            "label": item["label"],
                            "ncsCode": item.get("ncsCode", ""),
                            "definition": item.get("definition", ""),
                            "fitLevel": fit_level,
                            "fitLabel": safe_text(fit.get("label"), "확인 필요"),
                            "isSelectable": fit_level != "blocked",
                            "supportNeeds": list(fit.get("assistiveSupports") or []),
                            "notes": list(fit.get("notes") or [])[:2],
                        }
                    )
                next_group["counts"] = group_counts
                next_category["groups"].append(next_group)
            next_category["counts"] = category_counts
            categories.append(next_category)

        return {
            "ok": True,
            "source": capability_source,
            "version": "capability_catalog_v1",
            "profile": normalized["profile"],
            "categories": categories,
            "counts": totals,
            "meta": {
                "latencyMs": round((time.perf_counter() - started) * 1000, 2),
                "fitPolicy": "장애유형·중증도 기능 요구도 게이트를 역량 선택 단계와 도전추천 단계에 동일하게 적용합니다.",
                "catalogSource": capability_source,
                "fallbackReason": fallback_reason,
            },
        }

    def ncs_capabilities(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        request = request or {}
        normalized = self._normalize_request(request)
        payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
        query = first_text(
            request.get("q"),
            request.get("query"),
            payload.get("q"),
            payload.get("query"),
        )
        try:
            limit = max(1, min(int(first_text(request.get("limit"), payload.get("limit"), 24)), 50))
        except ValueError:
            limit = 24
        rows = self._search_ncs_standard_rows(query, limit=max(limit * 8, 80))
        items: list[dict[str, Any]] = []
        for row in rows:
            candidate = {
                "title": row["name"],
                "targetJobClass": self._prefix_label(row["code"]),
                "definition": row["name"],
                "keyword": "",
                "ncsCode": row["code"],
            }
            fit = self._disability_fit(candidate, normalized)
            fit_level = safe_text(fit.get("level"), "review")
            items.append(
                {
                    "id": f"ncs-{row['code']}",
                    "label": row["name"],
                    "ncsCode": row["code"],
                    "definition": row["name"],
                    "targetJobClass": self._prefix_label(row["code"]),
                    "level": row.get("level", ""),
                    "trainingHours": row.get("trainingHours", ""),
                    "fitLevel": fit_level,
                    "fitLabel": safe_text(fit.get("label"), "확인 필요"),
                    "isSelectable": fit_level != "blocked",
                    "supportNeeds": list(fit.get("assistiveSupports") or []),
                    "notes": list(fit.get("notes") or [])[:2],
                    "source": "ncs_standard_csv_20251231",
                    "_fitRank": self._ncs_fit_sort_rank(fit_level),
                    "_searchScore": int(row.get("_searchScore") or 0),
                }
            )
        items.sort(
            key=lambda item: (
                item.get("_fitRank", 9),
                -int(item.get("_searchScore") or 0),
                safe_text(item.get("ncsCode")),
                safe_text(item.get("label")),
            )
        )
        visible_items = [
            {key: value for key, value in item.items() if key not in {"_fitRank", "_searchScore"}}
            for item in items[:limit]
        ]
        counts = {"items": len(visible_items), "suitable": 0, "caution": 0, "blocked": 0, "review": 0}
        for item in visible_items:
            fit_level = safe_text(item.get("fitLevel"), "review")
            bucket = "blocked" if fit_level == "blocked" else ("caution" if fit_level == "caution" else ("review" if fit_level == "review" else "suitable"))
            counts[bucket] += 1
        return {
            "ok": True,
            "source": "ncs_standard_csv_20251231",
            "version": "ncs_capability_search_v1",
            "query": query,
            "items": visible_items,
            "counts": counts,
            "meta": {
                "latencyMs": round((time.perf_counter() - started) * 1000, 2),
                "csvRows": len(self._ncs_standard_rows()),
                "fitPolicy": "NCS 원천 역량을 검색한 뒤 장애유형·중증도 기준으로 선택 가능 여부를 표시합니다.",
            },
        }

    def _ncs_fit_sort_rank(self, fit_level: str) -> int:
        return {
            "preferred": 0,
            "supported": 0,
            "suitable": 0,
            "caution": 1,
            "review": 2,
            "unknown": 2,
            "blocked": 3,
        }.get(safe_text(fit_level), 2)

    def _ncs_standard_csv_path(self) -> Path:
        env_path = safe_text(os.getenv("JOBBRIDGE_NCS_STANDARD_CSV_PATH"))
        if env_path:
            candidate = Path(env_path)
            return candidate if candidate.is_absolute() else (PROJECT_ROOT / candidate).resolve()
        if DEFAULT_NCS_STANDARD_CSV_PATH.exists():
            return DEFAULT_NCS_STANDARD_CSV_PATH
        matches = sorted((PROJECT_ROOT / "Doc").glob("*국가직무능력표준*20251231.csv"))
        return matches[0] if matches else DEFAULT_NCS_STANDARD_CSV_PATH

    def _ncs_standard_rows(self) -> list[dict[str, str]]:
        global _NCS_STANDARD_CACHE
        if _NCS_STANDARD_CACHE is not None:
            return _NCS_STANDARD_CACHE
        path = self._ncs_standard_csv_path()
        rows: list[dict[str, str]] = []
        if not path.exists():
            _NCS_STANDARD_CACHE = rows
            return rows
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "cp949", "euc-kr"):
            try:
                with path.open("r", encoding=encoding, newline="") as handle:
                    reader = csv.DictReader(handle)
                    for raw in reader:
                        code = first_text(raw.get("분류번호"), raw.get("NCS_CL_CD"), raw.get("ncs_cl_cd"))
                        name = first_text(raw.get("명칭"), raw.get("name"), raw.get("NCS_CL_NM"))
                        if not code or not name:
                            continue
                        rows.append(
                            {
                                "code": code,
                                "name": name,
                                "level": safe_text(raw.get("수준")),
                                "trainingHours": safe_text(raw.get("훈련시간")),
                                "search": normalize_search_text(f"{code} {name} {raw.get('수준', '')} {raw.get('훈련시간', '')}"),
                            }
                        )
                _NCS_STANDARD_CACHE = rows
                return rows
            except UnicodeDecodeError as error:
                rows = []
                last_error = error
                continue
        if last_error:
            raise last_error
        _NCS_STANDARD_CACHE = rows
        return rows

    def _search_ncs_standard_rows(self, query: str, limit: int) -> list[dict[str, Any]]:
        needle = normalize_search_text(query)
        if len(needle) < 2:
            return []
        scored: list[tuple[int, dict[str, str]]] = []
        for row in self._ncs_standard_rows():
            code = safe_text(row.get("code"))
            name = safe_text(row.get("name"))
            search = safe_text(row.get("search"))
            score = 0
            if normalize_search_text(name) == needle:
                score += 120
            if normalize_search_text(name).startswith(needle):
                score += 95
            if needle in normalize_search_text(name):
                score += 80
            if code.replace("_", "").lower().startswith(needle):
                score += 70
            if needle in search:
                score += 40
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], item[1].get("code", ""), item[1].get("name", "")))
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for score, row in scored:
            key = safe_text(row.get("code")) or safe_text(row.get("name"))
            if key in seen:
                continue
            seen.add(key)
            row_with_score = dict(row)
            row_with_score["_searchScore"] = score
            deduped.append(row_with_score)
            if len(deduped) >= limit:
                break
        return deduped

    def _load_capability_catalog(self) -> tuple[list[dict[str, Any]], str, str]:
        mode = os.getenv("JOBBRIDGE_CAPABILITY_CATALOG_SOURCE", "auto").strip().lower()
        local_source = "jobbridge_capability_catalog_v1"
        if mode in {"local", "file", "static"}:
            return CAPABILITY_CATALOG, local_source, ""
        try:
            from .persistence import SupabaseRecorder

            recorder = SupabaseRecorder.from_env()
            if recorder.enabled:
                catalog = recorder.fetch_capability_catalog()
                if catalog:
                    return catalog, "supabase_capability_catalog_v1", ""
                return CAPABILITY_CATALOG, local_source, "Supabase capability catalog is empty; using local catalog"
            elif mode == "supabase":
                return CAPABILITY_CATALOG, local_source, "Supabase credentials are not configured; using local catalog"
        except Exception as exc:
            return CAPABILITY_CATALOG, local_source, "Supabase capability catalog is unavailable; using local catalog"
        return CAPABILITY_CATALOG, local_source, ""

    def summary(self) -> dict[str, Any]:
        started = time.perf_counter()
        base = self._base_summary()
        if not self.db_path.exists():
            base["referenceDb"]["status"] = "missing"
            base["diagnostics"]["latencyMs"] = round((time.perf_counter() - started) * 1000, 2)
            return base

        try:
            with self._connect() as conn:
                table_counts = self._table_counts(conn)
                base["tables"] = table_counts
                base["latestSync"] = self._latest_sync(conn, limit=8)
                has_reference_rows = any(
                    (table_counts.get(name) or {}).get("rows", 0) > 0
                    for name in REFERENCE_TABLES
                    if name != "api_sync_runs"
                )
                base["referenceDb"].update(
                    {
                        "readable": True,
                        "status": "ready" if has_reference_rows else "empty",
                    }
                )
        except Exception as exc:
            base["referenceDb"].update({"readable": False, "status": "unreadable"})
            base["diagnostics"]["error"] = f"{type(exc).__name__}: {exc}"
        base["diagnostics"]["latencyMs"] = round((time.perf_counter() - started) * 1000, 2)
        return base

    def challenge_recommendations(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        normalized = self._normalize_request(request)
        summary = self.summary()
        cards: list[dict[str, Any]] = []
        status = (summary.get("referenceDb") or {}).get("status")

        if status == "ready":
            try:
                with self._connect() as conn:
                    cards = self._build_cards(conn, normalized, limit=5)
            except Exception as exc:
                summary.setdefault("diagnostics", {})["recommendationError"] = f"{type(exc).__name__}: {exc}"
        challenge_recs = self._cards_to_challenge_recs(cards)

        return {
            "requestId": str(uuid.uuid4()),
            "generatedAt": utc_now_iso(),
            "source": "reference_sqlite_rules_v1",
            "referenceSummary": summary,
            "profile": normalized["profile"],
            "scoringPreferences": normalized["scoringPreferences"],
            "challengeRecommendations": cards,
            "cards": cards,
            "challengeRecs": challenge_recs,
            "fallback": {
                "used": len(cards) == 0,
                "reason": self._fallback_reason(summary, cards),
            },
            "diagnostics": {
                "latencyMs": round((time.perf_counter() - started) * 1000, 2),
                "challengeRecommendationVersion": "challenge_xai_contract_v1",
                "ncsMappingMode": "reference_cache_unreviewed",
                "scorePolicy": "Candidates that conflict with disability/severity functional requirements are removed first. Remaining challenge jobs are ranked by role similarity, NCS evidence, preparation path evidence, and accommodation feasibility.",
                "referenceDbStatus": status,
                "referenceDbExists": bool((summary.get("referenceDb") or {}).get("exists")),
                "tableRows": {
                    name: (summary.get("tables", {}).get(name) or {}).get("rows", 0)
                    for name in REFERENCE_TABLES
                },
                "candidateKeywords": normalized["keywords"],
                "inferredNcsPrefix": normalized["inferredNcsPrefix"],
            },
        }

    def _cards_to_challenge_recs(self, cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            self._card_to_challenge_rec(card, rank=index + 1)
            for index, card in enumerate(cards)
        ]

    def _card_to_challenge_rec(self, card: dict[str, Any], rank: int) -> dict[str, Any]:
        trainings = list(card.get("recommendedTrainings") or [])
        qualifications = list(card.get("relatedQualifications") or [])
        ncs_units = list(card.get("requiredNcsUnits") or [])
        score_meta = self._score_metadata(ncs_units, trainings, qualifications)
        return {
            "id": safe_text(card.get("id"), f"challenge-{rank}"),
            "rank": rank,
            "jobClass": safe_text(card.get("targetJobClass"), "NCS 참조 직무"),
            "displayTitle": safe_text(card.get("title"), "도전 추천").replace(" 도전", ""),
            "challengeScore": int(card.get("score") or 0),
            "readinessScore": None,
            "readinessStatus": "pending_detail_profile",
            "accessPathScore": score_meta["accessPathScore"],
            "scoreCompleteness": score_meta["scoreCompleteness"],
            "missingScoreInputs": score_meta["missingScoreInputs"],
            "ncsMappingMode": "reference_cache_unreviewed",
            "disabilityFit": card.get("disabilityFit") or {},
            "fitReview": card.get("fitReview") or {},
            "supportNeeds": list(card.get("supportNeeds") or []),
            "skillGaps": list(card.get("skillGaps") or []),
            "primaryAction": safe_text(card.get("primaryAction")),
            "ncsUnits": ncs_units,
            "gapItems": [
                {
                    "name": "세부 숙련도 평가",
                    "status": "pending_detail_profile",
                    "action": "선택한 역량은 후보 검색에 반영했습니다. 로그인 후 교육 이수, 자격, 경력 정보를 추가하면 세부 준비도를 산정할 수 있습니다.",
                }
            ],
            "trainingSuggestions": trainings,
            "qualificationSuggestions": qualifications,
            "explanations": self._xai_explanations(card),
            "nextActions": list(card.get("nextActions") or []),
            "summary": safe_text(card.get("summary")),
            "dataNotice": safe_text(
                card.get("dataNotice"),
                "현재는 참조 DB와 자동 매핑 기반 예상 준비 항목입니다. 관리자 검수 전에는 공식 NCS 역량 갭으로 표현하지 않습니다.",
            ),
        }

    def _score_metadata(
        self,
        ncs_units: list[dict[str, Any]],
        trainings: list[dict[str, Any]],
        qualifications: list[dict[str, Any]],
    ) -> dict[str, Any]:
        missing = ["user_capabilities", "ncs_competency_factors", "ncs_ksa"]
        completeness = 0.35
        if ncs_units:
            completeness += 0.15
        if trainings:
            completeness += 0.15
        if qualifications:
            completeness += 0.10
        access_path_score = min(100, len(trainings) * 30 + len(qualifications) * 12)
        return {
            "accessPathScore": access_path_score if access_path_score > 0 else None,
            "scoreCompleteness": round(min(completeness, 0.70), 2),
            "missingScoreInputs": missing,
        }

    def _xai_explanations(self, card: dict[str, Any]) -> dict[str, list[str]]:
        reasons = [safe_text(reason) for reason in (card.get("reasons") or []) if safe_text(reason)]
        disability_fit = card.get("disabilityFit") or {}
        disability_notes = [safe_text(note) for note in (disability_fit.get("notes") or []) if safe_text(note)]
        ncs_reasons = [
            reason
            for reason in reasons
            if any(token in reason for token in ("NCS", "Work24", "자격", "훈련"))
        ]
        other_reasons = [reason for reason in reasons if reason not in ncs_reasons]
        return {
            "model": [
                "도전 추천은 맞춤 추천 점수를 그대로 재사용하지 않고, 장애유형·중증도 적합도와 준비 경로를 함께 반영해 별도 산정합니다.",
                *disability_notes[:1],
            ],
            "ncs": ncs_reasons[:3] or ["NCS/Work24 참조 DB에서 자동 추정한 준비 항목입니다."],
            "profileGap": [
                "선택한 역량은 후보 검색에 반영했고, 세부 숙련도 평가는 로그인 후 교육·자격·경력 정보가 추가되면 확장됩니다."
            ],
            "dataLimit": [
                "이 카드는 관리자 검수 전 자동 매핑 후보입니다.",
                *other_reasons[:2],
            ],
        }

    def _connect(self) -> sqlite3.Connection:
        db_path = self._connectable_db_path()
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _connectable_db_path(self) -> Path:
        if not os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
            return self.db_path
        if self._runtime_db_path and self._runtime_db_path.exists():
            return self._runtime_db_path
        if not self.db_path.exists():
            return self.db_path
        tmp_path = Path("/tmp/jobbridge_reference.db")
        shutil.copy2(self.db_path, tmp_path)
        self._runtime_db_path = tmp_path
        return tmp_path

    def _base_summary(self) -> dict[str, Any]:
        exists = self.db_path.exists()
        return {
            "ok": True,
            "generatedAt": utc_now_iso(),
            "referenceDb": {
                "path": str(self.db_path),
                "exists": exists,
                "readable": False,
                "status": "missing",
                "sizeBytes": self.db_path.stat().st_size if exists else 0,
            },
            "tables": {
                name: {"exists": False, "rows": 0}
                for name in REFERENCE_TABLES
            },
            "latestSync": [],
            "diagnostics": {
                "latencyMs": 0.0,
                "secretsExposed": False,
            },
        }

    def _table_counts(self, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        counts: dict[str, dict[str, Any]] = {}
        existing = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in REFERENCE_TABLES:
            if table not in existing:
                counts[table] = {"exists": False, "rows": 0}
                continue
            rows = conn.execute(f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()["c"]
            counts[table] = {"exists": True, "rows": int(rows or 0)}
        return counts

    def _latest_sync(self, conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
        if not self._table_exists(conn, "api_sync_runs"):
            return []
        rows = conn.execute(
            """
            SELECT source, target, status, started_at, finished_at,
                   rows_inserted, rows_fetched, pages_requested, error
            FROM api_sync_runs
            ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 20)),),
        ).fetchall()
        return [
            {
                "source": safe_text(row["source"]),
                "target": safe_text(row["target"]),
                "status": safe_text(row["status"]),
                "startedAt": safe_text(row["started_at"]),
                "finishedAt": safe_text(row["finished_at"]),
                "rowsInserted": int(row["rows_inserted"] or 0),
                "rowsFetched": int(row["rows_fetched"] or 0),
                "pagesRequested": int(row["pages_requested"] or 0),
                "hasError": bool(safe_text(row["error"])),
            }
            for row in rows
        ]

    def _normalize_request(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
        profile = {}
        for source in (
            request.get("modelFeatures"),
            request.get("model_features"),
            request.get("clientProfile"),
            request.get("profile"),
        ):
            if isinstance(source, dict):
                profile.update(source)
        scoring = dict(request.get("scoringPreferences") or request.get("scoring_preferences") or {})
        for source in (
            payload.get("modelFeatures"),
            payload.get("model_features"),
            payload.get("clientProfile"),
            payload.get("profile"),
        ):
            if isinstance(source, dict):
                profile.update(source)
        scoring.update(payload.get("scoringPreferences") or payload.get("scoring_preferences") or {})

        desired_job_class = first_text(
            pick_key(scoring, ("desired_job_class", "desiredJobClass", "desiredJob_class", "targetJobClass")),
            pick_key(profile, ("desired_job_class", "desiredJobClass", "desiredJob_class", "targetJobClass", "desiredJobTitle", "desired_job_title", "jobTitle")),
        )
        desired_wage = first_text(
            pick_key(scoring, ("desired_wage", "desiredWage")),
            pick_key(profile, ("desired_wage", "desiredWage")),
        )
        region = first_text(
            pick_key(profile, ("sido", "region", "preferredRegion", "location")),
            pick_key(scoring, ("sido", "region", "preferredRegion")),
        )
        disability_type = first_text(
            pick_key(profile, ("disability_type", "disabilityType", "disability")),
            pick_key(scoring, ("disability_type", "disabilityType", "disability")),
        )
        severity = first_text(
            pick_key(profile, ("severity", "disabilitySeverity")),
            pick_key(scoring, ("severity", "disabilitySeverity")),
        )
        capability_values: list[Any] = []
        for source in (
            request.get("capabilities"),
            payload.get("capabilities"),
            profile.get("selfReportedCapabilities"),
        ):
            for item in as_list(source):
                if isinstance(item, dict):
                    capability_values.append(
                        first_text(
                            pick_key(item, ("name", "title", "label", "unitName", "unit", "description")),
                            item,
                        )
                    )
                else:
                    capability_values.append(item)
        skills = [
            *as_list(pick_key(profile, ("skills", "skillKeywords", "strengths", "experienceKeywords"))),
            *capability_values,
        ]
        interests = as_list(pick_key(profile, ("interests", "preferredTasks", "careerInterests")))
        extra_text = " ".join(safe_text(item) for item in [desired_job_class, *skills, *interests] if safe_text(item))
        keywords = tokenize(extra_text)
        if "데이터" in extra_text and "SQL" not in {item.upper() for item in keywords}:
            keywords.extend(["빅데이터", "SQL"])
        if "사무" in extra_text and "행정" not in keywords:
            keywords.append("행정")
        keywords = tokenize(*keywords)
        inferred_prefix = safe_text(pick_key(scoring, ("ncsPrefix", "ncs_prefix")), ncs_prefix_for_text(extra_text))
        return {
            "profile": {
                **profile,
                "disability_type": disability_type,
                "disabilityType": disability_type,
                "severity": severity,
                "desiredJobClass": desired_job_class,
                "region": region,
            },
            "scoringPreferences": {
                **scoring,
                "desired_job_class": desired_job_class,
                "desired_wage": desired_wage,
            },
            "keywords": keywords,
            "inferredNcsPrefix": inferred_prefix,
        }

    def _build_cards(self, conn: sqlite3.Connection, normalized: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        candidates = self._duty_candidates(conn, normalized)
        if len(candidates) < limit:
            candidates.extend(self._unit_candidates(conn, normalized, exclude=candidates, limit=limit * 2))
        cards: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_titles: set[str] = set()
        preferred_prefix = safe_text(normalized.get("inferredNcsPrefix"))
        def sort_key(item: dict[str, Any]) -> tuple[int, int]:
            code_prefix = self._code_prefix(item.get("ncsCode"))
            prefix_rank = 0 if not preferred_prefix or code_prefix == preferred_prefix else 1
            return (prefix_rank, -int(item.get("candidateScore") or 0))

        for candidate in sorted(candidates, key=sort_key):
            if self._is_unrequested_advanced_role(candidate, normalized):
                continue
            fit = candidate.get("disabilityFit") or self._disability_fit(candidate, normalized)
            if fit.get("level") == "blocked":
                continue
            card = self._candidate_to_card(conn, candidate, normalized, rank=len(cards) + 1)
            title_key = safe_text(card.get("title")).replace(" 도전", "")
            if card["id"] in seen_ids or title_key in seen_titles:
                continue
            seen_ids.add(card["id"])
            seen_titles.add(title_key)
            cards.append(card)
            if len(cards) >= max(3, min(int(limit), 5)):
                break
        return cards

    def _duty_candidates(self, conn: sqlite3.Connection, normalized: dict[str, Any]) -> list[dict[str, Any]]:
        if not self._table_exists(conn, "work24_duty_dictionary"):
            return []
        keywords = normalized["keywords"] or [normalized["scoringPreferences"].get("desired_job_class")]
        keywords = [item for item in keywords if safe_text(item)]
        rows: list[sqlite3.Row] = []
        if keywords:
            clauses = []
            params: list[str] = []
            for keyword in keywords[:6]:
                like = f"%{keyword}%"
                clauses.append(
                    "(keyword LIKE ? OR ability_name LIKE ? OR job_lcfn LIKE ? OR job_mcn LIKE ? OR job_scfn LIKE ? OR job_sdvn LIKE ?)"
                )
                params.extend([like] * 6)
            rows = conn.execute(
                f"""
                SELECT id, keyword, ability_name, job_lcfn, job_mcn, job_scfn, job_sdvn,
                       ablt_unit, ablt_def
                FROM work24_duty_dictionary
                WHERE {" OR ".join(clauses)}
                LIMIT 80
                """,
                params,
            ).fetchall()
        if not rows and normalized["inferredNcsPrefix"]:
            rows = conn.execute(
                """
                SELECT id, keyword, ability_name, job_lcfn, job_mcn, job_scfn, job_sdvn,
                       ablt_unit, ablt_def
                FROM work24_duty_dictionary
                WHERE ablt_unit LIKE ?
                LIMIT 80
                """,
                (f"{normalized['inferredNcsPrefix']}%",),
            ).fetchall()
        candidates = [self._score_duty_row(row, normalized) for row in rows]
        return self._dedupe_candidates(candidates)

    def _unit_candidates(
        self,
        conn: sqlite3.Connection,
        normalized: dict[str, Any],
        exclude: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        if not self._table_exists(conn, "ncs_competency_units"):
            return []
        excluded_codes = {safe_text(item.get("ncsCode")) for item in exclude}
        keywords = normalized["keywords"]
        rows: list[sqlite3.Row] = []
        if keywords:
            clauses = []
            params: list[str] = []
            for keyword in keywords[:6]:
                clauses.append("(name LIKE ? OR ncs_cl_cd LIKE ?)")
                params.extend([f"%{keyword}%", f"{keyword}%"])
            rows = conn.execute(
                f"""
                SELECT ncs_cl_cd, name, definition, level, training_hours, source
                FROM ncs_competency_units
                WHERE {" OR ".join(clauses)}
                LIMIT ?
                """,
                [*params, max(20, limit * 8)],
            ).fetchall()
        if len(rows) < limit and normalized["inferredNcsPrefix"]:
            rows.extend(
                conn.execute(
                    """
                    SELECT ncs_cl_cd, name, definition, level, training_hours, source
                    FROM ncs_competency_units
                    WHERE ncs_cl_cd LIKE ?
                    ORDER BY ncs_cl_cd
                    LIMIT ?
                    """,
                    (f"{normalized['inferredNcsPrefix']}%", max(20, limit * 8)),
                ).fetchall()
            )
        candidates = []
        for row in rows:
            code = safe_text(row["ncs_cl_cd"])
            if code in excluded_codes:
                continue
            candidates.append(self._score_unit_row(row, normalized))
        return self._dedupe_candidates(candidates)

    def _score_duty_row(self, row: sqlite3.Row, normalized: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(
            safe_text(row[key])
            for key in ("keyword", "ability_name", "job_lcfn", "job_mcn", "job_scfn", "job_sdvn", "ablt_def")
        )
        ablt_unit = safe_text(row["ablt_unit"])
        score = self._keyword_score(text, normalized)
        if normalized["inferredNcsPrefix"] and ablt_unit.startswith(normalized["inferredNcsPrefix"]):
            score += 14
        candidate = {
            "source": "work24_duty_dictionary",
            "candidateScore": score,
            "ncsCode": ablt_unit,
            "title": safe_text(row["ability_name"], safe_text(row["job_sdvn"], "직무역량")),
            "targetJobClass": self._target_job_class(row),
            "definition": safe_text(row["ablt_def"]),
            "keyword": safe_text(row["keyword"]),
        }
        fit = self._disability_fit(candidate, normalized)
        candidate["candidateScore"] = max(10, min(110, score + int(fit.get("scoreDelta") or 0)))
        candidate["disabilityFit"] = fit
        return candidate

    def _score_unit_row(self, row: sqlite3.Row, normalized: dict[str, Any]) -> dict[str, Any]:
        code = safe_text(row["ncs_cl_cd"])
        text = " ".join(safe_text(row[key]) for key in ("ncs_cl_cd", "name", "definition"))
        score = self._keyword_score(text, normalized)
        if normalized["inferredNcsPrefix"] and code.startswith(normalized["inferredNcsPrefix"]):
            score += 10
        candidate = {
            "source": "ncs_competency_units",
            "candidateScore": score,
            "ncsCode": code,
            "title": safe_text(row["name"], "NCS 능력단위"),
            "targetJobClass": self._prefix_label(code),
            "definition": safe_text(row["definition"]),
            "keyword": "",
            "level": safe_text(row["level"]),
            "trainingHours": safe_text(row["training_hours"]),
        }
        fit = self._disability_fit(candidate, normalized)
        candidate["candidateScore"] = max(10, min(110, score + int(fit.get("scoreDelta") or 0)))
        candidate["disabilityFit"] = fit
        return candidate

    def _keyword_score(self, text: str, normalized: dict[str, Any]) -> int:
        score = 50
        desired = safe_text(normalized["scoringPreferences"].get("desired_job_class"))
        if desired and desired in text:
            score += 18
        for keyword in normalized["keywords"]:
            if safe_text(keyword) and safe_text(keyword).lower() in text.lower():
                score += 6
        return min(score, 92)

    def _disability_fit(self, candidate: dict[str, Any], normalized: dict[str, Any]) -> dict[str, Any]:
        profile = normalized.get("profile") or {}
        disability = first_text(profile.get("disability_type"), profile.get("disabilityType"), profile.get("disability"))
        severity = safe_text(profile.get("severity"))
        if not disability:
            return {
                "level": "unknown",
                "label": "장애유형 미입력",
                "scoreDelta": 0,
                "notes": ["장애유형 정보가 없어 역량/NCS 기준만 반영했습니다."],
                "matchedTerms": [],
            }

        rule = DISABILITY_FIT_RULES.get(disability_rule_key(disability), {})
        text = compact_spaces(
            " ".join(
                safe_text(candidate.get(key))
                for key in ("title", "targetJobClass", "definition", "keyword", "ncsCode")
            )
        ).lower()
        positive_hits = [term for term in rule.get("positive", ()) if term.lower() in text]
        caution_hits = [term for term in rule.get("caution", ()) if term.lower() in text]
        blocked_hits = [term for term in rule.get("blocked", ()) if term.lower() in text]
        prefix = self._code_prefix(candidate.get("ncsCode"))
        if prefix in set(rule.get("blocked_prefixes", set())) and not positive_hits:
            blocked_hits.append(self._prefix_label(prefix))
        if prefix in set(rule.get("caution_prefixes", set())) and not positive_hits:
            caution_hits.append(self._prefix_label(prefix))

        strong_visual_match = disability == "시각장애" and any(term in text for term in ("안마", "마사지", "헬스키퍼"))
        severe = severity == "중증"
        if blocked_hits and not strong_visual_match:
            level = "blocked"
            label = "장애유형 기준 제외"
            score_delta = -100
            visible_hits = ", ".join(dict.fromkeys(blocked_hits[:4]))
            notes = [f"{disability}({severity or '중증도 미입력'}) 조건에서 '{visible_hits}' 요소는 직무 본질과 충돌할 수 있어 도전추천 후보에서 제외했습니다."]
        elif strong_visual_match:
            level = "preferred"
            label = "시각장애 우선 검토"
            score_delta = 18 if severe else 14
            notes = [f"{disability}({severity or '중증도 미입력'}) 조건에서 안마·헬스키퍼 계열은 우선 검토 가능한 직무로 보정했습니다."]
        elif caution_hits:
            level = "caution"
            label = "편의제공 필요"
            score_delta = -18 if severe else -10
            visible_hits = ", ".join(dict.fromkeys(caution_hits[:4]))
            notes = [f"{disability}({severity or '중증도 미입력'}) 기준으로 '{visible_hits}' 요소는 보조공학 또는 직무조정 가능 여부를 확인해야 합니다."]
        elif positive_hits:
            level = "supported"
            label = "적합"
            score_delta = 8 if severe else 6
            visible_hits = ", ".join(dict.fromkeys(positive_hits[:4]))
            notes = [f"{disability}({severity or '중증도 미입력'}) 조건에서 '{visible_hits}' 요소가 직무 수행 방향과 비교적 맞아 도전 후보로 유지했습니다."]
        else:
            level = "review"
            label = "확인 필요"
            score_delta = -4 if severe else 0
            notes = [f"{disability}({severity or '중증도 미입력'}) 조건과 직접 연결된 근무환경 근거가 부족해 지원 전 확인 항목으로 표시했습니다."]

        data_notice = ""
        if level in ("caution", "review"):
            data_notice = "장애유형·중증도 기준의 근무환경 적합성은 공고 원문과 문의처에서 추가 확인이 필요합니다."
        assistive = list(rule.get("assistive", ()))[:4] if level in ("supported", "preferred", "caution") else []
        return {
            "level": level,
            "label": label,
            "scoreDelta": score_delta,
            "notes": notes,
            "matchedTerms": list(dict.fromkeys([*positive_hits, *caution_hits, *blocked_hits]))[:6],
            "disabilityType": disability,
            "severity": severity,
            "dataNotice": data_notice,
            "assistiveSupports": assistive,
        }

    def _is_unrequested_advanced_role(self, candidate: dict[str, Any], normalized: dict[str, Any]) -> bool:
        text = compact_spaces(
            " ".join(
                safe_text(candidate.get(key))
                for key in ("title", "targetJobClass", "definition", "keyword")
            )
        )
        matched_terms = [term for term in ADVANCED_ROLE_TERMS if term in text]
        if not matched_terms:
            return False
        scoring = normalized.get("scoringPreferences") or {}
        desired_text = compact_spaces(safe_text(scoring.get("desired_job_class")))
        if any(term in text for term in EXECUTIVE_SUPPORT_TERMS):
            return not any(term in desired_text for term in EXECUTIVE_DESIRED_TERMS)
        requested_text = compact_spaces(
            " ".join(
                [
                    desired_text,
                    *[safe_text(item) for item in normalized.get("keywords", [])],
                ]
            )
        )
        return not any(term in requested_text for term in matched_terms)

    def _candidate_to_card(
        self,
        conn: sqlite3.Connection,
        candidate: dict[str, Any],
        normalized: dict[str, Any],
        rank: int,
    ) -> dict[str, Any]:
        units = self._related_units(conn, candidate)
        trainings = self._related_trainings(conn, candidate, normalized)
        qualifications = self._related_qualifications(conn, candidate)
        level_values = [numeric_level(unit.get("level")) for unit in units]
        level_values = [value for value in level_values if value is not None]
        primary_level = level_values[0] if level_values else numeric_level(candidate.get("level"))
        disability_fit = candidate.get("disabilityFit") or self._disability_fit(candidate, normalized)
        score = min(97, max(55, candidate["candidateScore"] + len(trainings) * 3 + len(qualifications) * 2 - (rank - 1) * 3))
        if disability_fit.get("level") == "caution":
            score = min(score, 72 if disability_fit.get("severity") == "중증" else 78)
        elif disability_fit.get("level") == "preferred":
            score = min(97, score + 4)
        title = candidate["title"]
        desired = safe_text(normalized["scoringPreferences"].get("desired_job_class"), "희망 직무")
        prefix = self._code_prefix(candidate.get("ncsCode"))
        support_needs = list(disability_fit.get("assistiveSupports") or [])
        if not support_needs and disability_fit.get("level") in ("caution", "review"):
            support_needs = ["공고 담당기관 확인"]
        skill_gaps = [
            safe_text(item.get("name"))
            for item in units[:2]
            if safe_text(item.get("name"))
        ]
        if not skill_gaps and trainings:
            skill_gaps = [f"{safe_text(trainings[0].get('title'))} 확인"]
        if not skill_gaps:
            skill_gaps = ["세부 숙련도 평가 준비 중"]
        primary_action = "연결 훈련 보기" if trainings else ("편의제공 확인" if disability_fit.get("level") in ("caution", "review") else "직무역량 확인")
        return {
            "id": f"challenge-{safe_text(candidate.get('ncsCode'), str(rank)).replace('_', '-')}-{rank}",
            "title": f"{title} 도전",
            "targetJobClass": candidate["targetJobClass"],
            "challengeLevel": level_label(primary_level),
            "score": int(score),
            "summary": self._card_summary(candidate, desired, trainings, qualifications),
            "disabilityFit": disability_fit,
            "fitReview": {
                "level": disability_fit.get("level"),
                "label": disability_fit.get("label"),
                "notes": disability_fit.get("notes") or [],
            },
            "dataNotice": safe_text(disability_fit.get("dataNotice")),
            "supportNeeds": support_needs[:3],
            "skillGaps": skill_gaps[:3],
            "primaryAction": primary_action,
            "requiredNcsUnits": units,
            "recommendedTrainings": trainings,
            "relatedQualifications": qualifications,
            "reasons": self._card_reasons(candidate, normalized, units, trainings, qualifications),
            "nextActions": [
                "NCS 능력단위명을 기준으로 현재 보유 경험을 체크한다.",
                "추천 훈련과정의 일정·비용·원격 여부를 Work24에서 확인한다.",
                "관련 자격이 있으면 필수/선택 능력단위와 최소 훈련시간을 비교한다.",
                f"{NCS_PREFIX_LABELS.get(prefix, '관련 분야')} 분야의 쉬운 과제 1개를 포트폴리오 증빙으로 남긴다.",
            ],
        }

    def _related_units(self, conn: sqlite3.Connection, candidate: dict[str, Any]) -> list[dict[str, Any]]:
        if not self._table_exists(conn, "ncs_competency_units"):
            return []
        prefixes = self._lookup_prefixes(candidate.get("ncsCode"))
        rows: list[sqlite3.Row] = []
        for prefix in prefixes:
            rows = conn.execute(
                """
                SELECT ncs_cl_cd, name, definition, level, training_hours
                FROM ncs_competency_units
                WHERE ncs_cl_cd LIKE ?
                ORDER BY CASE WHEN ncs_cl_cd LIKE ? THEN 0 ELSE 1 END, ncs_cl_cd
                LIMIT 4
                """,
                (f"{prefix}%", f"{safe_text(candidate.get('ncsCode'))}%"),
            ).fetchall()
            if rows:
                break
        return [
            {
                "code": safe_text(row["ncs_cl_cd"]),
                "name": safe_text(row["name"]),
                "level": safe_text(row["level"]),
                "trainingHours": safe_text(row["training_hours"]),
            }
            for row in rows
        ]

    def _related_trainings(
        self,
        conn: sqlite3.Connection,
        candidate: dict[str, Any],
        normalized: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not self._table_exists(conn, "work24_training_courses"):
            return []
        prefixes = self._lookup_prefixes(candidate.get("ncsCode"))
        keywords = [candidate.get("title"), *normalized["keywords"][:4]]
        clauses = []
        params: list[str] = []
        for prefix in prefixes:
            clauses.append("ncs_cd LIKE ?")
            params.append(f"{prefix}%")
        for keyword in keywords:
            keyword_text = safe_text(keyword)
            if len(keyword_text) >= 2:
                clauses.append("(title LIKE ? OR sub_title LIKE ? OR address LIKE ?)")
                params.extend([f"%{keyword_text}%"] * 3)
        if not clauses:
            return []
        rows = conn.execute(
            f"""
            SELECT id, trpr_id, trpr_degr, title, sub_title, ncs_cd, address, train_target,
                   tra_start_date, tra_end_date, course_man, real_man, title_link
            FROM work24_training_courses
            WHERE {" OR ".join(clauses)}
            LIMIT 60
            """,
            params,
        ).fetchall()
        region = safe_text(normalized["profile"].get("region"))
        sorted_rows = sorted(
            rows,
            key=lambda row: (
                int(any(safe_text(row["ncs_cd"]).startswith(prefix) for prefix in prefixes[:2])),
                int(bool(region and region in safe_text(row["address"]))),
                int(any(safe_text(keyword).lower() in safe_text(row["title"]).lower() for keyword in normalized["keywords"])),
                safe_text(row["tra_start_date"]),
            ),
            reverse=True,
        )
        return [
            {
                "id": safe_text(row["trpr_id"], safe_text(row["id"])),
                "title": safe_text(row["title"]),
                "provider": safe_text(row["sub_title"]),
                "ncsCode": safe_text(row["ncs_cd"]),
                "address": safe_text(row["address"]),
                "target": safe_text(row["train_target"]),
                "startDate": safe_text(row["tra_start_date"]),
                "endDate": safe_text(row["tra_end_date"]),
                "cost": safe_text(row["real_man"], safe_text(row["course_man"])),
                "url": safe_text(row["title_link"]),
            }
            for row in sorted_rows[:3]
        ]

    def _related_qualifications(self, conn: sqlite3.Connection, candidate: dict[str, Any]) -> list[dict[str, Any]]:
        if not self._table_exists(conn, "ncs_qualification_items"):
            return []
        rows: list[sqlite3.Row] = []
        for prefix in self._lookup_prefixes(candidate.get("ncsCode")):
            rows = conn.execute(
                """
                SELECT ncs_cl_cd, jm_cd, jm_nm, ablt_unit_type, min_training_time
                FROM ncs_qualification_items
                WHERE ncs_cl_cd LIKE ?
                ORDER BY ablt_unit_type DESC, jm_nm
                LIMIT 3
                """,
                (f"{prefix}%",),
            ).fetchall()
            if rows:
                break
        return [
            {
                "code": safe_text(row["jm_cd"]),
                "name": safe_text(row["jm_nm"]),
                "unitType": safe_text(row["ablt_unit_type"]),
                "ncsCode": safe_text(row["ncs_cl_cd"]),
                "minTrainingTime": safe_text(row["min_training_time"]),
            }
            for row in rows
        ]

    def _card_summary(
        self,
        candidate: dict[str, Any],
        desired: str,
        trainings: list[dict[str, Any]],
        qualifications: list[dict[str, Any]],
    ) -> str:
        source = "Work24 직무사전" if candidate["source"] == "work24_duty_dictionary" else "NCS 능력단위"
        training_note = f"연결 훈련 {len(trainings)}건" if trainings else "연결 훈련 없음"
        qualification_note = f"관련 자격 {len(qualifications)}건" if qualifications else "관련 자격은 추가 확인 필요"
        return f"{desired} 희망을 {source}의 '{candidate['title']}' 역량으로 확장한 카드입니다. {training_note}, {qualification_note} 기준으로 1차 도전 가능성을 계산했습니다."

    def _card_reasons(
        self,
        candidate: dict[str, Any],
        normalized: dict[str, Any],
        units: list[dict[str, Any]],
        trainings: list[dict[str, Any]],
        qualifications: list[dict[str, Any]],
    ) -> list[str]:
        disability_fit = candidate.get("disabilityFit") or self._disability_fit(candidate, normalized)
        candidate_text = compact_spaces(
            " ".join(
                safe_text(candidate.get(key))
                for key in ("title", "targetJobClass", "definition", "keyword")
            )
        ).lower()
        matched_keywords = [
            safe_text(keyword)
            for keyword in normalized["keywords"]
            if safe_text(keyword) and safe_text(keyword).lower() in candidate_text
        ][:4]
        reasons = [
            *(safe_text(note) for note in (disability_fit.get("notes") or []) if safe_text(note)),
            f"실제 매칭 키워드({', '.join(matched_keywords) or '직무군/코드'})와 '{candidate['title']}' 텍스트를 규칙 기반으로 매칭했습니다.",
            f"NCS 대분류는 {self._prefix_label(candidate.get('ncsCode'))}로 추정했습니다.",
        ]
        if units:
            reasons.append(f"NCS 능력단위 {len(units)}개를 같은 코드 계열에서 확인했습니다.")
        if trainings:
            reasons.append(f"Work24 훈련과정 {len(trainings)}개가 직무/코드/키워드 기준으로 연결되었습니다.")
        if qualifications:
            reasons.append(f"NCS 자격 항목 {len(qualifications)}개가 같은 계열에서 확인되었습니다.")
        if candidate.get("definition"):
            reasons.append(compact_spaces(candidate["definition"])[:160])
        return reasons[:5]

    def _fallback_reason(self, summary: dict[str, Any], cards: list[dict[str, Any]]) -> str:
        if cards:
            return ""
        status = (summary.get("referenceDb") or {}).get("status")
        if status == "missing":
            return "reference DB file is missing"
        if status == "empty":
            return "reference DB has no usable reference rows"
        if status == "unreadable":
            return "reference DB could not be opened read-only"
        return "no matching challenge candidates found"

    def _lookup_prefixes(self, code: Any) -> list[str]:
        clean = re.sub(r"[^0-9]", "", safe_text(code))
        prefixes = []
        for length in (10, 8, 6, 4, 2):
            if len(clean) >= length:
                prefixes.append(clean[:length])
        return prefixes or []

    def _code_prefix(self, code: Any) -> str:
        clean = re.sub(r"[^0-9]", "", safe_text(code))
        return clean[:2] if len(clean) >= 2 else ""

    def _prefix_label(self, code: Any) -> str:
        prefix = self._code_prefix(code)
        return NCS_PREFIX_LABELS.get(prefix, "NCS 참조 직무")

    def _target_job_class(self, row: sqlite3.Row) -> str:
        parts = [
            safe_text(row["job_lcfn"]),
            safe_text(row["job_mcn"]),
            safe_text(row["job_scfn"]),
            safe_text(row["job_sdvn"]),
        ]
        return " > ".join(part for part in parts if part) or "NCS 참조 직무"

    def _dedupe_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {}
        for item in candidates:
            key = safe_text(item.get("ncsCode")) or safe_text(item.get("title"))
            if not key:
                continue
            previous = by_key.get(key)
            if previous is None or item["candidateScore"] > previous["candidateScore"]:
                by_key[key] = item
        return list(by_key.values())

    def _table_exists(self, conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
