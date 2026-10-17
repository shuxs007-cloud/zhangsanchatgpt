import fs from "node:fs/promises";
import fssync from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ADMIN_ORIGIN, encryptPayload, parseResponseText } from "./opensms_binding_protocol.mjs";
import { acquireSharedOpenSmsSession } from "/Users/zhangsan/Downloads/430/scripts/opensms_shared_browser.mjs";
import { createRawBackgroundPage } from "/Users/zhangsan/Downloads/430/opensms_protocol_client.mjs";

const scriptRoot = path.dirname(fileURLToPath(import.meta.url));
const rules = JSON.parse(fssync.readFileSync(path.join(scriptRoot, "opensms_rules.json"), "utf8"));
const minIntervalMs = process.env.OPENSMS_REQUEST_INTERVAL_MS === undefined
  ? Math.max(2000, Number(rules.defaults.minIntervalMs))
  : Number(process.env.OPENSMS_REQUEST_INTERVAL_MS);
const blockedCooldownMs = Math.max(30000, Number(rules.defaults.blockedCooldownMs));
if (minIntervalMs < 1000 || ![minIntervalMs, blockedCooldownMs].every(value => Number.isFinite(value) && value <= 300000)) {
  throw new Error("Invalid OpenSMS request timing configuration");
}
const folder = process.env.OPENSMS_OUTPUT_DIR || scriptRoot;
await fs.mkdir(folder, { recursive: true });
const browserId = "a53ce74e6c9e4fa08aec35f81ce1e2a3";
const sharedBrowser = await acquireSharedOpenSmsSession();
process.once('exit',()=>sharedBrowser.release());

const mode = process.argv[2] || "";
const arg1 = process.argv[3] || "";
const arg2 = process.argv[4] || "";
const arg3 = process.argv[5] || "";
if (!["query-carriers", "query-agents-matching", "query-source-users", "query-source-identif", "query-account-bindings", "only-source", "only-accounts", "only-accounts-identifs", "add-source", "add-sources-union", "add-accounts", "add-accounts-identif", "add-prefix", "add-all", "add-all-excluding", "remove-source", "remove-source-identif", "remove-source-matching", "remove-sources-matching-excluding", "remove-binding-ids"].includes(mode) || !arg1 || (!["query-carriers", "query-agents-matching", "query-source-users", "query-source-identif", "query-account-bindings", "add-all", "remove-source", "remove-source-identif", "remove-binding-ids"].includes(mode) && !arg2) || (mode === "remove-sources-matching-excluding" && !arg3)) {
  throw new Error("Usage: node opensms_raw_cdp_binding_tmp.mjs query-carriers KEYWORDS | query-agents-matching KEYWORDS [EXACT_EXCLUSIONS] | query-source-users SOURCE | query-source-identif IDENTIF | query-account-bindings NAME | only-source SOURCE TARGETS | only-accounts NAMES TARGETS | only-accounts-identifs NAMES IDENTIFS | add-source SOURCE TARGETS | add-sources-union SOURCES TARGETS | add-accounts NAMES TARGETS | add-accounts-identif NAMES IDENTIF | add-prefix PREFIXES TARGETS | add-all TARGETS | add-all-excluding KEYWORDS TARGETS | remove-source SOURCE | remove-source-identif IDENTIF | remove-source-matching SOURCE KEYWORDS | remove-sources-matching-excluding SOURCES KEYWORDS EXCLUSIONS | remove-binding-ids RECORD.json");
}

function beijingNow() {
  return new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date());
}

function norm(value) {
  return String(value || "").replace(/\s+/g, "").toLowerCase();
}

function carrierNorm(value) {
  return norm(value).replace(/-/g, "");
}

function assertOk(label, result) {
  if (!result || Number(result.code) !== 200) throw new Error(`${label} failed: ${JSON.stringify(result)}`);
}

function findCarrier(carriers, query) {
  if (process.env.OPENSMS_STRICT_MATCH === "1") {
    const q = String(query).trim().toLowerCase();
    const exact = carriers.filter(row => String(row.name).trim().toLowerCase() === q);
    const matches = exact.length ? exact : carriers.filter(row => String(row.name).trim().toLowerCase().startsWith(`${q}-`));
    if (matches.length !== 1) throw new Error(`Carrier must match uniquely: ${query}`);
    return matches[0];
  }
  const raw = String(query || "").trim().toLowerCase();
  const exactPrefix = carriers.find((row) => {
    const name = String(row.name || "").trim().toLowerCase();
    return name === raw || name.startsWith(`${raw}-`);
  });
  if (exactPrefix) return exactPrefix;
  const q = carrierNorm(query);
  return carriers.find((row) => carrierNorm(row.name) === q) ||
    carriers.find((row) => carrierNorm(row.name).startsWith(q)) ||
    carriers.find((row) => carrierNorm(row.name).includes(q));
}

function compactBinding(row) {
  return {
    id: String(row.id || ""),
    proxyUserId: String(row.proxyUserId || ""),
    proxyUserName: String(row.proxyUserName || ""),
    carrierName: String(row.carrierName || ""),
    carrireIdentif: String(row.carrireIdentif || ""),
    createTime: row.createTime || row.createdAt || row.createDate || "",
    updateTime: row.updateTime || row.updatedAt || row.updateDate || "",
  };
}

class CdpClient {
  constructor(url) {
    this.url = url;
    this.nextId = 1;
    this.pending = new Map();
  }

  async connect() {
    this.ws = new WebSocket(this.url);
    this.ws.onmessage = (event) => {
      const message = JSON.parse(String(event.data));
      if (!message.id || !this.pending.has(message.id)) return;
      const { resolve, reject } = this.pending.get(message.id);
      this.pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message));
      else resolve(message.result || {});
    };
    await new Promise((resolve, reject) => {
      this.ws.onopen = resolve;
      this.ws.onerror = () => reject(new Error(`CDP websocket connection failed: ${this.url}`));
      setTimeout(() => reject(new Error(`CDP websocket connection timed out: ${this.url}`)), 10000).unref();
    });
    await this.send("Runtime.enable");
    await this.send("Page.enable");
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (!this.pending.has(id)) return;
        this.pending.delete(id);
        reject(new Error(`CDP command timed out: ${method}`));
      }, 30000).unref();
    });
  }

  async evaluate(expression) {
    const response = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (response.exceptionDetails) {
      throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text || "Runtime.evaluate failed");
    }
    return response.result?.value;
  }

  close() {
    this.ws?.close();
  }
}

