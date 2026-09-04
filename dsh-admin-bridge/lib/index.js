import http from "node:http";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import z from "@deepseek-ai/schemastery";
import { installModelSelection } from "@deepseek-ai/dsh-agent";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { SessionId, decodeStorageRecord } from "@deepseek-ai/dsh-session";

/**
 * dsh-admin-bridge — bridges the REAL DSH (shared DSH_HOME) sessions.
 *
 * Endpoints (127.0.0.1, token-gated):
 *   GET  /health
 *   POST /v1/list     {token} -> [{workspace(cwd), sessions:[{id,title,live}]}]
 *   POST /v1/chat     {session_id, qq, workspace, title, text, token} -> submit async task
 *   POST /v1/stop     {session_id, token} -> cancel the running task
 *   POST /v1/add      {session_id, text, token} -> inject guidance
 *
 * Concurrency: before resuming, check for an open turn (turn/start without
 * turn/end) — if the web is running that session, reject the QQ task.
 */

const name = "dsh-admin-bridge";
const inject = ["agentDefaultModel", "agents", "sessions", "sessionQuery", "credentials"];

const Config = z.object({
  port: z.number().default(Number(process.env.DSH_ADMIN_BRIDGE_PORT ?? 63003)),
  host: z.string().default("127.0.0.1"),
  token: z.string().default(process.env.DSH_ADMIN_BRIDGE_TOKEN ?? ""),
  astrbotPushUrl: z
    .string()
    .default(process.env.DSH_ADMIN_BRIDGE_ASTRBOT_PUSH_URL ?? "http://127.0.0.1:6200/dsh/send"),
});

function hasOpenTurn(events) {
  let open = false;
  for (const ev of events) {
    if (ev.type === "turn/start") open = true;
    else if (ev.type === "turn/end") open = false;
  }
  return open;
}

// ── 原始日志校验（防双写 fork：损坏时拒绝写入，避免加重）────────────────
import zlib from "node:zlib";

function encodeSegment(raw) {
  if (raw.length === 0) return "~";
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

const ZSTD_MAGIC = 4247762216;
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

function decompressLogText(raw) {
  const { frames } = scanZstdFrames(raw);
  if (frames.length === 0) return raw.toString("utf8");
  let out = "";
  for (const fr of frames) out += zlib.zstdDecompressSync(raw.subarray(fr.start, fr.end)).toString("utf8");
  return out;
}

/** 用 DSH 自己的解码器校验会话日志是否可完整读取（seq 连续）。 */
async function validateSessionLog(sessionId, workspace) {
  try {
    const home = process.env.DSH_HOME || "/root/.dsh";
    const dir = path.join(home, "sessions", projectKey(workspace), encodeSegment(sessionId));
    const candidates = ["session.jsonl.zstd", "session.jsonl"];
    for (const name of candidates) {
      const p = path.join(dir, name);
      if (!fs.existsSync(p)) continue;
      const raw = fs.readFileSync(p);
      let text;
      try {
        text = name.endsWith(".zstd") ? decompressLogText(raw) : raw.toString("utf8");
      } catch (e) {
        return { ok: false, issue: `日志解压失败: ${e?.message ?? e}` };
      }
      const lines = text.split("\n");
      let events = 0;
      let started = false;
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (!line.trim()) continue;
        let decoded;
        try {
          decoded = decodeStorageRecord(JSON.parse(line));
        } catch {
          if (i === lines.length - 1) break; // 尾部撕裂行（写入中），忽略
          return { ok: false, issue: `第 ${i + 1} 行无法解析` };
        }
        if (!started) {
          started = true;
          continue; // 第一行 header
        }
        for (const ev of decoded) {
          if (ev.seq !== events) {
            return { ok: false, issue: `seq 断档（期望 ${events}，实际 ${ev.seq}）——双写 fork` };
          }
          events += 1;
        }
      }
      return { ok: true, events };
    }
    return { ok: false, issue: "找不到会话日志文件" };
  } catch (e) {
    return { ok: false, issue: `校验异常: ${e?.message ?? e}` };
  }
}

