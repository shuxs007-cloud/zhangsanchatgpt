import fs from 'node:fs/promises';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { FileBlob, SpreadsheetFile } from '@oai/artifact-tool';

const [inputPath, liveReceiptPath, outputPath, artifactReceiptPath, verifyReceiptPath, refreshReceiptPath, readbackReceiptPath] = process.argv.slice(2);
if (!readbackReceiptPath) throw new Error('usage: node script input live output artifact verify refresh readback');
const text = value => value == null ? '' : String(value).trim();

function columnLetter(index) {
  let value = index + 1;
  let result = '';
  while (value > 0) {
    value -= 1;
    result = String.fromCharCode(65 + (value % 26)) + result;
    value = Math.floor(value / 26);
  }
  return result;
}

const xmlUnescape = value => String(value).replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const rxEscape = value => String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function unzipText(bookPath, member) {
  return execFileSync('/usr/bin/unzip', ['-p', bookPath, member], {
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
  });
}

function sheetTargets(bookPath) {
  const workbookXml = unzipText(bookPath, 'xl/workbook.xml');
  const relsXml = unzipText(bookPath, 'xl/_rels/workbook.xml.rels');
  const rels = new Map();
  for (const match of relsXml.matchAll(/<Relationship\b([^>]*)\/?\s*>/g)) {
    const attrs = match[1];
    const id = attrs.match(/\bId="([^"]+)"/)?.[1];
    const target = attrs.match(/\bTarget="([^"]+)"/)?.[1];
    if (id && target) rels.set(id, target.replace(/^\//, ''));
  }
  const result = new Map();
  for (const match of workbookXml.matchAll(/<(?:[A-Za-z0-9_]+:)?sheet\b([^>]*)\/?\s*>/g)) {
    const attrs = match[1];
    const name = xmlUnescape(attrs.match(/\bname="([^"]+)"/)?.[1] || '');
    const id = attrs.match(/\br:id="([^"]+)"/)?.[1];
    const target = rels.get(id);
    if (!name || !target) continue;
    result.set(name, target.startsWith('xl/') ? target : `xl/${target.replace(/^\.\//, '')}`);
  }
  return result;
}

function styleIdMap(bookPath, targetCellsBySheet) {
  const targets = sheetTargets(bookPath);
  const result = new Map();
  for (const [sheetName, cells] of targetCellsBySheet) {
    const member = targets.get(sheetName);
    if (!member) throw new Error(`OOXML缺少工作表映射: ${sheetName}`);
    const xml = unzipText(bookPath, member);
    for (const cell of cells) {
      const tag = xml.match(new RegExp(`<c\\b[^>]*\\br="${rxEscape(cell)}"[^>]*>`, 'i'))?.[0] || '';
      const styleId = tag.match(/\bs="([^"]+)"/)?.[1] || '0';
      result.set(`${sheetName}!${cell}`, styleId);
    }
  }
  return result;
}

const liveReceipt = JSON.parse(await fs.readFile(liveReceiptPath, 'utf8'));
if (!liveReceipt.passed) throw new Error('最终后台运营商回读未通过，拒绝刷新文档');
const liveRows = liveReceipt.rows || [];
const invalidLiveRows = liveRows.filter(row => !text(row.phone)
  || (!text(row.liveCarrierName) && !row.liveCarrierUnassigned));
if (invalidLiveRows.length) throw new Error('最终后台运营商回读存在未声明的空号码/空名称');
const liveByPhone = new Map(liveRows.map(row => [text(row.phone), text(row.liveCarrierName)]));
const liveRowByPhone = new Map(liveRows.map(row => [text(row.phone), row]));

