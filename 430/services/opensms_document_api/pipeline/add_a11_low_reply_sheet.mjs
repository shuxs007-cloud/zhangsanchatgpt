import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";

import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const FINAL_PATH = process.argv[2];
const ROOM_CONFIG = JSON.parse(await fs.readFile(new URL('../../../scripts/opensms_room_settings.json', import.meta.url), 'utf8'));
const RAW_PATH = process.argv[3];
const REFERENCE_DATE_TEXT = (process.env.EXPORT4DAY_SERVER_DATE || "").trim();
const SHEET_NAME = "A11连续低于3%";
const RATE_THRESHOLD = 0.03;
const MIN_DAILY_SENDS = 50;
const USE_DAYS_MIN_DAILY_SENDS = 80;
const MIN_USED_DAYS = 2;
const CANCELLED_REVIEW_SHEETS = [
  "已过期_未进过期下架卡",
  "快过期_今天明天",
  "今日总回复量为0",
  "今日昨天总回复量均为0",
];
const THREE_DAY_SEND_HEADER = "3日发送窗口数";
const THREE_DAY_RATE_HEADER = "3日回复率";
const FOUR_DAY_SEND_HEADER = "4日发送窗口数";
const FOUR_DAY_RATE_HEADER = "4日回复率";
const DECREASING_RATE_HEADER = "回复率持续降低";
const FAST_EXPIRY_EXCLUDED_CARRIERS = new Set(["过期下架卡", "快过期隔离区"]);

if (!FINAL_PATH || !RAW_PATH) {
  throw new Error("用法: add_a11_low_reply_sheet.mjs <最终工作簿> <export4day原始工作簿>");
}


function columnLetter(index) {
  let value = index + 1;
  let result = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    result = String.fromCharCode(65 + remainder) + result;
    value = Math.floor((value - 1) / 26);
  }
  return result;
}


const REPORT_COLORS = {
  header: "#1F4E78",
  headerBorder: "#B4C6E7",
  bodyText: "#1F2937",
  rowBorder: "#D9E2F3",
  labelFill: "#D9EAF7",
};


function standardColumnWidth(header) {
  if (/电话|手機號|手机号/.test(header)) return 16;
  if (/运营商|電信商|opr/i.test(header)) return 22;
  if (/卡槽|设备ID|設備ID/.test(header)) return 14;
  if (/日期|时间|時間|截止日|過期日|过期日|起始日|结束日/.test(header)) return 18;
  if (/持续降低|回复率趋势/.test(header)) return 18;
  if (/回复率/.test(header)) return 12;
  if (/发送窗口数|回复窗口数|总发送量|总回复量|設備口數|设备口数|端口/.test(header)) return 14;
  if (/规则|口径|来源|详情|备注|備註|区间/.test(header)) return 30;
  return Math.min(Math.max([...header].length + 5, 12), 24);
}


function applyStandardTableFormatting(targetSheet, options = {}) {
  const values = targetSheet.getUsedRange(true)?.values;
  if (!values || values.length === 0) return;

  const rowCount = values.length;
  const columnCount = Math.max(...values.map((row) => row?.length || 0));
  if (columnCount === 0) return;

  const headers = (values[0] || []).map(asText);
  const lastColumn = columnLetter(columnCount - 1);
  targetSheet.showGridLines = false;

  const headerRange = targetSheet.getRange(`A1:${lastColumn}1`);
  headerRange.format = {
    fill: REPORT_COLORS.header,
    font: { bold: true, color: "#FFFFFF", size: 10 },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: REPORT_COLORS.headerBorder },
  };
  headerRange.format.rowHeight = 32;

  if (rowCount >= 2) {
    const bodyRange = targetSheet.getRange(`A2:${lastColumn}${rowCount}`);
    bodyRange.format = {
      font: { color: REPORT_COLORS.bodyText, size: 10 },
      horizontalAlignment: "center",
      verticalAlignment: "center",
      wrapText: false,
      borders: {
        insideHorizontal: { style: "thin", color: REPORT_COLORS.rowBorder },
      },
    };
    bodyRange.format.rowHeight = 20;
  }

  targetSheet.freezePanes.freezeRows(1);
  if (options.freezeColumns) {
    targetSheet.freezePanes.freezeColumns(options.freezeColumns);
  }

  for (let index = 0; index < columnCount; index += 1) {
    const header = headers[index] || "";
    const column = columnLetter(index);
    const range = targetSheet.getRange(`${column}1:${column}${rowCount}`);
    range.format.columnWidth = options.widths?.[index] ?? standardColumnWidth(header);

    if (rowCount < 2) continue;
    const dataRange = targetSheet.getRange(`${column}2:${column}${rowCount}`);
    if (/电话|手機號|手机号|卡槽/.test(header)) {
      dataRange.format.numberFormat = "@";
    } else if (/回复率/.test(header) && !/持续降低|回复率趋势/.test(header)) {
      dataRange.format.numberFormat = "0.00%";
    } else if (/日期|截止日|過期日|过期日|起始日|结束日/.test(header)) {
      dataRange.format.numberFormat = "yyyy-mm-dd";
    } else if (/创建时间|送达时间|接收时间|激活时间|激活時間/.test(header)) {
      dataRange.format.numberFormat = "yyyy-mm-dd hh:mm:ss";
    } else if (/发送窗口数|回复窗口数|总发送量|总回复量|行数|数量|數量|端口数|端口數|设备口数|設備口數|最长连续天数|命中区间数/.test(header)) {
      dataRange.format.numberFormat = "#,##0";
    }
  }
}


function applySummaryFormatting(targetSheet) {
  const values = targetSheet.getUsedRange(true)?.values;
  if (!values || values.length === 0) return;

  const rowCount = values.length;
  targetSheet.showGridLines = false;
  targetSheet.getRange(`A1:A${rowCount}`).format = {
    fill: REPORT_COLORS.labelFill,
    font: { bold: true, color: REPORT_COLORS.bodyText, size: 10 },
    verticalAlignment: "center",
    borders: { preset: "all", style: "thin", color: REPORT_COLORS.headerBorder },
  };
  targetSheet.getRange(`B1:B${rowCount}`).format = {
    font: { color: REPORT_COLORS.bodyText, size: 10 },
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: REPORT_COLORS.rowBorder },
  };
  targetSheet.getRange(`A1:B${rowCount}`).format.rowHeight = 22;
  targetSheet.getRange(`A1:A${rowCount}`).format.columnWidth = 28;
  targetSheet.getRange(`B1:B${rowCount}`).format.columnWidth = 90;
  targetSheet.freezePanes.freezeColumns(1);
}


