# AI 视频印钞机 · 本地桌面端打包与分发指南（V17.4）

战略调整：本系统已从「云端部署」改为「本地桌面客户端」形态。
用户下载后**双击即可运行**，所有数据（数据库 + 生成的图片/视频/音频）全部存在
**本地硬盘**的 `用户文档/AIVideoStudio/` 目录下，无需任何服务器或挂载盘。

---

## 一、给最终用户（最简说明）

1. 你拿到的是一个文件夹，例如 `AI视频印钞机/`（Windows 里是 `AI视频印钞机.exe` 所在目录）。
2. **双击 `AI视频印钞机.exe`**（macOS / Linux 双击同名可执行文件）。
3. 程序会在后台静默启动服务，并**自动打开你的默认浏览器**进入操作界面。
4. 用完后：点界面左下角「🔌 关闭软件」按钮，或直接关掉浏览器 + 后台窗口。
5. 数据都在 `文档/AIVideoStudio/`，卸载时直接删除该文件夹即可。

> ⚠️ 不要只单独拷贝 `.exe`！必须连同整个文件夹一起发给用户，否则缺 `app/`、`frontend/` 等资源会闪退。

---

## 二、开发者：本地打包步骤

### 前置条件
- 已安装 Python 3.10+（macOS/Linux 自带或 brew/apt）
- 安装依赖：`pip install -r requirements.txt`
- 安装打包工具：`pip install pyinstaller`

### Windows
```bat
build_desktop.bat
```
产物：`dist\AI视频印钞机\AI视频印钞机.exe`

### macOS / Linux
```bash
chmod +x build_desktop.sh
./build_desktop.sh
```
产物：`dist/AI视频印钞机/AI视频印钞机`

打包脚本要点（`--windowed` 已内置，运行时不弹黑框框）：
- `--add-data "frontend;frontend"` / `"frontend:frontend"`：把前端页面带进包
- `--add-data "app;app"` / `"app:app"`：把后端代码带进包
- 一系列 `--hidden-import=uvicorn.*`：确保 uvicorn 子模块被打进单目录包

---

## 三、分发

1. 进入 `dist/` 目录，把 `AI视频印钞机/` 整个文件夹压缩成 `AI视频印钞机.zip`。
2. 通过网盘 / 邮件 / U 盘发给用户。
3. 用户解压后双击里面的 `AI视频印钞机.exe` 即可，**无需安装、无需联网（API Key 在你本地 .env 已配置）**。

> 注意：首次运行时 Windows 可能弹「SmartScreen 未知发布者」警告，点「仍要运行」即可；
> 若要消除该提示需对 exe 做代码签名（可选，超出本指南范围）。

---

## 四、本地存储路径（自适应，跨平台）

`app/core/config.py` 在启动时自动解析存储位置：

| 优先级 | 来源 | 用途 |
|--------|------|------|
| 1 | 环境变量 `AIVS_DATA_ROOT` / `AIVS_STORAGE_ROOT` | 服务器 / 挂载盘场景 |
| 2（默认） | `~/Documents/AIVideoStudio/data` 与 `~/Documents/AIVideoStudio/storage` | 普通桌面用户 |

- Windows：`C:\Users\你的用户名\Documents\AIVideoStudio\...`
- macOS：`/Users/你的用户名/Documents/AIVideoStudio/...`
- Linux：`/home/你的用户名/Documents/AIVideoStudio/...`

首次启动会自动创建上述目录，**无需手动初始化**。

---

## 五、优雅关闭原理

- `desktop_main.py` 以环境变量 `AIVS_DESKTOP=1` 启动后端，并在后台线程运行 uvicorn（127.0.0.1:8000）。
- 轮询 `http://127.0.0.1:8000/docs` 直到返回 200，再 `webbrowser.open` 打开界面。
- 前端「关闭软件」按钮 → `POST /api/shutdown` → 后端延迟 0.3s 后终止进程；
  启动器检测到子进程退出即干净收尾，不残留僵尸进程。
- 云端（Render）未设置 `AIVS_DESKTOP`，`/api/shutdown` 返回 403，不影响线上服务。

---

## 六、云端部署（历史兼容，非必须）

若仍需在 Render 运行：仓库的 `render.yaml` 已通过
`AIVS_DATA_ROOT=/data/db`、`AIVS_STORAGE_ROOT=/data/storage` 环境变量
让路径重新指向持久化磁盘，保持历史不丢。该模式仅作兼容保留。
