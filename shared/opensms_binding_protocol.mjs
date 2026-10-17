import crypto from "node:crypto";

export const ADMIN_ORIGIN = "https://adminsms.smsoug.com";
export const PROTOCOL_ASSET = "/static/js/app.e5c03d78.js";

// Public frontend protocol parameters, verified against the current app bundle.
const key = Buffer.from("yDyAaybxxWlwaZegx5wqMsCH+P1dhq+JkbcKt+FdvJ4=", "base64");
const iv = Buffer.from("ap6R+U4T5pZgszC/ctVmUQ==", "base64");

export function encryptPayload(payload) {
  const cipher = crypto.createCipheriv("aes-256-cbc", key, iv);
  return Buffer.concat([cipher.update(payload, "utf8"), cipher.final()]).toString("base64");
}

function decryptPayload(payload) {
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(payload) ||
      !payload.length || Buffer.from(payload, "base64").length % 16 !== 0) {
    throw new Error("OpenSMS response is not valid AES ciphertext");
  }
  try {
    const decipher = crypto.createDecipheriv("aes-256-cbc", key, iv);
    return Buffer.concat([decipher.update(payload, "base64"), decipher.final()]).toString("utf8");
  } catch {
    throw new Error("OpenSMS response decryption failed; recheck the current frontend protocol before retrying any write");
  }
}

export function parseResponseText(text, contentType = "", status = 200) {
  if (status === 401) throw new Error("OpenSMS authentication failed (HTTP 401)");
  if (status === 403) throw new Error("OpenSMS access denied (HTTP 403)");
  if (status < 200 || status >= 300) throw new Error(`OpenSMS HTTP ${status}`);
  if (/text\/html/i.test(contentType) || /<!doctype html|<html|Just a moment|cf_chl|challenge-platform/i.test(String(text).slice(0, 1200))) {
    throw new Error("OpenSMS returned an HTML page instead of API data (login, challenge, or route error)");
  }
  const raw = String(text).trim();
  if (!raw) throw new Error("OpenSMS returned an empty response");
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    data = raw;
  }
  if (typeof data === "string") {
    const decrypted = decryptPayload(data);
    try {
      data = JSON.parse(decrypted);
    } catch {
      throw new Error("OpenSMS decrypted response is not valid JSON");
    }
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new Error("OpenSMS returned an unexpected response shape");
  }
  if (Number(data.code) === 401) throw new Error("OpenSMS authentication failed (business code 401)");
  return data;
}
