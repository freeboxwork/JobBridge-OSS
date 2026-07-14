const crypto = require("crypto");
const { handleOptions, readJsonBody, requireAdmin, sendJson } = require("../_supabaseAdmin");

const ENDPOINTS = ["job_list_env", "job_list"];
const ENV_ENDPOINT = "job_list_env";
const PUBLIC_DATASET_ID = "15117692";
const DEFAULT_API_BASE_URL = "https://apis.data.go.kr/B552583/job";
const DEFAULT_SUPABASE_SCHEMA = "jobbridge_private";
const DEFAULT_SUPABASE_TABLE = "job_postings_live";
const MAX_PAGES_PER_ENDPOINT = 20;
const MAX_NUM_OF_ROWS = 100;
const DEFAULT_PAGE_CONCURRENCY = 4;
const MAX_PAGE_CONCURRENCY = 8;
const DEFAULT_REQUEST_RETRIES = 2;
const MAX_REQUEST_RETRIES = 4;
const DEFAULT_RETRY_BASE_MS = 300;
const DEFAULT_PUBLIC_REQUEST_TIMEOUT_MS = 20000;
const DEFAULT_SUPABASE_REQUEST_TIMEOUT_MS = 15000;
const DEFAULT_SYNC_TIMEOUT_MS = 180000;
const MAX_SYNC_TIMEOUT_MS = 240000;
const TRANSIENT_HTTP_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);

let activeSync = null;

const ENV_FIELD_MAP = {
  envBothHands: "env_both_hands",
  envEyesight: "env_eyesight",
  envHandWork: "env_handwork",
  envHandwork: "env_handwork",
  envLiftPower: "env_lift_power",
  envLstnTalk: "env_lstn_talk",
  envStndWalk: "env_stnd_walk",
};

const SIDO_ALIASES = {
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
  "강원특별자치도": "강원",
  "강원도": "강원",
  "강원": "강원",
  "충청북도": "충북",
  "충북": "충북",
  "충청남도": "충남",
  "충남": "충남",
  "전북특별자치도": "전북",
  "전라북도": "전북",
  "전북": "전북",
  "전라남도": "전남",
  "전남": "전남",
  "경상북도": "경북",
  "경북": "경북",
  "경상남도": "경남",
  "경남": "경남",
  "제주특별자치도": "제주",
  "제주": "제주",
};

const JOB_CLASS_RULES = [
  ["관리직(임원·부서장)", ["관리자", "부서장", "관리소장", "임원"]],
  ["사회복지·종교직", ["사회복지", "종교", "직업상담"]],
  ["돌봄 서비스직(간병·육아)", ["요양", "간병", "보육", "돌봄", "육아"]],
  ["보건·의료직", ["병원", "의료", "간호", "치료", "약사", "임상", "보건", "안마"]],
  ["교육직", ["교사", "강사", "교육", "조교"]],
  ["법률직", ["법률", "변호", "법무", "노무사"]],
  ["예술·디자인·방송직", ["디자이너", "디자인", "화가", "조각가", "예술", "방송", "사진", "공연"]],
  ["스포츠·레크리에이션직", ["운동선수", "스포츠", "레크리에이션", "체육"]],
  ["음식 서비스직", ["주방", "조리", "바리스타", "티마스터", "제과", "제빵", "급식", "음식", "식당"]],
  ["경호·경비직", ["경비", "보안", "경호"]],
  ["미용·예식 및 반려동물 서비스직", ["미용", "예식", "반려동물", "애견", "피부"]],
  ["여행·숙박·오락 서비스직", ["여행", "숙박", "호텔", "콘도", "오락"]],
  ["청소 및 기타 개인서비스직", ["청소", "미화", "세탁", "다림질", "주차", "검침", "서비스 단순"]],
  ["경영·행정·사무직", ["사무", "행정", "총무", "회계", "경리", "비서", "자료", "접수", "고객상담", "콜센터", "안내원", "데이터"]],
  ["금융·보험직", ["금융", "보험", "은행", "증권"]],
  ["정보통신 연구개발직 및 공학기술직", ["소프트웨어", "웹", "프로그래머", "개발자", "데이터", "시스템", "정보보안"]],
  ["정보통신 설치·정비직", ["정보통신", "통신", "네트워크"]],
  ["영업·판매직", ["판매", "영업", "매장", "계산원", "캐셔", "상품"]],
  ["운전·운송직", ["운전", "배송", "배달", "택배", "운송", "물류"]],
  ["농림어업직", ["농", "어업", "원예", "임업", "축산"]],
  ["식품 가공·생산직", ["식품", "음료", "제빵원", "제과원"]],
  ["전기·전자 설치·정비·생산직", ["전기", "전자"]],
  ["금속·재료 설치·정비·생산직(판금·단조·주조·용접·도장 등)", ["금속", "용접", "판금", "단조", "주조", "도장"]],
  ["기계 설치·정비·생산직", ["기계", "정비", "수리"]],
  ["화학·환경 설치·정비·생산직", ["화학", "재활용", "폐기물", "환경"]],
  ["섬유·의복 생산직", ["섬유", "의복", "봉제", "재봉", "의류"]],
  ["인쇄·목재·공예 및 기타 설치·정비·생산직", ["인쇄", "목재", "가구", "공예"]],
  ["건설·채굴직", ["건설", "건축", "토목", "채굴", "배관"]],
  ["제조 연구개발직 및 공학기술직", ["연구원", "기술자", "공학"]],
  ["제조 단순직", ["제조", "생산", "포장", "조립", "검사", "단순 종사"]],
];

