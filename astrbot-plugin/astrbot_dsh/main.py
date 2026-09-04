"""AstrBot 插件：DSH 桥（独立插件）。

只负责 DSH 相关内容：
  /dsh                     只读 agent 模式（白名单）
  /dsh admin <密码>        管理员模式（仅管理员 QQ）
  /dsh help                本插件指令列表（只列 DSH 指令）
  /dsh list [all]          会话列表（白名单）
  /dsh <ws> <session> <msg|stop|add|sub|ask|approve|reject|cmds>  任务（白名单）
  桥推送端点 POST /dsh/send（127.0.0.1:bridge_port）

聊天/角色模式、/chatmode、/character、/status、/help 由 astrbot_router 插件负责。
模式状态与 router 插件共享（plugin_data/shared_state.json，读写即最新）。
"""

import json
import os
import time

import aiohttp
import bcrypt
from aiohttp import web

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Plain
from astrbot.api.star import StarTools

PLUGIN_NAME = "astrbot_dsh"
SHARED_FILE = "shared_state.json"


class AstrbotDshPlugin(star.Star):
    def __init__(self, context, config=None):
        super().__init__(context, config)
        self.cfg = config if config is not None else {}
        self.state_dir = ""
        self._http_runner = None
        self._session: aiohttp.ClientSession | None = None

    # ---------------- 配置/共享状态 ----------------
    def _cfg(self, key, default=None):
        if not self.cfg:
            return default
        v = self.cfg.get(key, default)
        return default if v is None else v

    def _shared_path(self):
        return os.path.join(os.path.dirname(self.state_dir), SHARED_FILE)

    def _read_disk(self):
        try:
            with open(self._shared_path(), "r", encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                d = {}
        except Exception:
            d = {}
        d.setdefault("mode", {})
        d.setdefault("chat", {})
        d.setdefault("admin_authed", {})
        d.setdefault("login_tries", {})
        return d

    def _write_disk(self, d):
        try:
            with open(self._shared_path(), "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 保存共享状态失败: {e}")

    def _mode(self, qq):
        return self._read_disk()["mode"].get(qq, "chat")

    def _set_mode(self, qq, mode):
        d = self._read_disk()
        d["mode"][qq] = mode
        self._write_disk(d)

    def _is_admin_authed(self, qq):
        return time.time() < self._read_disk()["admin_authed"].get(qq, 0)

    def _whitelist(self):
        env = os.environ.get("DSH_WHITELIST_QQ", "").strip()
        if env:
            return {s.strip() for s in env.split(",") if s.strip()}
        return set(str(x) for x in self._cfg("whitelist_qq", []))

    def _admin_qq(self):
        return os.environ.get("DSH_ADMIN_QQ", "").strip() or str(self._cfg("admin_qq", ""))

    # ---------------- 生命周期 ----------------
    async def initialize(self):
        try:
            self.state_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            self.state_dir = os.path.join(os.getcwd(), "data", "plugin_data", PLUGIN_NAME)
            os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self._shared_path()), exist_ok=True)
        self._migrate_old_state()
        self._session = aiohttp.ClientSession()
        app = web.Application()
        app.router.add_post("/dsh/send", self._handle_send)
        self._http_runner = web.AppRunner(app)
        await self._http_runner.setup()
        port = int(self._cfg("bridge_port", 6200))
        await web.TCPSite(self._http_runner, "127.0.0.1", port).start()
        logger.info(f"[{PLUGIN_NAME}] 桥推送端点已监听 127.0.0.1:{port}/dsh/send")

    def _migrate_old_state(self):
        """一次性迁移：旧插件 state.json → 共享文件（仅首次）。"""
        if os.path.exists(self._shared_path()):
            return
        old = os.path.join(self.state_dir, "state.json")
        if not os.path.exists(old):
            return
        try:
            with open(old, "r", encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                return
            base = self._read_disk()
            for k in ("mode", "chat", "admin_authed", "login_tries"):
                if isinstance(d.get(k), dict):
                    base.setdefault(k, {}).update(d[k])
            self._write_disk(base)
            logger.info(f"[{PLUGIN_NAME}] 已迁移旧状态到共享文件")
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 旧状态迁移失败: {e}")

    async def terminate(self):
        if self._session:
            await self._session.close()
        if self._http_runner:
            await self._http_runner.cleanup()

    # ---------------- 桥调用 ----------------
    def _token(self):
        return self._cfg("bridge_token", "")

    async def _call_readonly(self, qq, text):
        url = str(self._cfg("readonly_bridge_url", "http://127.0.0.1:63002")).rstrip("/")
        try:
            async with self._session.post(
                url + "/v1/chat",
                json={"user_id": qq, "text": text, "level": 1, "token": self._token()},
                timeout=aiohttp.ClientTimeout(total=600),
            ) as resp:
                data = await resp.json()
                if resp.status == 200:
                    return data.get("reply") or "(空回复)"
                return f"[只读桥错误 {resp.status}] {data.get('error', '')}"
        except Exception as e:
            return f"[只读桥不可用] {type(e).__name__}: {e}"

    async def _admin_bridge(self, endpoint, payload):
        url = str(self._cfg("admin_bridge_url", "http://127.0.0.1:63003")).rstrip("/")
        payload["token"] = self._token()
        try:
            async with self._session.post(
                url + endpoint, json=payload, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                return resp.status, await resp.json()
        except Exception as e:
            return 0, {"ok": False, "error": f"[admin桥不可用] {type(e).__name__}: {e}"}

    async def _resolve_session(self, ws_hint, session_hint):
        _, data = await self._admin_bridge("/v1/list", {})
        if not data.get("ok"):
            return None, data.get("error", "list 失败")
        matches = []
        for w in data.get("list", []):
            cwd = w["workspace"]
            if ws_hint.lower() not in cwd.lower():
                continue
            for s in w["sessions"]:
                if session_hint.lower() in s["title"].lower() or s["id"].startswith(session_hint):
                    matches.append({"session_id": s["id"], "workspace": cwd, "title": s["title"]})
        if not matches:
            return None, f"未找到匹配的会话（workspace={ws_hint!r}, session={session_hint!r}），用 /dsh list 查看"
        if len(matches) > 1:
            return None, "匹配到多个会话，请用更精确的名称（/dsh list 查看）"
        m = matches[0]
        return m, None

    # ---------------- DSH 指令 ----------------
    async def cmd_dsh(self, event, cmd):
        qq = event.get_sender_id()
        parts = cmd.split()
        # /dsh 只读
        if len(parts) == 1:
            if qq not in self._whitelist():
                await event.send(MessageChain([Plain("❌ 无权限使用 DSH（不在白名单）")]))
                return
            self._set_mode(qq, "agent_ro")
            await event.send(MessageChain([Plain("✅ 已切换到 DSH 只读 agent 模式")]))
            return
        # /dsh admin <密码>
        if parts[1] == "admin":
            await self._cmd_dsh_admin(event, parts)
            return
        # 以下子指令全部需要白名单
        if qq not in self._whitelist():
            await event.send(MessageChain([Plain("❌ 无权限使用 DSH（不在白名单）")]))
            return
        # /dsh help
        if parts[1] == "help":
            await self._cmd_dsh_help(event)
            return
        # /dsh list
        if parts[1] == "list":
            await self._cmd_dsh_list(event, parts)
            return
        # /dsh <workspace> <session> <action>
        if len(parts) < 3:
            await event.send(MessageChain([Plain("用法：/dsh <workspace> <session> <msg|stop|sub|add>")]))
            return
        await self._cmd_dsh_task(event, parts)

    async def _cmd_dsh_help(self, event):
        """只列出 DSH 桥插件的指令。"""
        lines = [
            "📜 DSH 桥插件指令：",
            "  /dsh                        只读 agent 模式",
            "  /dsh admin <密码>           登录管理员模式（仅管理员 QQ）",
            "  /dsh list [all]             会话列表（all 含未命名）",
            "  /dsh <ws> <session> <msg>   发任务",
            "  /dsh <ws> <session> stop    停止任务",
            "  /dsh <ws> <session> add <内容>  追加引导",
            "  /dsh <ws> <session> sub     订阅结果",
            "  /dsh <ws> <session> ask <回答>  回答提问",
            "  /dsh <ws> <session> approve 同意审批",
            "  /dsh <ws> <session> reject  拒绝审批",
            "  /dsh <ws> <session> cmds    DSH 自带命令",
            "  /dsh help                   本列表",
            "",
            "聊天/角色/模型/人格卡指令见 /help（astrbot_router 插件）。",
        ]
        await event.send(MessageChain([Plain("\n".join(lines))]))

    async def _cmd_dsh_admin(self, event, parts):
        qq = event.get_sender_id()
        if qq != self._admin_qq():
            await event.send(MessageChain([Plain("❌ 只有管理员 QQ 才能登录管理员模式")]))
            return
        if len(parts) < 3:
            await event.send(MessageChain([Plain("用法：/dsh admin <密码>")]))
            return
        password = parts[2]
        pwd_hash = self._cfg("admin_password_hash", "")
        if not pwd_hash:
            await event.send(MessageChain([Plain("❌ 尚未配置管理员密码")]))
            return
        try:
            ok = bcrypt.checkpw(password.encode(), pwd_hash.encode())
        except Exception:
            ok = False
        d = self._read_disk()
        if ok:
            ttl = int(self._cfg("auth_ttl_days", 7)) * 86400
            d["admin_authed"][qq] = time.time() + ttl
            d.get("login_tries", {}).pop(qq, None)
            d["mode"][qq] = "agent_admin"
            self._write_disk(d)
            await event.send(MessageChain([Plain(f"✅ 已切换到 DSH 管理员模式（{self._cfg('auth_ttl_days', 7)} 天内免密）")]))
            await self._cmd_dsh_help(event)
        else:
            tries = d.setdefault("login_tries", {}).get(qq, 0) + 1
            d["login_tries"][qq] = tries
            self._write_disk(d)
            if tries >= int(self._cfg("max_login_tries", 5)):
                await event.send(MessageChain([Plain("❌ 错误次数过多，已暂时禁止登录")]))
            else:
                await event.send(MessageChain([Plain(f"❌ 密码错误（{tries}/{self._cfg('max_login_tries', 5)}）")]))

    async def _cmd_dsh_list(self, event, parts):
        show_all = len(parts) > 2 and parts[2].lower() == "all"
        _, data = await self._admin_bridge("/v1/list", {})
        if not data.get("ok"):
            await event.send(MessageChain([Plain(f"❌ {data.get('error', 'list 失败')}")]))
            return
        flat = []
        for w in data.get("list", []):
            for s in w.get("sessions", []):
                flat.append({
                    "workspace": w["workspace"],
                    "ws_title": w.get("title") or "",
                    "id": s["id"],
                    "title": s.get("title") or "",
                    "mtime": s.get("mtime") or 0,
                })
        flat.sort(key=lambda x: -x["mtime"])
        if not show_all:
            flat = [s for s in flat if s["title"]]
        lines = []
        last_ws = None
        for s in flat:
            if s["workspace"] != last_ws:
                label = s["workspace"]
                if s["ws_title"]:
                    label = f"{s['ws_title']} ({s['workspace']})"
                lines.append(f"📁 {label}")
                last_ws = s["workspace"]
            lines.append(f"   · {s['title'] or '(未命名)'}")
            lines.append(f"   /dsh {s['workspace']} {s['id']} <msg>")
        if not lines:
            lines = ["(无会话)"]
        await event.send(MessageChain([Plain("\n".join(lines))]))

    async def _cmd_dsh_cmds(self, event):
        _, data = await self._admin_bridge("/v1/commands", {})
        cmds = data.get("commands", [])
        lines = ["📜 DSH 自带命令："]
        for c in cmds:
            lines.append(f"  /{c['name']}" + (f" — {c['description']}" if c.get("description") else ""))
        await event.send(MessageChain([Plain("\n".join(lines))]))

    async def _cmd_dsh_task(self, event, parts):
        qq = event.get_sender_id()
        ws_hint, session_hint = parts[1], parts[2]
        rest = parts[3:]
        m, err = await self._resolve_session(ws_hint, session_hint)
        if not m:
            await event.send(MessageChain([Plain(f"❌ {err}")]))
            return
        sid, workspace, title = m["session_id"], m["workspace"], m["title"]

        if not rest:
            await self._cmd_dsh_cmds(event)
            return
        action = rest[0]
        if action == "cmds":
            await self._cmd_dsh_cmds(event)
            return
        if action == "stop":
            _, data = await self._admin_bridge("/v1/stop", {"session_id": sid})
            await event.send(MessageChain([Plain("✅ 已请求停止" if data.get("ok") else f"❌ {data.get('error')}")]))
            return
        if action == "sub":
            _, data = await self._admin_bridge("/v1/sub", {"session_id": sid, "qq": qq, "workspace": workspace, "title": title})
            if data.get("ok") and "reply" in data:
                await event.send(MessageChain([Plain(self._annotate(data["reply"], workspace, sid))]))
            elif data.get("ok"):
                await event.send(MessageChain([Plain("🔔 已订阅，任务完成后推送结果")]))
            else:
                await event.send(MessageChain([Plain(f"❌ {data.get('error')}")]))
            return
        if action == "approve":
            _, data = await self._admin_bridge("/v1/approve", {"session_id": sid})
            await event.send(MessageChain([Plain("✅ 已同意" if data.get("ok") else f"❌ {data.get('error', '同意失败')}")]))
            return
        if action == "reject":
            _, data = await self._admin_bridge("/v1/reject", {"session_id": sid})
            await event.send(MessageChain([Plain("✅ 已拒绝" if data.get("ok") else f"❌ {data.get('error', '拒绝失败')}")]))
            return
        if action == "add":
            content = " ".join(rest[1:])
            if not content:
                await event.send(MessageChain([Plain("用法：/dsh <workspace> <session> add <内容>")]))
                return
            _, data = await self._admin_bridge("/v1/add", {"session_id": sid, "text": content})
            await event.send(MessageChain([Plain("✅ 已追加引导" if data.get("ok") else f"❌ {data.get('error')}")]))
            return
        # 默认：发任务
        msg = " ".join(rest)
        _, data = await self._admin_bridge("/v1/chat", {"session_id": sid, "qq": qq, "workspace": workspace, "title": title, "text": msg})
        if data.get("ok"):
            if data.get("queued"):
                await event.send(MessageChain([Plain(data.get("note") or "🔔 任务已排队，网页空闲后自动执行并推送结果")]))
            else:
                await event.send(MessageChain([Plain("✅ 任务已发布，跑完会把结果发你")]))
        else:
            await event.send(MessageChain([Plain(f"❌ {data.get('error', '发布失败')}")]))

    def _annotate(self, reply, workspace, session_id):
        return f"{reply}\n\n\n/dsh {workspace} {session_id}"

    # ---------------- 消息入口 ----------------
    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if event.get_platform_name() != "aiocqhttp":
            return
        if not event.is_private_chat():
            return
        qq = event.get_sender_id()
        text = (event.get_message_str() or "").strip()
        if not text:
            return  # 空消息交给 router 插件统一处理
        cmd = text.lstrip("/")

        # 只处理 /dsh 指令
        if cmd == "dsh" or cmd.startswith("dsh "):
            await self.cmd_dsh(event, cmd)
            event.should_call_llm(True)
            event.stop_event()
            return

        # 非指令消息：按共享模式路由 DSH 侧模式
        mode = self._mode(qq)
        if mode == "agent_admin":
            await event.send(MessageChain([Plain("⚠️ 管理员模式下请用 /dsh <workspace> <session> <msg> 发任务，/dsh list 查看")]))
            event.should_call_llm(True)
            event.stop_event()
            return
        if mode == "agent_ro":
            await event.send(MessageChain([Plain("⏳ 处理中…")]))
            reply = await self._call_readonly(qq, text)
            event.should_call_llm(True)
            event.set_result(MessageEventResult().message(reply).stop_event())
            return
        # chat / character：放行给默认 LLM（router 插件负责注入）
        return

    # ---------------- dsh 桥推送端点 ----------------
    async def _handle_send(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        token = data.get("token", "")
        if self._cfg("bridge_token", "") and token != self._cfg("bridge_token", ""):
            return web.json_response({"error": "unauthorized"}, status=401)
        qq = str(data.get("qq", ""))
        text = data.get("text", "")
        if not qq or not text:
            return web.json_response({"error": "missing qq/text"}, status=400)
        try:
            await StarTools.send_message_by_id("PrivateMessage", qq, MessageChain([Plain(text)]), platform="aiocqhttp")
            return web.json_response({"ok": True})
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 推送失败: {e}")
            return web.json_response({"error": str(e)}, status=500)
