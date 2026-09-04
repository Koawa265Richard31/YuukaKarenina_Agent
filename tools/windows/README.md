# Windows 屏幕推送端（win-screen-sender.py，托盘版）

把 Windows 电脑屏幕截图推送到服务器 bot（astrbot_screen_watch 插件），
由 bot 判断是否有值得主动开口的变化；观察结果会进入对话上下文。

## 依赖安装（Windows 上执行）
```
pip install pillow requests pystray
```

## 运行（托盘版，推荐）
```
pythonw win-screen-sender.py
```
或直接双击（会弹黑窗，建议用 pythonw 无窗口运行）。

系统托盘出现绿色圆点图标 = 运行中。**右键菜单**：
- 实时状态：上次推送 / 下次检查 / 错误
- **立即推送一张**
- **开机自启动**（勾选 = 开，写入 HKCU 注册表，无需管理员、无黑窗口）
- 退出

## CLI 模式（调试）
```
python win-screen-sender.py --once    # 只推一次
python win-screen-sender.py --force   # 强制推一次（忽略变化检测）
```

## 行为
- 每 30 分钟截屏（多显示器全屏），与上一张感知哈希对比，变化不明显 → 不推送
- **锁屏 / 黑屏（睡眠/关机）→ 自动跳过，不推送**
- 屏幕有明显变化 → 推送 → bot 看图 → 值得说才主动聊；观察描述写入对话上下文

## 配置说明（同目录 screen-sender.conf，由 .gitignore 忽略，勿提交）
```
SERVER_URL=https://<你的服务器>:63000
UPLOAD_TOKEN=<与服务器插件 upload_token 一致>
```
也支持环境变量 `WSCREEN_SERVER` / `WSCREEN_TOKEN`。未配置 SERVER_URL 时回退本地调试地址并在托盘状态提示。
被动推送周期默认 30 分钟，可在服务器插件面板的 `passive_interval` 修改，客户端 poll 自动跟随。
本脚本以 **astrbot-screen-watch** 仓库（tools/windows/）为最新源，本目录仅作归档。

## 隐私提示
截图会上传到你的服务器（HTTPS + token 鉴权），由 bot（vision 模型）查看并写入对话上下文。
如涉及敏感内容，可调低频率；退出托盘即完全停止。
