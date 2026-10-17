import test from "node:test";
import assert from "node:assert/strict";
import { ADMIN_ORIGIN, encryptPayload, parseResponseText } from "./opensms_binding_protocol.mjs";

const data = { code: 200, rows: [{ id: "2094395639243870209", name: "test" }] };

test("uses the new admin origin", () => {
  assert.equal(ADMIN_ORIGIN, "https://adminsms.smsoug.com");
});

test("reads plaintext JSON without attempting decryption", () => {
  assert.deepEqual(parseResponseText(JSON.stringify(data), "application/json"), data);
});

test("reads both quoted and unquoted encrypted responses", () => {
  const encrypted = encryptPayload(JSON.stringify(data));
  assert.deepEqual(parseResponseText(JSON.stringify(encrypted)), data);
  assert.deepEqual(parseResponseText(encrypted, "text/plain"), data);
});

test("preserves UTF-8 text and large string identifiers", () => {
  const utf8 = { code: 200, name: "\u4e2d\u6587", id: "2094395639243870209" };
  assert.deepEqual(parseResponseText(encryptPayload(JSON.stringify(utf8))), utf8);
});

for (const status of [401, 403, 404, 500]) {
  test(`reports HTTP ${status} before trying to parse or decrypt the body`, () => {
    assert.throws(() => parseResponseText("not ciphertext", "text/html", status), new RegExp(`HTTP ${status}`));
  });
}

test("rejects login and challenge HTML even with HTTP 200", () => {
  assert.throws(() => parseResponseText("<!doctype html><html>login</html>"), /HTML page/);
  assert.throws(() => parseResponseText("Just a moment", "text/html"), /HTML page/);
});

test("rejects business authentication failures, including encrypted ones", () => {
  for (const body of ['{"code":401}', encryptPayload('{"code":401}')]) {
    assert.throws(() => parseResponseText(body), /authentication failed/);
  }
});

test("rejects malformed, empty, or unexpected responses", () => {
  for (const body of ["", "null", "[]", "42", "not base64"]) {
    assert.throws(() => parseResponseText(body));
  }
  assert.throws(() => parseResponseText(encryptPayload("not JSON")), /not valid JSON/);
});

test("decryption errors do not expose ciphertext or key material", () => {
  const invalid = Buffer.alloc(16).toString("base64");
  assert.throws(() => parseResponseText(JSON.stringify(invalid)), error => {
    assert.match(error.message, /recheck the current frontend protocol/);
    assert.ok(!error.message.includes(invalid));
    return true;
  });
});