function asText(value) {
  return value == null ? "" : String(value).trim();
}


function asNumber(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  const text = asText(value).replaceAll(",", "");
  if (!text) {
    return null;
  }
  const percent = text.endsWith("%");
  const parsed = Number(percent ? text.slice(0, -1) : text);
  if (!Number.isFinite(parsed)) {
    return null;
  }
  return percent ? parsed / 100 : parsed;
}


function aggregateWindowRate(points, dayCount) {
  if (points.length < dayCount) {
    return null;
  }
  const selected = points.slice(-dayCount);
  if (selected.some((point) => point.sendWindows == null || point.replies == null)) {
    return null;
  }
  const denominator = selected.reduce((sum, point) => sum + point.sendWindows, 0);
  if (denominator <= 0) {
    return null;
  }
  return selected.reduce((sum, point) => sum + point.replies, 0) / denominator;
}


function aggregateWindowSends(points, dayCount) {
  if (points.length < dayCount) {
    return null;
  }
  const selected = points.slice(-dayCount);
  if (selected.some((point) => point.sendWindows == null)) {
    return null;
  }
  return selected.reduce((sum, point) => sum + point.sendWindows, 0);
}


function decreasingRateClassification(points) {
  for (const dayCount of [4, 3, 2]) {
    if (points.length < dayCount) {
      continue;
    }
    const selected = points.slice(-dayCount);
    if (selected.some((point) => point.windowRate == null)) {
      continue;
    }
    const strictlyDecreasing = selected
      .slice(1)
      .every((point, index) => point.windowRate < selected[index].windowRate);
    if (strictlyDecreasing) {
      return `持续${dayCount}天降低`;
    }
  }
  return "";
}


function normalizeSlot(value) {
  const text = asText(value).replaceAll(" ", "").replaceAll("-", "_").replace(/\.0$/, "");
  if (!text) {
    return "";
  }
  return text.split("_").map((part) => {
    if (/^\d+(?:\.\d+)?$/.test(part)) {
      return String(Math.trunc(Number(part)));
    }
    return part.toLowerCase();
  }).join("_");
}


function roomForSlot(value) {
  const match = /^(\d+)_/.exec(normalizeSlot(value));
  if (!match) {
    return "未配置";
  }
  const deviceId = Number(match[1]);
  return ROOM_CONFIG.rooms.find(room => room.deviceRanges.some(([start, end]) => deviceId >= start && deviceId <= end))?.name || "未配置";
}


function startOfDay(value) {
  return new Date(value.getFullYear(), value.getMonth(), value.getDate());
}


function resolveReferenceDate(rawPath) {
  if (REFERENCE_DATE_TEXT) {
    const match = /^(20\d{2})-(\d{2})-(\d{2})$/.exec(REFERENCE_DATE_TEXT);
    if (!match) {
      throw new Error(`EXPORT4DAY_SERVER_DATE 无法解析: ${REFERENCE_DATE_TEXT}`);
    }
    return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  }
  return startOfDay(new Date());
}


function resolveHeaderDate(month, day, referenceDate) {
  const candidates = [
    new Date(referenceDate.getFullYear() - 1, month - 1, day),
    new Date(referenceDate.getFullYear(), month - 1, day),
    new Date(referenceDate.getFullYear() + 1, month - 1, day),
  ];
  return candidates.sort(
    (left, right) => Math.abs(left - referenceDate) - Math.abs(right - referenceDate),
  )[0];
}


function formatDate(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}


function dateOnlyKey(value) {
  if (value instanceof Date && Number.isFinite(value.getTime())) {
    return formatDate(value);
  }
  if (typeof value === "number" && Number.isFinite(value) && value >= 20000 && value <= 60000) {
    const excelEpoch = Date.UTC(1899, 11, 30);
    const parsed = new Date(excelEpoch + Math.floor(value) * 86400000);
    return `${parsed.getUTCFullYear()}-${String(parsed.getUTCMonth() + 1).padStart(2, "0")}-${String(parsed.getUTCDate()).padStart(2, "0")}`;
  }
  const text = asText(value)
    .replaceAll("年", "-")
    .replaceAll("月", "-")
    .replaceAll("日", "")
    .replaceAll("/", "-")
    .replaceAll(".", "-");
  const compact = /^(20\d{2})(\d{2})(\d{2})(?:\D|$)/.exec(text);
  if (compact) {
    return `${compact[1]}-${compact[2]}-${compact[3]}`;
  }
  const separated = /^(20\d{2})-(\d{1,2})-(\d{1,2})(?:\D|$)/.exec(text);
  if (!separated) {
    return "";
  }
  return `${separated[1]}-${separated[2].padStart(2, "0")}-${separated[3].padStart(2, "0")}`;
}


function dateDiffDays(left, right) {
  return Math.round((startOfDay(left) - startOfDay(right)) / 86400000);
}


function findRuns(points) {
  const runs = [];
  let current = [];
  for (const point of points) {
    const continues = current.length > 0
      && dateDiffDays(point.date, current[current.length - 1].date) === 1;
    if (point.low && (current.length === 0 || continues)) {
      current.push(point);
      continue;
    }
    if (current.length > 0) {
      runs.push(current);
    }
    current = point.low ? [point] : [];
  }
  if (current.length > 0) {
    runs.push(current);
  }
  return runs;
}


function rowClassification(longestDays) {
  if (longestDays === 2) {
    return "连续2天";
  }
  if (longestDays === 3) {
    return "连续3天";
  }
  if (longestDays >= 4) {
    return "连续4天及以上";
  }
  return "";
}


function mainSheetClassification(longestDays) {
  if (longestDays === 2) {
    return "连续2天低于3%";
  }
  if (longestDays === 3) {
    return "连续3天低于3%";
  }
  if (longestDays >= 4) {
    return "连续4天低于3%";
  }
  return "";
}


function zeroReplyClassification(longestDays) {
  if (longestDays === 2) {
    return "连续2天0回复";
  }
  if (longestDays === 3) {
    return "连续3天0回复";
  }
  if (longestDays >= 4) {
    return "连续4天0回复";
  }
  return "";
}


