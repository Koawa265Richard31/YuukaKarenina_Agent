#!/usr/bin/env node
// 修复被双写 fork 的 DSH 会话日志：
//   1) 剔除"向后跳变"的异流碎片行（保留主时间线）
//   2) 主时间线事件重编号 seq = 序号
//   3) 修 sourceEventSeqs 引用（落在剔除区间的引用丢弃，其余下移）
//   4) packChunkRuns 重打包 -> zstd -> 原子替换 -> 用 DSH 解码器自校验
// 用法: node dsh-session-repair.mjs <workspace> <sessionId> [dshHome]
import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { decodeStorageRecord, packChunkRuns } from "/srv/dsh/node_modules/@deepseek-ai/dsh-session/lib/index.js";

const [workspace, sessionId, dshHome = "/root/.dsh"] = process.argv.slice(2);
if (!workspace || !sessionId) {
  console.error("usage: dsh-session-repair.mjs <workspace> <sessionId> [dshHome]");
  process.exit(2);
}

const ZSTD_MAGIC = 4247762216;
const CHECKSUM = { params: { [zlib.constants.ZSTD_c_checksumFlag]: 1 } };

function encodeSegment(raw) {
  if (raw === ".") return "~002E";
  if (raw === "..") return "~002E~002E";
  let out = "";
  for (let i = 0; i < raw.length; i++) {
    const code = raw.charCodeAt(i);
    const ch = String.fromCharCode(code);
    if (ch !== "~" && /^[A-Za-z0-9._-]$/.test(ch)) out += ch;
    else out += "~" + code.toString(16).toUpperCase().padStart(4, "0");
  }
  return out;
}
function projectKey(cwd) {
  let readable = "";
  let separatorRun = false;
  for (let i = 0; i < cwd.length; i++) {
    const code = cwd.charCodeAt(i);
    const ch = String.fromCharCode(code);
    if (ch === "/" || ch === "\\" || ch === ":") {
      if (!separatorRun) readable += "-";
      separatorRun = true;
    } else if (ch !== "~" && /^[A-Za-z0-9._-]$/.test(ch)) {
      readable += ch;
      separatorRun = false;
    } else {
      readable += "~" + code.toString(16).toUpperCase().padStart(4, "0");
      separatorRun = false;
    }
  }
  return `--${(readable.replace(/^-+/, "") || "root").slice(0, 251)}--`;
}
function scanZstdFrames(buffer) {
  const frames = [];
  let offset = 0;
  while (offset < buffer.length) {
    const start = offset;
    if (buffer.length - offset < 4) return { frames, tornStart: start };
    if (buffer.readUInt32LE(offset) !== ZSTD_MAGIC) return { frames, tornStart: start };
    offset += 4;
    if (offset === buffer.length) return { frames, tornStart: start };
    const descriptor = buffer.readUInt8(offset);
    offset += 1;
    const contentSizeFlag = descriptor >>> 6;
    const singleSegment = (descriptor & 32) !== 0;
    const checksum = (descriptor & 4) !== 0;
    const dictionaryFlag = descriptor & 3;
    const dictionaryBytes = dictionaryFlag === 3 ? 4 : dictionaryFlag;
    const contentSizeBytes = contentSizeFlag === 0 ? (singleSegment ? 1 : 0) : (1 << contentSizeFlag);
    const remainingHeaderBytes = (singleSegment ? 0 : 1) + dictionaryBytes + contentSizeBytes;
    if (buffer.length - offset < remainingHeaderBytes) return { frames, tornStart: start };
    offset += remainingHeaderBytes;
    for (;;) {
      if (buffer.length - offset < 3) return { frames, tornStart: start };
      const blockHeader = buffer.readUIntLE(offset, 3);
      offset += 3;
      const lastBlock = (blockHeader & 1) !== 0;
      const blockType = (blockHeader >>> 1) & 3;
      const blockSize = blockHeader >>> 3;
      const payloadBytes = blockType === 1 ? 1 : blockSize;
      if (buffer.length - offset < payloadBytes) return { frames, tornStart: start };
      offset += payloadBytes;
      if (lastBlock) break;
    }
    if (checksum) {
      if (buffer.length - offset < 4) return { frames, tornStart: start };
      offset += 4;
    }
    frames.push({ start, end: offset });
  }
  return { frames };
}
function decompress(raw) {
  const { frames } = scanZstdFrames(raw);
  if (frames.length === 0) return raw.toString("utf8");
  let out = "";
  for (const fr of frames) out += zlib.zstdDecompressSync(raw.subarray(fr.start, fr.end)).toString("utf8");
  return out;
}