const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
const sheetNames = workbook.worksheets.items.map(sheet => sheet.name);
const mainSheetName = sheetNames[0];
const sourceValues = new Map();
const updates = [];
const found = new Set();
const allowed = new Set();
const targetCellsBySheet = new Map();
for (const sheet of workbook.worksheets.items) {
  const values = sheet.getUsedRange(true).values;
  sourceValues.set(sheet.name, values.map(row => [...row]));
  if (sheet.name !== mainSheetName || !values.length) continue;
  const headers = values[0].map(text);
  const phoneIndex = headers.indexOf('电话');
  const carrierIndex = headers.indexOf('运营商');
  if (phoneIndex < 0 || carrierIndex < 0) continue;
  for (let rowIndex = 1; rowIndex < values.length; rowIndex += 1) {
    const phone = text(values[rowIndex][phoneIndex]);
    if (!liveByPhone.has(phone)) continue;
    const liveCarrier = liveByPhone.get(phone);
    found.add(phone);
    const priorCarrier = text(values[rowIndex][carrierIndex]);
    const cell = `${columnLetter(carrierIndex)}${rowIndex + 1}`;
    const key = `${sheet.name}!${cell}`;
    allowed.add(key);
    if (!targetCellsBySheet.has(sheet.name)) targetCellsBySheet.set(sheet.name, []);
    targetCellsBySheet.get(sheet.name).push(cell);
    if (priorCarrier !== liveCarrier) sheet.getRange(cell).values = [[liveCarrier]];
    updates.push({
      sheet: sheet.name,
      row: rowIndex + 1,
      cell,
      phone,
      beforeCarrier: priorCarrier,
      afterCarrier: liveCarrier,
      changed: priorCarrier !== liveCarrier,
    });
  }
}

const unmatchedPhones = [...liveByPhone.keys()].filter(phone => !found.has(phone));
const supplementalSheetName = '后台更新未在源表';
if (unmatchedPhones.length) {
  if (sheetNames.includes(supplementalSheetName)) {
    throw new Error(`输入工作簿已存在保留工作表，拒绝覆盖: ${supplementalSheetName}`);
  }
  const supplemental = workbook.worksheets.add(supplementalSheetName);
  supplemental.showGridLines = false;
  supplemental.freezePanes.freezeRows(1);
  const headers = ['电话', '最新运营商', '预期运营商', '处理规则', '后台号码ID', '说明'];
  const supplementalRows = unmatchedPhones.sort().map(phone => {
    const row = liveRowByPhone.get(phone) || {};
    return [
      phone,
      text(row.liveCarrierName),
      text(row.expectedTarget),
      text(row.route),
      text(row.backendId),
      '本轮后台已更新并回读，但号码不在本轮10日报表源表中',
    ];
  });
  supplemental.getRange(`A1:F${supplementalRows.length + 1}`).values = [headers, ...supplementalRows];
  supplemental.getRange('A1:F1').format = {
    fill: '#1F4E78',
    font: { bold: true, color: '#FFFFFF' },
    horizontalAlignment: 'center',
  };
  supplemental.getRange(`A2:F${supplementalRows.length + 1}`).format = {
    font: { color: '#1F2937' },
    verticalAlignment: 'center',
  };
  supplemental.getRange(`A1:F${supplementalRows.length + 1}`).format.borders = {
    preset: 'inside', style: 'thin', color: '#D9E2F3',
  };
  supplemental.getRange(`A1:A${supplementalRows.length + 1}`).format.columnWidth = 16;
  supplemental.getRange(`B1:C${supplementalRows.length + 1}`).format.columnWidth = 24;
  supplemental.getRange(`D1:D${supplementalRows.length + 1}`).format.columnWidth = 42;
  supplemental.getRange(`E1:E${supplementalRows.length + 1}`).format.columnWidth = 22;
  supplemental.getRange(`F1:F${supplementalRows.length + 1}`).format.columnWidth = 56;
}
const sourceStyleIds = styleIdMap(inputPath, targetCellsBySheet);
const preExportFormulaErrors = await workbook.inspect({
  kind: 'match',
  searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A',
  options: { useRegex: true, maxResults: 300 },
  summary: 'carrier refresh formula error scan',
  maxChars: 5000,
});
await fs.mkdir(path.dirname(outputPath), { recursive: true });
await (await SpreadsheetFile.exportXlsx(workbook)).save(outputPath);

