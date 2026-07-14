from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd


OFFICIAL_DISABILITY_TYPES = [
    "지체장애",
    "뇌병변장애",
    "시각장애",
    "청각장애",
    "언어장애",
    "안면장애",
    "신장장애",
    "심장장애",
    "간장애",
    "호흡기장애",
    "장루요루장애",
    "뇌전증장애",
    "지적장애",
    "정신장애",
    "자폐성장애",
]

AGE_GROUPS = ["under_20", "20s", "30s", "40s", "50s", "60s", "70_plus"]
SEVERITY_VALUES = ["중증", "경증"]

FEATURE_COLUMNS = [
    "sido",
    "sigungu",
    "age",
    "age_group",
    "disability_type",
    "severity",
]

CATEGORICAL_COLUMNS = [
    "sido",
    "sigungu",
    "age_group",
    "disability_type",
    "severity",
]

SIDO_ALIASES = {
    "서울특별시": "서울",
    "서울": "서울",
    "부산광역시": "부산",
    "부산": "부산",
    "대구광역시": "대구",
    "대구": "대구",
    "인천광역시": "인천",
    "인천": "인천",
    "광주광역시": "광주",
    "광주": "광주",
    "대전광역시": "대전",
    "대전": "대전",
    "울산광역시": "울산",
    "울산": "울산",
    "세종특별자치시": "세종",
    "세종": "세종",
    "경기도": "경기",
    "경기": "경기",
    "강원도": "강원",
    "강원특별자치도": "강원",
    "강원": "강원",
    "충청북도": "충북",
    "충북": "충북",
    "충청남도": "충남",
    "충남": "충남",
    "전라북도": "전북",
    "전북특별자치도": "전북",
    "전북": "전북",
    "전라남도": "전남",
    "전남": "전남",
    "경상북도": "경북",
    "경북": "경북",
    "경상남도": "경남",
    "경남": "경남",
    "제주특별자치도": "제주",
    "제주": "제주",
    "전체": "전체",
    "기타": "기타",
}

REFERENCE_LARGE_TO_TARGET_CLASSES = {
    "경영·사무·금융·보험": [
        "관리직(임원·부서장)",
        "경영·행정·사무직",
        "금융·보험직",
    ],
    "연구 및 공학기술": [
        "정보통신 연구개발직 및 공학기술직",
        "건설·채굴 연구개발직 및 공학기술직",
        "제조 연구개발직 및 공학기술직",
        "자연·생명과학 연구직",
        "인문·사회과학 연구직",
    ],
    "교육·법률·사회복지·경찰·소방 및 군인": [
        "교육직",
        "법률직",
        "사회복지·종교직",
        "경호·경비직",
    ],
    "보건·의료": ["보건·의료직"],
    "예술·디자인·방송·스포츠": [
        "예술·디자인·방송직",
        "스포츠·레크리에이션직",
    ],
    "미용·여행·숙박·음식·경비·돌봄·청소": [
        "미용·예식 및 반려동물 서비스직",
        "여행·숙박·오락 서비스직",
        "음식 서비스직",
        "경호·경비직",
        "돌봄 서비스직(간병·육아)",
        "청소 및 기타 개인서비스직",
    ],
    "영업·판매·운전·운송": ["영업·판매직", "운전·운송직"],
    "건설·채굴": [
        "건설·채굴직",
        "건설·채굴 연구개발직 및 공학기술직",
    ],
    "설치·정비·생산-기계·금속·재료": [
        "기계 설치·정비·생산직",
        "금속·재료 설치·정비·생산직(판금·단조·주조·용접·도장 등)",
    ],
    "설치·정비·생산-전기·전자·정보통신": [
        "전기·전자 설치·정비·생산직",
        "정보통신 설치·정비직",
    ],
    "설치·정비·생산-화학·환경·섬유·의복·식품가공": [
        "화학·환경 설치·정비·생산직",
        "섬유·의복 생산직",
        "식품 가공·생산직",
    ],
    "설치·정비·생산-인쇄·목재·공예 및 제조 단순": [
        "인쇄·목재·공예 및 기타 설치·정비·생산직",
        "제조 단순직",
    ],
    "농림어업직": ["농림어업직"],
}