const PRESERVE_EXISTING_WHEN_NULL = new Set([
  "target_job_class_candidate",
  "job_class_mapping_method",
  "reference_large",
  "reference_mid",
  "reference_small",
]);

function envValue(...names) {
  for (const name of names) {
    const value = process.env[name];
    if (value && String(value).trim()) return String(value).trim();
  }
  return "";
}

function boundedNumber(value, fallback, minimum, maximum, integer) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  const bounded = Math.max(minimum, Math.min(parsed, maximum));
  return integer ? Math.trunc(bounded) : bounded;
}

function secondsToMilliseconds(value, fallbackMs, minimumSeconds, maximumSeconds) {
  const fallbackSeconds = fallbackMs / 1000;
  return boundedNumber(value, fallbackSeconds, minimumSeconds, maximumSeconds, false) * 1000;
}

function abortReason(signal, fallbackMessage) {
  if (signal && signal.reason instanceof Error) return signal.reason;
  const error = new Error(fallbackMessage || "Request aborted");
  error.name = "AbortError";
  return error;
}

function forwardAbort(sourceSignal, targetController) {
  if (!sourceSignal) return () => {};
  const onAbort = () => targetController.abort(abortReason(sourceSignal));
  if (sourceSignal.aborted) onAbort();
  else sourceSignal.addEventListener("abort", onAbort, { once: true });
  return () => sourceSignal.removeEventListener("abort", onAbort);
}

function requestController(parentSignal, timeoutMs, label) {
  const controller = new AbortController();
  const removeParentListener = forwardAbort(parentSignal, controller);
  const timer = setTimeout(() => {
    const error = new Error(`${label} timed out after ${timeoutMs}ms`);
    error.code = "REQUEST_TIMEOUT";
    controller.abort(error);
  }, timeoutMs);
  return {
    controller,
    cleanup() {
      clearTimeout(timer);
      removeParentListener();
    },
  };
}

function retryAfterMilliseconds(value) {
  const text = cleanText(value);
  if (!text) return 0;
  const seconds = Number(text);
  if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);
  const timestamp = Date.parse(text);
  return Number.isFinite(timestamp) ? Math.max(0, timestamp - Date.now()) : 0;
}

function isTransientRequestError(error) {
  if (!error) return false;
  if (typeof error.retryable === "boolean") return error.retryable;
  if (TRANSIENT_HTTP_STATUSES.has(Number(error.statusCode))) return true;
  if (error.code === "REQUEST_TIMEOUT" || error.name === "AbortError" || error.name === "TypeError") return true;
  return ["ECONNRESET", "ECONNREFUSED", "EAI_AGAIN", "ENOTFOUND", "ETIMEDOUT", "UND_ERR_CONNECT_TIMEOUT"].includes(error.code);
}

function waitForRetry(milliseconds, signal) {
  if (!milliseconds) return Promise.resolve();
  if (signal && signal.aborted) return Promise.reject(abortReason(signal));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      if (signal) signal.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    const onAbort = () => {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onAbort);
      reject(abortReason(signal));
    };
    if (signal) signal.addEventListener("abort", onAbort, { once: true });
  });
}

