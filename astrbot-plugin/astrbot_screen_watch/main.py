"""AstrBot 插件：Windows 屏幕监控 + Yuuka 会话主动回复 + 「看看我在干什么」拉图工具。

链路（方案①合一 + 反向轮询拉图）：
  截图到达（主动 force / 被动自动 / 工具拉图回传）
    → 纯色过滤
    → 带图走 Yuuka 会话一次调用（图直接发，不转述）：
        主动/工具：必回；被动：Yuuka 自判（reply / {"skip","desc"}）
    → 回复入会话 + 推 QQ
  「看看我在干什么」→ LLM 自行调工具 screenshot_user_device：
    创建 pending（60s 过期）→ Windows 轮询 /screen/poll 取走 → 截屏回传（带 request_id）
    → 工具等图 → 走 _screen_chat(force) 看图回复
  并发：per-umo 锁串行；被动带冷却；Windows 端线程锁。
"""

import asyncio
import io
import json
import os
import random
import time
import uuid

from aiohttp import web

from astrbot.api import llm_tool, logger, sp, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import StarTools

PLUGIN_NAME = "astrbot_screen_watch"
OBS_KEY = "screen_observation"
MAX_OBS = 3
HISTORY_LIMIT = 8
PENDING_TTL = 60  # 拉图请求有效期（秒）
PENDING_POLL_STEP = 1  # 工具等待检查间隔
CLEAN_INTERVAL = 20  # 过期清理间隔


def _looks_plain(data: bytes) -> bool:
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("RGB").resize((24, 24))
        uniq = len(set(img.getdata()))
        return uniq < 5
    except Exception:
        return False


