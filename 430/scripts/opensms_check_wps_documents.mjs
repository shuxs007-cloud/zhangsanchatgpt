#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { basename, dirname, resolve } from "node:path";
import { mkdirSync, writeFileSync } from "node:fs";

function usage() {
  console.error("usage: opensms_check_wps_documents.mjs <source.xlsx> <result.xlsx> <receipt.json>");
  process.exit(2);
}

if (process.argv.length !== 5) usage();

const sourceDocument = resolve(process.argv[2]);
const resultDocument = resolve(process.argv[3]);
const receiptPath = resolve(process.argv[4]);
const targets = new Map([
  [basename(sourceDocument), sourceDocument],
  [basename(resultDocument), resultDocument],
]);

let windowNames = [];
let checkError = null;
try {
  const output = execFileSync(
    "osascript",
    ["-e", 'tell application "System Events" to tell process "wpsoffice" to get name of every window'],
    { encoding: "utf8" },
  ).trim();
  if (output) windowNames = output.split(/,\s*/).map((value) => value.trim()).filter(Boolean);
} catch (error) {
  checkError = String(error.stderr || error.message || error).trim();
}

const currentOpen = windowNames.filter((name) => targets.has(name));
const receipt = {
  checkedAt: new Date().toISOString(),
  sourceDocument,
  resultDocument,
  sourceOpenInWps: currentOpen.includes(basename(sourceDocument)),
  resultOpenInWps: currentOpen.includes(basename(resultDocument)),
  currentOpenDocuments: currentOpen,
  unrelatedOpenDocuments: windowNames.filter((name) => !targets.has(name)),
  closedDocuments: [],
  wpsExited: false,
  unrelatedDocumentsTouched: false,
  checkError,
};

mkdirSync(dirname(receiptPath), { recursive: true });
writeFileSync(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, { flag: "wx" });
console.log(JSON.stringify({ path: receiptPath, ...receipt }, null, 2));