async function requestText(url, init, options) {
  const retries = boundedNumber(options.retries, DEFAULT_REQUEST_RETRIES, 0, MAX_REQUEST_RETRIES, true);
  const retryBaseMs = boundedNumber(options.retryBaseMs, DEFAULT_RETRY_BASE_MS, 50, 5000, true);
  let lastError = null;

  for (let attempt = 0; attempt <= retries; attempt += 1) {
    if (options.signal && options.signal.aborted) throw abortReason(options.signal);
    const request = requestController(options.signal, options.timeoutMs, options.label);
    try {
      const response = await fetch(url, { ...init, signal: request.controller.signal });
      const text = await response.text();
      if (!response.ok) {
        const error = new Error(`${options.label} HTTP ${response.status}: ${text.slice(0, 200)}`);
        error.statusCode = response.status;
        error.retryable = TRANSIENT_HTTP_STATUSES.has(response.status);
        error.retryAfterMs = retryAfterMilliseconds(response.headers.get("retry-after"));
        throw error;
      }
      const value = typeof options.validate === "function" ? options.validate(text, response) : undefined;
      return { response, text, attempts: attempt + 1, value };
    } catch (caught) {
      if (options.signal && options.signal.aborted) throw abortReason(options.signal);
      let error = caught;
      if (request.controller.signal.aborted) {
        error = abortReason(request.controller.signal, `${options.label} timed out`);
        if (error.code === "REQUEST_TIMEOUT") error.retryable = true;
      }
      lastError = error;
      if (attempt >= retries || !isTransientRequestError(error)) throw error;
      const exponentialBackoff = retryBaseMs * (2 ** attempt);
      await waitForRetry(Math.max(exponentialBackoff, Number(error.retryAfterMs) || 0), options.signal);
    } finally {
      request.cleanup();
    }
  }

  throw lastError || new Error(`${options.label} failed`);
}

function createConcurrencyLimiter(concurrency) {
  const limit = boundedNumber(concurrency, DEFAULT_PAGE_CONCURRENCY, 1, MAX_PAGE_CONCURRENCY, true);
  const queue = [];
  let active = 0;

  function drain() {
    while (active < limit && queue.length) {
      const entry = queue.shift();
      active += 1;
      Promise.resolve()
        .then(entry.task)
        .then(entry.resolve, entry.reject)
        .finally(() => {
          active -= 1;
          drain();
        });
    }
  }

  return (task) => new Promise((resolve, reject) => {
    queue.push({ task, resolve, reject });
    drain();
  });
}

function cleanText(value) {
  if (value === undefined || value === null) return null;
  const text = String(value).trim();
  return text || null;
}

function encodeServiceKey(serviceKey) {
  return serviceKey.includes("%") ? serviceKey : encodeURIComponent(serviceKey);
}

function buildPublicDataUrl(apiBaseUrl, endpoint, serviceKey, pageNo, numOfRows) {
  const base = apiBaseUrl.replace(/\/+$/, "");
  return `${base}/${endpoint}?serviceKey=${encodeServiceKey(serviceKey)}&pageNo=${pageNo}&numOfRows=${numOfRows}`;
}

function decodeXml(value) {
  return String(value || "")
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, "\"")
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&")
    .trim();
}

function xmlText(xml, tag) {
  const match = String(xml || "").match(new RegExp(`<${tag}>([\\s\\S]*?)<\\/${tag}>`, "i"));
  return match ? cleanText(decodeXml(match[1])) : null;
}