class ScreenWatchPlugin(star.Star):
    def __init__(self, context, config=None):
        super().__init__(context, config)
        self.cfg = config if config is not None else {}
        self.state_dir = ""
        self._http_runner = None
        self._last_active = 0.0
        self._last_chat_done = 0.0  # 最近一次会话看图完成时间（被动冷却用）
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[str, dict] = {}  # request_id -> {created,state,path}
        self._cleaner = None

    def _cfg(self, key, default=None):
        if not self.cfg:
            return default
        v = self.cfg.get(key, default)
        return default if v is None else v

    async def initialize(self):
        try:
            self.state_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            self.state_dir = os.path.join(os.getcwd(), "data", "plugin_data", PLUGIN_NAME)
        os.makedirs(self.state_dir, exist_ok=True)

        app = web.Application()
        app.router.add_post("/screen/upload", self._handle_upload)
        app.router.add_get("/screen/poll", self._handle_poll)
        self._http_runner = web.AppRunner(app)
        await self._http_runner.setup()
        port = int(self._cfg("http_port", 6201))
        await web.TCPSite(self._http_runner, "127.0.0.1", port).start()
        logger.info(f"[{PLUGIN_NAME}] 截图端点已监听 127.0.0.1:{port}/screen/upload + /screen/poll")
        logger.info(f"[{PLUGIN_NAME}] 屏幕监控插件已加载（目标 QQ: {self._cfg('target_qq', '')}）")
        self._cleaner = asyncio.create_task(self._pending_cleaner())

    async def terminate(self):
        if self._cleaner:
            self._cleaner.cancel()
        if self._http_runner:
            await self._http_runner.cleanup()

    # ---------------- 屏幕观察记忆 ----------------
    def _target_umo(self) -> str:
        qq = str(self._cfg("target_qq", ""))
        return f"default:FriendMessage:{qq}" if qq else ""

    async def _store_observation(self, desc: str):
        umo = self._target_umo()
        if not umo or not desc:
            return
        try:
            cur = await sp.get_async(scope="umo", scope_id=umo, key=OBS_KEY, default=[]) or []
            cur.append({"t": time.strftime("%m-%d %H:%M"), "desc": desc[:200]})
            cur = cur[-MAX_OBS:]
            await sp.put_async(scope="umo", scope_id=umo, key=OBS_KEY, value=cur)
            logger.info(f"[{PLUGIN_NAME}] 屏幕观察已存入上下文: {desc[:60]}")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 存观察失败: {e}")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, request):
        try:
            umo = self._target_umo()
            if not umo or umo not in str(event.unified_msg_origin or ""):
                return
            obs = await sp.get_async(scope="umo", scope_id=umo, key=OBS_KEY, default=[]) or []
            if not obs:
                return
            lines = ["[你最近看到的用户电脑屏幕（时间顺序，供自然衔接，不要主动提'观察/截图'机制）]"]
            for o in obs:
                lines.append(f"- {o.get('t', '')}：{o.get('desc', '')}")
            hint = "\n".join(lines)
            request.system_prompt = hint + "\n\n" + (request.system_prompt or "")
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] on_llm 注入观察失败: {e}")

    # ---------------- pending 拉图请求管理 ----------------
    def _pending_lock(self):
        return self._pending  # 单事件循环，dict 操作天然安全（无 await 间隙则原子）

    async def _pending_cleaner(self):
        while True:
            try:
                now = time.time()
                expired = [
                    rid for rid, p in self._pending.items()
                    if now - p["created"] > PENDING_TTL
                ]
                for rid in expired:
                    self._pending.pop(rid, None)
                    logger.info(f"[{PLUGIN_NAME}] 清理过期拉图请求 {rid[:8]}")
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] cleaner: {e}")
            await asyncio.sleep(CLEAN_INTERVAL)

    async def _handle_poll(self, request: web.Request):
        token = request.query.get("token", "") or (request.headers.get("X-Token") or "")
        expect = str(self._cfg("upload_token", ""))
        if expect and token != expect:
            return web.json_response({"error": "unauthorized"}, status=401)
        now = time.time()
        # 找最早 waiting 的请求（FIFO）
        rid = None
        for k, p in self._pending.items():
            if p["state"] == "waiting":
                rid = k
                break
        if rid is None:
            return web.json_response({"command": "none"})
        self._pending[rid]["state"] = "taken"
        self._pending[rid]["taken_at"] = now
        return web.json_response({"command": "take", "request_id": rid})

    # ---------------- 接收截图 ----------------
    async def _handle_upload(self, request: web.Request):
        token = request.query.get("token", "") or (request.headers.get("X-Token") or "")
        expect = str(self._cfg("upload_token", ""))
        if expect and token != expect:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            if request.headers.get("X-Static") == "1":
                await self._maybe_static_chat()
                return web.json_response({"ok": True, "static": True})
            force = request.headers.get("X-Force") == "1"
            req_id = request.headers.get("X-Request-Id", "") or ""
            reader = await request.multipart() if request.headers.get("Content-Type", "").startswith("multipart") else None
            data = b""
            if reader:
                part = await reader.next()
                while part is not None:
                    if part.name == "file":
                        data = await part.read()
                        break
                    part = await reader.next()
            else:
                data = await request.read()
            if not data:
                return web.json_response({"error": "empty body"}, status=400)
            if _looks_plain(data):
                logger.info(f"[{PLUGIN_NAME}] 低信息量截图（纯色/息屏），忽略")
                return web.json_response({"ok": True, "skipped": "plain"})
            fname = f"screen-{int(time.time())}.jpg"
            path = os.path.join(self.state_dir, fname)
            with open(path, "wb") as f:
                f.write(data)
            with open(os.path.join(self.state_dir, "latest.jpg"), "wb") as f:
                f.write(data)

            # 若是拉图回传：仅接受 waiting/taken 状态的合法回传，防重复/伪造
            if req_id and req_id in self._pending:
                p = self._pending[req_id]
                if p["state"] in ("waiting", "taken"):
                    p["state"] = "done"
                    p["path"] = path
                    logger.info(f"[{PLUGIN_NAME}] 拉图回传完成 request_id={req_id[:8]}")
                else:
                    logger.warning(f"[{PLUGIN_NAME}] 忽略重复回传 request_id={req_id[:8]}")
                return web.json_response({"ok": True, "file": fname, "pulled": True})

            # 被动冷却：刚主动看过则跳过被动触发
            if not force:
                cooldown = float(self._cfg("passive_cooldown", 10))
                if time.time() - self._last_chat_done < cooldown:
                    logger.info(f"[{PLUGIN_NAME}] 被动截图在冷却期（刚看过），跳过触发")
                    return web.json_response({"ok": True, "skipped": "cooldown"})

            logger.info(f"[{PLUGIN_NAME}] 收到截图 {fname} force={force}，走 Yuuka 会话...")
            asyncio.create_task(self._screen_chat(path, force))
            return web.json_response({"ok": True, "file": fname})
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 接收截图失败: {e}")
            return web.json_response({"error": str(e)}, status=500)

    # ---------------- Yuuka 会话锁 + 合一链路 ----------------
    def _session_lock(self):
        umo = self._target_umo()
        return self._session_locks.setdefault(umo, asyncio.Lock())

    async def _screen_chat(self, image_path: str, force: bool):
        async with self._session_lock():
            await self._screen_chat_locked(image_path, force)

    async def _screen_chat_locked(self, image_path: str, force: bool):
        try:
            # 看图必须用目标用户会话绑定的（视觉）供应商，而非全局默认
            provider = self.context.get_using_provider(umo=self._target_umo())
            if provider is None:
                provider = self.context.get_using_provider()
            if provider is None:
                logger.warning(f"[{PLUGIN_NAME}] 无可用 provider，跳过")
                return
            persona_prompt, history, cid = await self._build_yuuka_context()
            if force:
                trigger = str(self._cfg(
                    "active_trigger",
                    "优香，这是我设备当前的界面，你看看~",
                ))
            else:
                trigger = str(self._cfg(
                    "passive_trigger",
                    "（这是我设备当前界面的截图。先看画面：如果只是普通日常、没有值得主动说的内容，"
                    '只输出 JSON {"skip": true, "desc": "画面一句话描述"}；'
                    '如果有值得注意或值得回应的地方，输出 JSON {"reply": "以优香语气自然回应的内容"}。只输出 JSON。）'
                    "优香，我设备当前界面，你看看~",
                ))
            resp = await provider.text_chat(
                contexts=history,
                prompt=trigger,
                image_urls=[image_path],
                system_prompt=persona_prompt or None,
            )
            text = (resp.completion_text or "").strip()
            logger.info(f"[{PLUGIN_NAME}] Yuuka 回应: {text[:150]}")

            if not force:
                reply = None
                skip_desc = ""
                try:
                    s = text[text.find("{"): text.rfind("}") + 1] if "{" in text else ""
                    obj = json.loads(s) if s else {}
                    reply = str(obj.get("reply", "")).strip()
                    skip_desc = str(obj.get("desc", "")).strip()
                except Exception:
                    reply = text
                if not reply:
                    await self._store_observation(skip_desc or "（画面普通，判断无需回应）")
                    return
                text = reply

            await self._append_conversation(cid, text)
            self._last_chat_done = time.time()
            qq = str(self._cfg("target_qq", ""))
            if qq:
                await StarTools.send_message_by_id(
                    "PrivateMessage", qq, MessageChain([Plain(text)]), platform="aiocqhttp"
                )
                logger.info(f"[{PLUGIN_NAME}] 已推 QQ {qq}: {text[:60]}")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] Yuuka 会话链路失败: {e}")

    async def _build_yuuka_context(self):
        umo = self._target_umo()
        cm = self.context.conversation_manager
        persona_prompt = ""
        cid = None
        history = []
        try:
            cid = await cm.get_curr_conversation_id(umo)
            conv = None
            if cid:
                conv = await cm.get_conversation(unified_msg_origin=umo, conversation_id=cid, create_if_not_exists=False)
            persona_id = conv.persona_id if conv else None
            pm = self.context.persona_manager
            if persona_id and persona_id != "[%None]":
                for p in pm.personas_v3:
                    if p.get("name") == persona_id:
                        persona_prompt = p.get("prompt") or ""
                        break
            else:
                try:
                    dp = await pm.get_default_persona_v3(umo=umo)
                    persona_prompt = dp.get("prompt") or ""
                except Exception:
                    pass
            if conv and isinstance(getattr(conv, "content", None), list):
                limit = int(self._cfg("history_limit", 8))
                history = [
                    m for m in conv.content
                    if isinstance(m, dict) and m.get("role") in ("user", "assistant")
                ][-limit:]
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 组装 Yuuka 上下文失败: {e}")
        return persona_prompt, history, cid

    async def _append_conversation(self, cid, assistant_text: str):
        if not cid:
            return
        try:
            cm = self.context.conversation_manager
            # 写前重读最新会话，减小与 astrbot 管线交错的窗口
            conv = await cm.get_conversation(unified_msg_origin=self._target_umo(), conversation_id=cid, create_if_not_exists=False)
            await cm.add_message_pair(
                cid,
                {"role": "user", "content": "（我让你看了我设备当前界面的截图，你看看~）"},
                {"role": "assistant", "content": assistant_text},
            )
            logger.info(f"[{PLUGIN_NAME}] 已写入 Yuuka 会话（cid={cid[:8]}）")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 写入会话失败: {e}")

    # ---------------- LLM 工具：看看我在干什么 ----------------
    @llm_tool(name="screenshot_user_device")
    async def llm_screenshot_device(self, event: AstrMessageEvent) -> str:
        """截取用户 Windows 设备的当前屏幕并查看。

        当用户请你「看看他在干什么 / 看看他的电脑 / 看看他的屏幕」等，需要查看用户设备实时画面时调用。
        会请求用户的 Windows 设备截一张当前屏幕的图，然后你（优香）查看画面并回复用户。
        """
        # 安全：仅设备所有者（target_qq）可调用，防止其他 QQ 好友借 bot 偷看设备屏幕
        qq = str(event.get_sender_id() or "")
        target = str(self._cfg("target_qq", ""))
        if target and qq != target:
            logger.warning(f"[{PLUGIN_NAME}] 拒绝非所有者调用拉图工具（qq={qq}）")
            return "该操作仅限设备所有者本人使用。"
        # 并发上限：同一时间只允许一个未完成的拉图请求（防刷/防内存膨胀）
        active = [p for p in self._pending.values() if p["state"] in ("waiting", "taken")]
        if len(active) >= 1:
            return "已有截图请求正在处理中，请稍后再试。"
        rid = uuid.uuid4().hex
        self._pending[rid] = {"created": time.time(), "state": "waiting", "path": None}
        logger.info(f"[{PLUGIN_NAME}] 拉图请求已创建 {rid[:8]}，等待 Windows 轮询...")
        try:
            ttl = int(self._cfg("pending_ttl", 60))
            for _ in range(max(1, ttl // PENDING_POLL_STEP)):
                await asyncio.sleep(PENDING_POLL_STEP)
                p = self._pending.get(rid)
                if p is None:
                    return "截图请求超时已被清理，设备未响应，请告知用户设备可能不在线。"
                if p["state"] == "done" and p.get("path"):
                    path = p["path"]
                    self._pending.pop(rid, None)
                    logger.info(f"[{PLUGIN_NAME}] 拉图成功，走 Yuuka 会话看图")
                    await self._screen_chat(path, force=True)
                    return "已截取用户设备屏幕并完成查看回复，无需再重复回复同一内容。"
        finally:
            self._pending.pop(rid, None)
        return "截图超时（60秒），设备未响应，请告知用户稍后重试。"

    # ---------------- 静态心跳冒泡 ----------------
    async def _maybe_static_chat(self):
        now = time.time()
        if now - self._last_active < float(self._cfg("min_active_interval", 1500)):
            return
        prob = float(self._cfg("static_reply_prob", 0.3))
        if random.random() >= prob:
            return
        qq = str(self._cfg("target_qq", ""))
        if not qq:
            return
        try:
            provider = self.context.get_using_provider()
            umo = self._target_umo()
            obs = []
            if umo:
                obs = await sp.get_async(scope="umo", scope_id=umo, key=OBS_KEY, default=[]) or []
            ctx = "；".join(f"{o.get('t', '')}: {o.get('desc', '')}" for o in obs[-2:]) or "（无近期记录）"
            prompt = (
                f"你观察到用户电脑屏幕已经有一段时间没有变化（最近的屏幕状态：{ctx}）。"
                "请以自然、像真人朋友的口吻主动说一句关心或搭话的话（10~40字）。"
                "不要提'观察''截图''检测'等机制，不要模板化，要自然。只输出要说的话。"
            )
            resp = await provider.text_chat(prompt=prompt)
            text = (resp.completion_text or "").strip()
            if not text:
                return
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 静态冒泡生成失败: {e}")
            return
        self._last_active = now
        try:
            await StarTools.send_message_by_id(
                "PrivateMessage", qq, MessageChain([Plain(text)]), platform="aiocqhttp"
            )
            logger.info(f"[{PLUGIN_NAME}] 静态冒泡已推 QQ {qq}: {text[:60]}")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 静态冒泡推送失败: {e}")
