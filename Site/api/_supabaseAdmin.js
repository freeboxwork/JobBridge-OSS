const MAX_PER_PAGE = 1000;

function envValue(...names) {
  for (const name of names) {
    const value = process.env[name];
    if (value && String(value).trim()) return String(value).trim();
  }
  return "";
}

function splitEnv(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function supabaseConfig() {
  const url = envValue("SUPABASE_URL", "JOBBRIDGE_SUPABASE_URL").replace(/\/+$/, "");
  const publicKey = envValue(
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_ANON_KEY",
    "JOBBRIDGE_SUPABASE_PUBLISHABLE_KEY",
    "JOBBRIDGE_SUPABASE_ANON_KEY"
  );
  const serviceKey = envValue(
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_SECRET_KEY",
    "JOBBRIDGE_SUPABASE_SERVICE_ROLE_KEY"
  );
  return { url, publicKey, serviceKey };
}

function publicAuthConfig() {
  const { url, publicKey } = supabaseConfig();
  return {
    ok: Boolean(url && publicKey),
    hasUrl: Boolean(url),
    hasAnonKey: Boolean(publicKey),
    supabaseUrl: url,
    supabaseAnonKey: publicKey,
    secretsExposed: false,
  };
}

function setJsonHeaders(req, res) {
  const allowedOrigin = envValue("JOBBRIDGE_ALLOWED_ORIGIN");
  if (allowedOrigin) {
    res.setHeader("Access-Control-Allow-Origin", allowedOrigin);
  } else if (req.headers.origin) {
    res.setHeader("Access-Control-Allow-Origin", req.headers.origin);
    res.setHeader("Vary", "Origin");
  }
  res.setHeader("Access-Control-Allow-Headers", "content-type,authorization,x-jobbridge-admin-token");
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
}

function sendJson(req, res, statusCode, payload) {
  setJsonHeaders(req, res);
  res.statusCode = statusCode;
  res.end(JSON.stringify(payload));
}

function handleOptions(req, res) {
  if (req.method !== "OPTIONS") return false;
  setJsonHeaders(req, res);
  res.statusCode = 204;
  res.end();
  return true;
}

function headerValue(req, name) {
  const value = req.headers[String(name).toLowerCase()];
  return Array.isArray(value) ? value[0] || "" : String(value || "");
}

function bearerToken(req) {
  const authHeader = headerValue(req, "authorization").trim();
  return authHeader.toLowerCase().startsWith("bearer ") ? authHeader.slice(7).trim() : "";
}

async function requireAdmin(req) {
  const configuredToken = envValue("JOBBRIDGE_ADMIN_TOKEN");
  const suppliedToken = headerValue(req, "x-jobbridge-admin-token").trim();
  if (configuredToken && suppliedToken && suppliedToken === configuredToken) {
    return { ok: true, mode: "admin_token" };
  }

  const { url, publicKey, serviceKey } = supabaseConfig();
  const accessToken = bearerToken(req);
  if (!accessToken) {
    return {
      ok: false,
      statusCode: 401,
      message: "Admin Supabase session is required.",
    };
  }
  if (!url || !(publicKey || serviceKey)) {
    return {
      ok: false,
      statusCode: 503,
      message: "SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY must be set on the server.",
    };
  }

  const response = await fetch(`${url}/auth/v1/user`, {
    headers: {
      apikey: publicKey || serviceKey,
      Authorization: `Bearer ${accessToken}`,
      Accept: "application/json",
    },
  });
  const user = await response.json().catch(() => ({}));
  if (!response.ok || !user || !user.id) {
    return {
      ok: false,
      statusCode: 401,
      message: "Supabase session could not be verified.",
    };
  }

  const allowedIds = new Set(splitEnv(envValue("JOBBRIDGE_ADMIN_USER_IDS")));
  const allowedEmails = new Set(splitEnv(envValue("JOBBRIDGE_ADMIN_EMAILS")).map((email) => email.toLowerCase()));
  if (!allowedIds.size && !allowedEmails.size) {
    return {
      ok: false,
      statusCode: 403,
      message: "JOBBRIDGE_ADMIN_USER_IDS or JOBBRIDGE_ADMIN_EMAILS must be set on the server.",
    };
  }

  const userEmail = String(user.email || "").toLowerCase();
  if (allowedIds.has(String(user.id)) || (userEmail && allowedEmails.has(userEmail))) {
    return { ok: true, mode: "supabase_session", user };
  }
  return {
    ok: false,
    statusCode: 403,
    message: "Current Supabase user is not allowed to manage auth users.",
  };
}

function requireSupabaseAdminConfig() {
  const config = supabaseConfig();
  if (!config.url || !config.serviceKey) {
    return {
      ok: false,
      statusCode: 503,
      message: "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SECRET_KEY must be set on the server.",
    };
  }
  return { ok: true, ...config };
}

async function supabaseRequest(config, method, path, query, payload, profile) {
  const url = new URL(`${config.url}${path}`);
  Object.entries(query || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
  });

  const headers = {
    apikey: config.serviceKey,
    Authorization: `Bearer ${config.serviceKey}`,
    Accept: "application/json",
  };
  if (profile) {
    headers["Accept-Profile"] = profile;
    headers["Content-Profile"] = profile;
  }
  const options = { method, headers };
  if (payload !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  }

  const response = await fetch(url, options);
  const text = await response.text();
  let body = {};
  try {
    body = text ? JSON.parse(text) : {};
  } catch (_error) {
    body = { message: text };
  }
  if (!response.ok) {
    const detail = body.message || body.error_description || body.error || text || `HTTP ${response.status}`;
    throw new Error(`Supabase ${method} ${path} failed: ${detail}`);
  }
  return body;
}

