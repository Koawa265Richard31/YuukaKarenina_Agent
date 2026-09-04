"""AstrBot 插件：自定义路由指令（独立插件）。

负责聊天/角色模式的路由与指令：
  /chatmode [web|depth]                纯聊天模式 + 联网/思考深度（模型/供应商由 provider 决定）
  /character list                       列出 astrbot 人格卡
  /character <名字>                     切换到指定人格卡
  /character <prompt> [名字]            添加人格卡并切换
  /character default                    回到默认人格（纯聊天）
  /status                               当前模式/人格卡/联网/深度
  /help                                 聚合帮助（astrbot 内置 + 所有插件，含权限过滤）
  on_llm                                聊天/角色模式注入模型与提示
  on_decorating_result                  卡片消息转纯文本（降风控）

DSH 相关内容（/dsh*、只读/管理员 agent）由 astrbot_dsh 插件负责。
模式状态与 dsh 插件共享（plugin_data/shared_state.json，读写即最新）。
"""

import json
import os
import time

import aiohttp

from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import StarTools

PLUGIN_NAME = "astrbot_router"
SHARED_FILE = "shared_state.json"


class AstrbotRouterPlugin(star.Star):
    def __init__(self, context, config=None):
        super().__init__(context, config)
        self.cfg = config if config is not None else {}
        self.state_dir = ""

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

    def _chat(self, qq):
        return self._read_disk()["chat"].get(qq, {})

    def _set_chat(self, qq, key, value):
        d = self._read_disk()
        d.setdefault("chat", {}).setdefault(qq, {})[key] = value
        self._write_disk(d)

    def _dsh_whitelist(self):
        env = os.environ.get("DSH_WHITELIST_QQ", "").strip()
        if env:
            return {s.strip() for s in env.split(",") if s.strip()}
        return set()

    def _persona_visible(self, qq, persona_id):
        """受限人格卡：仅所有者可见/可用/可修改（非所有者按“不存在”处理，不泄露卡的存在）。"""
        try:
            owners = json.loads(self._cfg("persona_owners", "{}") or "{}")
        except Exception:
            owners = {}
        owner = owners.get(persona_id)
        if not owner:
            return True
        return str(qq) == str(owner)

    # ---------------- 生命周期 ----------------
    async def initialize(self):
        try:
            self.state_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            self.state_dir = os.path.join(os.getcwd(), "data", "plugin_data", PLUGIN_NAME)
            os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self._shared_path()), exist_ok=True)
        logger.info(f"[{PLUGIN_NAME}] 路由插件已加载，共享状态: {self._shared_path()}")

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        # 等所有插件（含 builtin_commands）加载完成后禁用内置 /persona、/provider，
        # 由本插件接管（受限卡权限拦截 / 供应商权限矩阵）。不能在 initialize 里做：builtin 尚未注册。
        try:
            from astrbot.core.star.command_management import toggle_command
            from astrbot.core.star.star_handler import star_handlers_registry

            # /persona 接管（受限卡，仅当 intercept_persona 开启）
            if self._cfg("intercept_persona", True):
                targets = [
                    h.handler_full_name
                    for h in star_handlers_registry
                    if h.handler_name == "persona"
                    and h.handler_module_path.startswith("astrbot.builtin_stars")
                ]
                if not targets:
                    targets = ["astrbot.builtin_stars.builtin_commands.main_persona"]
                for _t in targets:
                    await toggle_command(_t, False)
                    logger.info(f"[{PLUGIN_NAME}] 已禁用内置 /persona handler: {_t}")
                logger.info(f"[{PLUGIN_NAME}] 已接管内置 /persona（受限卡权限拦截生效）")

            # /provider 接管（供应商权限矩阵：DS 官方仅 owner+admin 可见可切）
            if self._cfg("intercept_provider", True):
                targets2 = [
                    h.handler_full_name
                    for h in star_handlers_registry
                    if h.handler_name == "provider"
                    and h.handler_module_path.startswith("astrbot.builtin_stars")
                ]
                if not targets2:
                    targets2 = ["astrbot.builtin_stars.builtin_commands.main_provider"]
                for _t in targets2:
                    await toggle_command(_t, False)
                    logger.info(f"[{PLUGIN_NAME}] 已禁用内置 /provider handler: {_t}")
                logger.info(f"[{PLUGIN_NAME}] 已接管内置 /provider（供应商权限矩阵生效）")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 禁用内置指令失败: {e}")

    async def terminate(self):
        return

    # ---------------- 指令 ----------------
    async def cmd_status(self, event):
        qq = event.get_sender_id()
        mode = self._mode(qq)
        names = {"chat": "纯聊天", "character": "角色", "agent_ro": "DSH 只读 agent", "agent_admin": "DSH 管理员"}
        lines = [f"当前模式：{names.get(mode, mode)}"]
        if mode == "character":
            umo = event.unified_msg_origin
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            pname = None
            if cid:
                conv = await self.context.conversation_manager.get_conversation(umo, cid)
                pname = conv.persona_id if conv else None
            lines.append(f"人格卡：{pname or '默认'}")
        elif mode == "chat":
            chat = self._chat(qq)
            lines.append(f"联网：{'开' if chat.get('web_search') else '关'}")
            lines.append(f"思考深度：{chat.get('depth', '默认')}")
        elif mode == "agent_admin":
            lines.append("管理员授权：见 /dsh status 提示（本插件不管理 DSH 授权）")
        await event.send(MessageChain([Plain("\n".join(lines))]))

    async def cmd_chatmode(self, event, cmd):
        qq = event.get_sender_id()
        parts = cmd.split()
        if len(parts) == 1:
            await self._apply_persona(event.unified_msg_origin, None)
            self._set_mode(qq, "chat")
            await event.send(MessageChain([Plain("✅ 已切换到纯聊天模式（默认人格）")]))
            return
        if parts[1] == "web" and len(parts) >= 3:
            on = parts[2].lower() in ("on", "开", "1", "true", "yes")
            self._set_chat(qq, "web_search", on)
            await event.send(MessageChain([Plain(f"✅ 联网已{'开启' if on else '关闭'}")]))
            return
        if parts[1] == "depth" and len(parts) >= 3:
            d = {"低": "low", "中": "medium", "高": "high", "low": "low", "medium": "medium", "high": "high"}.get(parts[2].lower())
            if not d:
                await event.send(MessageChain([Plain("用法：/chatmode depth 低|中|高")]))
                return
            self._set_chat(qq, "depth", d)
            await event.send(MessageChain([Plain(f"✅ 思考深度已设为 {d}")]))
            return
        # 模型/供应商由 provider 决定，/chatmode 不再提供切换
        if parts[1] in ("model", "models"):
            await event.send(MessageChain([Plain("ℹ️ 模型与供应商由当前 provider 决定，/chatmode 不再支持切换模型")]))
            return
        await event.send(MessageChain([Plain("用法：/chatmode [web on|off] [depth 低|中|高]")]))

    async def cmd_character(self, event, cmd):
        qq = event.get_sender_id()
        args = cmd.split()
        if len(args) == 1:
            await event.send(MessageChain([Plain(
                "用法：\n"
                "/character list              列出所有人格卡\n"
                "/character <名字>            切换到指定人格卡\n"
                "/character <prompt> [名字]    添加人格卡并切换\n"
                "/character default           回到默认人格（纯聊天）"
            )]))
            return
        sub = args[1]
        pm = self.context.persona_manager
        umo = event.unified_msg_origin
        if sub == "list":
            personas = [p for p in pm.personas if self._persona_visible(qq, p.persona_id)]
            if not personas:
                await event.send(MessageChain([Plain("📂 暂无任何人格卡。添加：/character <prompt> [名字]")]))
                return
            lines = ["📂 astrbot 人格卡："]
            for p in personas:
                first = ""
                prompt = (p.system_prompt or "").strip()
                if prompt:
                    first = prompt.splitlines()[0][:40]
                lines.append(f"👤 {p.persona_id}" + (f" — {first}" if first else ""))
            lines.append(f"\n共 {len(personas)} 张。切换：/character <名字>；添加：/character <prompt> [名字]")
            await event.send(MessageChain([Plain("\n".join(lines))]))
            return
        if sub == "default":
            await self._apply_persona(umo, None)
            self._set_mode(qq, "chat")
            await event.send(MessageChain([Plain("✅ 已回到默认人格（纯聊天模式）")]))
            return
        existing = {p.persona_id for p in pm.personas}
        if sub in existing:
            if not self._persona_visible(qq, sub):
                await event.send(MessageChain([Plain(f"❌ 人格卡「{sub}」不存在或不可用")]))
                return
            await self._apply_persona(umo, sub)
            self._set_mode(qq, "character")
            await event.send(MessageChain([Plain(f"✅ 已切换到人格卡「{sub}」")]))
            return
        if len(args) >= 3:
            name = args[-1]
            prompt = " ".join(args[1:-1])
        else:
            name = f"p_{int(time.time())}"
            prompt = sub
        if not self._persona_visible(qq, name):
            await event.send(MessageChain([Plain(f"❌ 人格卡「{name}」不存在或不可用")]))
            return
        try:
            await pm.create_persona(name, prompt)
        except ValueError:
            if not self._persona_visible(qq, name):
                await event.send(MessageChain([Plain(f"❌ 人格卡「{name}」不存在或不可用")]))
                return
            await self._apply_persona(umo, name)
            self._set_mode(qq, "character")
            await event.send(MessageChain([Plain(f"✅ 人格卡「{name}」已存在，已切换")]))
            return
        await self._apply_persona(umo, name)
        self._set_mode(qq, "character")
        await event.send(MessageChain([Plain(f"✅ 已添加人格卡「{name}」并切换")]))

    async def _apply_persona(self, umo, persona_id):
        cm = self.context.conversation_manager
        if persona_id is None:
            persona_id = "[%None]"
        cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            await cm.new_conversation(umo, persona_id=persona_id)
            return
        await cm.update_conversation_persona_id(umo, persona_id)

    # ---------------- /persona 接管（内置指令被禁用，受限卡权限拦截） ----------------
    async def cmd_persona(self, event, cmd):
        qq = event.get_sender_id()
        l = cmd.split()
        pm = self.context.persona_manager
        cm = self.context.conversation_manager
        umo = event.unified_msg_origin
        curr = "无"
        cid = await cm.get_curr_conversation_id(umo)
        if cid:
            conv = await cm.get_conversation(
                unified_msg_origin=umo,
                conversation_id=cid,
                create_if_not_exists=False,
            )
            if conv and conv.persona_id and conv.persona_id != "[%None]":
                curr = conv.persona_id
        if len(l) == 1:
            lines = [
                "[Persona]",
                "",
                f"当前对话人格情景: {curr}",
                "",
                "- 人格情景列表: /persona list",
                "- 设置人格情景: /persona <人格名>",
                "- 人格情景详细信息: /persona view <人格名>",
                "- 取消人格: /persona unset",
            ]
            await event.send(MessageChain([Plain("\n".join(lines))]))
            return
        if l[1] == "list":
            personas = [p for p in pm.personas if self._persona_visible(qq, p.persona_id)]
            if not personas:
                await event.send(MessageChain([Plain("📂 暂无任何人格卡。")]))
                return
            lines = ["📂 人格列表："]
            for p in personas:
                first = ""
                prompt = (p.system_prompt or "").strip()
                if prompt:
                    first = prompt.splitlines()[0][:40]
                lines.append(f"👤 {p.persona_id}" + (f" — {first}" if first else ""))
            lines.append(f"\n共 {len(personas)} 个。使用 /persona <人格名> 设置")
            await event.send(MessageChain([Plain("\n".join(lines))]))
            return
        if l[1] == "view":
            if len(l) < 3:
                await event.send(MessageChain([Plain("请输入人格情景名")]))
                return
            name = l[2].strip()
            p = next(
                (x for x in pm.personas if x.persona_id == name and self._persona_visible(qq, x.persona_id)),
                None,
            )
            if not p:
                await event.send(MessageChain([Plain(f"人格{name}不存在")]))
                return
            prompt = (p.system_prompt or "").strip()
            msg = f"人格{name}的详细信息：\n{prompt[:1500]}" if prompt else f"人格{name}（无描述）"
            await event.send(MessageChain([Plain(msg)]))
            return
        if l[1] == "unset":
            await cm.update_conversation_persona_id(umo, "[%None]")
            self._set_mode(qq, "chat")
            await event.send(MessageChain([Plain("取消人格成功。")]))
            return
        name = "".join(l[1:]).strip()
        if not any(x.persona_id == name for x in pm.personas):
            await event.send(MessageChain([Plain("不存在该人格情景。使用 /persona list 查看所有。")]))
            return
        if not self._persona_visible(qq, name):
            await event.send(MessageChain([Plain("不存在该人格情景。使用 /persona list 查看所有。")]))
            return
        await cm.update_conversation_persona_id(umo, name)
        self._set_mode(qq, "character")
        await event.send(MessageChain([Plain("设置成功。")]))

    # ---------------- /provider 接管（供应商权限矩阵） ----------------
    # DS 官方（deepseek/ 前缀）仅「所有者 QQ + 管理员(op)」双条件满足时可见/可切；
    # 一般用户不能切换供应商；管理员可切换但看不到/切不了 DS 官方。
    async def cmd_provider(self, event, cmd):
        qq = str(event.get_sender_id() or "")
        is_admin = event.is_admin()
        owner = str(self._cfg("ds_provider_owner", "")).strip()
        parts = cmd.split()
        umo = event.unified_msg_origin
        try:
            providers = list(self.context.get_all_providers() or [])
        except Exception:
            providers = []

        def _is_ds(p):
            return str(getattr(p.meta(), "id", "") or "").startswith("deepseek/")

        can_ds = bool(owner) and qq == owner and is_admin
        visible = [p for p in providers if can_ds or not _is_ds(p)]

        # 当前使用的供应商
        try:
            cur = self.context.get_using_provider(umo=umo)
            cur_id = str(getattr(cur.meta(), "id", "")) if cur else ""
        except Exception:
            cur_id = ""

        if len(parts) == 1:
            lines = [f"当前供应商：{cur_id or '（默认）'}"]
            if not is_admin:
                lines.append("⚠️ 供应商切换仅限管理员。")
            if not visible:
                lines.append("（无可用供应商）")
            else:
                lines.append("可用供应商：")
                for i, p in enumerate(visible, 1):
                    tag = "（当前）" if p.meta().id == cur_id else ""
                    lines.append(f"  {i}. {p.meta().id}{tag}")
            await event.send(MessageChain([Plain("\n".join(lines))]))
            return

        # 切换
        if not is_admin:
            await event.send(MessageChain([Plain("❌ 供应商切换仅限管理员。")]))
            return
        try:
            idx = int(parts[1])
        except ValueError:
            await event.send(MessageChain([Plain("用法：/provider 查看；/provider <序号> 切换")]))
            return
        if idx < 1 or idx > len(visible):
            await event.send(MessageChain([Plain(f"❌ 序号无效（1~{len(visible)}）。")]))
            return
        target = visible[idx - 1]
        if _is_ds(target) and not can_ds:
            await event.send(MessageChain([Plain("❌ 无权限使用该供应商（仅设备所有者可用）。")]))
            return
        try:
            from astrbot.core.provider.entities import ProviderType

            await self.context.provider_manager.set_provider(
                target.meta().id, ProviderType.CHAT_COMPLETION, umo=umo
            )
            await event.send(MessageChain([Plain(f"✅ 当前会话供应商已切换为 {target.meta().id}")]))
        except Exception as e:
            await event.send(MessageChain([Plain(f"❌ 切换失败：{e}")]))

    # ---------------- /draw AI 生图（朋友反代 /images/generations） ----------------
    async def cmd_draw(self, event, prompt):
        qq = event.get_sender_id()
        if qq not in self._dsh_whitelist():
            await event.send(MessageChain([Plain("❌ 无权限使用 /draw（仅白名单）")]))
            return
        if not prompt:
            await event.send(MessageChain([Plain("用法：/draw <描述>，AI 生图（走朋友反代）")]))
            return
        key = os.environ.get("FRIEND_PROXY_KEY", "").strip()
        if not key:
            await event.send(MessageChain([Plain("❌ 未配置 FRIEND_PROXY_KEY 环境变量")]))
            return
        base = str(self._cfg("draw_base_url", "") or "").rstrip("/")
        if not base:
            await event.send(MessageChain([Plain("❌ 未配置 draw_base_url（插件配置里填生图反代地址）")]))
            return
        model = self._cfg("draw_model", "") or "doubao-seedream-4-0-250828"
        size = self._cfg("draw_size", "") or "1024x1024"
        await event.send(MessageChain([Plain(f"🎨 正在生成（{model}）…")]))
        try:
            timeout = aiohttp.ClientTimeout(total=240)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(
                    f"{base}/images/generations",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "prompt": prompt, "n": 1, "size": size},
                ) as r:
                    data = await r.json(content_type=None)
                if r.status != 200:
                    await event.send(MessageChain([Plain(f"❌ 生图失败 [{r.status}] {str(data)[:200]}")]))
                    return
                url = (data.get("data") or [{}])[0].get("url", "")
                if not url:
                    await event.send(MessageChain([Plain(f"❌ 生图接口未返回图片: {str(data)[:200]}")]))
                    return
                async with s.get(url) as img:
                    if img.status != 200:
                        await event.send(MessageChain([Plain(f"❌ 下载图片失败 [{img.status}]")]))
                        return
                    raw = await img.read()
            path = os.path.join(self.state_dir, f"draw_{int(time.time())}.png")
            with open(path, "wb") as f:
                f.write(raw)
            await event.send(MessageChain([Plain("✅ 已生成："), Image.fromFileSystem(path)]))
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] /draw 异常: {e}")
            await event.send(MessageChain([Plain(f"❌ 生图异常: {type(e).__name__}: {e}")]))

    # ---------------- /help 聚合 ----------------
    async def cmd_help(self, event):
        qq = event.get_sender_id()
        is_admin = bool(getattr(event, "is_admin", lambda: False)())
        lines = ["📖 帮助（按插件分组，已按权限过滤）："]

        # 1. 全部注册指令（内置 + 插件），用 astrbot 实时指令表
        try:
            from astrbot.core.star import command_management
            commands = await command_management.list_commands()
            groups = {}  # 分组名 -> [(cmd, desc)]
            for item in commands:
                if not item.get("enabled", True):
                    continue
                if item.get("is_sub_command") or item.get("type") == "sub_command":
                    continue  # 子指令挂在组下面
                permission = item.get("permission", "everyone")
                if permission == "admin" and not is_admin:
                    continue
                effective = item.get("effective_command") or item.get("original_command")
                if not effective:
                    continue
                desc = item.get("description") or ""
                group = "内置指令" if item.get("reserved") else (item.get("plugin_display_name") or item.get("plugin_name") or "插件")
                groups.setdefault(group, []).append((effective, desc))
            for group in sorted(groups.keys()):
                entries = groups[group]
                lines.append(f"▍{group}（{len(entries)}）")
                for effective, desc in sorted(entries):
                    lines.append(f"  /{effective}" + (f" - {desc}" if desc else ""))
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 枚举指令失败: {e}")

        # 2. 本插件（自定义路由，手动解析不在注册表里）
        lines.append("▍astrbot_router（自定义路由）")
        lines.append("  /chatmode                 纯聊天模式（默认人格）")
        lines.append("  /chatmode web on|off      联网开关")
        lines.append("  /chatmode depth 低|中|高   思考深度")
        lines.append("  （模型/供应商由 provider 决定）")
        lines.append("  /character list           列出人格卡")
        lines.append("  /character <名字>         切换人格卡（default=纯聊天）")
        lines.append("  /character <prompt> [名字] 添加人格卡")
        lines.append("  /draw <描述>              AI 生图（朋友反代，白名单）")
        lines.append("  /status                   查看当前模式")
        lines.append("  /help                     本帮助")

        # 3. dsh 插件（权限校验：仅白名单展示）
        if qq in self._dsh_whitelist():
            lines.append("▍astrbot_dsh（DSH 桥）")
            lines.append("  /dsh /dsh list /dsh admin /dsh <ws> <session> <动作>")
            lines.append("  详见 /dsh help（仅列 DSH 指令）")
        else:
            lines.append("▍astrbot_dsh（DSH 桥）— 仅白名单用户可用（指令已隐藏）")

        logger.info(f"[{PLUGIN_NAME}] /help 输出:\n" + "\n".join(lines))
        await event.send(MessageChain([Plain("\n".join(lines))]))

    # ---------------- 限流 ----------------
    def _rate_limited(self, qq):
        window = float(self._cfg("rate_limit_window", 60))
        maxn = int(self._cfg("rate_limit_max", 20))
        if maxn <= 0:
            return False
        now = time.time()
        d = self._read_disk()
        bucket = d.setdefault("rate", {}).setdefault(qq, [])
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= maxn:
            return True
        bucket.append(now)
        self._write_disk(d)
        return False

    # ---------------- 消息入口 ----------------
    @filter.event_message_type(EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        if event.get_platform_name() != "aiocqhttp":
            return
        if not event.is_private_chat():
            return
        qq = event.get_sender_id()
        text = (event.get_message_str() or "").strip()
        if not text:
            # 空消息（如图片无文本）交给默认 LLM
            event.should_call_llm(True)
            event.stop_event()
            return
        if self._rate_limited(qq):
            await event.send(MessageChain([Plain("⚠️ 发送太快了，请稍后再试")]))
            event.should_call_llm(True)
            event.stop_event()
            return
        cmd = text.lstrip("/")

        if cmd == "help":
            await self.cmd_help(event)
            event.should_call_llm(True)
            event.stop_event()
            return
        if cmd == "status":
            await self.cmd_status(event)
            event.should_call_llm(True)
            event.stop_event()
            return
        if cmd.startswith("chatmode"):
            await self.cmd_chatmode(event, cmd)
            event.should_call_llm(True)
            event.stop_event()
            return
        if cmd.startswith("character"):
            await self.cmd_character(event, cmd)
            event.should_call_llm(True)
            event.stop_event()
            return
        if cmd.startswith("persona"):
            await self.cmd_persona(event, cmd)
            event.should_call_llm(True)
            event.stop_event()
            return
        if cmd == "provider" or cmd.startswith("provider "):
            await self.cmd_provider(event, cmd)
            event.should_call_llm(True)
            event.stop_event()
            return
        if cmd == "draw" or cmd.startswith("draw "):
            await self.cmd_draw(event, cmd[4:].strip())
            event.should_call_llm(True)
            event.stop_event()
            return
        # /dsh 及其余消息交给 dsh 插件 / 默认 LLM
        return

    # ---------------- LLM 工具：读平台日志（内置 agent 可自主查看） ----------------
    @llm_tool(name="read_bot_log")
    async def llm_read_bot_log(self, event: AstrMessageEvent, lines: int = 80) -> str:
        """读取 astrbot 平台日志文件（/srv/astrbot/data/logs/astrbot.log）的最近内容，用于排查 bot 自身状态、错误和平台连接问题。当需要了解 bot 最近发生了什么、排查报错时调用此工具。

        Args:
            lines(number): 读取最近多少行，默认 80，最大 500。

        """
        path = "/srv/astrbot/data/logs/astrbot.log"
        try:
            n = max(1, min(int(lines), 500))
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-n:]
            if not tail:
                return "日志文件为空（可能刚启动还没输出）"
            return f"[astrbot.log 最近 {len(tail)} 行]\n" + "".join(tail)
        except FileNotFoundError:
            return "日志文件不存在（文件日志未开启，或尚未重启生效；当前日志在 journalctl -u astrbot）"
        except Exception as e:
            return f"读取日志失败: {e}"

    # ---------------- 默认 LLM 的模型/提示注入 ----------------
    @filter.on_llm_request()
    async def on_llm(self, event: AstrMessageEvent, request):
        qq = event.get_sender_id()
        mode = self._mode(qq)
        # 模型与供应商由当前 provider 决定，插件不再注入模型名
        if mode == "chat":
            chat = self._chat(qq)
            hints = []
            if chat.get("web_search"):
                hints.append("本次对话需要联网搜索获取最新信息")
            depth = chat.get("depth")
            if depth:
                hints.append(f"思考深度：{depth}")
            if hints:
                request.system_prompt = "（" + "；".join(hints) + "）\n" + (request.system_prompt or "")
        # character 模式系统人设由 astrbot 核心按对话人格卡注入

    # ---------------- 卡片消息转纯文本（降风控） ----------------
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        qq = event.get_sender_id()
        if self._mode(qq) not in ("chat", "character"):
            return
        result = event.get_result()
        if not result or not getattr(result, "chain", None):
            return
        new_chain = []
        for comp in result.chain:
            name = comp.__class__.__name__
            if name == "Share":
                url = getattr(comp, "url", "") or ""
                title = getattr(comp, "title", "") or ""
                content = getattr(comp, "content", "") or ""
                text = "\n".join(x for x in (title, content, url) if x)
                if text:
                    new_chain.append(Plain(text))
                continue
            if name == "Json":
                data = getattr(comp, "data", None)
                parts = []
                if isinstance(data, dict):
                    for key in ("text", "content", "title", "summary", "url", "prompt"):
                        v = data.get(key)
                        if isinstance(v, str) and v.strip():
                            parts.append(v.strip())
                text = "\n".join(parts)
                if text:
                    new_chain.append(Plain(text))
                continue
            if name == "Unknown":
                text = (getattr(comp, "text", "") or "").strip()
                if text:
                    new_chain.append(Plain(text))
                continue
            new_chain.append(comp)
        if len(new_chain) != len(result.chain):
            result.chain = new_chain
