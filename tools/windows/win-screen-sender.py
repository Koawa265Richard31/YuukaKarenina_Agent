# -*- coding: utf-8 -*-
"""Windows 屏幕推送脚本（托盘版）。

功能：
  - 每 N 分钟截屏，与上一张感知哈希对比，变化明显才推送（锁屏/黑屏/关机自动跳过）
  - 系统托盘图标（右键菜单）：
      · 实时状态（运行中 / 上次推送 / 下次检查 / 开机自启开关）
      · 立即推送一张
      · 开机自启动开关（注册表 HKCU Run，无管理员权限）
      · 退出
  - CLI 模式（--once / --force）兼容无托盘/调试场景

依赖：pip install pillow requests pystray
"""
import argparse
import ctypes
import hashlib
import io
import os
import subprocess
import sys
import threading
import time

import requests
from PIL import Image, ImageDraw, ImageGrab

# ============ 配置 ============
# 服务器地址/token 一律从本地配置读（仓库公开，绝不写死公网地址/凭据）：
#   环境变量 WSCREEN_SERVER / WSCREEN_TOKEN，或同目录 screen-sender.conf：
#     SERVER_URL=https://<你的服务器>:<端口>
#     UPLOAD_TOKEN=xxx
# 未配置 SERVER_URL 时回退本机调试地址（无法远程推送，托盘状态会提示）。
def _load_server_base():
    env = os.environ.get("WSCREEN_SERVER", "").strip().rstrip("/")
    if env:
        return env
    conf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screen-sender.conf")
    if os.path.exists(conf):
        try:
            with open(conf, encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("SERVER_URL="):
                        v = line.strip().split("=", 1)[1].strip().strip('"').rstrip("/")
                        if v:
                            return v
        except Exception:
            pass
    return "http://127.0.0.1:6201"  # 仅本地调试

_SERVER_BASE = _load_server_base()
SERVER_URL = _SERVER_BASE + "/screen/upload"
POLL_URL = _SERVER_BASE + "/screen/poll"
POLL_INTERVAL = 6  # 拉图命令轮询间隔（秒）
# token 从环境变量 WSCREEN_TOKEN 或同目录 screen-sender.conf（UPLOAD_TOKEN=xxx）读取，
# 不要写死在脚本里（仓库是公开的）
UPLOAD_TOKEN = os.environ.get("WSCREEN_TOKEN", "")
INTERVAL_SECONDS = 30 * 60
HASH_DIFF_THRESHOLD = 32
JPEG_QUALITY = 80
BLACK_MEAN_THRESHOLD = 10
APP_NAME = "WinScreenSender"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# ==============================

_upload_lock = threading.Lock()  # 上传动作串行（周期推送 vs 拉图回传）

# 本地诊断日志（同目录 wscreen-sender.log；写失败静默，不影响主功能）
def _log(msg):
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wscreen-sender.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def load_token():
    tok = os.environ.get("WSCREEN_TOKEN", "").strip()
    if tok:
        return tok
    conf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screen-sender.conf")
    if os.path.exists(conf):
        try:
            with open(conf, encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("UPLOAD_TOKEN="):
                        tok = line.strip().split("=", 1)[1].strip().strip('"')
                        break
        except Exception:
            pass
    return tok

_state = {
    "running": True,
    "last_push": "—",
    "next_check": "—",
    "last_error": "",
    "force": threading.Event(),
    "quit": threading.Event(),
    # 被动推送周期（秒）：默认 30 分钟；服务端插件面板改 passive_interval 后，poll 下发自动更新
    "interval_seconds": INTERVAL_SECONDS,
    "reschedule": False,
}


# ---------------- 系统状态 ----------------
def session_locked():
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return False
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 512)
        cls = buf.value
        return "LockApp" in cls or cls == "Windows.UI.Core.CoreWindow"
    except Exception:
        return False


def screen_is_black(img):
    try:
        g = img.convert("L").resize((32, 32))
        px = list(g.getdata())
        return sum(px) / len(px) < BLACK_MEAN_THRESHOLD
    except Exception:
        return False


def phash(img, size=16):
    g = img.convert("L").resize((size, size))
    px = list(g.getdata())
    avg = sum(px) / len(px)
    return sum(1 << i for i, p in enumerate(px) if p > avg)


def hamming(a, b):
    return bin(a ^ b).count("1")


def capture():
    try:
        return ImageGrab.grab(all_screens=True)
    except TypeError:
        return ImageGrab.grab()


def upload(img, force=False, request_id=None) -> bool:
    _log(f"upload 开始 force={force} request_id={request_id or ''}")
    with _upload_lock:
        buf = io.BytesIO()
        # 多屏全景截图可能很宽（如 6400px），先缩到合理尺寸——
        # 超大图会让 vision 模型降采样漏看细节/区域
        MAX_W = 2560
        if img.width > MAX_W:
            ratio = MAX_W / img.width
            img = img.resize((MAX_W, max(1, int(img.height * ratio))), Image.LANCZOS)
        img.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY)
        buf.seek(0)
        digest = hashlib.md5(buf.getvalue()).hexdigest()[:8]
        token = load_token()
        if not token:
            _state["last_error"] = "未配置 UPLOAD_TOKEN（环境变量 WSCREEN_TOKEN 或同目录 screen-sender.conf）"
            return False
        try:
            headers = {"X-Token": token}  # token 走 header，避免出现在 URL/访问日志
            if force:
                headers["X-Force"] = "1"  # 主动推送标记：服务器必回
            if request_id:
                headers["X-Request-Id"] = request_id  # 拉图回传标记
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            # 连接类失败（瞬时网络抖动/服务器 reset）自动重试，避免拉图回传因一次抖动失败
            last_err = ""
            for attempt in range(3):
                try:
                    buf.seek(0)
                    s = requests.Session()
                    s.trust_env = False  # 绕过本机代理（系统代理会拦截/误转发 https 流量）
                    r = s.post(
                        SERVER_URL,
                        headers=headers,
                        files={"file": (f"screen-{int(time.time())}-{digest}.jpg", buf, "image/jpeg")},
                        timeout=60,
                        verify=False,  # 服务器 nginx 为自签证书；安全由 token 保证
                    )
                    ok = r.status_code == 200
                    _state["last_push"] = time.strftime("%H:%M:%S")
                    _state["last_error"] = "" if ok else f"HTTP {r.status_code}"
                    _log(f"upload 完成 ok={ok} http={r.status_code} (attempt {attempt+1})")
                    return ok
                except Exception as e:
                    last_err = str(e)
                    _log(f"upload 第 {attempt+1} 次异常: {e}")
                    time.sleep(1.5 * (attempt + 1))
            _state["last_error"] = last_err
            _log(f"upload 最终失败: {last_err}")
            return False
        except Exception as e:
            _state["last_error"] = str(e)
            _log(f"upload 异常: {e}")
            return False