async function profileMap(config) {
  try {
    const rows = await supabaseRequest(
      config,
      "GET",
      "/rest/v1/profiles",
      { select: "id,email,display_name,created_at,updated_at", limit: "1000" },
      undefined,
      "public"
    );
    if (!Array.isArray(rows)) {
      return { profiles: new Map(), profileStatus: { ok: false, message: "profiles response was not a list" } };
    }
    return {
      profiles: new Map(rows.filter((row) => row && row.id).map((row) => [String(row.id), row])),
      profileStatus: { ok: true },
    };
  } catch (error) {
    return { profiles: new Map(), profileStatus: { ok: false, message: String(error.message || error) } };
  }
}

async function listAuthUsers(req) {
  const configCheck = requireSupabaseAdminConfig();
  if (!configCheck.ok) return configCheck;

  const currentUrl = new URL(req.url, `https://${req.headers.host || "localhost"}`);
  const provider = String(currentUrl.searchParams.get("provider") || "all").trim().toLowerCase();
  const q = String(currentUrl.searchParams.get("q") || "").trim().toLowerCase();
  const page = Math.max(1, Number.parseInt(currentUrl.searchParams.get("page") || "1", 10) || 1);
  const perPage = Math.max(
    1,
    Math.min(Number.parseInt(currentUrl.searchParams.get("perPage") || currentUrl.searchParams.get("per_page") || "1000", 10) || 1000, MAX_PER_PAGE)
  );

  const raw = await supabaseRequest(configCheck, "GET", "/auth/v1/admin/users", {
    page: String(page),
    per_page: String(perPage),
  });
  const rawUsers = Array.isArray(raw) ? raw : Array.isArray(raw.users) ? raw.users : [];
  const { profiles, profileStatus } = await profileMap(configCheck);
  let users = rawUsers
    .filter((user) => user && typeof user === "object")
    .map((user) => normalizeAuthUser(user, profiles.get(String(user.id || ""))));

  if (provider && provider !== "all") {
    users = users.filter((user) => user.providers.includes(provider));
  }
  if (q) {
    users = users.filter((user) => userMatchesQuery(user, q));
  }

  const providerCounts = {};
  users.forEach((user) => {
    (user.providers.length ? user.providers : ["unknown"]).forEach((item) => {
      providerCounts[item] = (providerCounts[item] || 0) + 1;
    });
  });

  return {
    ok: true,
    enabled: true,
    page,
    perPage,
    total: users.length,
    providerCounts,
    profileStatus,
    users,
  };
}