JOB_CLASS_RULES = [
    ("관리직(임원·부서장)", ["관리자", "부서장", "관리소장", "임원"]),
    ("사회복지·종교직", ["사회복지", "종교", "직업상담"]),
    ("돌봄 서비스직(간병·육아)", ["요양", "간병", "보육", "돌봄", "육아"]),
    ("보건·의료직", ["병원", "의료", "간호", "치료", "약사", "임상", "보건"]),
    ("교육직", ["교사", "강사", "교육", "조교"]),
    ("법률직", ["법률", "변호", "법무", "노무사"]),
    ("예술·디자인·방송직", ["디자이너", "디자인", "화가", "조각가", "예술", "방송", "사진", "공연"]),
    ("스포츠·레크리에이션직", ["운동선수", "스포츠", "레크리에이션", "체육"]),
    ("음식 서비스직", ["주방", "조리", "바리스타", "티마스터", "제과", "제빵", "급식", "음식", "식당"]),
    ("경호·경비직", ["경비", "보안", "경호"]),
    ("미용·예식 및 반려동물 서비스직", ["미용", "예식", "반려동물", "애견", "피부"]),
    ("여행·숙박·오락 서비스직", ["여행", "숙박", "호텔", "콘도", "오락"]),
    ("청소 및 기타 개인서비스직", ["청소", "미화", "세탁", "다림질", "주차", "검침", "서비스 단순"]),
    ("경영·행정·사무직", ["사무", "행정", "총무", "회계", "경리", "비서", "자료", "접수", "고객상담", "콜센터", "안내원"]),
    ("금융·보험직", ["금융", "보험", "은행", "증권"]),
    ("정보통신 연구개발직 및 공학기술직", ["소프트웨어", "웹", "프로그래머", "개발자", "데이터", "시스템", "정보보안"]),
    ("정보통신 설치·정비직", ["정보통신", "통신", "네트워크"]),
    ("영업·판매직", ["판매", "영업", "매장", "계산원", "캐셔", "상품"]),
    ("운전·운송직", ["운전", "배송", "배달", "택배", "운송", "물류"]),
    ("농림어업직", ["농", "어업", "원예", "임업", "축산"]),
    ("식품 가공·생산직", ["식품", "음료", "제빵원", "제과원"]),
    ("전기·전자 설치·정비·생산직", ["전기", "전자"]),
    ("금속·재료 설치·정비·생산직(판금·단조·주조·용접·도장 등)", ["금속", "용접", "판금", "단조", "주조", "도장"]),
    ("기계 설치·정비·생산직", ["기계", "정비", "수리"]),
    ("화학·환경 설치·정비·생산직", ["화학", "재활용", "폐기물", "환경"]),
    ("섬유·의복 생산직", ["섬유", "의복", "봉제", "재봉", "의류"]),
    ("인쇄·목재·공예 및 기타 설치·정비·생산직", ["인쇄", "목재", "가구", "공예"]),
    ("건설·채굴직", ["건설", "건축", "토목", "채굴", "배관"]),
    ("제조 연구개발직 및 공학기술직", ["연구원", "기술자", "공학"]),
    ("제조 단순직", ["제조", "생산", "포장", "조립", "검사", "단순 종사"]),
]

WAGE_RE = re.compile(r"\((?P<type>[^)]*)\)\s*(?P<amount>[0-9,]*)")


def read_csv_with_fallback(path: Path) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path)


def normalize_text(value: Any) -> str | None:
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def normalize_text_series(series: pd.Series) -> pd.Series:
    return series.astype("string").str.strip().replace({"": pd.NA})


def age_group(age: float | int | None) -> str | None:
    if pd.isna(age):
        return None
    age_int = int(age)
    if age_int < 20:
        return "under_20"
    if age_int >= 70:
        return "70_plus"
    return f"{age_int // 10 * 10}s"


def normalize_sido(value: Any) -> str | None:
    text = normalize_text(value)
    if text is None:
        return None
    return SIDO_ALIASES.get(text, text)