function parsePublicDataResponse(text, endpoint) {
  if (!/<resultCode>[\s\S]*?<\/resultCode>/i.test(String(text || ""))) {
    const error = new Error(`${endpoint} API returned an invalid XML response`);
    error.code = "INVALID_API_RESPONSE";
    error.retryable = true;
    throw error;
  }
  const resultCode = xmlText(text, "resultCode");
  const resultMsg = xmlText(text, "resultMsg");
  if (resultCode && resultCode !== "0000") {
    const error = new Error(`${endpoint} API returned ${resultCode}: ${resultMsg || "no message"}`);
    const normalizedCode = resultCode.replace(/^0+(?=\d)/, "");
    error.code = "PUBLIC_DATA_API_ERROR";
    error.apiResultCode = resultCode;
    error.retryable = ["1", "2", "4", "5", "21", "22", "99"].includes(normalizedCode);
    throw error;
  }

  const fields = [
    "termDate",
    "busplaName",
    "cntctNo",
    "compAddr",
    "empType",
    "enterType",
    "envBothHands",
    "envEyesight",
    "envHandWork",
    "envHandwork",
    "envLiftPower",
    "envLstnTalk",
    "envStndWalk",
    "jobNm",
    "offerregDt",
    "regDt",
    "regagnName",
    "reqCareer",
    "reqEduc",
    "rno",
    "rnum",
    "salary",
    "salaryType",
  ];

  const items = [];
  const matches = String(text || "").matchAll(/<item>([\s\S]*?)<\/item>/gi);
  for (const match of matches) {
    const rawItem = match[1];
    const row = {};
    fields.forEach((field) => {
      row[field] = xmlText(rawItem, field);
    });
    items.push(row);
  }

  return {
    items,
    meta: {
      endpoint,
      resultCode,
      resultMsg,
      totalCount: Number(xmlText(text, "totalCount") || 0),
      pageNo: Number(xmlText(text, "pageNo") || 0),
      numOfRows: Number(xmlText(text, "numOfRows") || 0),
      items: items.length,
    },
  };
}

async function fetchPage(apiBaseUrl, endpoint, serviceKey, pageNo, numOfRows, requestOptions) {
  const label = `${endpoint} page ${pageNo}`;
  const result = await requestText(
    buildPublicDataUrl(apiBaseUrl, endpoint, serviceKey, pageNo, numOfRows),
    {
      headers: { Accept: "application/xml,*/*", "User-Agent": "JobBridgeLiveJobs/1.0" },
    },
    { ...requestOptions, label, validate: (text) => parsePublicDataResponse(text, endpoint) }
  );
  const parsed = result.value;
  parsed.meta.attempts = result.attempts;
  return parsed;
}

async function fetchEndpoint(apiBaseUrl, endpoint, serviceKey, numOfRows, maxPages, requestOptions, pageLimiter) {
  const first = await pageLimiter(() => {
    if (requestOptions.signal && requestOptions.signal.aborted) throw abortReason(requestOptions.signal);
    return fetchPage(apiBaseUrl, endpoint, serviceKey, 1, numOfRows, requestOptions);
  });
  const pageCount = Math.max(1, Math.ceil((first.meta.totalCount || first.items.length) / numOfRows));
  const requestedPages = Math.min(pageCount, maxPages || MAX_PAGES_PER_ENDPOINT);
  const pageNumbers = [];
  for (let pageNo = 2; pageNo <= requestedPages; pageNo += 1) pageNumbers.push(pageNo);

  const remaining = await Promise.all(pageNumbers.map((pageNo) => pageLimiter(() => {
    if (requestOptions.signal && requestOptions.signal.aborted) throw abortReason(requestOptions.signal);
    return fetchPage(apiBaseUrl, endpoint, serviceKey, pageNo, numOfRows, requestOptions);
  })));
  const rows = [...first.items];
  const pages = [first.meta];
  for (const page of remaining) {
    rows.push(...page.items);
    pages.push(page.meta);
  }

  return {
    rows,
    summary: {
      endpoint,
      totalCount: first.meta.totalCount,
      requestedPages,
      fetchedRows: rows.length,
      requestAttempts: pages.reduce((total, page) => total + (page.attempts || 1), 0),
      pages,
    },
  };
}

function sourcePostingKey(row) {
  const parts = [
    cleanText(row.offerregDt),
    cleanText(row.busplaName),
    cleanText(row.jobNm),
    cleanText(row.termDate),
    cleanText(row.compAddr),
    cleanText(row.salaryType),
    cleanText(row.salary),
  ];
  const digest = crypto.createHash("sha256").update(parts.map((part) => part || "").join("|")).digest("hex").slice(0, 24);
  return `kead_live:hash:${digest}`;
}