const finalBook = await SpreadsheetFile.importXlsx(await FileBlob.load(outputPath));
const finalSheetNames = finalBook.worksheets.items.map(sheet => sheet.name);
const expectedFinalSheetNames = unmatchedPhones.length ? [...sheetNames, supplementalSheetName] : sheetNames;
const nonCarrierValueMismatches = [];
const targetMismatches = [];
const dimensionMismatches = [];
for (const sheetName of sheetNames) {
  const before = sourceValues.get(sheetName) || [];
  const after = finalBook.worksheets.getItem(sheetName).getUsedRange(true).values;
  if (before.length !== after.length || Math.max(0, ...before.map(row => row.length)) !== Math.max(0, ...after.map(row => row.length))) {
    dimensionMismatches.push({ sheet: sheetName });
  }
  for (let row = 0; row < Math.max(before.length, after.length); row += 1) {
    for (let column = 0; column < Math.max(before[row]?.length || 0, after[row]?.length || 0); column += 1) {
      const cell = `${columnLetter(column)}${row + 1}`;
      const key = `${sheetName}!${cell}`;
      const beforeText = text(before[row]?.[column]);
      const afterText = text(after[row]?.[column]);
      if (allowed.has(key)) {
        const update = updates.find(item => item.sheet === sheetName && item.cell === cell);
        if (!update || afterText !== update.afterCarrier) {
          targetMismatches.push({ sheet: sheetName, cell, expected: update?.afterCarrier || '', actual: afterText });
        }
      } else if (beforeText !== afterText) {
        nonCarrierValueMismatches.push({ sheet: sheetName, cell, before: beforeText, after: afterText });
      }
    }
  }
}
const finalStyleIds = styleIdMap(outputPath, targetCellsBySheet);
const styleMismatches = [...sourceStyleIds]
  .filter(([key, value]) => finalStyleIds.get(key) !== value)
  .map(([key, before]) => ({ key, before, after: finalStyleIds.get(key) }));
const finalFormulaErrors = await finalBook.inspect({
  kind: 'match',
  searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A',
  options: { useRegex: true, maxResults: 300 },
  summary: 'reimported final workbook formula error scan',
  maxChars: 5000,
});
const formulaScanText = (finalFormulaErrors.ndjson || '').trim();
const formulaErrorCount = !formulaScanText || formulaScanText.includes('Cell search matched 0 entries.')
  ? 0
  : (formulaScanText.match(/\n/g)?.length || 0) + 1;
const supplementalMismatches = [];
if (unmatchedPhones.length) {
  const values = finalBook.worksheets.getItem(supplementalSheetName).getUsedRange(true).values;
  const supplementalByPhone = new Map(values.slice(1).map(row => [text(row[0]), text(row[1])]));
  for (const phone of unmatchedPhones) {
    if (supplementalByPhone.get(phone) !== liveByPhone.get(phone)) {
      supplementalMismatches.push({ phone, expected: liveByPhone.get(phone), actual: supplementalByPhone.get(phone) || '' });
    }
  }
}
const verifyPassed = JSON.stringify(expectedFinalSheetNames) === JSON.stringify(finalSheetNames)
  && !dimensionMismatches.length
  && !nonCarrierValueMismatches.length
  && !targetMismatches.length
  && !styleMismatches.length
  && !supplementalMismatches.length
  && formulaErrorCount === 0;