def poll_worker():
    """反向轮询：定期问服务器有没有「拉图请求」，有就截屏回传（用户主动要看）；
    同时接收服务端插件面板下发的配置（如被动推送周期 passive_interval）。"""
    while not _state["quit"].is_set():
        try:
            token = load_token()
            if token:
                s = requests.Session()
                s.trust_env = False
                r = s.get(POLL_URL, headers={"X-Token": token}, timeout=20, verify=False)
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                if r.status_code == 200:
                    j = r.json()
                    cfg = j.get("config") or {}
                    iv = cfg.get("passive_interval_sec")
                    if isinstance(iv, (int, float)) and iv >= 10:
                        old = _state.get("interval_seconds")
                        if old != iv:
                            _state["interval_seconds"] = int(iv)
                            _state["reschedule"] = True  # 让 worker 立即按新周期重排
                            _log(f"服务端配置更新：被动推送周期 = {int(iv)//60} 分钟（{int(iv)}s）")
                    if j.get("command") == "take":
                        rid = j.get("request_id", "")
                        _state["last_push"] = time.strftime("%H:%M:%S")
                        # 用户主动要看：即使锁屏/黑屏也截（Yuuka 会看到真实状态）
                        img = capture()
                        ok = upload(img, force=True, request_id=rid)
                        _state["last_error"] = "" if ok else "拉图回传失败"
        except Exception as e:
            _state["last_error"] = f"poll: {e}"
        _state["quit"].wait(POLL_INTERVAL)