function summarize(events, firstSeq) {
  let started = false;
  let text = "";
  for (const ev of events) {
    if (ev.seq < firstSeq) continue;
    if (ev.type === "turn/start") {
      started = true;
      continue;
    }
    if (!started) continue;
    if (ev.type === "assistant/message") {
      const joined = ev.data.message.content
        .filter((b) => b.type === "text")
        .map((b) => b.text)
        .join("");
      if (joined !== "") text = joined;
    }
  }
  return text;
}

function lastTurnStartSeq(events) {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    if (events[i].type === "turn/start") return events[i].seq;
  }
  return 0;
}

// ── 余额 + 每轮消耗（与 DSH 小鲸鱼挂件同款峰谷定价）────────────────────────
const PEAK_HOURS = [
  [9, 12],
  [14, 18],
];
const BASE_PRICE = { hit: [0.05, 0.1], miss: [1.5, 3.0], out: [4.5, 9.0] };
const PRO_PRICE = { hit: [0.15, 0.3], miss: [4.5, 9.0], out: [13.5, 27.0] };
const WEEKEND_VALLEY_FROM_SEC = Math.floor(Date.UTC(2026, 7, 22, 16, 0, 0) / 1000);

function priceFor(model) {
  const m = String(model || "").toLowerCase();
  if (m.indexOf("deepseek-v4-pro") !== -1) return PRO_PRICE;
  return BASE_PRICE;
}

function isPeakTime(timeSec) {
  if (!isFinite(Number(timeSec))) return false;
  const n = Number(timeSec);
  const bj = new Date(n * 1000 + 8 * 3600 * 1000);
  if (n >= WEEKEND_VALLEY_FROM_SEC) {
    const dow = bj.getUTCDay(); // 0=周日 6=周六（bj 按 UTC 读即北京日历日）
    if (dow === 0 || dow === 6) return false;
  }
  const hour = bj.getUTCHours();
  for (const [start, end] of PEAK_HOURS) {
    if (hour >= start && hour < end) return true;
  }
  return false;
}

// 从本轮（firstSeq 起）的 assistant/message 事件聚合真实 usage，换算成金额
function computeTurnCost(events, firstSeq) {
  let started = false;
  let cost = 0;
  let tokens = 0;
  for (const ev of events) {
    if (ev.seq < firstSeq) continue;
    if (ev.type === "turn/start") {
      started = true;
      continue;
    }
    if (!started) continue;
    if (ev.type !== "assistant/message") continue;
    const usage = ev.data && ev.data.usage;
    if (!usage || typeof usage !== "object") continue;
    const input = Number(usage.inputTokens) || 0;
    const cache = Number(usage.cacheReadTokens) || 0;
    const output = Number(usage.outputTokens) || 0;
    const reasoning = Number(usage.reasoningTokens) || 0;
    tokens += input + cache + output + reasoning;
    const model = ev.data.message && ev.data.message.source ? ev.data.message.source.model : "";
    const p = priceFor(model);
    const off = isPeakTime(Math.floor(Date.now() / 1000)) ? 1 : 0;
    cost +=
      (cache / 1e6) * p.hit[off] +
      (input / 1e6) * p.miss[off] +
      ((output + reasoning) / 1e6) * p.out[off];
  }
  return { cost, tokens };
}

function fmtMoney(n) {
  if (!isFinite(n)) return "--";
  const s = n.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  return "¥" + s;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function json(res, code, obj) {
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(obj));
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (c) => {
      data += c;
      if (data.length > 1e7) {
        reject(new Error("payload too large"));
        req.destroy();
      }
    });
    req.on("end", () => {
      try {
        resolve(JSON.parse(data || "{}"));
      } catch (e) {
        reject(e);
      }
    });
    req.on("error", reject);
  });
}

