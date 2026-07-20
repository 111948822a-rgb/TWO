"""桌面端启动器:双击运行,本地静默启动 FastAPI,自动打开浏览器。

产品形态(V17.4):用户下载后双击运行,数据全部存储在本地硬盘,无需云端。

设计要点:
    1. 后台线程运行 uvicorn(监听 127.0.0.1:8000)。
       —— 采用线程而非 `python -m uvicorn` 子进程,可避免 PyInstaller 冻结后
          `sys.executable -m uvicorn` 重新触发 bootloader 的坑,确保打包 .exe 也能跑。
       —— 无额外控制台窗口(天然满足"不弹黑框框";Windows 打包时再用 --windowed 隐藏本进程)。
    2. 每秒轮询 http://127.0.0.1:8000/docs,直到返回 200 确认服务就绪。
    3. 用 webbrowser.open 自动打开默认浏览器展示系统界面。
    4. 捕获 Ctrl+C / 关闭信号,通过 os._exit 干净退出(线程为 daemon,随进程结束)。
       前端"关闭软件"按钮会调用 /api/shutdown 触发同样的优雅退出。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser

import urllib.request

# 标记桌面模式:后端据此开放 POST /api/shutdown
os.environ.setdefault("AIVS_DESKTOP", "1")

HOST = "127.0.0.1"
PORT = 8000
DOCS_URL = f"http://{HOST}:{PORT}/docs"
APP_URL = f"http://{HOST}:{PORT}/"


def _run_server() -> None:
    """在后台线程中启动 uvicorn(import app.main 在桌面环境中已随包打包)。"""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=PORT,
        log_level="info",
    )


def _wait_until_ready(timeout: int = 60) -> bool:
    """轮询 /docs,直到返回 HTTP 200 或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(DOCS_URL, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main() -> None:
    print("🚀 正在启动 AI 视频印钞机…")
    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    try:
        if not _wait_until_ready():
            print("⚠️ 服务启动超时,请检查日志或端口是否被占用。")
            os._exit(1)

        print(f"✅ 服务已就绪,正在打开浏览器: {APP_URL}")
        webbrowser.open(APP_URL)

        # 主线程保持存活,直到整个进程被关闭(Ctrl+C / 前端关闭软件按钮)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 收到关闭信号,正在退出…")
    finally:
        # daemon 线程会随进程退出;此处确保进程彻底结束,不残留
        os._exit(0)


if __name__ == "__main__":
    main()