function mergeRows(endpointRows) {
  const merged = new Map();
  for (const endpoint of ENDPOINTS) {
    for (const row of endpointRows[endpoint] || []) {
      const key = sourcePostingKey(row);
      const current = merged.get(key);
      const incoming = { ...row, _source_posting_key: key };
      if (!current) {
        merged.set(key, { ...incoming, _source_endpoints: [endpoint], _preferred_endpoint: endpoint });
        continue;
      }
      const endpoints = new Set([...(current._source_endpoints || []), endpoint]);
      const preferIncoming = endpoint === ENV_ENDPOINT;
      const preferCurrent = current._preferred_endpoint === ENV_ENDPOINT;
      const next = { ...current };
      Object.entries(incoming).forEach(([field, value]) => {
        if ((preferIncoming || !preferCurrent) && value !== undefined && value !== null) next[field] = value;
        else if ((next[field] === undefined || next[field] === null) && value !== undefined && value !== null) next[field] = value;
      });
      next._preferred_endpoint = preferIncoming || !preferCurrent ? endpoint : current._preferred_endpoint;
      next._source_endpoints = [...endpoints];
      merged.set(key, next);
    }
  }
  return [...merged.values()];
}

function parseDate(value) {
  let text = cleanText(value);
  if (!text) return null;
  text = text.replace(/\./g, "-").replace(/\//g, "-");
  if (/^\d{8}$/.test(text)) text = `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}`;
  const date = text.slice(0, 10);
  return /^\d{4}-\d{2}-\d{2}$/.test(date) ? date : null;
}

function parseRecruitPeriod(value) {
  const text = cleanText(value);
  if (!text || !text.includes("~")) return [null, null];
  const [start, end] = text.split("~", 2);
  return [parseDate(start), parseDate(end)];
}

function parseNumber(value) {
  const text = cleanText(value);
  if (!text) return null;
  const number = Number(text.replace(/,/g, ""));
  return Number.isFinite(number) ? number : null;
}

function splitRegion(value) {
  const text = cleanText(value);
  if (!text) return [null, null];
  const parts = text.split(/\s+/);
  const sido = SIDO_ALIASES[parts[0]] || parts[0] || null;
  const rest = parts.slice(1);
  if (!rest.length) return [sido, null];
  if (rest.length >= 2 && rest[0].endsWith("시") && rest[1].endsWith("구")) return [sido, `${rest[0]} ${rest[1]}`];
  if (/[시군구]$/.test(rest[0])) return [sido, rest[0]];
  return [sido, null];
}

function compactJobTitle(value) {
  const text = cleanText(value);
  return text ? text.replace(/\([^)]*\)/g, "").replace(/[\s·ㆍ,/]+/g, "") : "";
}

function inferTargetJobClass(jobTitle) {
  const compact = compactJobTitle(jobTitle);
  if (!compact) return [null, "unmapped"];
  for (const [target, patterns] of JOB_CLASS_RULES) {
    if (patterns.some((pattern) => compact.includes(pattern.replace(/\s/g, "")))) {
      return [target, "keyword"];
    }
  }
  return [null, "unmapped"];
}

function payloadHash(payload) {
  const stable = {};
  Object.keys(payload)
    .sort()
    .forEach((key) => {
      stable[key] = payload[key];
    });
  return crypto.createHash("sha256").update(JSON.stringify(stable)).digest("hex");
}

function isCurrentPayload(row, today) {
  return !row.recruit_end || row.recruit_end >= today;
}