function apply(ctx, config) {
  const liveAgents = new Map(); // sessionId -> {handle, qq, workspace, title, firstSeq}

  // —— 并发护栏：会话文件最近被「本桥以外」写入时拒绝任务，防止双进程 fork 损坏日志 ——
  let fileStatsCache = { at: 0, map: new Map() }; // id -> {mtime, size}
  function sessionFileStats() {
    const now = Date.now();
    if (now - fileStatsCache.at < 20000) return fileStatsCache.map;
    const dshHome = process.env.DSH_HOME || path.join(os.homedir(), ".dsh");
    const root = path.join(dshHome, "sessions");
    const map = new Map();
    try {
      for (const wsDir of fs.readdirSync(root)) {
        const wsPath = path.join(root, wsDir);
        let wst;
        try {
          wst = fs.statSync(wsPath);
        } catch {
          continue;
        }
        if (!wst.isDirectory()) continue;
        for (const sDir of fs.readdirSync(wsPath)) {
          const sp = path.join(wsPath, sDir);
          let sst;
          try {
            sst = fs.statSync(sp);
          } catch {
            continue;
          }
          if (!sst.isDirectory()) continue;
          let mtime = sst.mtimeMs;
          try {
            mtime = fs.statSync(path.join(sp, "session.jsonl.zstd")).mtimeMs;
          } catch {}
          map.set(sDir, { mtime, size: sst.size });
        }
      }
    } catch (e) {
      console.error("[dsh-admin-bridge] session file scan failed:", e.message);
    }
    fileStatsCache = { at: now, map };
    return map;
  }

  const lastTaskAt = new Map(); // sessionId -> 本桥最后一次跑任务的时间
  function guardActiveElsewhere(sessionId) {
    const last = lastTaskAt.get(sessionId) ?? 0;
    if (Date.now() - last < 300000) return null; // 本桥最近跑过，放行
    const st = sessionFileStats().get(sessionId);
    if (st && Date.now() - st.mtime < 300000) {
      return "该会话最近 5 分钟有活动（网页可能正在使用它），为避免会话日志损坏请稍后再试";
    }
    return null;
  }

  // 审批回流：answerer + 待决表（按 sessionId 键，一次一个待决审批）
  const pendingApprovals = new Map(); // sessionId -> settle(outcome)
  if (ctx.get("approval") !== undefined) {
    ctx.on("approval/request", (req, next) => {
      if (req.signal?.aborted === true) return Promise.resolve("cancelled");
      const sessionId = String(req.agent?.session?.id ?? "");
      const rec = liveAgents.get(sessionId);
      if (!rec) return next();
      const text = req.reason ?? `工具 ${req.toolName ?? ""} 请求授权，是否批准？`;
      return new Promise((resolve) => {
        let settled = false;
        const settle = (outcome) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          pendingApprovals.delete(sessionId);
          resolve(outcome);
        };
        const timer = setTimeout(() => settle("rejected"), 120000); // 超时 fail-closed 不阻塞
        pendingApprovals.set(sessionId, settle);
        pushResult(rec.qq, rec.workspace, sessionId, `🔐 审批请求：${text}\n\n回复「批准」或「拒绝」`);
      });
    });
  }

  // 提问回流：userQuestions provider + 待答表（按 sessionId 键）
  const pendingQuestions = new Map(); // sessionId -> {resolve, reject, questions, timer}
  if (ctx.get("userQuestions") !== undefined) {
    const disposeProvider = ctx.get("userQuestions").registerProvider({
      ask(request) {
        const sessionId = String(request.agent?.id ?? "");
        const rec = liveAgents.get(sessionId);
        if (!rec) return Promise.reject(new Error("no live task for this session"));
        return new Promise((resolve, reject) => {
          const lines = ["❓ DSH 提问："];
          for (const q of request.questions ?? []) {
            lines.push(`· ${q.question}`);
            for (const o of q.options ?? []) lines.push(`    ${o.label}`);
          }
          lines.push("\n回复 /dsh <ws> <session> ask <选项或内容>");
          pushResult(rec.qq, rec.workspace, sessionId, lines.join("\n"));
          const timer = setTimeout(() => {
            pendingQuestions.delete(sessionId);
            reject(new Error("ask_user_question 等待超时"));
          }, 120000);
          pendingQuestions.set(sessionId, { resolve, reject, questions: request.questions ?? [], timer });
        });
      },
    });
    ctx.effect(() => () => {
      disposeProvider();
      for (const p of pendingQuestions.values()) {
        clearTimeout(p.timer);
        p.reject(new Error("provider disposed"));
      }
    });
  }

  async function resumeAgent(sessionId) {
    await ctx.get("loader")?.await();
    const selection = ctx.get("agentDefaultModel").currentSelection();
    if (!selection) throw new Error("no default model selection configured");
    const agentOptions = { provider: selection.provider, model: selection.model };
    const setup = (agentCtx) => {
      installModelSelection(agentCtx, { current: selection, assembled: undefined });
    };
    return ctx.get("agents").resume({
      resumeSessionId: SessionId(sessionId),
      agentOptions,
      setup,
    });
  }

  function pushResult(qq, workspace, sessionId, reply) {
    const text = `${reply}\n\n\n/dsh ${workspace} ${sessionId}`;
    fetch(config.astrbotPushUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "notify", qq, text, token: config.token }),
    }).catch((e) => console.error("[dsh-admin-bridge] push result failed:", e.message));
  }

  // 余额缓存：避免每轮都打余额接口（30s TTL，瞬时失败沿用旧值）
  let balanceCache = { at: 0, payload: null };
  async function fetchBalance() {
    const now = Date.now();
    if (balanceCache.payload && now - balanceCache.at < 30000) return balanceCache.payload;
    let key = "";
    try {
      const cred = await ctx.get("credentials").resolve("DEEPSEEK_API_KEY");
      if (cred) key = cred.value;
    } catch {}
    if (!key) return balanceCache.payload;
    try {
      const res = await fetch("https://api.deepseek.com/user/balance", {
        headers: { Authorization: "Bearer " + key },
        signal: AbortSignal.timeout(8000),
      });
      if (!res.ok) return balanceCache.payload;
      const data = await res.json();
      const infos = Array.isArray(data && data.balance_infos) ? data.balance_infos : [];
      const info =
        infos.find((x) => x && x.currency === "CNY" && Number(x.total_balance) > 0) ||
        infos.find((x) => Number(x.total_balance) > 0) ||
        infos[0];
      if (!info || info.total_balance === undefined) return balanceCache.payload;
      const payload = { balance: Number(info.total_balance), currency: String(info.currency || "CNY") };
      balanceCache = { at: now, payload };
      return payload;
    } catch {
      return balanceCache.payload;
    }
  }

  // 拼上「余额 · 本轮消耗」；仅在推送时拼接，绝不回灌进 agent 上下文
  async function costLine(events, firstSeq) {
    const { cost } = computeTurnCost(events, firstSeq);
    const bal = await fetchBalance();
    const parts = [];
    if (bal && isFinite(bal.balance)) parts.push(`余额 ${fmtMoney(bal.balance)}`);
    parts.push(`本轮消耗 ${fmtMoney(cost)}`);
    return `💰 ${parts.join(" · ")}`;
  }

  async function handleList() {
    const records = await ctx.get("sessionQuery").listSessions();
    const ids = records.map((r) => r.header.id);
    // 会话标题：优先读 web 界面的投影缓存（用户在 web 里重命名会话只写这里，
    // readTitleSnapshots 看不到改名）；缓存缺失的会话才走慢路径补标题。
    let titles = new Map();
    const dshHome = process.env.DSH_HOME || path.join(os.homedir(), ".dsh");
    try {
      const pc = JSON.parse(
        fs.readFileSync(path.join(dshHome, "storages", "session_projcache.json"), "utf8"),
      );
      const table = pc?.tables?.sessions ?? {};
      for (const [sid, v] of Object.entries(table)) {
        const t = v?.rows?.title?.val;
        if (typeof t === "string" && t) titles.set(sid, t);
      }
    } catch (e) {
      console.error("[dsh-admin-bridge] projcache title scan failed:", e.message);
    }
    const missingIds = ids.filter((id) => !titles.has(id));
    if (missingIds.length > 0) {
      try {
        const snapshots = await ctx.get("sessionQuery").readTitleSnapshots(missingIds);
        snapshots.forEach((s, i) => {
          if (s?.status === "fulfilled" && s.value?.title) {
            const t = typeof s.value.title === "string" ? s.value.title : s.value.title?.title;
            if (t) titles.set(missingIds[i], t);
          }
        });
      } catch (e) {
        console.error("[dsh-admin-bridge] title snapshot failed:", e.message);
      }
    }
    // 会话「最近修改时间」：直接读会话存储目录的 mtime，用于把当前/最近会话排前面
    const sessionsRoot = path.join(dshHome, "sessions");
    const mtimeById = new Map();
    try {
      for (const wsDir of fs.readdirSync(sessionsRoot)) {
        const wsPath = path.join(sessionsRoot, wsDir);
        let wst;
        try {
          wst = fs.statSync(wsPath);
        } catch {
          continue;
        }
        if (!wst.isDirectory()) continue;
        for (const sDir of fs.readdirSync(wsPath)) {
          const sp = path.join(wsPath, sDir);
          let sst;
          try {
            sst = fs.statSync(sp);
          } catch {
            continue;
          }
          if (!sst.isDirectory()) continue;
          // 用会话数据文件（session.jsonl.zstd）的 mtime 作为「最近活动时间」，
          // 目录 mtime 只在增删文件时变化，不反映最近一次 flush。
          let mtime = sst.mtimeMs;
          try {
            mtime = fs.statSync(path.join(sp, "session.jsonl.zstd")).mtimeMs;
          } catch {}
          mtimeById.set(sDir, mtime);
        }
      }
    } catch (e) {
      console.error("[dsh-admin-bridge] session mtime scan failed:", e.message);
    }

    // 工作区标题：web 界面里用户命名的名字（如「dsh和astrbot」），list 里一并展示
    const wsTitleByPath = new Map();
    try {
      const wsJson = JSON.parse(
        fs.readFileSync(path.join(dshHome, "storages", "workspace.json"), "utf8"),
      );
      const table = wsJson?.tables?.workspaces ?? {};
      for (const v of Object.values(table)) {
        if (v && typeof v.path === "string" && typeof v.title === "string" && v.title) {
          wsTitleByPath.set(v.path, v.title);
        }
      }
    } catch (e) {
      console.error("[dsh-admin-bridge] workspace title scan failed:", e.message);
    }

    const byCwd = new Map();
    for (const r of records) {
      const cwd = r.header.cwd ?? "(无工作区)";
      if (!byCwd.has(cwd)) byCwd.set(cwd, []);
      byCwd.get(cwd).push({
        id: r.header.id,
        title: titles.get(r.header.id) ?? "",
        live: r.live,
        mtime: mtimeById.get(r.header.id) ?? 0,
      });
    }
    const workspaces = [...byCwd.entries()].map(([workspace, sessions]) => {
      sessions.sort((a, b) => (b.mtime ?? 0) - (a.mtime ?? 0) || a.id.localeCompare(b.id));
      return { workspace, title: wsTitleByPath.get(workspace) ?? "", sessions };
    });
    workspaces.sort(
      (a, b) =>
        (b.sessions[0]?.mtime ?? 0) - (a.sessions[0]?.mtime ?? 0) ||
        a.workspace.localeCompare(b.workspace),
    );
    return workspaces;
  }

  // —— 排队派送：会话被网页占用时，QQ 任务排队，空闲后自动执行并推结果 ——
  const taskQueue = new Map(); // sessionId -> [{qq, workspace, title, text}]
  const queuePollers = new Set(); // 正在轮询的 sessionId
  const QUEUE_MAX_ATTEMPTS = 60; // 最多等 30 分钟

  function pumpQueue(sessionId) {
    if (queuePollers.has(sessionId)) return;
    queuePollers.add(sessionId);
    (async () => {
      try {
        for (;;) {
          const q = taskQueue.get(sessionId);
          if (!q || q.length === 0) break;
          const t = q[0];
          const status = await tryRunTask(t);
          if (status.busy) {
            if (status.attempts >= QUEUE_MAX_ATTEMPTS) {
              q.shift();
              pushResult(t.qq, t.workspace, sessionId, `[排队任务超时] ${status.note ?? "网页一直在使用该会话"}`);
              continue;
            }
            await sleep(30000);
            continue;
          }
          q.shift();
          if (!status.ok) {
            pushResult(t.qq, t.workspace, sessionId, `[排队任务失败] ${status.error}`);
            break;
          }
        }
      } finally {
        queuePollers.delete(sessionId);
        if (taskQueue.get(sessionId)?.length === 0) taskQueue.delete(sessionId);
      }
    })();
  }

  /** 尝试真正执行一个 QQ 任务；返回 {ok} | {busy, note}。 */
  async function tryRunTask(t) {
    const { sessionId, qq, text, workspace, title } = t;

    // 并发护栏：其他进程最近在写这个会话 -> 视为繁忙，排队等待
    const guardErr = guardActiveElsewhere(sessionId);
    if (guardErr) return { busy: true, note: guardErr };

    // 日志完整性护栏：损坏（双写 fork）时绝不写入，防止加重
    const logCheck = await validateSessionLog(sessionId, workspace);
    if (!logCheck.ok) {
      return {
        ok: false,
        error: `会话日志校验失败（疑似双写损坏，已停止派发防加重）: ${logCheck.issue}。请先在网页端重新打开该会话（或等待修复）后再试`,
      };
    }

    // 并发保护：网页正在跑 -> 排队等待
    try {
      const { events } = await ctx.get("sessionQuery").readSession(sessionId);
      if (hasOpenTurn(events)) {
        return { busy: true, note: "该会话网页正在运行任务" };
      }
    } catch (e) {
      // 读不了（如正在修复日志）：继续等待，不视为最终失败
      return { busy: true, note: `会话暂不可读: ${String(e?.message ?? e).slice(0, 60)}` };
    }

    let handle;
    try {
      handle = await resumeAgent(sessionId);
      await handle.agent.whenIdle();
    } catch (e) {
      return { ok: false, error: `续接会话失败: ${e?.message ?? e}` };
    }

    const firstSeq = handle.agent.session.seq;
    handle.agent.followup(
      createUserMessage({
        content: [{ type: "text", text }],
        source: { kind: "user" },
      }),
    );
    liveAgents.set(sessionId, { handle, qq, workspace, title, firstSeq });

    // 异步：等任务跑完 -> flush -> 汇总 -> 推 astrbot -> 释放
    (async () => {
      try {
        await handle.agent.whenIdle();
        await ctx.get("sessions").flush(handle.agent.session);
        lastTaskAt.set(sessionId, Date.now());
        const afterCheck = await validateSessionLog(sessionId, workspace);
        if (!afterCheck.ok) {
          pushResult(
            qq,
            workspace,
            sessionId,
            `[警告] 任务已执行，但会话日志出现双写损坏（${afterCheck.issue}）。网页端重新打开该会话可恢复；请避免在网页打开该会话时用 QQ 派任务`,
          );
          return;
        }
        const reply = summarize(handle.agent.session.events, firstSeq) || "(无回复)";
        const line = await costLine(handle.agent.session.events, firstSeq);
        pushResult(qq, workspace, sessionId, `${reply}\n\n${line}`);
      } catch (e) {
        pushResult(qq, workspace, sessionId, `[任务异常] ${e?.message ?? e}`);
      } finally {
        liveAgents.delete(sessionId);
        try {
          await handle.dispose();
        } catch {}
      }
    })();
    return { ok: true };
  }

  async function handleChat(body) {
    const sessionId = String(body.session_id ?? "");
    const qq = String(body.qq ?? "");
    const text = String(body.text ?? "");
    const workspace = String(body.workspace ?? "");
    const title = String(body.title ?? sessionId);
    if (!sessionId || !qq || !text) return { ok: false, error: "missing session_id/qq/text" };

    const t = { sessionId, qq, text, workspace, title };
    const status = await tryRunTask(t);
    if (status.busy) {
      const attempts = (taskQueue.get(sessionId) ?? []).length;
      enqueueTask(sessionId, t);
      return {
        ok: true,
        queued: true,
        note: `🔔 ${status.note}。任务已排队${attempts > 0 ? `（前面还有 ${attempts} 个）` : ""}，网页空闲后自动执行并推送结果`,
      };
    }
    if (!status.ok) return { ok: false, error: status.error };
    return { ok: true, note: "任务已发布" };
  }

  function enqueueTask(sessionId, t) {
    if (!taskQueue.has(sessionId)) taskQueue.set(sessionId, []);
    taskQueue.get(sessionId).push(t);
    pumpQueue(sessionId);
  }

  async function handleStop(body) {
    const sessionId = String(body.session_id ?? "");
    const rec = liveAgents.get(sessionId);
    if (!rec) return { ok: false, error: "该会话当前没有正在运行的任务" };
    rec.handle.agent.cancel({ kind: "user" });
    return { ok: true, note: "已请求停止" };
  }

  async function handleAdd(body) {
    const sessionId = String(body.session_id ?? "");
    const text = String(body.text ?? "");
    if (!sessionId || !text) return { ok: false, error: "missing session_id/text" };
    const guardErr = guardActiveElsewhere(sessionId);
    if (guardErr) return { ok: false, error: guardErr };
    const rec = liveAgents.get(sessionId);
    if (rec) {
      rec.handle.agent.inject(
        createUserMessage({ content: [{ type: "text", text }], source: { kind: "user" } }),
      );
      return { ok: true, note: "已注入引导" };
    }
    // 无活动 agent：临时续接注入后立即 flush + 释放
    let handle;
    try {
      handle = await resumeAgent(sessionId);
      await handle.agent.whenIdle();
      handle.agent.inject(
        createUserMessage({ content: [{ type: "text", text }], source: { kind: "user" } }),
      );
      await ctx.get("sessions").flush(handle.agent.session);
      return { ok: true, note: "已追加引导" };
    } catch (e) {
      return { ok: false, error: `追加失败: ${e?.message ?? e}` };
    } finally {
      try {
        await handle?.dispose();
      } catch {}
    }
  }

  async function handleSub(body) {
    const sessionId = String(body.session_id ?? "");
    const qq = String(body.qq ?? "");
    const workspace = String(body.workspace ?? "");
    const title = String(body.title ?? sessionId);
    if (!sessionId || !qq) return { ok: false, error: "missing session_id/qq" };

    let events;
    try {
      ({ events } = await ctx.get("sessionQuery").readSession(sessionId));
    } catch (e) {
      return { ok: false, error: `会话不可用: ${e?.message ?? e}` };
    }

    if (hasOpenTurn(events)) {
      const firstSeq = lastTurnStartSeq(events);
      (async () => {
        try {
          for (;;) {
            await sleep(3000);
            const cur = await ctx.get("sessionQuery").readSession(sessionId);
            if (!hasOpenTurn(cur.events)) {
              const reply = summarize(cur.events, firstSeq) || "(无回复)";
              const line = await costLine(cur.events, firstSeq);
              pushResult(qq, workspace, sessionId, `${reply}\n\n${line}`);
              break;
            }
          }
        } catch (e) {
          pushResult(qq, workspace, sessionId, `[订阅失败] ${e?.message ?? e}`);
        }
      })();
      return { ok: true, note: "已订阅，任务完成后推送结果" };
    }

    // 空闲：返回上一轮 turn 的最终回复
    const firstSeq = lastTurnStartSeq(events);
    const reply = summarize(events, firstSeq) || "(无回复)";
    const line = await costLine(events, firstSeq);
    return { ok: true, reply: `${reply}\n\n${line}` };
  }

  const server = http.createServer(async (req, res) => {
    const checkToken = (t) => !config.token || t === config.token;
    try {
      if (req.method === "GET" && req.url === "/health") return json(res, 200, { ok: true });
      if (req.method === "POST" && req.url === "/v1/list") {
        const body = await readBody(req);
        if (!checkToken(body.token)) return json(res, 401, { error: "unauthorized" });
        return json(res, 200, { ok: true, list: await handleList() });
      }
      if (req.method === "POST" && req.url === "/v1/chat") {
        const body = await readBody(req);
        if (!checkToken(body.token)) return json(res, 401, { error: "unauthorized" });
        return json(res, 200, await handleChat(body));
      }
      if (req.method === "POST" && req.url === "/v1/stop") {
        const body = await readBody(req);
        if (!checkToken(body.token)) return json(res, 401, { error: "unauthorized" });
        return json(res, 200, await handleStop(body));
      }
      if (req.method === "POST" && req.url === "/v1/add") {
        const body = await readBody(req);
        if (!checkToken(body.token)) return json(res, 401, { error: "unauthorized" });
        return json(res, 200, await handleAdd(body));
      }
      if (req.method === "POST" && req.url === "/v1/sub") {
        const body = await readBody(req);
        if (!checkToken(body.token)) return json(res, 401, { error: "unauthorized" });
        return json(res, 200, await handleSub(body));
      }
      if (req.method === "POST" && req.url === "/v1/commands") {
        const body = await readBody(req);
        if (!checkToken(body.token)) return json(res, 401, { error: "unauthorized" });
        const svc = ctx.get("commands");
        const list = svc ? svc.list() : [];
        return json(res, 200, {
          ok: true,
          commands: list.map((c) => ({ name: c.name, description: c.description ?? "" })),
        });
      }
      if (req.method === "POST" && req.url === "/v1/approve") {
        const body = await readBody(req);
        if (!checkToken(body.token)) return json(res, 401, { error: "unauthorized" });
        const settle = pendingApprovals.get(String(body.session_id ?? ""));
        if (!settle) return json(res, 200, { ok: false, error: "该会话没有待处理的审批" });
        settle("allowed-once");
        return json(res, 200, { ok: true });
      }
      if (req.method === "POST" && req.url === "/v1/reject") {
        const body = await readBody(req);
        if (!checkToken(body.token)) return json(res, 401, { error: "unauthorized" });
        const settle = pendingApprovals.get(String(body.session_id ?? ""));
        if (!settle) return json(res, 200, { ok: false, error: "该会话没有待处理的审批" });
        settle("rejected");
        return json(res, 200, { ok: true });
      }
      if (req.method === "POST" && req.url === "/v1/ask") {
        const body = await readBody(req);
        if (!checkToken(body.token)) return json(res, 401, { error: "unauthorized" });
        const pending = pendingQuestions.get(String(body.session_id ?? ""));
        if (!pending) return json(res, 200, { ok: false, error: "该会话没有待回答的提问" });
        const answer = String(body.answer ?? "").trim();
        const answers = pending.questions.map((q) => {
          const opts = q.options ?? [];
          const matched = opts.find((o) => o.label.toLowerCase() === answer.toLowerCase());
          const idx = parseInt(answer, 10);
          const byIndex = !Number.isNaN(idx) && idx >= 1 && idx <= opts.length ? opts[idx - 1] : undefined;
          const chosen = matched ?? byIndex;
          return { id: q.id, ...(chosen ? { selected: [chosen.label] } : { custom: answer }) };
        });
        clearTimeout(pending.timer);
        pendingQuestions.delete(String(body.session_id ?? ""));
        pending.resolve({ answers });
        return json(res, 200, { ok: true });
      }
      return json(res, 404, { error: "not found" });
    } catch (e) {
      return json(res, 500, { error: String(e?.message ?? e) });
    }
  });

  server.listen(config.port, config.host, () => {
    console.error(`[dsh-admin-bridge] listening on ${config.host}:${config.port}`);
  });

  ctx.effect(() => () => {
    server.close();
  });
}

export { name, inject, Config, apply };