const referenceDate = resolveReferenceDate(RAW_PATH);
// Both workbooks are immutable inputs at this point.  Import them in parallel
// so the final report parse overlaps the raw-metrics parse instead of adding a
// second full-workbook wait later in the pipeline.
const rawWorkbookPromise = (async () => SpreadsheetFile.importXlsx(await FileBlob.load(RAW_PATH)))();
const finalWorkbookPromise = (async () => SpreadsheetFile.importXlsx(await FileBlob.load(FINAL_PATH)))();
const rawWorkbook = await rawWorkbookPromise;
const rawSheet = rawWorkbook.worksheets.getItemAt(0);
const rawValues = rawSheet.getUsedRange(true).values;

if (!rawValues || rawValues.length < 2) {
  throw new Error("export4day 原始工作簿没有可分析的数据");
}

const headers = rawValues[0].map(asText);
const headerIndex = new Map(headers.map((header, index) => [header, index]));
for (const required of ["电话", "运营商", "opr", "卡槽"]) {
  if (!headerIndex.has(required)) {
    throw new Error(`export4day 缺少必要列: ${required}`);
  }
}

const dailyMap = new Map();
for (let index = 0; index < headers.length; index += 1) {
  const match = /^(\d{2})-(\d{2})(发送窗口数|回复窗口数|总发送量|总回复量)$/.exec(headers[index]);
  if (!match) {
    continue;
  }
  const key = `${match[1]}-${match[2]}`;
  if (!dailyMap.has(key)) {
    dailyMap.set(key, {
      label: key,
      date: resolveHeaderDate(Number(match[1]), Number(match[2]), referenceDate),
    });
  }
  const indexKey = match[3] === "总发送量"
    ? "sendIndex"
    : match[3] === "总回复量"
      ? "totalReplyIndex"
      : match[3] === "发送窗口数"
        ? "sendWindowIndex"
        : "replyIndex";
  dailyMap.get(key)[indexKey] = index;
}

const dailyDefinitions = [...dailyMap.values()]
  .filter((item) => item.sendIndex != null
    && item.sendWindowIndex != null
    && item.replyIndex != null
    && item.totalReplyIndex != null)
  .sort((left, right) => left.date - right.date);

if (dailyDefinitions.length < 4) {
  throw new Error("export4day 没有足够的逐日发送窗口数/回复窗口数列，至少需要连续4日");
}
const latestFourDefinitions = dailyDefinitions.slice(-4);
for (let index = 1; index < latestFourDefinitions.length; index += 1) {
  if (dateDiffDays(latestFourDefinitions[index].date, latestFourDefinitions[index - 1].date) !== 1) {
    throw new Error("export4day 最新4个统计日期不是连续自然日，停止计算3日/4日回复率");
  }
}

const phoneIndex = headerIndex.get("电话");
const carrierIndex = headerIndex.get("运营商");
const oprIndex = headerIndex.get("opr");
const slotIndex = headerIndex.get("卡槽");
const createdIndex = headerIndex.get("创建时间");
const outputRecords = [];
const zeroReplyLongestByPhone = new Map();
const zeroTotalReplyByPhone = new Map();
const useDaysByPhone = new Map();
const windowMetricsByRow = [];
let a11Rows = 0;

for (const sourceRow of rawValues.slice(1)) {
  const carrier = asText(sourceRow[carrierIndex]);
  const opr = asText(sourceRow[oprIndex]);
  const phone = asText(sourceRow[phoneIndex]);
  const points = dailyDefinitions.map((definition) => {
    const sends = asNumber(sourceRow[definition.sendIndex]);
    const sendWindows = asNumber(sourceRow[definition.sendWindowIndex]);
    const replies = asNumber(sourceRow[definition.replyIndex]);
    const totalReplies = asNumber(sourceRow[definition.totalReplyIndex]);
    const active = sends != null && sends > MIN_DAILY_SENDS && replies != null;
    const rate = active ? replies / sends : null;
    return {
      ...definition,
      sends: sends ?? null,
      sendWindows: sendWindows ?? null,
      replies: replies ?? null,
      totalReplies: totalReplies ?? null,
      rate,
      windowRate: sendWindows != null && sendWindows > 0 && replies != null
        ? replies / sendWindows
        : null,
      active,
      low: active && rate < RATE_THRESHOLD,
    };
  });
  windowMetricsByRow.push({
    threeDaySends: aggregateWindowSends(points, 3),
    threeDayRate: aggregateWindowRate(points, 3),
    fourDaySends: aggregateWindowSends(points, 4),
    fourDayRate: aggregateWindowRate(points, 4),
    decreasingRate: decreasingRateClassification(points),
  });
  const zeroReplyRuns = findRuns(points.map((point) => ({
    ...point,
    low: point.sendWindows != null && point.sendWindows > 40 && point.replies === 0,
  }))).filter((run) => run.length >= 2);
  const longestZeroReplyDays = Math.max(0, ...zeroReplyRuns.map((run) => run.length));
  if (phone && longestZeroReplyDays >= 2) {
    zeroReplyLongestByPhone.set(
      phone,
      Math.max(zeroReplyLongestByPhone.get(phone) || 0, longestZeroReplyDays),
    );
  }

  const zeroTotalReplyPoints = points.map((point) => ({
    ...point,
    low: point.totalReplies === 0,
  }));
  const trailingZeroTotalReplyRun = [];
  for (let index = zeroTotalReplyPoints.length - 1; index >= 0; index -= 1) {
    const point = zeroTotalReplyPoints[index];
    const newerPoint = trailingZeroTotalReplyRun[0];
    if (!point.low || (newerPoint && newerPoint.date - point.date !== 86400000)) {
      break;
    }
    trailingZeroTotalReplyRun.unshift(point);
  }
  if (phone && trailingZeroTotalReplyRun.length > 0) {
    const latestPoint = points[points.length - 1];
    const previousPoint = points.length >= 2 ? points[points.length - 2] : null;
    zeroTotalReplyByPhone.set(phone, {
      trailingDays: trailingZeroTotalReplyRun.length,
      start: trailingZeroTotalReplyRun[0].date,
      end: trailingZeroTotalReplyRun[trailingZeroTotalReplyRun.length - 1].date,
      latestLabel: latestPoint.label,
      latestTotalReplies: latestPoint.totalReplies,
      previousLabel: previousPoint?.label || "",
      previousTotalReplies: previousPoint?.totalReplies ?? null,
    });
  }

  const usedDays = points.filter(
    (point) => point.sendWindows != null && point.sendWindows > USE_DAYS_MIN_DAILY_SENDS,
  ).length;
  if (phone && /koo|kini|telus1/i.test(`${carrier} ${opr}`) && usedDays >= MIN_USED_DAYS) {
    useDaysByPhone.set(phone, "2天");
  }

  if (!/^A11(?:-|$)/i.test(carrier)) {
    continue;
  }
  a11Rows += 1;
  const qualifyingRuns = findRuns(points).filter((run) => run.length >= 2);
  if (qualifyingRuns.length === 0) {
    continue;
  }
  qualifyingRuns.sort((left, right) => {
    if (right.length !== left.length) {
      return right.length - left.length;
    }
    return right[right.length - 1].date - left[left.length - 1].date;
  });
  const longest = qualifyingRuns[0];
  const classification = rowClassification(longest.length);
  const runRanges = qualifyingRuns
    .map((run) => `${formatDate(run[0].date)}至${formatDate(run[run.length - 1].date)}(${run.length}天)`)
    .join("；");
  outputRecords.push({
    classification,
    phone,
    carrier,
    opr: asText(sourceRow[oprIndex]),
    slot: asText(sourceRow[slotIndex]),
    room: roomForSlot(sourceRow[slotIndex]),
    createdAt: createdIndex == null ? null : sourceRow[createdIndex],
    longestDays: longest.length,
    longestStart: longest[0].date,
    longestEnd: longest[longest.length - 1].date,
    qualifyingRunCount: qualifyingRuns.length,
    runRanges,
    points,
  });
}