const dir = path.join(dshHome, "sessions", projectKey(workspace), encodeSegment(sessionId));
const zstdPath = path.join(dir, "session.jsonl.zstd");
const jsonlPath = path.join(dir, "session.jsonl");
const srcPath = fs.existsSync(zstdPath) ? zstdPath : jsonlPath;
if (!fs.existsSync(srcPath)) {
  console.error("log not found:", srcPath);
  process.exit(2);
}

// ── 读 + 解析 ──
const raw = fs.readFileSync(srcPath);
const text = srcPath.endsWith(".zstd") ? decompress(raw) : raw.toString("utf8");
const lines = text.split("\n").filter((l) => l.trim() !== "");
const header = lines[0];
const rows = [];
let parseFail = 0;
for (let i = 1; i < lines.length; i++) {
  try {
    rows.push({ line: i + 1, events: decodeStorageRecord(JSON.parse(lines[i])) });
  } catch {
    parseFail++; // 尾部撕裂行等，直接丢弃
  }
}

// ── Pass1: 分类 keep/drop，收集被剔除的原始 seq ──
const keptRows = [];
const droppedSeqs = [];
let running = 0;
for (const row of rows) {
  const first = row.events[0];
  if (first && first.seq < running) {
    for (const ev of row.events) droppedSeqs.push(ev.seq);
  } else {
    keptRows.push(row);
    running += row.events.length;
  }
}
droppedSeqs.sort((a, b) => a - b);
function droppedBelow(ref) {
  let lo = 0, hi = droppedSeqs.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (droppedSeqs[mid] < ref) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}
function inDropped(ref) {
  let lo = 0, hi = droppedSeqs.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (droppedSeqs[mid] === ref) return true;
    if (droppedSeqs[mid] < ref) lo = mid + 1;
    else hi = mid - 1;
  }
  return false;
}

// ── Pass2: 重编号 + 修引用 ──
const keptEvents = [];
let idx = 0;
for (const row of keptRows) {
  for (const ev of row.events) {
    if (Array.isArray(ev.sourceEventSeqs)) {
      const refs = [];
      for (const r of ev.sourceEventSeqs) {
        if (inDropped(r)) continue;
        const adj = r - droppedBelow(r);
        if (adj >= idx || adj < 0) continue;
        refs.push(adj);
      }
      if (refs.length > 0) ev.sourceEventSeqs = refs;
      else delete ev.sourceEventSeqs;
    }
    ev.seq = idx++;
  }
}
keptEvents.sort((a, b) => a.seq - b.seq);

// ── 重打包写盘 ──
const records = packChunkRuns(keptEvents);
const body = records.map((r) => JSON.stringify(r)).join("\n") + "\n";
const newText = header + "\n" + body;
const outBuf = srcPath.endsWith(".zstd")
  ? zlib.zstdCompressSync(Buffer.from(newText, "utf8"), CHECKSUM)
  : Buffer.from(newText, "utf8");
const bak = `${srcPath}.bak-${Date.now()}`;
fs.copyFileSync(srcPath, bak);
const tmp = `${srcPath}.repair-tmp`;
fs.writeFileSync(tmp, outBuf);
fs.renameSync(tmp, srcPath);

// ── 自校验（用 DSH 解码器完整重读）──
const check = decompress(fs.readFileSync(srcPath));
let ok = true, events = 0, started = false, issue = "";
for (const l of check.split("\n")) {
  if (!l.trim()) continue;
  let decoded;
  try { decoded = decodeStorageRecord(JSON.parse(l)); } catch { continue; }
  if (!started) { started = true; continue; }
  for (const ev of decoded) {
    if (ev.seq !== events) {
      ok = false;
      issue = `seq 断档（期望 ${events}，实际 ${ev.seq}）`;
      break;
    }
    events++;
  }
  if (!ok) break;
}
console.log(
  JSON.stringify({
    sessionId,
    workspace,
    droppedLines: rows.length - keptRows.length,
    droppedEvents: droppedSeqs.length,
    parseFail,
    finalEvents: events,
    verify: ok ? "READ OK" : `CORRUPT: ${issue}`,
    backup: bak,
  }),
);
process.exit(ok ? 0 : 1);