async function deleteAuthUser(userId, shouldSoftDelete) {
  const configCheck = requireSupabaseAdminConfig();
  if (!configCheck.ok) return configCheck;
  const cleanUserId = String(userId || "").trim();
  if (!cleanUserId) {
    return { ok: false, statusCode: 400, message: "userId is required" };
  }
  const data = await supabaseRequest(
    configCheck,
    "DELETE",
    `/auth/v1/admin/users/${encodeURIComponent(cleanUserId)}`,
    undefined,
    { should_soft_delete: Boolean(shouldSoftDelete) }
  );
  return {
    ok: true,
    enabled: true,
    deletedUserId: cleanUserId,
    softDeleted: Boolean(shouldSoftDelete),
    data,
  };
}

function normalizeAuthUser(user, profile) {
  const metadata = user.user_metadata && typeof user.user_metadata === "object" ? user.user_metadata : {};
  const appMetadata = user.app_metadata && typeof user.app_metadata === "object" ? user.app_metadata : {};
  const identities = Array.isArray(user.identities) ? user.identities : [];
  const kakaoProfile = metadata.kakao_account && metadata.kakao_account.profile ? metadata.kakao_account.profile : {};
  const kakaoProperties = metadata.properties && typeof metadata.properties === "object" ? metadata.properties : {};
  const providers = providerNames(user, identities, appMetadata);
  const displayName = firstText(
    profile && profile.display_name,
    metadata.name,
    metadata.full_name,
    metadata.display_name,
    metadata.nickname,
    metadata.preferred_username,
    kakaoProfile.nickname,
    kakaoProperties.nickname,
    user.email,
    user.phone,
    user.id
  );
  const avatarUrl = firstText(
    metadata.avatar_url,
    metadata.picture,
    metadata.profile_image_url,
    metadata.profile_image,
    kakaoProfile.profile_image_url,
    kakaoProperties.profile_image,
    kakaoProperties.thumbnail_image
  );
  return {
    id: user.id,
    email: user.email || (profile && profile.email) || "",
    phone: user.phone || "",
    displayName,
    avatarUrl,
    providers,
    primaryProvider: providers[0] || "unknown",
    createdAt: user.created_at,
    updatedAt: user.updated_at,
    lastSignInAt: user.last_sign_in_at,
    confirmedAt: user.confirmed_at || user.email_confirmed_at || user.phone_confirmed_at,
    bannedUntil: user.banned_until,
    isAnonymous: Boolean(user.is_anonymous),
    profile: profile
      ? {
          email: profile.email || "",
          displayName: profile.display_name || "",
          createdAt: profile.created_at,
          updatedAt: profile.updated_at,
        }
      : null,
    identities: identities
      .filter((item) => item && typeof item === "object")
      .map((item) => ({
        id: item.id,
        provider: item.provider || "unknown",
        createdAt: item.created_at,
        lastSignInAt: item.last_sign_in_at,
      })),
  };
}

function providerNames(user, identities, appMetadata) {
  const names = [];
  if (appMetadata.provider) names.push(String(appMetadata.provider).toLowerCase());
  if (Array.isArray(appMetadata.providers)) {
    appMetadata.providers.forEach((provider) => names.push(String(provider).toLowerCase()));
  }
  identities.forEach((item) => {
    if (item && item.provider) names.push(String(item.provider).toLowerCase());
  });
  if (!names.length && user.email) names.push("email");
  if (!names.length && user.phone) names.push("phone");
  return [...new Set(names)].sort();
}

function userMatchesQuery(user, q) {
  return [
    user.id,
    user.email,
    user.phone,
    user.displayName,
    ...(user.providers || []),
  ]
    .join(" ")
    .toLowerCase()
    .includes(q);
}

function firstText(...values) {
  for (const value of values) {
    if (value === undefined || value === null) continue;
    const text = String(value).trim();
    if (text) return text;
  }
  return "";
}

async function readJsonBody(req) {
  if (req.body && typeof req.body === "object") return req.body;
  if (typeof req.body === "string") {
    try {
      return JSON.parse(req.body || "{}");
    } catch (_error) {
      return {};
    }
  }
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const text = Buffer.concat(chunks).toString("utf8");
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch (_error) {
    return {};
  }
}

module.exports = {
  deleteAuthUser,
  handleOptions,
  listAuthUsers,
  publicAuthConfig,
  readJsonBody,
  requireAdmin,
  sendJson,
};
