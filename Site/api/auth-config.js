const { handleOptions, publicAuthConfig, sendJson } = require("./_supabaseAdmin");

module.exports = async function handler(req, res) {
  if (handleOptions(req, res)) return;
  if (req.method !== "GET") {
    sendJson(req, res, 405, { ok: false, message: "Method not allowed" });
    return;
  }
  sendJson(req, res, 200, publicAuthConfig());
};