function normalizePayload(row, fetchedAt) {
  const sourceKey = cleanText(row._source_posting_key) || sourcePostingKey(row);
  const [recruitStart, recruitEnd] = parseRecruitPeriod(row.termDate);
  const [sido, sigungu] = splitRegion(row.compAddr);
  const [targetJobClass, mappingMethod] = inferTargetJobClass(row.jobNm);
  const rawPayload = {};
  Object.entries(row).forEach(([key, value]) => {
    if (!key.startsWith("_")) rawPayload[key] = value;
  });

  const normalized = {
    posting_id: sourceKey,
    source_system: "kead",
    source_dataset_id: PUBLIC_DATASET_ID,
    source_endpoint: cleanText(row._preferred_endpoint) || ENV_ENDPOINT,
    source_endpoints: row._source_endpoints || [],
    source_posting_key: sourceKey,
    rno: cleanText(row.rno),
    posting_date: parseDate(row.offerregDt),
    offer_registered_date: parseDate(row.offerregDt),
    registered_date: parseDate(row.regDt),
    recruit_period_raw: cleanText(row.termDate),
    recruit_start: recruitStart,
    recruit_end: recruitEnd,
    company_name: cleanText(row.busplaName),
    job_title: cleanText(row.jobNm),
    employment_type: cleanText(row.empType),
    entry_type: cleanText(row.enterType),
    wage_type: cleanText(row.salaryType),
    wage_raw: cleanText(row.salary),
    wage_amount: parseNumber(row.salary),
    required_career: cleanText(row.reqCareer),
    required_education: cleanText(row.reqEduc),
    address_raw: cleanText(row.compAddr),
    sido,
    sigungu,
    target_job_class_candidate: targetJobClass,
    job_class_mapping_method: mappingMethod,
    reference_large: null,
    reference_mid: null,
    reference_small: null,
    agency_name: cleanText(row.regagnName),
    contact_phone: cleanText(row.cntctNo),
    has_environment_detail: Object.keys(ENV_FIELD_MAP).some((field) => Boolean(cleanText(row[field]))),
    raw_payload: rawPayload,
    fetched_at: fetchedAt,
    last_seen_at: fetchedAt,
    is_active: true,
  };

  Object.entries(ENV_FIELD_MAP).forEach(([sourceField, targetField]) => {
    normalized[targetField] = normalized[targetField] || cleanText(row[sourceField]);
  });
  normalized.payload_hash = payloadHash(Object.fromEntries(Object.entries(normalized).filter(([key]) => !["fetched_at", "last_seen_at"].includes(key))));
  return normalized;
}

function supabaseConfig() {
  const url = envValue("SUPABASE_URL", "JOBBRIDGE_SUPABASE_URL").replace(/\/+$/, "");
  const serviceKey = envValue("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY", "JOBBRIDGE_SUPABASE_SERVICE_ROLE_KEY");
  return { url, serviceKey, schema: envValue("SUPABASE_DB_SCHEMA", "JOBBRIDGE_SUPABASE_DB_SCHEMA") || DEFAULT_SUPABASE_SCHEMA };
}

async function supabaseFetch(config, method, path, payload, prefer, requestOptions) {
  try {
    const result = await requestText(
      `${config.url}${path}`,
      {
        method,
        headers: {
          apikey: config.serviceKey,
          Authorization: `Bearer ${config.serviceKey}`,
          "Content-Type": "application/json",
          "Content-Profile": config.schema,
          "Accept-Profile": config.schema,
          Prefer: prefer || "return=minimal",
        },
        body: payload === undefined ? undefined : JSON.stringify(payload),
      },
      { ...requestOptions, label: `Supabase ${method} ${path.split("?")[0]}` }
    );
    return result.text ? JSON.parse(result.text) : null;
  } catch (error) {
    const responseText = String(error.message || error).replace(/^Supabase [^:]+:\s*/, "");
    let body = {};
    try {
      const jsonStart = responseText.indexOf("{");
      body = jsonStart >= 0 ? JSON.parse(responseText.slice(jsonStart)) : {};
    } catch (_error) {
      body = {};
    }
    throw new Error(body.message || body.error_description || body.error || error.message || String(error));
  }
}

async function upsertPayloads(config, payloads, requestOptions) {
  const table = envValue("JOBBRIDGE_SUPABASE_LIVE_JOBS_TABLE") || DEFAULT_SUPABASE_TABLE;
  const chunkSize = 500;
  let upserted = 0;
  for (let index = 0; index < payloads.length; index += chunkSize) {
    const groupedBatches = new Map();
    payloads.slice(index, index + chunkSize).forEach((row) => {
      const next = {};
      Object.entries(row).forEach(([key, value]) => {
        if (value === null && PRESERVE_EXISTING_WHEN_NULL.has(key)) return;
        next[key] = value;
      });
      const keySignature = Object.keys(next).sort().join("|");
      if (!groupedBatches.has(keySignature)) groupedBatches.set(keySignature, []);
      groupedBatches.get(keySignature).push(next);
    });
    const batches = [...groupedBatches.values()];
    const counts = await Promise.all(batches.map(async (batch) => {
      await supabaseFetch(
        config,
        "POST",
        `/rest/v1/${table}?on_conflict=source_posting_key`,
        batch,
        "resolution=merge-duplicates,return=minimal",
        requestOptions
      );
      return batch.length;
    }));
    upserted += counts.reduce((total, count) => total + count, 0);
  }
  return upserted;
}

