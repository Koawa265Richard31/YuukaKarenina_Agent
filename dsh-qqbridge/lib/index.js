import http from "node:http";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import z from "@deepseek-ai/schemastery";
import { installModelSelection } from "@deepseek-ai/dsh-agent";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { SessionId } from "@deepseek-ai/dsh-session";

/**
 * dsh-qqbridge — HTTP bridge for the AstrBot plugin.
 *
 * Endpoints (127.0.0.1 only, token-gated):
 *   GET  /health       liveness
 *   POST /v1/chat      {user_id, text, level, token} -> run one agent turn -> {ok, reply}
 *   POST /v1/auth      {user_id, mode: upgrade|downgrade, token} -> record level switch
 *
 * Sandbox: the profile is launched with DSH_PERMISSION_MODE=read-only, so every
 * session fails closed to read-only. level>=2 flips the session to
 * workspace-write via ctx.sandboxPolicy.setSandboxMode() on the next turn.
 */

const name = "dsh-qqbridge";
const inject = ["agentDefaultModel", "agents", "sessions", "sandboxPolicy"];

const Config = z.object({
  port: z.number().default(Number(process.env.DSH_QQBRIDGE_PORT ?? 63002)),
  host: z.string().default(process.env.DSH_QQBRIDGE_HOST ?? "127.0.0.1"),
  token: z.string().default(process.env.DSH_QQBRIDGE_TOKEN ?? ""),
  workspaceRoot: z
    .string()
    .default(process.env.DSH_QQBRIDGE_WORKSPACE ?? process.cwd()),
  astrbotPushUrl: z
    .string()
    .default(process.env.DSH_QQBRIDGE_ASTRBOT_PUSH_URL ?? "http://127.0.0.1:6200/dsh/send"),
});

const DSH_HOME = process.env.DSH_HOME ?? path.join(os.homedir(), ".dsh");