# ---------------- 主循环（后台线程） ----------------
def send_static_bubble() -> bool:
    """画面无变化时发的轻量心跳（不带图），服务器随机问候。"""
    token = load_token()
    if not token:
        _state["last_error"] = "未配置 UPLOAD_TOKEN"
        return False
    try:
        s = requests.Session()
        s.trust_env = False
        r = s.post(
            SERVER_URL,
            headers={"X-Token": token, "X-Static": "1"},
            timeout=30,
            verify=False,
        )
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return r.status_code == 200
    except Exception as e:
        _state["last_error"] = f"static: {e}"
        return False


def loop_once(prev_hash, force):
    if session_locked():
        return prev_hash, "锁屏，跳过"
    img = capture()
    if screen_is_black(img):
        # 记录当前黑屏 hash，避免黑屏切换时误触发
        return phash(img), "黑屏，跳过"
    cur = phash(img)
    diff = hamming(cur, prev_hash) if prev_hash is not None else 999
    if force or prev_hash is None or diff >= HASH_DIFF_THRESHOLD:
        ok = upload(img, force=force)
        return cur, f"推送{'成功' if ok else '失败'}（diff={diff}）"
    # 画面变化不大：不推图、也不发冒泡，直接跳过（保持安静）
    return cur, f"画面未变，跳过（diff={diff}）"


def worker():
    prev_hash = None
    deadline = time.time() + _state["interval_seconds"]
    while not _state["quit"].is_set():
        try:
            # 服务端面板改周期 → poll 置 reschedule：从当前时刻起按新周期重排
            if _state.pop("reschedule", False):
                deadline = time.time() + _state["interval_seconds"]
                _state["next_check"] = time.strftime(
                    "%H:%M:%S", time.localtime(time.time() + _state["interval_seconds"])
                )
            force = _state["force"].is_set()
            _state["force"].clear()
            due = force or time.time() >= deadline
            if due:
                prev_hash, note = loop_once(prev_hash, force)
                deadline = time.time() + _state["interval_seconds"]
                _state["next_check"] = time.strftime(
                    "%H:%M:%S", time.localtime(deadline)
                )
        except Exception as e:
            _state["last_error"] = str(e)
        # 每 5 秒醒一次检查：force 立即响应（≤5s），到期则执行周期推送
        _state["quit"].wait(min(5.0, max(0.5, deadline - time.time())))


# ---------------- 开机自启动（注册表 HKCU Run） ----------------
def _pythonw():
    exe = sys.executable
    return exe.replace("python.exe", "pythonw.exe") if exe.lower().endswith("python.exe") else exe


def _autostart_cmd():
    return f'"{_pythonw()}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def set_autostart(on: bool):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_cmd())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ---------------- 托盘 ----------------
def _tray_icon_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], fill=(46, 204, 113, 255))
    d.rectangle([22, 18, 42, 34], fill=(255, 255, 255, 255))
    return img


