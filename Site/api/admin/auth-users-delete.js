const { deleteAuthUser, handleOptions, readJsonBody, requireAdmin, sendJson } = require("../_supabaseAdmin");

module.exports = async function handler(req, res) {
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
    const payload = await deleteAuthUser(body.userId, body.shouldSoftDelete);
    sendJson(req, res, payload.statusCode || (payload.enabled === false ? 503 : 200), payload);
  } catch (error) {
    sendJson(req, res, 502, { ok: false, message: String(error.message || error) });
  }
};