function summarize(events, firstSeq) {
  let started = false;
  let text = "";
  let reason;
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
    if (ev.type === "turn/end") reason = ev.data.reason;
  }
  return { text, reason };
}

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
  const stateFile = path.join(DSH_HOME, "qqbridge-users.json");
  const loadState = () => {
    try {
      return JSON.parse(fs.readFileSync(stateFile, "utf8"));
    } catch {
      return { users: {} };
    }
  };
  const state = loadState();
  const saveState = () => {
    try {
      fs.mkdirSync(path.dirname(stateFile), { recursive: true });
      fs.writeFileSync(stateFile, JSON.stringify(state, null, 2));
    } catch (e) {
      console.error("[dsh-qqbridge] save state failed:", e.message);
    }
  };

  const locks = new Map(); // user_id -> Promise

  // 审批回流：answerer 瀑布监听 + 待决表。审批请求 → 推给 astrbot → QQ 用户答复 → /v1/approve 回填。
  const pendingApprovals = new Map(); // approvalId -> settle(outcome)
  if (ctx.get("approval") !== undefined) {
    ctx.on("approval/request", (req, next) => {
      if (req.signal?.aborted === true) return Promise.resolve("cancelled");
      const sessionId = String(req.agent?.session?.id ?? "");
      let userId = null;
      for (const [uid, rec] of Object.entries(state.users)) {
        if (String(rec.sessionId) === sessionId) {
          userId = uid;
          break;
        }
      }
      if (userId === null) return next();
      const rec = state.users[userId];
      const userLevel = rec?.appliedLevel || rec?.pendingLevel || 1;
      if (userLevel < 2) return Promise.resolve("rejected"); // 只读层禁止审批升级
      const approvalId = randomUUID();
      const text = req.reason ?? `工具 ${req.toolName ?? ""} 请求授权，是否批准？`;
      return new Promise((resolve) => {
        let settled = false;
        const settle = (outcome) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          pendingApprovals.delete(approvalId);
          resolve(outcome);
        };
        const timer = setTimeout(() => settle("rejected"), 120000); // 2 分钟无应答 fail-closed
        pendingApprovals.set(approvalId, settle);
        fetch(config.astrbotPushUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            kind: "approval",
            qq: userId,
            approval_id: approvalId,
            text,
            token: config.token,
          }),
        }).catch((e) => {
          console.error("[dsh-qqbridge] push approval failed:", e.message);
          settle("unavailable");
        });
      });
    });
  }

  async function withLock(userId, fn) {
    const prev = locks.get(userId) ?? Promise.resolve();
    const next = prev.then(fn, fn);
    locks.set(userId, next.catch(() => {}));
    try {
      return await next;
    } finally {
      if (locks.get(userId) === next) locks.delete(userId);
    }
  }

  async function runTurn(userId, text, level) {
    await ctx.get("loader")?.await();
    const agents = ctx.get("agents");
    const sessions = ctx.get("sessions");
    const defaultModel = ctx.get("agentDefaultModel");
    const sandboxPolicy = ctx.get("sandboxPolicy");
    if (!agents || !sessions || !defaultModel) {
      return { ok: false, error: "core services unavailable" };
    }

    const selection = defaultModel.currentSelection();
    if (!selection) {
      return { ok: false, error: "no default model selection configured" };
    }

    const agentOptions = { provider: selection.provider, model: selection.model };
    const setup = (agentCtx) => {
      installModelSelection(agentCtx, { current: selection, assembled: undefined });
    };

    let handle;
    let rec = state.users[userId];
    const isNew = !rec || !rec.sessionId;
    if (isNew) {
      const sid = SessionId(`session-${randomUUID()}`);
      handle = await agents.create({
        sessionId: sid,
        meta: { cwd: config.workspaceRoot },
        agentOptions,
        setup,
      });
      rec = { sessionId: sid, appliedLevel: 0, pendingLevel: 0 };
    } else {
      try {
        handle = await agents.resume({
          resumeSessionId: SessionId(rec.sessionId),
          agentOptions,
          setup,
        });
      } catch (e) {
        const sid = SessionId(`session-${randomUUID()}`);
        handle = await agents.create({
          sessionId: sid,
          meta: { cwd: config.workspaceRoot },
          agentOptions,
          setup,
        });
        rec = { sessionId: sid, appliedLevel: 0, pendingLevel: 0 };
      }
    }

    try {
      await handle.agent.whenIdle();
      const effectiveLevel =
        Number.isInteger(level) && level >= 1 ? level : rec.pendingLevel || 1;
      const wantMode = effectiveLevel >= 2 ? "workspace-write" : "read-only";
      if (rec.appliedLevel !== effectiveLevel) {
        handle.agent.session.append("sandbox/mode", { mode: wantMode });
        rec.appliedLevel = effectiveLevel;
        state.users[userId] = rec;
        saveState();
      }
      const firstSeq = handle.agent.session.seq;
      handle.agent.followup(
        createUserMessage({
          content: [{ type: "text", text }],
          source: { kind: "user" },
        }),
      );
      await handle.agent.whenIdle();
      await sessions.flush(handle.agent.session);
      const { text: reply, reason } = summarize(handle.agent.session.events, firstSeq);
      if (reason?.kind === "error") {
        return {
          ok: false,
          error: reason.error?.message ?? "turn error",
          reply,
        };
      }
      return { ok: true, reply };
    } finally {
      try {
        await handle.dispose();
      } catch {}
    }
  }

  const server = http.createServer(async (req, res) => {
    const checkToken = (t) => !config.token || t === config.token;
    try {
      if (req.method === "GET" && req.url === "/health") {
        return json(res, 200, { ok: true });
      }
      if (req.method === "POST" && req.url === "/v1/chat") {
        const body = await readBody(req);
        if (!checkToken(body.token)) return json(res, 401, { error: "unauthorized" });
        const userId = String(body.user_id ?? "");
        const text = String(body.text ?? "");
        const level = Number(body.level ?? 1);
        if (!userId || !text) return json(res, 400, { error: "missing user_id/text" });
        const out = await withLock(userId, () => runTurn(userId, text, level));
        return json(res, out.ok ? 200 : 500, out);
      }
      if (req.method === "POST" && req.url === "/v1/approve") {
        const body = await readBody(req);
        if (!checkToken(body.token)) return json(res, 401, { error: "unauthorized" });
        const id = String(body.approval_id ?? "");
        const settle = pendingApprovals.get(id);
        if (!settle) return json(res, 404, { error: "unknown approval" });
        settle(body.decision === "allow" ? "allowed-once" : "rejected");
        return json(res, 200, { ok: true });
      }
      if (req.method === "POST" && req.url === "/v1/auth") {
        const body = await readBody(req);
        if (!checkToken(body.token)) return json(res, 401, { error: "unauthorized" });
        const userId = String(body.user_id ?? "");
        const mode = body.mode === "upgrade" ? "workspace-write" : "read-only";
        const rec = state.users[userId];
        if (rec) {
          rec.pendingLevel = mode === "workspace-write" ? 2 : 1;
          saveState();
        }
        return json(res, 200, { ok: true, note: rec ? "level recorded" : "no session yet" });
      }
      return json(res, 404, { error: "not found" });
    } catch (e) {
      return json(res, 500, { error: String(e?.message ?? e) });
    }
  });

  server.listen(config.port, config.host, () => {
    console.error(`[dsh-qqbridge] listening on ${config.host}:${config.port}`);
  });

  ctx.effect(() => () => {
    server.close();
  });
}

export { name, inject, Config, apply };