async function deactivateStale(config, fetchedAt, requestOptions) {
  const table = envValue("JOBBRIDGE_SUPABASE_LIVE_JOBS_TABLE") || DEFAULT_SUPABASE_TABLE;
  const query = new URLSearchParams({
    source_system: "eq.kead",
    last_seen_at: `lt.${fetchedAt}`,
    is_active: "eq.true",
  });
  await supabaseFetch(config, "PATCH", `/rest/v1/${table}?${query.toString()}`, { is_active: false }, "return=minimal", requestOptions);
}

function kstDate() {
  return new Date(Date.now() + 9 * 60 * 60 * 1000).toISOString().slice(0, 10);
}

async function runSyncUnlocked(options, signal) {
  const serviceKey = envValue("JOBBRIDGE_PUBLIC_DATA_SERVICE_KEY");
  if (!serviceKey) {
    return { ok: false, statusCode: 503, message: "JOBBRIDGE_PUBLIC_DATA_SERVICE_KEY must be set on the server." };
  }

  const supabase = supabaseConfig();
  if (!options.dryRun && (!supabase.url || !supabase.serviceKey)) {
    return { ok: false, statusCode: 503, message: "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set on the server." };
  }

  const fetchedAt = new Date().toISOString();
  const apiBaseUrl = envValue("JOBBRIDGE_LIVE_JOBS_API_BASE_URL") || DEFAULT_API_BASE_URL;
  const numOfRows = boundedNumber(options.numOfRows || process.env.JOBBRIDGE_LIVE_JOBS_NUM_OF_ROWS, MAX_NUM_OF_ROWS, 1, MAX_NUM_OF_ROWS, true);
  const requestTimeoutMs = secondsToMilliseconds(
    options.timeoutSeconds || process.env.JOBBRIDGE_LIVE_JOBS_TIMEOUT_SECONDS,
    DEFAULT_PUBLIC_REQUEST_TIMEOUT_MS,
    3,
    30
  );
  const supabaseTimeoutMs = secondsToMilliseconds(
    options.supabaseTimeoutSeconds || process.env.JOBBRIDGE_SUPABASE_TIMEOUT_SECONDS,
    DEFAULT_SUPABASE_REQUEST_TIMEOUT_MS,
    3,
    30
  );
  const requestRetries = boundedNumber(
    options.requestRetries ?? process.env.JOBBRIDGE_LIVE_JOBS_REQUEST_RETRIES,
    DEFAULT_REQUEST_RETRIES,
    0,
    MAX_REQUEST_RETRIES,
    true
  );
  const retryBaseMs = boundedNumber(
    options.retryBaseMs || process.env.JOBBRIDGE_LIVE_JOBS_RETRY_BASE_MS,
    DEFAULT_RETRY_BASE_MS,
    50,
    5000,
    true
  );
  const pageConcurrency = boundedNumber(
    options.pageConcurrency || process.env.JOBBRIDGE_LIVE_JOBS_PAGE_CONCURRENCY,
    DEFAULT_PAGE_CONCURRENCY,
    1,
    MAX_PAGE_CONCURRENCY,
    true
  );
  const maxPages = boundedNumber(options.maxPages, MAX_PAGES_PER_ENDPOINT, 1, MAX_PAGES_PER_ENDPOINT, true);
  const today = cleanText(options.today) || kstDate();

  const collectionController = new AbortController();
  const removeSyncAbortListener = forwardAbort(signal, collectionController);
  const pageLimiter = createConcurrencyLimiter(pageConcurrency);
  let endpointResults;
  try {
    endpointResults = await Promise.all(ENDPOINTS.map((endpoint) => fetchEndpoint(
      apiBaseUrl,
      endpoint,
      serviceKey,
      numOfRows,
      maxPages,
      {
        timeoutMs: requestTimeoutMs,
        retries: requestRetries,
        retryBaseMs,
        signal: collectionController.signal,
      },
      pageLimiter
    ).catch((error) => {
      if (!collectionController.signal.aborted) collectionController.abort(error);
      throw error;
    })));
  } finally {
    removeSyncAbortListener();
  }

  const endpointRows = {};
  const endpointSummaries = [];
  endpointResults.forEach((result, index) => {
    const endpoint = ENDPOINTS[index];
    endpointRows[endpoint] = result.rows;
    endpointSummaries.push(result.summary);
  });

  const mergedRows = mergeRows(endpointRows);
  let payloads = mergedRows.map((row) => normalizePayload(row, fetchedAt));
  const expiredCount = payloads.filter((row) => !isCurrentPayload(row, today)).length;
  payloads = payloads.filter((row) => isCurrentPayload(row, today));
  payloads.sort((a, b) => String(a.source_posting_key).localeCompare(String(b.source_posting_key)));

  const mappedCount = payloads.filter((row) => row.target_job_class_candidate).length;
  let upserted = 0;
  if (!options.dryRun) {
    const supabaseRequestOptions = {
      timeoutMs: supabaseTimeoutMs,
      retries: requestRetries,
      retryBaseMs,
      signal,
    };
    upserted = await upsertPayloads(supabase, payloads, supabaseRequestOptions);
    await deactivateStale(supabase, fetchedAt, supabaseRequestOptions);
  }

  return {
    ok: true,
    fetchedAt,
    apiBaseUrl,
    endpoints: endpointSummaries,
    mergedRows: mergedRows.length,
    normalizedPayloads: payloads.length,
    excludedExpiredRows: expiredCount,
    mappedJobClassRows: mappedCount,
    currentFilterDate: today,
    dedupeKey: "source_posting_key = sha256(offerregDt|busplaName|jobNm|termDate|compAddr|salaryType|salary)",
    mergePolicy: "job_list_env values override job_list values for the same source_posting_key; job_list fills missing fields.",
    fetchPolicy: {
      endpointConcurrency: ENDPOINTS.length,
      pageConcurrency,
      requestRetries,
      requestTimeoutMs,
      supabaseTimeoutMs,
    },
    supabase: {
      enabled: !options.dryRun,
      schema: supabase.schema,
      table: envValue("JOBBRIDGE_SUPABASE_LIVE_JOBS_TABLE") || DEFAULT_SUPABASE_TABLE,
      upserted,
    },
  };
}