const common = { previewSkipped: true, previewSkipReason: 'OPENSMS巡检策略明确禁止生成或打开预览' };
const artifactReceipt = {
  createdAt: new Date().toISOString(), operation: 'post-write-carrier-refresh-artifact-tool',
  inputWorkbook: path.resolve(inputPath), liveCarrierReceipt: path.resolve(liveReceiptPath), outputWorkbook: path.resolve(outputPath),
  liveCandidateCount: liveByPhone.size, matchedPhoneCount: found.size, changedCellCount: updates.filter(row => row.changed).length,
  occurrenceCount: updates.length, unmatchedPhones, supplementalSheetName: unmatchedPhones.length ? supplementalSheetName : '',
  supplementalRowCount: unmatchedPhones.length, updates, preExportFormulaErrorScan: preExportFormulaErrors.ndjson,
  ...common, passed: verifyPassed,
};
const verifyReceipt = {
  checkedAt: new Date().toISOString(), mode: 'artifact-tool-reimport-plus-ooxml-style-verify',
  sourceWorkbook: path.resolve(inputPath), finalWorkbook: path.resolve(outputPath), sheetNames, expectedFinalSheetNames, finalSheetNames,
  liveCandidateCount: liveByPhone.size, matchedPhoneCount: found.size, carrierOccurrenceCount: updates.length,
  dimensionMismatchCount: dimensionMismatches.length, dimensionMismatches,
  nonCarrierValueMismatchCount: nonCarrierValueMismatches.length, nonCarrierValueMismatchSample: nonCarrierValueMismatches.slice(0, 20),
  targetMismatchCount: targetMismatches.length, targetMismatches,
  styleMismatchCount: styleMismatches.length, styleMismatches,
  supplementalRowCount: unmatchedPhones.length, supplementalMismatchCount: supplementalMismatches.length, supplementalMismatches,
  formulaErrorCount, formulaErrorScan: finalFormulaErrors.ndjson, ...common, passed: verifyPassed,
};
const refreshReceipt = {
  createdAt: new Date().toISOString(), operation: 'post-write-carrier-refresh',
  inputPath: path.resolve(inputPath), outputPath: path.resolve(outputPath), candidateCount: liveReceipt.candidateCount,
  backendResolvedCount: liveReceipt.backendValidCount,
  backendMissingCount: liveReceipt.rows.filter(row => row.backendMatchCount === 0).length,
  backendDuplicateCount: liveReceipt.rows.filter(row => row.backendMatchCount > 1).length,
  backendUnassignedCarrierCount: liveReceipt.rows.filter(row => row.liveCarrierUnassigned).length,
  matchedWorkbookRowCount: found.size, sourceMissingCount: unmatchedPhones.length,
  supplementalSheetName: unmatchedPhones.length ? supplementalSheetName : '', supplementalRowCount: unmatchedPhones.length,
  mergedWorkbookCoveredCount: found.size + unmatchedPhones.length, missingInWorkbookCount: supplementalMismatches.length,
  duplicateWorkbookCount: Math.max(0, updates.length - found.size), updatedCellCount: updates.filter(row => row.changed).length,
  alreadyCurrentCount: updates.filter(row => !row.changed).length, stillStaleCount: targetMismatches.length,
  formulaErrorCount, nonCarrierValueMismatchCount: nonCarrierValueMismatches.length, styleMismatchCount: styleMismatches.length,
  ...common, passed: verifyPassed,
  rows: updates.map(row => ({ phone: row.phone, sheet: row.sheet, cell: row.cell, beforeCarrier: row.beforeCarrier, currentCarrierName: row.afterCarrier })),
  artifactReceipt: path.resolve(artifactReceiptPath), independentVerificationReceipt: path.resolve(verifyReceiptPath),
};
const readbackReceipt = {
  checkedAt: liveReceipt.checkedAt, mode: liveReceipt.mode, apiTransport: liveReceipt.apiTransport,
  silentBrowserMode: true, browserForegroundActivationAttempted: false, candidateCount: liveReceipt.candidateCount,
  targetCount: liveReceipt.targetMatchCount,
  missingCount: liveReceipt.rows.filter(row => row.backendMatchCount === 0).length,
  duplicateBackendCount: liveReceipt.rows.filter(row => row.backendMatchCount > 1).length,
  mismatchCount: liveReceipt.failureCount, passed: liveReceipt.passed,
  rows: liveReceipt.rows.map(row => ({
    phone: row.phone, backendId: row.backendId, currentCarrierName: row.liveCarrierName,
    currentCarrierIdentif: row.liveCarrierIdentif, expectedCarrierName: row.expectedTarget, passed: row.passed,
  })),
  sourceReceipt: path.resolve(liveReceiptPath),
};
for (const [file, data] of [
  [artifactReceiptPath, artifactReceipt],
  [verifyReceiptPath, verifyReceipt],
  [refreshReceiptPath, refreshReceipt],
  [readbackReceiptPath, readbackReceipt],
]) {
  await fs.writeFile(file, `${JSON.stringify(data, null, 2)}\n`);
}
console.log(JSON.stringify({
  outputPath,
  passed: verifyPassed,
  changed: updates.filter(row => row.changed).length,
  styleMismatchCount: styleMismatches.length,
  formulaErrorCount,
}, null, 2));
if (!verifyPassed) process.exitCode = 2;