const startedAtBeijing = beijingNow();
const port = String(sharedBrowser.session.port);
if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) throw new Error("Invalid local CDP port");
const background = await createRawBackgroundPage({cdp:sharedBrowser.session.cdp,
  url:`${ADMIN_ORIGIN}/proxyCarrierConfig`,authenticatedTargetId:sharedBrowser.target.id});
const target = background.target;

const cdp = new CdpClient(target.webSocketDebuggerUrl);
try {
  await cdp.connect();
  const location = await cdp.evaluate("location.href");
  if (/\/login(?:\?|$)/i.test(location)) throw new Error("OpenSMS login expired");
  if (new URL(location).origin !== ADMIN_ORIGIN) throw new Error(`Unexpected page origin: ${location}`);

  let refreshCount = 0;
  let nextRequestAt = Date.now() + minIntervalMs;
  console.error(JSON.stringify({ requestPacing: { minIntervalMs, blockedCooldownMs } }));
  let fatalProtocolError;
  function protocolStop(message) {
    fatalProtocolError = new Error(message);
    throw fatalProtocolError;
  }
  async function requestOnce(method, apiPath, data, searchParams) {
    if (fatalProtocolError) throw fatalProtocolError;
    const waitMs = Math.max(0, nextRequestAt - Date.now());
    if (waitMs) await new Promise((resolve) => setTimeout(resolve, waitMs));
    nextRequestAt = Date.now() + minIntervalMs;
    const url = new URL(`${ADMIN_ORIGIN}/api${apiPath}`);
    for (const [name, value] of Object.entries(searchParams || {})) {
      if (value !== undefined && value !== null) url.searchParams.set(name, String(value));
    }
    const body = data === undefined || data === null ? null : encryptPayload(JSON.stringify(data));
    const result = await cdp.evaluate(`(async () => {
      const raw = document.cookie.split("; ").find((part) => part.startsWith("Admin-Token="));
      const token = raw ? decodeURIComponent(raw.slice("Admin-Token=".length)) : "";
      if (!token) return { authMissing: true, status: 0, contentType: "", text: "" };
      const headers = {
        Accept: "application/json, text/plain, */*",
        Authorization: token.startsWith("Bearer ") ? token : "Bearer " + token
      };
      const body = ${JSON.stringify(body)};
      if (body !== null) headers["Content-Type"] = "text/plain";
      const response = await fetch(${JSON.stringify(url.toString())}, {
        method: ${JSON.stringify(method)},
        headers,
        body,
        credentials: "include"
      });
      return {
        authMissing: false,
        status: response.status,
        contentType: response.headers.get("content-type") || "",
        text: await response.text()
      };
    })()`);
    nextRequestAt = Date.now() + (result.status === 429 ? Math.max(60000, minIntervalMs) : minIntervalMs);
    return result;
  }

  async function api(method, apiPath, data, searchParams) {
    let response = await requestOnce(method, apiPath, data, searchParams);
    if (response.authMissing || response.status === 401) protocolStop(`${method} ${apiPath}: authentication failed`);
    const blocked = response.status === 403 || (response.status >= 200 && response.status < 300 &&
      (/text\/html/i.test(response.contentType) || /Just a moment|cf_chl/i.test(response.text)));
    if (blocked && method === 'GET' && refreshCount === 0) {
      refreshCount += 1;
      console.error(JSON.stringify({ blocked: true, status: response.status, cooldownMs: blockedCooldownMs, retry: 1 }));
      await new Promise((resolve) => setTimeout(resolve, blockedCooldownMs));
      await cdp.send("Page.reload", { ignoreCache: true });
      await new Promise((resolve) => setTimeout(resolve, 3500));
      response = await requestOnce(method, apiPath, data, searchParams);
    }
    if (response.authMissing || response.status === 401) protocolStop(`${method} ${apiPath}: authentication failed`);
    if (response.status === 403) protocolStop(`${method} ${apiPath}: access denied (HTTP 403) after refresh`);
    if (response.status === 400) {
      let detail = "";
      try {
        const parsed = JSON.parse(response.text);
        detail = String(parsed.msg || parsed.message || parsed.error || "").slice(0, 500);
      } catch {}
      protocolStop(`${method} ${apiPath}: HTTP 400 ${detail}`);
    }
    if (response.status >= 400) protocolStop(`${method} ${apiPath}: HTTP ${response.status}`);
    try {
      const parsed = parseResponseText(response.text, response.contentType, response.status);
      if (Number(parsed.code) !== 200) protocolStop(`${method} ${apiPath}: business code ${parsed.code}`);
      return parsed;
    } catch (error) {
      protocolStop(`${method} ${apiPath}: ${error.message}`);
    }
  }

  const carrierResp = await api("GET", "/carrier/config/listNotDeleted", null, { pageNum: 1, pageSize: 9999 });
  assertOk("读取运营商配置", carrierResp);
  const carriers = carrierResp.rows || [];
  async function readCarrierBindings(identif) {
    identif = String(identif);
    const rows = [];
    for (let pageNum = 1; pageNum <= 10000; pageNum += 1) {
      const response = await api("GET", "/proxycarrier/list", null, {
        pageNum, pageSize: 100, carrireIdentif: identif,
      });
      assertOk(`读取运营商标识 ${identif} 绑定`, response);
      if (!Array.isArray(response.rows)) throw new Error("Binding rows missing");
      rows.push(...response.rows.map(compactBinding));
      if (rows.some((row) => row.carrireIdentif !== identif)) throw new Error("Carrier filter mismatch");
      if (new Set(rows.map((row) => row.id)).size !== rows.length) throw new Error("Duplicate pagination rows");
      const total = Number(response.total);
      if (!Number.isFinite(total) || total < 0) throw new Error("Binding total missing");
      if (rows.length === total) return rows;
      if (rows.length > total || response.rows.length === 0) throw new Error("Binding pagination incomplete");
    }
    throw new Error("Binding pagination limit exceeded");
  }
  if (mode === "query-carriers") {
    const keywords = arg1.split(",").map((value) => norm(value)).filter(Boolean);
    const matches = carriers
      .filter((row) => keywords.includes("*") || keywords.some((keyword) => norm(row.name).includes(keyword)))
      .map((row) => ({ name: String(row.name || ""), identif: String(row.identif || ""), id: String(row.id || "") }));
    console.log(JSON.stringify({ ok: true, mode, keywords: arg1, count: matches.length, matches }, null, 2));
    cdp.close();
    process.exit(0);
  }
  if (mode === "query-source-users") {
    const sourceCarrier = findCarrier(carriers, arg1);
    if (!sourceCarrier) throw new Error(`缺少来源运营商配置: ${arg1}`);
    const bindings = await readCarrierBindings(sourceCarrier.identif);
    const users = [...new Map(bindings.map((row) => [String(row.proxyUserId), {
      proxyUserId: String(row.proxyUserId || ""),
      proxyUserName: String(row.proxyUserName || ""),
    }])).values()].filter((row) => row.proxyUserId);
    const accountBindings = arg2 === "--with-bindings" ? [] : undefined;
    if (accountBindings) {
      for (const user of users) {
        accountBindings.push({ ...user, bindings: await bindingsForUser(user) });
        if (accountBindings.length % 10 === 0) {
          console.error(JSON.stringify({ checked: accountBindings.length, total: users.length }));
        }
      }
    }
    console.log(JSON.stringify({
      ok: true,
      mode,
      source: {
        query: arg1,
        name: sourceCarrier.name,
        identif: String(sourceCarrier.identif),
      },
      count: users.length,
      users,
      bindings,
      accountBindings,
    }, null, 2));
    cdp.close();
    process.exit(0);
  }
  if (mode === "query-source-identif" || mode === "remove-source-identif") {
    const identif = String(arg1).trim();
    const readBindings = () => readCarrierBindings(identif);
    const before = await readBindings();
    if (mode === "query-source-identif") {
      console.log(JSON.stringify({ ok: true, mode, identif, count: before.length, bindings: before }, null, 2));
      cdp.close();
      process.exit(0);
    }

    const results = [];
    const failures = [];
    for (const binding of before) {
      try {
        if (!binding.id) throw new Error("绑定配置 ID 为空");
        await new Promise((resolve) => setTimeout(resolve, 600));
        const response = await api("DELETE", `/proxycarrier/${binding.id}`);
        assertOk(`删除 ${binding.proxyUserName}/${binding.carrierName}`, response);
        results.push({ ...binding, ok: true });
      } catch (error) {
        failures.push({ ...binding, error: error.message });
      }
      if ((results.length + failures.length) % 25 === 0) {
        console.error(JSON.stringify({ processed: results.length + failures.length, total: before.length, deleted: results.length, failures: failures.length }));
      }
    }
    const after = await readBindings();
    const result = {
      ok: failures.length === 0 && after.length === 0,
      mode,
      protocol: "raw-cdp",
      startedAtBeijing,
      finishedAtBeijing: beijingNow(),
      browserId,
      port,
      pageTargetId: target.id,
      refreshCount,
      identif,
      beforeCount: before.length,
      deletedCount: results.length,
      failureCount: failures.length,
      remainingCount: after.length,
      results,
      failures,
      remaining: after,
    };
    const outputPath = path.join(folder, `opensms_remove_source_identif_${new Date().toISOString().replace(/[:.]/g, "-")}.json`);
    await fs.writeFile(outputPath, JSON.stringify(result, null, 2), "utf8");
    console.log(JSON.stringify({ outputPath, ...result }, null, 2));
    cdp.close();
    process.exit(result.ok ? 0 : 2);
  }
  const targetArg = mode === "add-all" ? arg1 : arg2;
  const targetCarriers = (["query-agents-matching", "add-accounts-identif", "only-accounts-identifs", "remove-source-matching", "remove-sources-matching-excluding"].includes(mode) ? [] : targetArg.split(",")).map((query) => query.trim()).filter(Boolean).map((query) => {
    const carrier = findCarrier(carriers, query);
    if (!carrier) throw new Error(`缺少目标运营商配置: ${query}`);
    return { query, carrier };
  });

  let cachedAgents;
  async function loadAgents() {
    if (cachedAgents) return cachedAgents;
    const agents = [];
    const seen = new Set();
    for (let pageNum = 1; pageNum <= 10000; pageNum += 1) {
      const response = await api("GET", "/system/agent/list", null, { pageNum, pageSize: 100 });
      assertOk("读取代理列表", response);
      const rows = response.rows || response.data?.rows;
      const total = Number(response.total ?? response.data?.total);
      if (!Array.isArray(rows) || !Number.isInteger(total) || total < 0) {
        throw new Error("Agent pagination metadata missing");
      }
      for (const row of rows) {
        const proxyUserId = String(row.id || row.proxyUserId || row.userId || "");
        if (!proxyUserId || seen.has(proxyUserId)) throw new Error("Invalid or duplicate agent pagination row");
        seen.add(proxyUserId);
        agents.push({
          proxyUserId,
          proxyUserName: String(row.userName || row.name || row.proxyUserName || ""),
          nickName: String(row.nickName || ""),
        });
      }
      if (agents.length === total) return (cachedAgents = agents);
      if (agents.length > total || rows.length === 0) throw new Error("Agent pagination incomplete");
    }
    throw new Error("Agent pagination limit exceeded");
  }

  if (mode === "query-agents-matching") {
    const keywords = arg1.split(",").map((value) => norm(value)).filter(Boolean);
    const exclusions = new Set(arg2.split(",").map((value) => norm(value)).filter(Boolean));
    const users = (await loadAgents()).filter((row) => {
      const names = [row.proxyUserName, row.nickName].map((value) => norm(value));
      return names.some((name) => keywords.some((keyword) => name.includes(keyword))) &&
        !names.some((name) => exclusions.has(name));
    });
    console.log(JSON.stringify({
      ok: true,
      mode,
      keywords: arg1.split(",").map((value) => value.trim()).filter(Boolean),
      exclusions: arg2.split(",").map((value) => value.trim()).filter(Boolean),
      count: users.length,
      users,
    }, null, 2));
    cdp.close();
    process.exit(0);
  }

  async function findAgent(name) {
    const agents = await loadAgents();
    if (process.env.OPENSMS_STRICT_MATCH === "1") {
      const matches = agents.filter(row => String(row.proxyUserName).trim().toLowerCase() === String(name).trim().toLowerCase());
      if (matches.length !== 1) throw new Error(`Account must match exactly and uniquely: ${name}`);
      return matches[0];
    }
    const exact = agents.find((row) => [row.proxyUserName, row.nickName].some((value) => norm(value) === norm(name)));
    const partial = agents.filter((row) => [row.proxyUserName, row.nickName].some((value) => norm(value).includes(norm(name))));
    if (exact) return exact;
    if (partial.length === 1) return partial[0];
    if (partial.length > 1) throw new Error(`账号名称不唯一: ${name} -> ${partial.map((row) => row.proxyUserName).join(", ")}`);
    throw new Error(`未找到账号: ${name}`);
  }

  async function bindingsForUser(user) {
    const response = await api("GET", `/proxycarrier/listByProxyUserId/${user.proxyUserId}`, null, { pageNum: 1, pageSize: 1000 });
    assertOk(`读取 ${user.proxyUserName}`, response);
    return (response.rows || []).map(compactBinding);
  }

  if (mode === "add-accounts-identif") {
    const identif = String(arg2).trim();
    const users = [];
    for (const name of arg1.split(",").map((value) => value.trim()).filter(Boolean)) users.push(await findAgent(name));
    const results = [];
    const failures = [];
    for (const user of users) {
      try {
        const before = await bindingsForUser(user);
        const existing = before.find((row) => row.carrireIdentif === identif);
        let added = false;
        if (!existing) {
          const response = await api("POST", "/proxycarrier", {
            carrireIdentif: identif,
            proxyUserId: Number(user.proxyUserId),
            smsCost: 0,
            smsRetail: 0,
            mmsCost: 0,
            mmsRetail: 0,
            monthlyCost: 0,
            remark: "",
          });
          assertOk(`新增 ${user.proxyUserName}/${identif}`, response);
          added = true;
        }
        const after = await bindingsForUser(user);
        const finalBinding = after.find((row) => row.carrireIdentif === identif);
        if (!finalBinding) throw new Error(`新增后未找到目标运营商标识: ${identif}`);
        results.push({ ...user, ok: true, added, binding: finalBinding });
      } catch (error) {
        failures.push({ ...user, error: error.message });
      }
    }
    const result = {
      ok: failures.length === 0,
      mode,
      protocol: "raw-cdp",
      startedAtBeijing,
      finishedAtBeijing: beijingNow(),
      browserId,
      port,
      pageTargetId: target.id,
      refreshCount,
      identif,
      totalAgents: users.length,
      changedCount: results.filter((row) => row.added).length,
      addCount: results.filter((row) => row.added).length,
      alreadyCount: results.filter((row) => !row.added).length,
      results,
      failures,
    };
    const outputPath = path.join(folder, `opensms_add_accounts_identif_${new Date().toISOString().replace(/[:.]/g, "-")}.json`);
    await fs.writeFile(outputPath, JSON.stringify(result, null, 2), "utf8");
    console.log(JSON.stringify({ outputPath, ...result }, null, 2));
    cdp.close();
    process.exit(result.ok ? 0 : 1);
  }

  if (mode === "only-accounts-identifs") {
    const requestedNames = arg1.split(",").map((value) => value.trim()).filter(Boolean);
    const desiredList = [...new Set(arg2.split(",").map((value) => value.trim()).filter(Boolean))];
    if (!desiredList.length) throw new Error("目标运营商标识为空");
    const desiredIdents = new Set(desiredList);
    const agents = await loadAgents();
    const users = requestedNames.map((name) => {
      const matches = agents.filter((row) => [row.proxyUserName, row.nickName].some((value) => norm(value) === norm(name)));
      if (matches.length !== 1) throw new Error(`账号精确匹配失败: ${name} -> ${matches.map((row) => row.proxyUserName).join(", ")}`);
      return matches[0];
    });
    const results = [];
    const failures = [];
    for (const user of users) {
      try {
        const before = await bindingsForUser(user);
        const added = [];
        const deleted = [];
        const existingIdents = new Set(before.map((row) => row.carrireIdentif));
        for (const identif of desiredList) {
          if (existingIdents.has(identif)) continue;
          const response = await api("POST", "/proxycarrier", {
            carrireIdentif: identif,
            proxyUserId: Number(user.proxyUserId),
            smsCost: 0,
            smsRetail: 0,
            mmsCost: 0,
            mmsRetail: 0,
            monthlyCost: 0,
            remark: "",
          });
          assertOk(`新增 ${user.proxyUserName}/${identif}`, response);
          added.push(identif);
          existingIdents.add(identif);
        }

        const afterAdd = await bindingsForUser(user);
        const kept = new Set();
        for (const row of afterAdd) {
          const identif = row.carrireIdentif;
          if (desiredIdents.has(identif) && !kept.has(identif)) {
            kept.add(identif);
            continue;
          }
          const response = await api("DELETE", `/proxycarrier/${row.id}`);
          assertOk(`删除 ${user.proxyUserName}/${row.carrierName || identif}`, response);
          deleted.push({ id: row.id, carrierName: row.carrierName, carrireIdentif: identif });
        }

        const finalRows = await bindingsForUser(user);
        const finalIdents = finalRows.map((row) => row.carrireIdentif);
        const missing = desiredList.filter((identif) => !finalIdents.includes(identif));
        const extra = finalRows.filter((row) => !desiredIdents.has(row.carrireIdentif));
        const duplicates = desiredList.filter((identif) => finalIdents.filter((value) => value === identif).length !== 1);
        if (missing.length || extra.length || duplicates.length) {
          throw new Error(`复核失败 missing=${missing.join(",")} extra=${extra.map((row) => row.carrierName).join(",")} duplicates=${duplicates.join(",")}`);
        }
        results.push({
          ...user,
          ok: true,
          before: before.map((row) => row.carrierName || row.carrireIdentif),
          after: finalRows.map((row) => row.carrierName || row.carrireIdentif),
          added,
          deleted,
        });
      } catch (error) {
        failures.push({ ...user, error: error.message });
      }
    }
    const result = {
      ok: failures.length === 0,
      mode,
      protocol: "raw-cdp",
      startedAtBeijing,
      finishedAtBeijing: beijingNow(),
      browserId,
      port,
      pageTargetId: target.id,
      refreshCount,
      targetIdents: desiredList,
      totalAgents: users.length,
      changedCount: results.filter((row) => row.added.length || row.deleted.length).length,
      addCount: results.reduce((sum, row) => sum + row.added.length, 0),
      deleteCount: results.reduce((sum, row) => sum + row.deleted.length, 0),
      results,
      failures,
    };
    const outputPath = path.join(folder, `opensms_only_accounts_identifs_${new Date().toISOString().replace(/[:.]/g, "-")}.json`);
    await fs.writeFile(outputPath, JSON.stringify(result, null, 2), "utf8");
    console.log(JSON.stringify({ outputPath, ...result }, null, 2));
    cdp.close();
    process.exit(result.ok ? 0 : 1);
  }

  if (mode === "remove-binding-ids") {
    const recordPath = path.resolve(arg1);
    const record = JSON.parse(await fs.readFile(recordPath, "utf8"));
    const requestedBindings = Array.isArray(record.bindings) ? record.bindings : [];
    if (!requestedBindings.length) throw new Error(`取消记录没有绑定 ID: ${recordPath}`);

    const results = [];
    const failures = [];
    for (const requested of requestedBindings) {
      const user = {
        proxyUserId: String(requested.proxyUserId || ""),
        proxyUserName: String(requested.proxyUserName || requested.proxyUserId || ""),
      };
      const requestedId = String(requested.id || "");
      const expectedIdentif = String(requested.carrireIdentif || record.target?.identif || "");
      if (!user.proxyUserId || !requestedId || !expectedIdentif) {
        failures.push({ ...user, id: requestedId, error: "取消记录字段不完整" });
        continue;
      }

      try {
        const beforeRows = await bindingsForUser(user);
        const current = beforeRows.find((row) => row.id === requestedId);
        if (!current) {
          results.push({ ...user, id: requestedId, ok: true, alreadyGone: true, deleted: false });
          continue;
        }
        if (current.carrireIdentif !== expectedIdentif) {
          throw new Error(`配置 ID 的运营商不匹配: ${current.carrierName}/${current.carrireIdentif}`);
        }

        const response = await api("DELETE", `/proxycarrier/${requestedId}`);
        assertOk(`删除 ${user.proxyUserName}/${current.carrierName}`, response);
        const finalRows = await bindingsForUser(user);
        const remaining = finalRows.some((row) => row.id === requestedId);
        if (remaining) throw new Error(`配置 ID 删除后仍存在: ${requestedId}`);
        results.push({
          ...user,
          id: requestedId,
          carrierName: current.carrierName,
          ok: true,
          alreadyGone: false,
          deleted: true,
        });
      } catch (error) {
        failures.push({ ...user, id: requestedId, error: error.message });
      }
    }

    const result = {
      ok: failures.length === 0,
      mode,
      protocol: "raw-cdp",
      startedAtBeijing,
      finishedAtBeijing: beijingNow(),
      browserId,
      port,
      pageTargetId: target.id,
      refreshCount,
      recordPath,
      requestedCount: requestedBindings.length,
      deletedCount: results.filter((row) => row.deleted).length,
      alreadyGoneCount: results.filter((row) => row.alreadyGone).length,
      results,
      failures,
    };
    const outputPath = path.join(folder, `opensms_remove_binding_ids_${new Date().toISOString().replace(/[:.]/g, "-")}.json`);
    await fs.writeFile(outputPath, JSON.stringify(result, null, 2), "utf8");
    console.log(JSON.stringify({ outputPath, ...result }, null, 2));
    cdp.close();
    process.exit(result.ok ? 0 : 1);
  }

  if (mode === "query-account-bindings") {
    const user = await findAgent(arg1);
    const bindings = await bindingsForUser(user);
    console.log(JSON.stringify({ ok: true, mode, user, count: bindings.length, bindings }, null, 2));
    cdp.close();
    process.exit(0);
  }

  if (mode === "remove-sources-matching-excluding") {
    const sourceCarriers = arg1.split(",").map((query) => query.trim()).filter(Boolean).map((query) => {
      const carrier = findCarrier(carriers, query);
      if (!carrier) throw new Error(`缺少来源运营商配置: ${query}`);
      return { query, carrier };
    });
    const sourceIdents = new Set(sourceCarriers.map(({ carrier }) => String(carrier.identif)));
    const keywords = arg2.split(",").map((value) => norm(value)).filter(Boolean);
    const exclusions = arg3.split(",").map((value) => norm(value)).filter(Boolean);
    const agents = await loadAgents();
    const users = agents.filter((row) => {
      const names = [row.proxyUserName, row.nickName].map((value) => norm(value));
      const matched = names.some((name) => keywords.some((keyword) => name.includes(keyword)));
      const excluded = names.some((name) => exclusions.includes(name));
      return matched && !excluded;
    });

    const results = [];
    const failures = [];
    for (const user of users) {
      try {
        const beforeRows = await bindingsForUser(user);
        const deleted = [];
        for (const row of beforeRows.filter((item) => sourceIdents.has(String(item.carrireIdentif || "")))) {
          const response = await api("DELETE", `/proxycarrier/${row.id}`);
          assertOk(`删除 ${user.proxyUserName}/${row.carrierName}`, response);
          deleted.push({ id: row.id, carrierName: row.carrierName, carrireIdentif: row.carrireIdentif });
        }
        const finalRows = await bindingsForUser(user);
        const remaining = finalRows.filter((row) => sourceIdents.has(String(row.carrireIdentif || "")));
        if (remaining.length) throw new Error(`目标运营商仍有残留: ${remaining.map((row) => row.carrierName).join(",")}`);
        results.push({ ...user, ok: true, before: beforeRows.map((row) => row.carrierName), after: finalRows.map((row) => row.carrierName), deleted });
      } catch (error) {
        failures.push({ ...user, error: error.message });
      }
    }

    const remainingMatches = [];
    for (const { carrier } of sourceCarriers) {
      const response = await api("GET", "/proxycarrier/list", null, {
        pageNum: 1,
        pageSize: 50000,
        carrireIdentif: String(carrier.identif),
      });
      assertOk(`复核 ${carrier.name} 关键词绑定`, response);
      for (const row of response.rows || []) {
        const name = norm(row.proxyUserName);
        if (keywords.some((keyword) => name.includes(keyword)) && !exclusions.includes(name)) {
          remainingMatches.push(compactBinding(row));
        }
      }
    }
    const deletedByCarrier = Object.fromEntries(sourceCarriers.map(({ carrier }) => [String(carrier.name), results.reduce((sum, row) => sum + row.deleted.filter((item) => String(item.carrireIdentif) === String(carrier.identif)).length, 0)]));
    const result = {
      ok: failures.length === 0 && remainingMatches.length === 0,
      mode,
      protocol: "raw-cdp",
      startedAtBeijing,
      finishedAtBeijing: beijingNow(),
      browserId,
      port,
      pageTargetId: target.id,
      refreshCount,
      sources: sourceCarriers.map(({ query, carrier }) => ({ query, name: carrier.name, identif: String(carrier.identif) })),
      keywords: arg2.split(",").map((value) => value.trim()).filter(Boolean),
      exclusions: arg3.split(",").map((value) => value.trim()).filter(Boolean),
      matchedAccountCount: users.length,
      changedCount: results.filter((row) => row.deleted.length).length,
      deletedCount: results.reduce((sum, row) => sum + row.deleted.length, 0),
      deletedByCarrier,
      remainingMatchCount: remainingMatches.length,
      results,
      failures,
      remainingMatches,
    };
    const outputPath = path.join(folder, `opensms_raw_cdp_binding_${new Date().toISOString().replace(/[:.]/g, "-")}.json`);
    await fs.writeFile(outputPath, JSON.stringify(result, null, 2), "utf8");
    console.log(JSON.stringify({ outputPath, ...result }, null, 2));
    cdp.close();
    process.exit(result.ok ? 0 : 1);
  }

  let users;
  let sourceCarrier = null;
  let unionSources = [];
  let unionSourceBindingCounts = {};
  if (mode === "only-source" || mode === "add-source" || mode === "remove-source" || mode === "remove-source-matching") {
    sourceCarrier = findCarrier(carriers, arg1);
    if (!sourceCarrier) throw new Error(`缺少来源运营商配置: ${arg1}`);
    const sourceBindings = await readCarrierBindings(sourceCarrier.identif);
    users = [...new Map(sourceBindings.map((row) => [String(row.proxyUserId), {
      proxyUserId: String(row.proxyUserId || ""),
      proxyUserName: String(row.proxyUserName || ""),
    }])).values()].filter((row) => row.proxyUserId);
    if (mode === "remove-source-matching") {
      const keywords = arg2.split(",").map((value) => norm(value)).filter(Boolean);
      users = users.filter((row) => keywords.some((keyword) => norm(row.proxyUserName).includes(keyword)));
    }
  } else if (mode === "add-sources-union") {
    const userMap = new Map();
    unionSources = arg1.split(",").map((query) => query.trim()).filter(Boolean).map((query) => {
      const carrier = findCarrier(carriers, query) || (/^\d+$/.test(query) ? { name: query, identif: query } : null);
      if (!carrier) throw new Error(`缺少来源运营商配置: ${query}`);
      return { query, name: String(carrier.name), identif: String(carrier.identif) };
    });
    for (const source of unionSources) {
      const sourceRows = await readCarrierBindings(source.identif);
      if (source.name === source.identif && sourceRows[0]?.carrierName) {
        source.name = String(sourceRows[0].carrierName);
      }
      const sourceUsers = [...new Map(sourceRows.map((row) => [String(row.proxyUserId), {
        proxyUserId: String(row.proxyUserId || ""),
        proxyUserName: String(row.proxyUserName || ""),
      }])).values()].filter((row) => row.proxyUserId);
      unionSourceBindingCounts[source.name] = sourceUsers.length;
      for (const user of sourceUsers) userMap.set(user.proxyUserId, user);
    }
    users = [...userMap.values()];
  } else if (mode === "add-prefix") {
    const prefixes = arg1.split(",").map((value) => norm(value)).filter(Boolean);
    const agents = await loadAgents();
    users = [...new Map(agents
      .filter((row) => [row.proxyUserName, row.nickName].some((value) =>
        prefixes.some((prefix) => norm(value).startsWith(prefix))
      ))
      .map((row) => [row.proxyUserId, row])).values()];
    if (!users.length) throw new Error(`未找到账号前缀: ${arg1}`);
  } else if (mode === "add-all" || mode === "add-all-excluding") {
    users = await loadAgents();
    if (mode === "add-all-excluding") {
      const excludedKeywords = arg1.split(",").map((value) => norm(value)).filter(Boolean);
      users = users.filter((row) => ![row.proxyUserName, row.nickName].some((value) =>
        excludedKeywords.some((keyword) => norm(value).includes(keyword))
      ));
    }
  } else {
    users = [];
    for (const name of arg1.split(",").map((value) => value.trim()).filter(Boolean)) users.push(await findAgent(name));
  }

  if (mode === "add-all" || mode === "add-all-excluding" || mode === "add-sources-union") {
    const existingByIdent = new Map();
    for (const { carrier } of targetCarriers) {
      const rows = await readCarrierBindings(carrier.identif);
      existingByIdent.set(String(carrier.identif), new Set(rows.map((row) => String(row.proxyUserId || ""))));
    }

    const results = [];
    const failures = [];
    for (let index = 0; index < users.length; index += 1) {
      const user = users[index];
      const added = [];
      const already = [];
      try {
        for (const { carrier } of targetCarriers) {
          const ident = String(carrier.identif);
          const existing = existingByIdent.get(ident);
          if (existing.has(user.proxyUserId)) {
            already.push(String(carrier.name));
            continue;
          }
          const response = await api("POST", "/proxycarrier", {
            carrireIdentif: ident,
            proxyUserId: Number(user.proxyUserId),
            smsCost: 0,
            smsRetail: 0,
            mmsCost: 0,
            mmsRetail: 0,
            monthlyCost: 0,
            remark: "",
          });
          assertOk(`新增 ${user.proxyUserName || user.proxyUserId}/${carrier.name}`, response);
          existing.add(user.proxyUserId);
          added.push(String(carrier.name));
        }
        results.push({ ...user, ok: true, added, already });
      } catch (error) {
        failures.push({ ...user, error: error.message });
      }
      if ((index + 1) % 10 === 0) console.error(JSON.stringify({ processed: index + 1, total: users.length, added: results.reduce((n, r) => n + r.added.length, 0), failures: failures.length }));
    }

    const missingAfter = [];
    const finalBindingsByIdent = new Map();
    for (const { carrier } of targetCarriers) {
      const finalRows = await readCarrierBindings(carrier.identif);
      finalBindingsByIdent.set(String(carrier.identif), finalRows);
      const finalIds = new Set(finalRows.map((row) => String(row.proxyUserId || "")));
      for (const user of users) {
        if (!finalIds.has(user.proxyUserId)) missingAfter.push({ ...user, carrierName: String(carrier.name) });
      }
    }

    for (const row of results) {
      row.createdBindings = targetCarriers.flatMap(({ carrier }) => {
        if (!row.added.includes(String(carrier.name))) return [];
        const binding = (finalBindingsByIdent.get(String(carrier.identif)) || [])
          .find((item) => item.proxyUserId === row.proxyUserId);
        return binding ? [binding] : [];
      });
    }
    const createdBindings = results.flatMap((row) => row.createdBindings);

    const result = {
      ok: failures.length === 0 && missingAfter.length === 0,
      mode,
      protocol: "raw-cdp",
      startedAtBeijing,
      finishedAtBeijing: beijingNow(),
      browserId,
      port,
      pageTargetId: target.id,
      refreshCount,
      totalAgents: users.length,
      sources: mode === "add-sources-union" ? unionSources : undefined,
      silentBrowserMode: process.env.OPENSMS_SILENT_BROWSER === "1",
      browserForegroundActivationAttempted: false,
      deleteCount: 0,
      sourceBindingCounts: mode === "add-sources-union" ? unionSourceBindingCounts : undefined,
      excludedKeywords: mode === "add-all-excluding" ? arg1.split(",").map((value) => value.trim()).filter(Boolean) : undefined,
      targets: targetCarriers.map(({ query, carrier }) => ({ query, name: carrier.name, identif: String(carrier.identif) })),
      changedCount: results.filter((row) => row.added.length).length,
      addCount: results.reduce((sum, row) => sum + row.added.length, 0),
      alreadyCount: results.reduce((sum, row) => sum + row.already.length, 0),
      createdBindings,
      missingAfterCount: missingAfter.length,
      results,
      failures,
      missingAfter,
    };
    const outputPath = path.join(folder, `opensms_raw_cdp_binding_${new Date().toISOString().replace(/[:.]/g, "-")}.json`);
    await fs.writeFile(outputPath, JSON.stringify(result, null, 2), "utf8");
    console.log(JSON.stringify({ outputPath, ...result }, null, 2));
    cdp.close();
    process.exit(result.ok ? 0 : 1);
  }

  if (mode === "remove-source-matching") {
    const sourceIdent = String(sourceCarrier.identif);
    const keywords = arg2.split(",").map((value) => norm(value)).filter(Boolean);
    const results = [];
    const failures = [];
    for (const user of users) {
      try {
        const beforeRows = await bindingsForUser(user);
        const deleted = [];
        for (const row of beforeRows.filter((item) => String(item.carrireIdentif || "") === sourceIdent)) {
          const response = await api("DELETE", `/proxycarrier/${row.id}`);
          assertOk(`删除 ${user.proxyUserName}/${sourceCarrier.name}`, response);
          deleted.push({ id: row.id, carrierName: row.carrierName || sourceCarrier.name });
        }
        const finalRows = await bindingsForUser(user);
        const remaining = finalRows.filter((row) => String(row.carrireIdentif || "") === sourceIdent);
        const ok = remaining.length === 0;
        if (!ok) failures.push({ ...user, remaining: remaining.map((row) => row.id) });
        results.push({
          ...user,
          ok,
          before: beforeRows.map((row) => row.carrierName || row.carrireIdentif),
          after: finalRows.map((row) => row.carrierName || row.carrireIdentif),
          deleted,
        });
      } catch (error) {
        failures.push({ ...user, error: error.message });
      }
    }

    const verification = await api("GET", "/proxycarrier/list", null, {
      pageNum: 1,
      pageSize: 50000,
      carrireIdentif: sourceIdent,
    });
    assertOk(`复核 ${sourceCarrier.name} 关键词绑定`, verification);
    const remainingMatches = (verification.rows || []).filter((row) =>
      keywords.some((keyword) => norm(row.proxyUserName).includes(keyword))
    ).map(compactBinding);
    const result = {
      ok: failures.length === 0 && remainingMatches.length === 0,
      mode,
      protocol: "raw-cdp",
      startedAtBeijing,
      finishedAtBeijing: beijingNow(),
      browserId,
      port,
      pageTargetId: target.id,
      refreshCount,
      source: { query: arg1, name: sourceCarrier.name, identif: sourceIdent },
      keywords: arg2.split(",").map((value) => value.trim()).filter(Boolean),
      matchedCount: users.length,
      deletedCount: results.reduce((sum, row) => sum + row.deleted.length, 0),
      remainingMatchCount: remainingMatches.length,
      userNames: users.map((row) => row.proxyUserName),
      results,
      failures,
      remainingMatches,
    };
    const outputPath = path.join(folder, `opensms_raw_cdp_binding_${new Date().toISOString().replace(/[:.]/g, "-")}.json`);
    await fs.writeFile(outputPath, JSON.stringify(result, null, 2), "utf8");
    console.log(JSON.stringify({ outputPath, ...result }, null, 2));
    cdp.close();
    process.exit(result.ok ? 0 : 1);
  }

  const desiredIdents = new Set(targetCarriers.map(({ carrier }) => String(carrier.identif)));
  const results = [];
  const failures = [];
  for (const user of users) {
    try {
      const beforeRows = await bindingsForUser(user);
      const beforeIds = new Set(beforeRows.map((row) => String(row.id || "")));
      const kept = new Set();
      const deleted = [];
      const added = [];

      if (mode === "remove-source") {
        const sourceIdent = String(sourceCarrier.identif);
        for (const row of beforeRows.filter((item) => String(item.carrireIdentif || "") === sourceIdent)) {
          const response = await api("DELETE", `/proxycarrier/${row.id}`);
          assertOk(`删除 ${user.proxyUserName}/${sourceCarrier.name}`, response);
          deleted.push(row.carrierName || sourceCarrier.name);
        }
        const finalRows = await bindingsForUser(user);
        const sourceRemaining = finalRows.filter((row) => String(row.carrireIdentif || "") === sourceIdent);
        const ok = sourceRemaining.length === 0;
        if (!ok) failures.push({ ...user, sourceRemaining: sourceRemaining.map((row) => row.carrierName || row.carrireIdentif) });
        results.push({
          ...user,
          ok,
          before: beforeRows.map((row) => row.carrierName || row.carrireIdentif),
          after: finalRows.map((row) => row.carrierName || row.carrireIdentif),
          added,
          deleted,
          targetMissing: [],
          extra: [],
        });
        continue;
      }

      for (const row of beforeRows) {
        const ident = String(row.carrireIdentif || "");
        if (!mode.startsWith("only-") || (desiredIdents.has(ident) && !kept.has(ident))) {
          if (ident) kept.add(ident);
          continue;
        }
        const response = await api("DELETE", `/proxycarrier/${row.id}`);
        assertOk(`删除 ${user.proxyUserName}/${row.carrierName || ident}`, response);
        deleted.push(row.carrierName || ident);
      }

      for (const { carrier } of targetCarriers) {
        const ident = String(carrier.identif);
        if (kept.has(ident)) continue;
        const response = await api("POST", "/proxycarrier", {
          carrireIdentif: ident,
          proxyUserId: Number(user.proxyUserId),
          smsCost: 0,
          smsRetail: 0,
          mmsCost: 0,
          mmsRetail: 0,
          monthlyCost: 0,
          remark: "",
        });
        assertOk(`新增 ${user.proxyUserName}/${carrier.name}`, response);
        kept.add(ident);
        added.push(String(carrier.name));
      }

      const finalRows = await bindingsForUser(user);
      const createdBindings = finalRows.filter((row) =>
        !beforeIds.has(String(row.id || "")) && desiredIdents.has(String(row.carrireIdentif || ""))
      ).map((row) => ({
        id: String(row.id || ""),
        proxyUserId: user.proxyUserId,
        proxyUserName: user.proxyUserName,
        carrierName: row.carrierName,
        carrireIdentif: String(row.carrireIdentif || ""),
      }));
      const targetMissing = targetCarriers.filter(({ carrier }) =>
        !finalRows.some((row) => String(row.carrireIdentif) === String(carrier.identif))
      ).map(({ carrier }) => carrier.name);
      const extra = mode.startsWith("only-")
        ? finalRows.filter((row) => !desiredIdents.has(String(row.carrireIdentif))).map((row) => row.carrierName || row.carrireIdentif)
        : [];
      const ok = targetMissing.length === 0 && extra.length === 0;
      if (!ok) failures.push({ ...user, targetMissing, extra });
      results.push({
        ...user,
        ok,
        before: beforeRows.map((row) => row.carrierName || row.carrireIdentif),
        after: finalRows.map((row) => row.carrierName || row.carrireIdentif),
        added,
        deleted,
        createdBindings,
        targetMissing,
        extra,
      });
    } catch (error) {
      failures.push({ ...user, error: error.message });
    }
  }

  let remainingSourceCount;
  if (mode === "remove-source") {
    const verification = await api("GET", "/proxycarrier/list", null, {
      pageNum: 1,
      pageSize: 20000,
      carrireIdentif: String(sourceCarrier.identif),
    });
    assertOk(`复核 ${sourceCarrier.name} 绑定`, verification);
    remainingSourceCount = (verification.rows || []).length;
  }

  const result = {
    ok: failures.length === 0 && (mode !== "remove-source" || remainingSourceCount === 0),
    mode,
    protocol: "raw-cdp",
    startedAtBeijing,
    finishedAtBeijing: beijingNow(),
    browserId,
    port,
    pageTargetId: target.id,
    refreshCount,
    source: sourceCarrier ? {
      query: arg1,
      name: sourceCarrier.name,
      identif: String(sourceCarrier.identif),
      count: users.length,
    } : null,
    requestedName: mode.endsWith("-accounts") ? arg1 : undefined,
    requestedPrefix: mode === "add-prefix" ? arg1 : undefined,
    requestedAll: mode === "add-all" ? true : undefined,
    targets: targetCarriers.map(({ query, carrier }) => ({
      query,
      name: carrier.name,
      identif: String(carrier.identif),
    })),
    userNames: users.map((row) => row.proxyUserName),
    changedCount: results.filter((row) => row.added.length || row.deleted.length).length,
    addCount: results.reduce((sum, row) => sum + row.added.length, 0),
    deleteCount: results.reduce((sum, row) => sum + row.deleted.length, 0),
    remainingSourceCount,
    results,
    failures,
  };
  const outputPath = path.join(folder, `opensms_raw_cdp_binding_${new Date().toISOString().replace(/[:.]/g, "-")}.json`);
  await fs.writeFile(outputPath, JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify({ outputPath, ...result }, null, 2));
  if (!result.ok) process.exitCode = 1;
} finally {
  cdp.close();
  try {await background.close();} finally {sharedBrowser.release();}
}