def repo_watcher():
    """后台监视线程：每 5 分钟把 E:\YuukaMemory 拉取到与 GitHub 归档一致（与屏幕脚本同生命周期）。"""
    REPO = r"E:\YuukaMemory"
    INTERVAL = 300
    while not _state["quit"].is_set():
        try:
            if os.path.isdir(os.path.join(REPO, ".git")):
                r = subprocess.run(
                    ["git", "-C", REPO, "pull", "--ff-only"],
                    capture_output=True, text=True, timeout=90,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                out = (r.stdout or "").strip().splitlines()
                if r.returncode != 0:
                    _log(f"repo_pull 失败: {(r.stderr or '').strip()[:160]}")
                elif any("Already up to date" in l for l in out):
                    pass  # 无更新，静默
                else:
                    _log(f"repo_pull 已更新: {' | '.join([l for l in out if l][-2:])}")
        except Exception as e:
            _log(f"repo_pull 异常: {e}")
        _state["quit"].wait(INTERVAL)


def tray_main():
    import pystray

    _last_click = {"t": 0.0}  # do_force 防抖：托盘双击/菜单点击可能连发，2s 内忽略第二次

    def do_force(icon, item):
        now = time.time()
        if now - _last_click["t"] < 2.0:
            _log(f"do_force 防抖忽略（距上次 {now - _last_click['t']:.1f}s）")
            return
        _last_click["t"] = now
        _log("do_force 触发（托盘点击/菜单）")
        _state["force"].set()

    def do_autostart(icon, item):
        set_autostart(not autostart_enabled())

    def do_quit(icon, item):
        _state["quit"].set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: "🟢 屏幕推送运行中", None, enabled=False),
        pystray.MenuItem(lambda item: f"上次推送: {_state['last_push']}", None, enabled=False),
        pystray.MenuItem(lambda item: f"下次检查: {_state['next_check']}", None, enabled=False),
        pystray.MenuItem(
            lambda item: ("⚠ " + _state["last_error"]) if _state["last_error"] else "状态正常",
            None,
            enabled=False,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("立即推送一张", do_force, default=True),
        pystray.MenuItem(
            "开机自启动",
            do_autostart,
            checked=lambda item: autostart_enabled(),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", do_quit),
    )
    icon = pystray.Icon(APP_NAME, _tray_icon_image(), "WinScreenSender", menu)
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    tp = threading.Thread(target=poll_worker, daemon=True)
    tp.start()

    def _menu_refresher():
        # pystray 菜单 callable 文本可能不随状态变化刷新，定时强制重绘菜单
        while not _state["quit"].is_set():
            try:
                icon.update_menu()
            except Exception:
                pass
            _state["quit"].wait(3)

    tr = threading.Thread(target=_menu_refresher, daemon=True)
    tr.start()
    tw = threading.Thread(target=repo_watcher, daemon=True)
    tw.start()
    icon.run()


# ---------------- CLI（无托盘/调试） ----------------
def cli_main(args):
    prev_hash = None
    while True:
        try:
            prev_hash, note = loop_once(prev_hash, args.force)
            print(f"[{time.strftime('%H:%M:%S')}] {note}")
            if args.once:
                break
        except Exception as e:
            print(f"错误: {e}")
        time.sleep(INTERVAL_SECONDS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只推一次（CLI）")
    ap.add_argument("--force", action="store_true", help="忽略变化检测强制推送")
    args = ap.parse_args()

    # 服务器地址未配置（本地调试默认）时给出醒目提示
    if _SERVER_BASE.startswith("http://127.0.0.1"):
        _state["last_error"] = (
            "未配置 SERVER_URL（screen-sender.conf 加 SERVER_URL=https://<服务器>:63000），"
            "当前为本地调试地址，无法远程推送"
        )
        _log("警告：" + _state["last_error"])

    # 本文件被系统以"推送一次并退出"方式调用（可选调度）不在此实现；直接进托盘/CLI
    if args.once or args.force:
        cli_main(args)
        return
    try:
        import pystray  # noqa: F401
        tray_main()
    except ImportError:
        print("未安装 pystray，使用 CLI 模式（pip install pystray 可启用托盘）")
        cli_main(args)


if __name__ == "__main__":
    main()