def split_region(value: Any, *, address: bool = False) -> tuple[str | None, str | None]:
    text = normalize_text(value)
    if text is None:
        return None, None

    parts = text.split()
    if not parts:
        return None, None

    sido = normalize_sido(parts[0])
    rest = parts[1:]
    if not rest:
        return sido, None

    if rest[0] in {"전체", "기타"}:
        return sido, "unknown"

    if len(rest) >= 2 and rest[0].endswith("시") and rest[1].endswith("구"):
        return sido, f"{rest[0]} {rest[1]}"

    if rest[0].endswith(("시", "군", "구")):
        return sido, rest[0]

    if address:
        return sido, None

    return sido, " ".join(rest)


def compact_job_title(value: Any) -> str | None:
    text = normalize_text(value)
    if text is None:
        return None
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"[\s·ㆍ,/]+", "", text)
    return text or None


def parse_desired_wage(value: Any) -> tuple[str | None, float | None]:
    text = normalize_text(value)
    if text is None:
        return None, None
    match = WAGE_RE.search(text)
    if not match:
        return None, pd.to_numeric(text.replace(",", ""), errors="coerce")
    wage_type = normalize_text(match.group("type"))
    amount_text = match.group("amount").replace(",", "")
    amount = pd.to_numeric(amount_text, errors="coerce") if amount_text else None
    if pd.isna(amount):
        amount = None
    return wage_type, float(amount) if amount is not None else None


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if isinstance(value, tuple):
        return [json_ready(v) for v in value]
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def build_training_dataset(path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    df = read_csv_with_fallback(path)
    required = ["순번", "취업일자", "근무지역", "취업직종대분류", "연령", "장애유형", "중증여부"]
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(f"Missing employment success columns: {missing}")

    prepared = pd.DataFrame()
    prepared["row_id"] = pd.to_numeric(df["순번"], errors="coerce").astype("Int64")
    prepared["employment_date"] = pd.to_datetime(df["취업일자"], errors="coerce")
    prepared["employment_year"] = prepared["employment_date"].dt.year.astype("Int64")
    prepared["employment_month"] = prepared["employment_date"].dt.month.astype("Int64")

    prepared["work_region_raw"] = normalize_text_series(df["근무지역"])
    region = prepared["work_region_raw"].apply(split_region)
    prepared["sido"] = region.apply(lambda item: item[0])
    prepared["sigungu"] = region.apply(lambda item: item[1] or "unknown")

    prepared["age"] = pd.to_numeric(df["연령"], errors="coerce").astype("Int64")
    prepared["age_group"] = prepared["age"].apply(age_group)
    prepared["disability_type"] = normalize_text_series(df["장애유형"])
    prepared["is_official_disability_type"] = prepared["disability_type"].isin(
        OFFICIAL_DISABILITY_TYPES
    )
    prepared["severity"] = normalize_text_series(df["중증여부"])
    prepared["target_job_class"] = normalize_text_series(df["취업직종대분류"])

    critical = [
        "sido",
        "age",
        "age_group",
        "disability_type",
        "severity",
        "employment_date",
        "target_job_class",
    ]
    before_drop = len(prepared)
    prepared = prepared.dropna(subset=critical).copy()

    columns = [
        "row_id",
        "employment_date",
        "employment_year",
        "employment_month",
        "work_region_raw",
        *FEATURE_COLUMNS,
        "is_official_disability_type",
        "target_job_class",
    ]
    prepared = prepared[columns]

    summary = {
        "raw_rows": int(len(df)),
        "prepared_rows": int(len(prepared)),
        "dropped_critical_na": int(before_drop - len(prepared)),
        "target_class_count": int(prepared["target_job_class"].nunique()),
        "disability_type_count": int(prepared["disability_type"].nunique()),
        "non_official_disability_values": prepared.loc[
            ~prepared["is_official_disability_type"], "disability_type"
        ]
        .value_counts()
        .to_dict(),
        "age_min": int(prepared["age"].min()),
        "age_max": int(prepared["age"].max()),
        "date_min": str(prepared["employment_date"].min().date()),
        "date_max": str(prepared["employment_date"].max().date()),
    }
    return prepared, summary


def normalize_job_codes(path: Path) -> tuple[pd.DataFrame, dict[str, dict[str, str | None]]]:
    raw = read_csv_with_fallback(path)
    required = ["카테고리 ID", "대분류", "중분류", "소분류"]
    missing = [column for column in required if column not in raw.columns]
    if missing:
        raise ValueError(f"Missing job code columns: {missing}")

    rows = []
    current_large = None
    current_mid = None
    for _, row in raw.iterrows():
        large = normalize_text(row["대분류"])
        mid = normalize_text(row["중분류"])
        small = normalize_text(row["소분류"])
        if large:
            current_large = large
            current_mid = None
        if mid:
            current_mid = mid
        if large:
            level = "large"
        elif mid:
            level = "mid"
        elif small:
            level = "small"
        else:
            level = "blank"

        rows.append(
            {
                "category_id": normalize_text(row["카테고리 ID"]),
                "level": level,
                "reference_large": current_large,
                "reference_mid": current_mid,
                "reference_small": small,
                "raw_large": large,
                "raw_mid": mid,
                "raw_small": small,
            }
        )

    normalized = pd.DataFrame(rows)
    lookup: dict[str, dict[str, str | None]] = {}
    for _, row in normalized.dropna(subset=["reference_small"]).iterrows():
        key = compact_job_title(row["reference_small"])
        if key and key not in lookup:
            lookup[key] = {
                "reference_large": row["reference_large"],
                "reference_mid": row["reference_mid"],
                "reference_small": row["reference_small"],
            }
    return normalized, lookup


def infer_reference_job(
    job_title: Any,
    lookup: dict[str, dict[str, str | None]],
) -> dict[str, str | None]:
    compact = compact_job_title(job_title)
    if not compact:
        return {"reference_large": None, "reference_mid": None, "reference_small": None}

    if compact in lookup:
        return lookup[compact]

    for key in sorted(lookup, key=len, reverse=True):
        if len(key) < 3:
            continue
        if key in compact or compact in key:
            return lookup[key]

    return {"reference_large": None, "reference_mid": None, "reference_small": None}


def infer_target_job_class(
    job_title: Any,
    lookup: dict[str, dict[str, str | None]],
) -> tuple[str | None, str, str | None, str | None, str | None]:
    compact = compact_job_title(job_title)
    if compact:
        for target, patterns in JOB_CLASS_RULES:
            if any(pattern.replace(" ", "") in compact for pattern in patterns):
                ref = infer_reference_job(job_title, lookup)
                return (
                    target,
                    "keyword",
                    ref["reference_large"],
                    ref["reference_mid"],
                    ref["reference_small"],
                )

    ref = infer_reference_job(job_title, lookup)
    reference_large = ref["reference_large"]
    candidates = REFERENCE_LARGE_TO_TARGET_CLASSES.get(reference_large or "", [])
    if len(candidates) == 1:
        return (
            candidates[0],
            "job_code",
            ref["reference_large"],
            ref["reference_mid"],
            ref["reference_small"],
        )

    return (
        None,
        "unmapped" if not reference_large else "ambiguous_job_code",
        ref["reference_large"],
        ref["reference_mid"],
        ref["reference_small"],
    )


def apply_job_mapping(
    series: pd.Series,
    lookup: dict[str, dict[str, str | None]],
) -> pd.DataFrame:
    mapped = series.apply(lambda value: infer_target_job_class(value, lookup))
    return pd.DataFrame(
        mapped.tolist(),
        columns=[
            "target_job_class_candidate",
            "job_class_mapping_method",
            "reference_large",
            "reference_mid",
            "reference_small",
        ],
        index=series.index,
    )


def normalize_job_postings(
    path: Path,
    lookup: dict[str, dict[str, str | None]],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    df = read_csv_with_fallback(path)
    required = [
        "연번",
        "구인신청일자",
        "모집기간",
        "사업장명",
        "모집직종",
        "고용형태",
        "임금형태",
        "임금",
        "사업장 주소",
    ]
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(f"Missing job posting columns: {missing}")

    out = pd.DataFrame()
    out["posting_id"] = pd.to_numeric(df["연번"], errors="coerce").astype("Int64")
    out["posting_date"] = pd.to_datetime(df["구인신청일자"], errors="coerce")
    out["recruit_period_raw"] = normalize_text_series(df["모집기간"])
    out["company_name"] = normalize_text_series(df["사업장명"])
    out["job_title"] = normalize_text_series(df["모집직종"])
    out["employment_type"] = normalize_text_series(df["고용형태"])
    out["wage_type"] = normalize_text_series(df["임금형태"])
    out["wage_amount"] = pd.to_numeric(df["임금"], errors="coerce")
    out["address_raw"] = normalize_text_series(df["사업장 주소"])

    region = out["address_raw"].apply(lambda value: split_region(value, address=True))
    out["sido"] = region.apply(lambda item: item[0])
    out["sigungu"] = region.apply(lambda item: item[1])

    mapping = apply_job_mapping(out["job_title"], lookup)
    out = pd.concat([out, mapping], axis=1)

    summary = {
        "raw_rows": int(len(df)),
        "normalized_rows": int(len(out)),
        "wage_missing_rows": int(out["wage_amount"].isna().sum()),
        "mapped_job_class_rows": int(out["target_job_class_candidate"].notna().sum()),
        "mapping_coverage": float(out["target_job_class_candidate"].notna().mean()),
        "mapping_methods": out["job_class_mapping_method"].value_counts(dropna=False).to_dict(),
    }
    return out, summary


def normalize_job_seekers(
    path: Path,
    lookup: dict[str, dict[str, str | None]],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    df = read_csv_with_fallback(path)
    required = [
        "연번",
        "구직등록일",
        "연령",
        "희망지역",
        "희망직종",
        "희망임금",
        "장애유형",
        "중증여부",
        "기관분류",
    ]
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(f"Missing job seeker columns: {missing}")

    out = pd.DataFrame()
    out["seeker_id"] = pd.to_numeric(df["연번"], errors="coerce").astype("Int64")
    out["registration_date"] = pd.to_datetime(df["구직등록일"], errors="coerce")
    out["age"] = pd.to_numeric(df["연령"], errors="coerce").astype("Int64")
    out["age_group"] = out["age"].apply(age_group)
    out["desired_region_raw"] = normalize_text_series(df["희망지역"])
    region = out["desired_region_raw"].apply(split_region)
    out["sido"] = region.apply(lambda item: item[0])
    out["sigungu"] = region.apply(lambda item: item[1])
    out["desired_job_title"] = normalize_text_series(df["희망직종"])

    wage = df["희망임금"].apply(parse_desired_wage)
    out["desired_wage_type"] = wage.apply(lambda item: item[0])
    out["desired_wage_amount"] = wage.apply(lambda item: item[1])
    out["disability_type"] = normalize_text_series(df["장애유형"])
    out["is_official_disability_type"] = out["disability_type"].isin(
        OFFICIAL_DISABILITY_TYPES
    )
    out["severity"] = normalize_text_series(df["중증여부"])
    out["agency_type"] = normalize_text_series(df["기관분류"])

    mapping = apply_job_mapping(out["desired_job_title"], lookup)
    out = pd.concat([out, mapping], axis=1)

    summary = {
        "raw_rows": int(len(df)),
        "normalized_rows": int(len(out)),
        "non_official_disability_values": out.loc[
            ~out["is_official_disability_type"], "disability_type"
        ]
        .value_counts()
        .to_dict(),
        "desired_wage_missing_or_zero_rows": int(
            out["desired_wage_amount"].isna().sum()
            + (out["desired_wage_amount"].fillna(-1) == 0).sum()
        ),
        "mapped_job_class_rows": int(out["target_job_class_candidate"].notna().sum()),
        "mapping_coverage": float(out["target_job_class_candidate"].notna().mean()),
        "mapping_methods": out["job_class_mapping_method"].value_counts(dropna=False).to_dict(),
    }
    return out, summary


def build_market_priors(
    postings: pd.DataFrame,
    seekers: pd.DataFrame,
    group_columns: list[str],
) -> pd.DataFrame:
    post_base = postings.dropna(subset=["target_job_class_candidate"]).copy()
    seeker_base = seekers.dropna(subset=["target_job_class_candidate"]).copy()

    for column in group_columns:
        post_base[column] = post_base[column].fillna("unknown")
        seeker_base[column] = seeker_base[column].fillna("unknown")

    post_group = (
        post_base.groupby([*group_columns, "target_job_class_candidate"], dropna=False)
        .agg(
            posting_count=("posting_id", "count"),
            posting_wage_median=("wage_amount", "median"),
            posting_wage_mean=("wage_amount", "mean"),
        )
        .reset_index()
    )
    seeker_group = (
        seeker_base.groupby([*group_columns, "target_job_class_candidate"], dropna=False)
        .agg(
            seeker_count=("seeker_id", "count"),
            desired_wage_median=("desired_wage_amount", "median"),
            desired_wage_mean=("desired_wage_amount", "mean"),
        )
        .reset_index()
    )

    result = post_group.merge(
        seeker_group,
        on=[*group_columns, "target_job_class_candidate"],
        how="outer",
    )
    result = result.rename(columns={"target_job_class_candidate": "target_job_class"})

    for count_column in ("posting_count", "seeker_count"):
        result[count_column] = result[count_column].fillna(0).astype(int)

    denominator = result.groupby(group_columns)["posting_count"].transform("sum")
    result["posting_share_in_region"] = (
        result["posting_count"] / denominator.where(denominator > 0)
    ).fillna(0)

    denominator = result.groupby(group_columns)["seeker_count"].transform("sum")
    result["seeker_share_in_region"] = (
        result["seeker_count"] / denominator.where(denominator > 0)
    ).fillna(0)

    return result.sort_values([*group_columns, "target_job_class"]).reset_index(drop=True)


def build_national_priors(postings: pd.DataFrame, seekers: pd.DataFrame) -> pd.DataFrame:
    postings = postings.copy()
    seekers = seekers.copy()
    postings["sido"] = "ALL"
    seekers["sido"] = "ALL"
    return build_market_priors(postings, seekers, ["sido"])


def build_sigungu_reference(*frames: pd.DataFrame) -> dict[str, list[str]]:
    values: dict[str, set[str]] = {}
    for frame in frames:
        subset = frame.dropna(subset=["sido", "sigungu"])
        for sido, sigungu in subset[["sido", "sigungu"]].itertuples(index=False):
            values.setdefault(str(sido), set()).add(str(sigungu))
    return {sido: sorted(sigungu_values) for sido, sigungu_values in sorted(values.items())}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare JobBridge profile-contract training and matching resources."
    )
    parser.add_argument(
        "--employment-success",
        default="Data/01_training_employment_success/disabled_employment_success_20251231.csv",
    )
    parser.add_argument(
        "--job-postings",
        default="Data/02_matching_job_postings/disabled_job_postings_20251231.csv",
    )
    parser.add_argument(
        "--job-seekers",
        default="Data/03_job_seekers/disabled_job_seekers_20251231.csv",
    )
    parser.add_argument(
        "--job-codes",
        default="Data/04_reference_job_codes/job_codes_20230825.csv",
    )
    parser.add_argument("--out-dir", default="Data/processed/profile_contract_v1")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    training, training_summary = build_training_dataset(Path(args.employment_success))
    job_codes, job_code_lookup = normalize_job_codes(Path(args.job_codes))
    postings, postings_summary = normalize_job_postings(Path(args.job_postings), job_code_lookup)
    seekers, seekers_summary = normalize_job_seekers(Path(args.job_seekers), job_code_lookup)

    priors_by_sido = build_market_priors(postings, seekers, ["sido"])
    priors_by_sigungu = build_market_priors(postings, seekers, ["sido", "sigungu"])
    national_priors = build_national_priors(postings, seekers)

    training_path = out_dir / "training_dataset.csv"
    postings_path = out_dir / "job_postings_normalized.csv"
    seekers_path = out_dir / "job_seekers_normalized.csv"
    job_codes_path = out_dir / "job_codes_normalized.csv"
    priors_sido_path = out_dir / "market_priors_by_sido.csv"
    priors_sigungu_path = out_dir / "market_priors_by_sigungu.csv"
    priors_national_path = out_dir / "market_priors_national.csv"
    metadata_path = out_dir / "profile_contract_metadata.json"
    summary_path = out_dir / "summary.json"

    training.to_csv(training_path, index=False, encoding="utf-8-sig")
    postings.to_csv(postings_path, index=False, encoding="utf-8-sig")
    seekers.to_csv(seekers_path, index=False, encoding="utf-8-sig")
    job_codes.to_csv(job_codes_path, index=False, encoding="utf-8-sig")
    priors_by_sido.to_csv(priors_sido_path, index=False, encoding="utf-8-sig")
    priors_by_sigungu.to_csv(priors_sigungu_path, index=False, encoding="utf-8-sig")
    national_priors.to_csv(priors_national_path, index=False, encoding="utf-8-sig")

    target_classes = sorted(training["target_job_class"].dropna().unique().tolist())
    training_valid_sido_values = sorted(training["sido"].dropna().unique().tolist())
    matching_valid_sido_values = sorted(
        set(training["sido"].dropna())
        | set(postings["sido"].dropna())
        | set(seekers["sido"].dropna())
    )

    metadata = {
        "profile_contract_version": "profile_contract_v1",
        "profile_contract": {
            "input_fields": {
                "birthYear": "number; API derives age and age_group before prediction",
                "age": "number",
                "age_group": AGE_GROUPS,
                "disability_type": OFFICIAL_DISABILITY_TYPES,
                "severity": SEVERITY_VALUES,
                "sido": "one of valid_sido_values",
                "sigungu": "one of valid_sigungu_by_sido[sido], blank, or unknown",
                "desired_job_class": "optional; postprocessing/filtering only",
                "desired_wage": "optional; postprocessing/filtering only",
            },
            "model_feature_columns": FEATURE_COLUMNS,
            "categorical_columns": CATEGORICAL_COLUMNS,
            "target_column": "target_job_class",
            "postprocess_only_fields": ["desired_job_class", "desired_wage"],
            "unknown_policy": {
                "sigungu": "Use 'unknown' when blank, missing, or not present in the reference list.",
                "desired_job_class": "Do not feed into LightGBM features; use to boost or filter ranked classes/jobs.",
                "desired_wage": "Do not feed into LightGBM features; use with wage_type/wage_amount resources.",
            },
        },
        "valid_sido_values": training_valid_sido_values,
        "valid_sigungu_by_sido": build_sigungu_reference(training),
        "matching_valid_sido_values": matching_valid_sido_values,
        "matching_valid_sigungu_by_sido": build_sigungu_reference(
            training, postings, seekers
        ),
        "target_job_classes": target_classes,
        "postprocessing_resources": {
            "job_postings_normalized": str(postings_path),
            "job_seekers_normalized": str(seekers_path),
            "job_codes_normalized": str(job_codes_path),
            "market_priors_by_sido": str(priors_sido_path),
            "market_priors_by_sigungu": str(priors_sigungu_path),
            "market_priors_national": str(priors_national_path),
        },
        "join_strategy": {
            "supervised_training": "employment_success only, because it has target_job_class labels.",
            "job_postings": "No row-level key to success rows; normalized for class/wage/region availability priors and desired_wage filtering.",
            "job_seekers": "No outcome label; normalized for profile-contract coverage and weak desired-job validation.",
            "job_codes": "Used as a reference taxonomy for job title mapping before postprocessing.",
        },
    }

    summary = {
        "input_files": {
            "employment_success": args.employment_success,
            "job_postings": args.job_postings,
            "job_seekers": args.job_seekers,
            "job_codes": args.job_codes,
        },
        "outputs": {
            "training_dataset": str(training_path),
            "profile_contract_metadata": str(metadata_path),
            "summary": str(summary_path),
            **metadata["postprocessing_resources"],
        },
        "training": training_summary,
        "job_postings": postings_summary,
        "job_seekers": seekers_summary,
        "job_codes": {
            "raw_rows": int(len(job_codes)),
            "small_lookup_rows": int(len(job_code_lookup)),
            "duplicate_raw_rows": int(read_csv_with_fallback(Path(args.job_codes)).duplicated().sum()),
        },
        "market_priors": {
            "sido_rows": int(len(priors_by_sido)),
            "sigungu_rows": int(len(priors_by_sigungu)),
            "national_rows": int(len(national_priors)),
        },
        "decision": metadata["join_strategy"],
    }

    metadata_path.write_text(
        json.dumps(json_ready(metadata), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps(json_ready(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(json_ready(summary), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