const classOrder = new Map([
  ["连续2天", 2],
  ["连续3天", 3],
  ["连续4天及以上", 4],
]);
outputRecords.sort((left, right) => {
  const classDiff = classOrder.get(left.classification) - classOrder.get(right.classification);
  if (classDiff !== 0) {
    return classDiff;
  }
  if (right.longestDays !== left.longestDays) {
    return right.longestDays - left.longestDays;
  }
  return left.phone.localeCompare(right.phone, "zh-Hans-CN");
});

const zeroReplyClassByPhone = new Map(
  [...zeroReplyLongestByPhone.entries()].map(([phone, longestDays]) => [
    phone,
    zeroReplyClassification(longestDays),
  ]),
);
const zeroReplyCounts = {
  "连续2天0回复": [...zeroReplyClassByPhone.values()].filter((value) => value === "连续2天0回复").length,
  "连续3天0回复": [...zeroReplyClassByPhone.values()].filter((value) => value === "连续3天0回复").length,
  "连续4天0回复": [...zeroReplyClassByPhone.values()].filter((value) => value === "连续4天0回复").length,
};

const finalWorkbook = await finalWorkbookPromise;
const existingNames = finalWorkbook.worksheets.items.map((sheet) => sheet.name);
if (existingNames.includes(SHEET_NAME)) {
  throw new Error(`最终工作簿已经存在工作表 ${SHEET_NAME}，请使用本轮新生成的结果文件`);
}
const sheet = finalWorkbook.worksheets.add(SHEET_NAME);
sheet.showGridLines = false;