async function runSync(options = {}) {
  if (activeSync) {
    return {
      ok: false,
      statusCode: 409,
      message: "A live-job sync is already running in this server process.",
      activeSyncStartedAt: activeSync.startedAt,
    };
  }

  const syncTimeoutMs = secondsToMilliseconds(
    options.syncTimeoutSeconds || process.env.JOBBRIDGE_LIVE_JOBS_SYNC_TIMEOUT_SECONDS,
    DEFAULT_SYNC_TIMEOUT_MS,
    15,
    MAX_SYNC_TIMEOUT_MS / 1000
  );
  const startedAt = new Date().toISOString();
  const controller = new AbortController();
  const timer = setTimeout(() => {
    const error = new Error(`Live-job sync timed out after ${syncTimeoutMs}ms`);
    error.code = "SYNC_TIMEOUT";
    controller.abort(error);
  }, syncTimeoutMs);
  const promise = runSyncUnlocked(options, controller.signal);
  activeSync = { promise, startedAt };

  try {
    return await promise;
  } catch (error) {
    const reason = controller.signal.aborted ? abortReason(controller.signal) : error;
    if (reason && reason.code === "SYNC_TIMEOUT") {
      return { ok: false, statusCode: 504, message: reason.message, startedAt };
    }
    throw error;
  } finally {
    clearTimeout(timer);
    if (activeSync && activeSync.promise === promise) activeSync = null;
  }
}

async function handler(req, res) {
  if (handleOptions(req, res)) return;
  if (req.method !== "POST") {
    sendJson(req, res, 405, { ok: false, message: "Method not allowed" });
    return;
  }

  try {
    const admin = await requireAdmin(req);
    if (!admin.ok) {
      sendJson(req, res, admin.statusCode || 403, { ok: false, message: admin.message });
      return;
    }

    const body = await readJsonBody(req);
    const payload = await runSync(body || {});
    sendJson(req, res, payload.statusCode || (payload.ok ? 200 : 500), payload);
  } catch (error) {
    sendJson(req, res, 502, { ok: false, message: String(error.message || error) });
  }
}

module.exports = handler;
module.exports.runSync = runSync;