const mainSheet = finalWorkbook.worksheets.getItemAt(0);
const mainValues = mainSheet.getUsedRange(true).values;
const mainHeaders = mainValues[0].map(asText);
const mainPhoneIndex = mainHeaders.indexOf("电话");
const mainCarrierIndex = mainHeaders.indexOf("运营商");
const mainExpiryStatusIndex = mainHeaders.indexOf("过期状态");
const mainExpiryDateIndex = mainHeaders.indexOf("截止日");
const mainChangeStatusIndex = mainHeaders.indexOf("换号状态");
const mainUseDaysIndex = mainHeaders.indexOf("koo是否2天");
const mainActivationAgeIndex = mainHeaders.indexOf("激活后天数");
const mainLowIndex = mainHeaders.indexOf("连续低于x%");
const mainZeroReplyIndex = mainHeaders.indexOf("连续0回复");
const mainSlotIndex = mainHeaders.indexOf("卡槽");
const mainTenDayRateIndex = Math.max(
  mainHeaders.indexOf("10日回复率"),
  mainHeaders.indexOf("近10日回复率"),
);
const mainThreeDaySendIndex = mainHeaders.indexOf(THREE_DAY_SEND_HEADER);
const mainThreeDayRateIndex = mainHeaders.indexOf(THREE_DAY_RATE_HEADER);
const mainFourDaySendIndex = mainHeaders.indexOf(FOUR_DAY_SEND_HEADER);
const mainFourDayRateIndex = mainHeaders.indexOf(FOUR_DAY_RATE_HEADER);
const mainDecreasingRateIndex = mainHeaders.indexOf(DECREASING_RATE_HEADER);
if (mainPhoneIndex < 0) {
  throw new Error("最终工作簿主表缺少电话列");
}
if (mainChangeStatusIndex < 0) {
  throw new Error("最终工作簿主表缺少换号状态列");
}
if (mainCarrierIndex < 0 || mainExpiryStatusIndex < 0 || mainExpiryDateIndex < 0) {
  throw new Error("最终工作簿主表缺少运营商/过期状态/截止日列");
}
if (mainSlotIndex < 0
  || mainTenDayRateIndex < 0
  || mainThreeDaySendIndex !== mainTenDayRateIndex + 1
  || mainThreeDayRateIndex !== mainTenDayRateIndex + 2
  || mainFourDaySendIndex !== mainTenDayRateIndex + 3
  || mainFourDayRateIndex !== mainTenDayRateIndex + 4
  || mainDecreasingRateIndex !== mainTenDayRateIndex + 5) {
  throw new Error("最终工作簿主表缺少10日回复率后的3日/4日发送窗口数、回复率和持续降低列");
}
if (mainChangeStatusIndex !== 3
  || mainUseDaysIndex !== 4
  || mainActivationAgeIndex !== 5
  || mainLowIndex !== 8
  || mainZeroReplyIndex !== 9) {
  throw new Error("最终工作簿主表第4列及后续统计列位置不正确");
}
if (mainValues.length !== rawValues.length || windowMetricsByRow.length !== mainValues.length - 1) {
  throw new Error("原始工作簿与最终工作簿行数不一致，停止写入回复率统计列");
}
for (let index = 1; index < mainValues.length; index += 1) {
  const mainKey = `${asText(mainValues[index][mainPhoneIndex])}|${normalizeSlot(mainValues[index][mainSlotIndex])}`;
  const rawKey = `${asText(rawValues[index][phoneIndex])}|${normalizeSlot(rawValues[index][slotIndex])}`;
  if (mainKey !== rawKey) {
    throw new Error(`原始工作簿与最终工作簿第${index + 1}行号码/卡槽不一致，停止写入回复率统计列`);
  }
}
const mainClassByPhone = new Map(
  outputRecords.map((record) => [record.phone, mainSheetClassification(record.longestDays)]),
);
const mainUseDaysColumn = columnLetter(mainUseDaysIndex);
const mainActivationAgeColumn = columnLetter(mainActivationAgeIndex);
const mainLowColumn = columnLetter(mainLowIndex);
const mainZeroReplyColumn = columnLetter(mainZeroReplyIndex);
const mainThreeDaySendColumn = columnLetter(mainThreeDaySendIndex);
const mainThreeDayRateColumn = columnLetter(mainThreeDayRateIndex);
const mainFourDaySendColumn = columnLetter(mainFourDaySendIndex);
const mainFourDayRateColumn = columnLetter(mainFourDayRateIndex);
const mainDecreasingRateColumn = columnLetter(mainDecreasingRateIndex);
const mainLastRow = mainValues.length;
if (mainLastRow >= 2) {
  mainSheet.getRange(`${mainUseDaysColumn}2:${mainUseDaysColumn}${mainLastRow}`).values = mainValues
    .slice(1)
    .map((row) => [useDaysByPhone.get(asText(row[mainPhoneIndex])) || null]);
  mainSheet.getRange(`${mainLowColumn}2:${mainLowColumn}${mainLastRow}`).values = mainValues
    .slice(1)
    .map((row) => [mainClassByPhone.get(asText(row[mainPhoneIndex])) || null]);
  mainSheet.getRange(`${mainZeroReplyColumn}2:${mainZeroReplyColumn}${mainLastRow}`).values = mainValues
    .slice(1)
    .map((row) => [zeroReplyClassByPhone.get(asText(row[mainPhoneIndex])) || null]);
  mainSheet.getRange(`${mainThreeDaySendColumn}2:${mainThreeDaySendColumn}${mainLastRow}`).values = windowMetricsByRow
    .map((metrics) => [metrics.threeDaySends]);
  mainSheet.getRange(`${mainThreeDayRateColumn}2:${mainThreeDayRateColumn}${mainLastRow}`).values = windowMetricsByRow
    .map((metrics) => [metrics.threeDayRate]);
  mainSheet.getRange(`${mainFourDaySendColumn}2:${mainFourDaySendColumn}${mainLastRow}`).values = windowMetricsByRow
    .map((metrics) => [metrics.fourDaySends]);
  mainSheet.getRange(`${mainFourDayRateColumn}2:${mainFourDayRateColumn}${mainLastRow}`).values = windowMetricsByRow
    .map((metrics) => [metrics.fourDayRate]);
  mainSheet.getRange(`${mainDecreasingRateColumn}2:${mainDecreasingRateColumn}${mainLastRow}`).values = windowMetricsByRow
    .map((metrics) => [metrics.decreasingRate || null]);
}
mainSheet.getRange(`${mainUseDaysColumn}:${mainActivationAgeColumn}`).format.columnWidth = 14;
mainSheet.getRange(`${mainLowColumn}:${mainZeroReplyColumn}`).format.columnWidth = 20;
mainSheet.getRange(`${mainThreeDaySendColumn}:${mainThreeDaySendColumn}`).format.columnWidth = 16;
mainSheet.getRange(`${mainThreeDayRateColumn}:${mainThreeDayRateColumn}`).format.columnWidth = 12;
mainSheet.getRange(`${mainFourDaySendColumn}:${mainFourDaySendColumn}`).format.columnWidth = 16;
mainSheet.getRange(`${mainFourDayRateColumn}:${mainFourDayRateColumn}`).format.columnWidth = 12;
mainSheet.getRange(`${mainDecreasingRateColumn}:${mainDecreasingRateColumn}`).format.columnWidth = 18;
mainSheet.getRange(`${mainUseDaysColumn}1:${mainActivationAgeColumn}1`).format = {
  fill: "#D9D9D9",
  font: { bold: true, color: "#000000" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  borders: { preset: "all", style: "thin", color: "#808080" },
};
mainSheet.getRange(`${mainLowColumn}1:${mainZeroReplyColumn}1`).format = {
  fill: "#D9D9D9",
  font: { bold: true, color: "#000000" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  borders: { preset: "all", style: "thin", color: "#808080" },
};
mainSheet.getRange(`${mainThreeDaySendColumn}1:${mainDecreasingRateColumn}1`).format = {
  fill: "#D9D9D9",
  font: { bold: true, color: "#000000" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  borders: { preset: "all", style: "thin", color: "#808080" },
};
if (mainLastRow >= 2) {
  mainSheet.getRange(`${mainUseDaysColumn}2:${mainActivationAgeColumn}${mainLastRow}`).format = {
    horizontalAlignment: "center",
    verticalAlignment: "center",
    borders: { preset: "all", style: "thin", color: "#808080" },
  };
  mainSheet.getRange(`${mainLowColumn}2:${mainZeroReplyColumn}${mainLastRow}`).format = {
    horizontalAlignment: "center",
    verticalAlignment: "center",
    borders: { preset: "all", style: "thin", color: "#808080" },
  };
  const recentMetricRange = mainSheet.getRange(
    `${mainThreeDaySendColumn}2:${mainFourDayRateColumn}${mainLastRow}`,
  );
  recentMetricRange.format = {
    horizontalAlignment: "center",
    verticalAlignment: "center",
    borders: { preset: "all", style: "thin", color: "#808080" },
  };
  mainSheet.getRange(`${mainThreeDaySendColumn}2:${mainThreeDaySendColumn}${mainLastRow}`).format.numberFormat = "#,##0";
  mainSheet.getRange(`${mainThreeDayRateColumn}2:${mainThreeDayRateColumn}${mainLastRow}`).format.numberFormat = "0.00%";
  mainSheet.getRange(`${mainFourDaySendColumn}2:${mainFourDaySendColumn}${mainLastRow}`).format.numberFormat = "#,##0";
  mainSheet.getRange(`${mainFourDayRateColumn}2:${mainFourDayRateColumn}${mainLastRow}`).format.numberFormat = "0.00%";
  mainSheet.getRange(`${mainDecreasingRateColumn}2:${mainDecreasingRateColumn}${mainLastRow}`).format = {
    horizontalAlignment: "center",
    verticalAlignment: "center",
    borders: { preset: "all", style: "thin", color: "#808080" },
  };
  const changedMainRows = mainValues
    .slice(1)
    .map((row, index) => (asText(row[mainChangeStatusIndex]) === "已换号" ? index + 2 : null))
    .filter((row) => row != null);
  let runStart = null;
  let previous = null;
  for (const row of [...changedMainRows, null]) {
    if (row != null && (previous == null || row === previous + 1)) {
      runStart ??= row;
      previous = row;
      continue;
    }
    if (runStart != null) {
      mainSheet.getRange(`${mainUseDaysColumn}${runStart}:${mainUseDaysColumn}${previous}`).format.fill = "#FFFF00";
      mainSheet.getRange(`${mainLowColumn}${runStart}:${mainZeroReplyColumn}${previous}`).format.fill = "#FFFF00";
    }
    runStart = row;
    previous = row;
  }
}

// Review sheets are built only after every main-sheet enrichment is complete,
// so they always carry the final expiry, Koo, streak, date, and change fields.
const processedMainValues = mainSheet.getUsedRange(true).values;
const todayKey = formatDate(referenceDate);
const expiredReviewRows = processedMainValues.slice(1).filter((row) => (
  asText(row[mainExpiryStatusIndex]) === "已过期"
  && asText(row[mainCarrierIndex]) !== "过期下架卡"
));
const fastExpiryReviewRows = processedMainValues.slice(1).filter((row) => (
  asText(row[mainExpiryStatusIndex]) === "快过期"
  && !FAST_EXPIRY_EXCLUDED_CARRIERS.has(asText(row[mainCarrierIndex]))
));

const zeroTotalReplyEvidenceHeaders = [
  "截至最新日连续总回复量0天数",
  "统计截止日",
  "连续起始日",
  "连续结束日",
  `${dailyDefinitions[dailyDefinitions.length - 1].label}总回复量`,
  `${dailyDefinitions[dailyDefinitions.length - 2].label}总回复量`,
];
function buildZeroTotalReplyRows(matchesDays) {
  return processedMainValues.slice(1).flatMap((row) => {
    const evidence = zeroTotalReplyByPhone.get(asText(row[mainPhoneIndex]));
    if (!evidence || !matchesDays(evidence.trailingDays)) {
      return [];
    }
    return [[
      ...row.slice(0, 13),
      evidence.trailingDays,
      evidence.end,
      evidence.start,
      evidence.end,
      evidence.latestTotalReplies,
      evidence.previousTotalReplies,
      ...row.slice(13),
    ]];
  });
}
const todayZeroTotalReplyRows = buildZeroTotalReplyRows((days) => days >= 1);
const todayYesterdayZeroTotalReplyRows = buildZeroTotalReplyRows((days) => days >= 2);
const zeroTotalReplyHeaders = [
  ...mainHeaders.slice(0, 13),
  ...zeroTotalReplyEvidenceHeaders,
  ...mainHeaders.slice(13),
];

const decreasingRateCounts = {
  "持续2天降低": windowMetricsByRow.filter((metrics) => metrics.decreasingRate === "持续2天降低").length,
  "持续3天降低": windowMetricsByRow.filter((metrics) => metrics.decreasingRate === "持续3天降低").length,
  "持续4天降低": windowMetricsByRow.filter((metrics) => metrics.decreasingRate === "持续4天降低").length,
};
const summarySheet = finalWorkbook.worksheets.getItem("处理汇总");
const summaryStartRow = summarySheet.getUsedRange(true).values.length + 1;
summarySheet.getRange(`A${summaryStartRow}:B${summaryStartRow + 22}`).values = [
  ["koo是否2天统计口径", "Kooda/T系号码逐日发送窗口数>80的日期达到2天及以上，日期可连续或不连续"],
  ["koo是否2天命中号码数", useDaysByPhone.size],
  ["A11低回复统计口径", "逐日总发送量>50，回复窗口数/总发送量严格低于3%，按最长连续自然日分类；总发送量<=50的日期中断连续天数"],
  ["A11总行数", a11Rows],
  ["A11低回复命中号码数", outputRecords.length],
  ["A11连续2天号码数", outputRecords.filter((record) => record.classification === "连续2天").length],
  ["A11连续3天号码数", outputRecords.filter((record) => record.classification === "连续3天").length],
  ["A11连续4天及以上号码数", outputRecords.filter((record) => record.classification === "连续4天及以上").length],
  ["主表连续0回复统计口径", "逐日发送窗口数>40且回复窗口数=0，按最长连续自然日分类"],
  ["主表连续0回复命中号码数", zeroReplyClassByPhone.size],
  ["主表连续2天0回复号码数", zeroReplyCounts["连续2天0回复"]],
  ["主表连续3天0回复号码数", zeroReplyCounts["连续3天0回复"]],
  ["主表连续4天及以上0回复号码数", zeroReplyCounts["连续4天0回复"]],
  ["已取消工作表", CANCELLED_REVIEW_SHEETS.join("、")],
  ["最近4日统计日期", latestFourDefinitions.map((definition) => formatDate(definition.date)).join("、")],
  ["3日发送窗口数口径", "最新连续3日发送窗口数合计；任一日缺值则留空"],
  ["3日回复率口径", "最新连续3日回复窗口数合计/发送窗口数合计；任一日缺值或合计发送窗口数为0则留空"],
  ["4日发送窗口数口径", "最新连续4日发送窗口数合计；任一日缺值则留空"],
  ["4日回复率口径", "最新连续4日回复窗口数合计/发送窗口数合计；任一日缺值或合计发送窗口数为0则留空"],
  ["回复率持续降低口径", "每日回复率=回复窗口数/发送窗口数；从最新日向前严格逐日降低，优先显示最长4天，其次3天、2天；相等不命中"],
  ["持续2天降低号码数", decreasingRateCounts["持续2天降低"]],
  ["持续3天降低号码数", decreasingRateCounts["持续3天降低"]],
  ["持续4天降低号码数", decreasingRateCounts["持续4天降低"]],
];
summarySheet.getRange(`A${summaryStartRow}:A${summaryStartRow + 22}`).format.font = { bold: true };
summarySheet.getRange("A:A").format.columnWidth = 24;
summarySheet.getRange("B:B").format.columnWidth = 90;

const googleMissingSheet = finalWorkbook.worksheets.getItem("谷歌文档中有激活日期-系统无法使用");
const googleHeaders = googleMissingSheet.getUsedRange(true).values[0] || [];
for (let index = 0; index < googleHeaders.length; index += 1) {
  if (/日期|时间|時間|截止日|過期日|过期日/.test(asText(googleHeaders[index]))) {
    const column = columnLetter(index);
    googleMissingSheet.getRange(`${column}:${column}`).format.columnWidth = 20;
  }
}

const coreHeaders = [
  "分类",
  "电话",
  "运营商",
  "opr",
  "卡槽",
  "机房",
  "创建时间",
  "最长连续天数",
  "最长连续起始日",
  "最长连续结束日",
  "命中区间数",
  "全部命中区间",
];
const dailyHeaders = dailyDefinitions.flatMap((definition) => [
  `${definition.label}总发送量`,
  `${definition.label}回复窗口数`,
  `${definition.label}计算回复率`,
]);
const outputHeaders = [...coreHeaders, ...dailyHeaders];
const headerRow = 5;
const firstDataRow = headerRow + 1;
const lastDataRow = firstDataRow + outputRecords.length - 1;
const lastColumn = columnLetter(outputHeaders.length - 1);

sheet.getRange("A1:H1").merge();
sheet.getRange("A1").values = [["A11 连续低回复率号码"]];
sheet.getRange("A2:L2").merge();
sheet.getRange("A2").values = [[
  "口径：逐日总发送量 > 50；日回复率 = 回复窗口数 ÷ 总发送量；严格低于3%；总发送量不达门槛的日期中断连续天数；分类采用每个号码的最长连续自然日天数。",
]];
sheet.getRange("A3:H3").values = [[
  "A11总行数",
  "命中号码数",
  "连续2天",
  "连续3天",
  "连续4天及以上",
  "阈值",
  "基准日期",
  "逐日范围",
]];
sheet.getRange("A4:B4").values = [[a11Rows, outputRecords.length]];
if (outputRecords.length > 0) {
  sheet.getRange("C4").formulas = [[`=COUNTIF($A$${firstDataRow}:$A$${lastDataRow},"连续2天")`]];
  sheet.getRange("D4").formulas = [[`=COUNTIF($A$${firstDataRow}:$A$${lastDataRow},"连续3天")`]];
  sheet.getRange("E4").formulas = [[`=COUNTIF($A$${firstDataRow}:$A$${lastDataRow},"连续4天及以上")`]];
} else {
  sheet.getRange("C4:E4").values = [[0, 0, 0]];
}
sheet.getRange("F4:H4").values = [[
  RATE_THRESHOLD,
  referenceDate,
  `${formatDate(dailyDefinitions[0].date)}至${formatDate(dailyDefinitions[dailyDefinitions.length - 1].date)}`,
]];
sheet.getRange(`A${headerRow}:${lastColumn}${headerRow}`).values = [outputHeaders];

if (outputRecords.length > 0) {
  const outputRows = outputRecords.map((record) => [
    record.classification,
    record.phone,
    record.carrier,
    record.opr,
    record.slot,
    record.room,
    record.createdAt,
    record.longestDays,
    record.longestStart,
    record.longestEnd,
    record.qualifyingRunCount,
    record.runRanges,
    ...record.points.flatMap((point) => [point.sends, point.replies, point.rate]),
  ]);
  sheet.getRange(`A${firstDataRow}:${lastColumn}${lastDataRow}`).values = outputRows;
}

sheet.getRange("A1:H1").format = {
  fill: "#1F4E78",
  font: { bold: true, color: "#FFFFFF", size: 16 },
  horizontalAlignment: "left",
  verticalAlignment: "center",
};
sheet.getRange("A2:L2").format = {
  fill: "#D9EAF7",
  font: { color: "#1F2937", size: 10 },
  verticalAlignment: "center",
};
sheet.getRange("A3:H3").format = {
  fill: "#D9E1F2",
  font: { bold: true, color: "#1F2937" },
  horizontalAlignment: "center",
  borders: { preset: "all", style: "thin", color: "#B4C6E7" },
};
sheet.getRange("A4:H4").format = {
  fill: "#F4F7FB",
  font: { bold: true, color: "#1F2937" },
  horizontalAlignment: "center",
  borders: { preset: "all", style: "thin", color: "#D9E2F3" },
};
sheet.getRange(`A${headerRow}:${lastColumn}${headerRow}`).format = {
  fill: "#4472C4",
  font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  wrapText: true,
  borders: { preset: "all", style: "thin", color: "#D9E2F3" },
};

if (outputRecords.length > 0) {
  const dataRange = sheet.getRange(`A${firstDataRow}:${lastColumn}${lastDataRow}`);
  dataRange.format = {
    verticalAlignment: "center",
    borders: {
      insideHorizontal: { style: "thin", color: "#E7EAF0" },
    },
  };
  sheet.getRange(`B${firstDataRow}:B${lastDataRow}`).format.numberFormat = "@";
  sheet.getRange(`E${firstDataRow}:E${lastDataRow}`).format.numberFormat = "@";
  sheet.getRange(`G${firstDataRow}:G${lastDataRow}`).format.numberFormat = "yyyy-mm-dd hh:mm:ss";
  sheet.getRange(`H${firstDataRow}:H${lastDataRow}`).format.numberFormat = "0";
  sheet.getRange(`I${firstDataRow}:J${lastDataRow}`).format.numberFormat = "yyyy-mm-dd";
  sheet.getRange(`K${firstDataRow}:K${lastDataRow}`).format.numberFormat = "0";
  for (let index = 0; index < dailyDefinitions.length; index += 1) {
    const startIndex = coreHeaders.length + index * 3;
    const sendColumn = columnLetter(startIndex);
    const replyColumn = columnLetter(startIndex + 1);
    const rateColumn = columnLetter(startIndex + 2);
    sheet.getRange(`${sendColumn}${firstDataRow}:${replyColumn}${lastDataRow}`).format.numberFormat = "#,##0";
    const rateRange = sheet.getRange(`${rateColumn}${firstDataRow}:${rateColumn}${lastDataRow}`);
    rateRange.format.numberFormat = "0.00%";
    rateRange.conditionalFormats.add("cellIs", {
      operator: "lessThan",
      formula: RATE_THRESHOLD,
      format: {
        fill: "#FCE8E6",
        font: { color: "#B91C1C", bold: true },
      },
    });
  }
  sheet.getRange(`A${firstDataRow}:A${lastDataRow}`).conditionalFormats.add("containsText", {
    text: "连续4天",
    format: { fill: "#F8D7DA", font: { color: "#842029", bold: true } },
  });
  sheet.getRange(`A${firstDataRow}:A${lastDataRow}`).conditionalFormats.add("containsText", {
    text: "连续3天",
    format: { fill: "#FFF3CD", font: { color: "#664D03", bold: true } },
  });
  sheet.getRange(`A${firstDataRow}:A${lastDataRow}`).conditionalFormats.add("containsText", {
    text: "连续2天",
    format: { fill: "#D1E7DD", font: { color: "#0F5132", bold: true } },
  });
}

sheet.getRange("F4").format.numberFormat = "0.00%";
sheet.getRange("G4").format.numberFormat = "yyyy-mm-dd";
sheet.getRange("A1").format.rowHeight = 28;
sheet.getRange("A2").format.rowHeight = 30;
sheet.getRange(`A${headerRow}:${lastColumn}${headerRow}`).format.rowHeight = 34;
sheet.freezePanes.freezeRows(headerRow);
sheet.freezePanes.freezeColumns(2);

const fixedWidths = [
  15, 16, 22, 22, 14, 12, 20, 14, 16, 16, 12, 46,
];
for (let index = 0; index < outputHeaders.length; index += 1) {
  const column = columnLetter(index);
  sheet.getRange(`${column}:${column}`).format.columnWidth = fixedWidths[index] ?? 14;
}

// Apply deterministic workbook-wide formatting. The backend export does not
// consistently carry a default style, so report appearance must not depend on
// whichever source styling happened to arrive in this run.
applyStandardTableFormatting(mainSheet, {
  freezeColumns: 2,
  widths: [16, 22, 12, 14, 24, 14, 20, 20, 14, 14, 14, 12, 12],
});
applySummaryFormatting(summarySheet);
applyStandardTableFormatting(finalWorkbook.worksheets.getItem("未匹配卡槽"));
applyStandardTableFormatting(finalWorkbook.worksheets.getItem("谷歌有-系统无"), { freezeColumns: 1 });
applyStandardTableFormatting(googleMissingSheet, { freezeColumns: 3 });
if (mainLastRow >= 2) {
  mainSheet.getRange(`B2:B${mainLastRow}`).format.horizontalAlignment = "left";
  mainSheet.getRange(`E2:E${mainLastRow}`).format.horizontalAlignment = "left";
  mainSheet.getRange(`I2:K${mainLastRow}`).format.numberFormat = "yyyy-mm-dd";

  const expiryStatusRange = mainSheet.getRange(`C2:C${mainLastRow}`);
  expiryStatusRange.conditionalFormats.add("containsText", {
    text: "已过期",
    format: { fill: "#F8D7DA", font: { color: "#842029", bold: true } },
  });
  expiryStatusRange.conditionalFormats.add("containsText", {
    text: "快过期",
    format: { fill: "#FFF3CD", font: { color: "#664D03", bold: true } },
  });
  expiryStatusRange.conditionalFormats.add("containsText", {
    text: "6天",
    format: { fill: "#D1E7DD", font: { color: "#0F5132", bold: true } },
  });
  mainSheet.getRange(`D2:D${mainLastRow}`).conditionalFormats.add("containsText", {
    text: "2天",
      format: { fill: "#D9EAF7", font: { color: "#1F4E78", bold: true } },
  });
  const decreasingRange = mainSheet.getRange(
    `${mainDecreasingRateColumn}2:${mainDecreasingRateColumn}${mainLastRow}`,
  );
  decreasingRange.conditionalFormats.add("containsText", {
    text: "持续4天降低",
    format: { fill: "#F8D7DA", font: { color: "#842029", bold: true } },
  });
  decreasingRange.conditionalFormats.add("containsText", {
    text: "持续3天降低",
    format: { fill: "#FFF3CD", font: { color: "#664D03", bold: true } },
  });
  decreasingRange.conditionalFormats.add("containsText", {
    text: "持续2天降低",
    format: { fill: "#D1E7DD", font: { color: "#0F5132", bold: true } },
  });
}

await fs.mkdir(path.dirname(FINAL_PATH), { recursive: true });
const outputBlob = await SpreadsheetFile.exportXlsx(finalWorkbook);
await outputBlob.save(FINAL_PATH);

const classCounts = {
  "连续2天": outputRecords.filter((record) => record.classification === "连续2天").length,
  "连续3天": outputRecords.filter((record) => record.classification === "连续3天").length,
  "连续4天及以上": outputRecords.filter((record) => record.classification === "连续4天及以上").length,
};

console.log(JSON.stringify({
  status: "success",
  output_path: FINAL_PATH,
  sheet_name: SHEET_NAME,
  threshold: RATE_THRESHOLD,
  minimum_daily_sends_exclusive: MIN_DAILY_SENDS,
  low_reply_rule: "总发送量>50且回复窗口数/总发送量严格低于3%",
  reference_date: formatDate(referenceDate),
  daily_start: formatDate(dailyDefinitions[0].date),
  daily_end: formatDate(dailyDefinitions[dailyDefinitions.length - 1].date),
  a11_rows: a11Rows,
  matched_phone_rows: outputRecords.length,
  main_sheet_columns: [
    "koo是否2天",
    "激活后天数",
    "连续低于x%",
    "连续0回复",
    THREE_DAY_SEND_HEADER,
    THREE_DAY_RATE_HEADER,
    FOUR_DAY_SEND_HEADER,
    FOUR_DAY_RATE_HEADER,
    DECREASING_RATE_HEADER,
  ],
  recent_window_rate_dates: latestFourDefinitions.map((definition) => formatDate(definition.date)),
  three_day_send_rule: "最新连续3日发送窗口数合计",
  three_day_rate_rule: "最新连续3日回复窗口数合计/发送窗口数合计",
  four_day_send_rule: "最新连续4日发送窗口数合计",
  four_day_rate_rule: "最新连续4日回复窗口数合计/发送窗口数合计",
  decreasing_rate_rule: "每日回复窗口数/发送窗口数从最新日向前严格逐日降低，优先显示4天，其次3天、2天",
  decreasing_rate_counts: decreasingRateCounts,
  use_days_rule: "Kooda/T系逐日发送窗口数>80的日期达到2天及以上时固定写“2天”，不足2天留空，日期可连续或不连续",
  use_two_days_rows: useDaysByPhone.size,
  main_sheet_classified_rows: outputRecords.length,
  class_counts: classCounts,
  zero_reply_classified_rows: zeroReplyClassByPhone.size,
  zero_reply_send_window_rule: "发送窗口数>40且回复窗口数=0",
  zero_reply_counts: zeroReplyCounts,
  cancelled_review_sheets: CANCELLED_REVIEW_SHEETS,
  cell_type_policy: {
    numeric_text_conversion_enabled: false,
    mode: "preserve_source_cell_types",
  },
}, null, 2));
