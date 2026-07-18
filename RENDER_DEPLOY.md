# Render 部署说明

> 本系统已改造为 **Docker 部署模式**，强依赖 FFmpeg 与中文字体，所有可变数据通过 Render 持久化磁盘（Disk）落盘，避免每次部署被清空。

## 一、部署方式
- **Runtime**：`Docker`（使用仓库根目录的 `Dockerfile`）
- **启动命令**：无需填写，由 `Dockerfile` 的 `CMD` 提供（Gunicorn + Uvicorn Worker，监听 Render 注入的 `$PORT`）
- **Docker Image 已包含**：
  - `ffmpeg`（阶段⑤ 视频合成，直接调用系统二进制）
  - `fonts-noto-cjk`（中文字体，自动被合成模块用于字幕/花字，避免中文变方块）
  - Python 依赖（含 `gunicorn`），见 `requirements.txt`

## 二、必配环境变量（Render → Environment）
复制以下变量到 Render 服务的环境变量配置中（密钥类请勿提交到代码仓库）：

### 应用 / 数据库
- `APP_NAME=ai-video-commerce`
- `DEBUG=false`
- `DATABASE_URL=sqlite:////data/db/data.db`
- `DATA_ROOT=/data/db`
- `STORAGE_ROOT=/data/storage`

> ⚠️ 路径已**强制锁死**为上述绝对路径（见 `app/core/config.py` 的 `_PERSISTENT_DATA_DIR` / `_PERSISTENT_STORAGE_DIR`）。即便 Render 环境变量设成相对路径也会被覆盖，请勿改回相对路径，否则每次部署 `/app` 代码区被重建会导致历史清空。

### AI 能力（必填，否则卡在文案/生图阶段）
- `DEEPSEEK_API_KEY=...`（文案 / 分镜）
- `DEEPSEEK_BASE_URL=https://api.deepseek.com`
- `DASHSCOPE_API_KEY=...`（通义万相 图/视频 + CosyVoice 配音）
- `DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/api/v1`

### 视频 / 配音参数
- `VIDEO_MODEL=wan2.2-i2v-flash`
- `VIDEO_RESOLUTION=1080P`
- `VIDEO_DURATION=5`
- `TTS_MODEL=cosyvoice-v2`
- `TTS_VOICE=longxiaochun_v2`

### 阿里云 OSS（素材托管，强烈建议配置）
- `OSS_ACCESS_KEY_ID=...`
- `OSS_ACCESS_KEY_SECRET=...`
- `OSS_BUCKET_NAME=...`
- `OSS_ENDPOINT=oss-cn-hangzhou.aliyuncs.com`
- `OSS_BASE_URL=`
- `SKIP_MATTING=false`

### 合成阶段（可选）
- `FFMPEG_PATH=`（留空则使用镜像内 ffmpeg）
- `BGM_PATH=`（留空则使用低音量占位音；填入 `.mp3` 路径可启用真实 BGM，建议放到 `/data/storage/assets/bgm/`）
- `SUBTITLE_FONT=`（留空即可；容器内会自动探测 `fonts-noto-cjk` 的中文字体，无需手动指定）

## 三、持久化磁盘（关键，防数据丢失）
Render 每次部署都会重置容器的本地文件系统，因此 **必须** 挂载 Disk，将可变数据落到持久卷：

| Disk 名称（建议） | Mount Path | 用途 |
|---|---|---|
| `data-disk` | `/data` | 整个持久化根：SQLite 数据库 `data.db`（位于 `/data/db`，产品库 + 历史记录），以及所有生成文件（`/data/storage/` 子目录：上传图片、临时素材、配音、输出视频等） |

> ⚠️ **Render 每服务仅支持一块磁盘**。数据库与生成文件必须都放在同一块盘的挂载点之下，因此数据库落 `/data/db`、存储根设为 `/data/storage`。切勿分挂两块盘，也**不要**把盘挂到 `/app` 整盘（会遮盖代码导致启动失败）。

配置要点：
1. 在 Render 服务 → **Disks** 中创建**一块** Disk，挂载到 `/data`（容量建议 10 GB 起，视频产物较大，可按需扩容）。
2. 应用启动时（`app/main.py`）会 **自动创建** `/data/db` 与 `/data/storage` 及其子目录（`outputs/`、`uploads/`、`temp/`、`audios/`、`images/`、`videos/`、`assets/`、`assets/bgm/`），无需手动初始化。

> 注意：`.dockerignore` 已排除本地的 `data/`、`storage/`、`outputs/`、`uploads/`，这些目录不会进入镜像；运行时完全依赖 Render Disk 提供的空目录，由应用自动建好结构。

## 四、部署检查项
- [ ] 镜像构建成功，且内部已安装 `ffmpeg`（`which ffmpeg` 有输出）
- [ ] 镜像内已安装 `fonts-noto-cjk`（查看 `/usr/share/fonts/**/NotoSansCJK*` 存在）
- [ ] 单块 Disk 已挂载到 `/data`
- [ ] 首次启动后检查 `/data/db/data.db` 是否自动创建
- [ ] 发起一次生成任务，确认 `/data/storage/outputs` 产出最终视频，且字幕为中文（非方块）
- [ ] 重新部署后，旧的历史记录（`/data/db/data.db`）与已生成视频仍在

## 五、已知架构说明（非阻塞）
任务状态由 SQLite 持久化驱动（多 Gunicorn worker 共享同一数据库，跨重启保留进度），未使用 Celery + Redis。当前单实例 + 持久化磁盘已可支撑上线。

## 六、V17.0 极简注册登录系统
系统已**放弃飞书登录**，改为内置的极简账号体系（仅供内部协作使用）：

- **账号模型**：全员共享同一份产品库与历史记录（不做数据隔离），但每条历史/视频会标记 `creator_name`（创建者显示名），方便内部复用与交流。
- **密码安全**：使用 `bcrypt`（`passlib`）哈希存储，**绝不保存明文**；登录校验比对哈希。
- **会话机制**：登录成功后签发 **JWT**，写入 **HttpOnly + SameSite=Lax** 的 Cookie（`access_token`）。所有业务接口（`/api/projects`、`/api/history`、`/api/products`、`/api/dashboard`、`/api/chat`）均通过 `get_current_user` 依赖强制登录，未登录返回 401。
- **前端路由守卫**：页面加载时调用 `GET /api/auth/me`，401 则弹出全屏毛玻璃登录/注册遮罩；登录成功后进入主应用，并在左侧导航底部展示当前用户显示名与「退出」按钮。

### 必配环境变量
- `JWT_SECRET_KEY`：JWT 签名密钥，**由 `render.yaml` 的 `generateValue: true` 自动生成并持久化**（部署/重启后保持不变）。⚠️ 请勿在控制台手动填写明文，也不要删除该变量，否则所有已登录会话失效。
  - 本地开发未设置时，会回退到代码内置的开发密钥（仅用于本地测试，非机密系统）。

### 关键文件
- `app/core/security.py`：bcrypt 哈希 + JWT 签发/校验。
- `app/core/database.py`：`users` 表 + `projects.creator_name` 列及迁移。
- `app/api/routes/auth.py`：`register` / `login` / `me` / `logout` 与全局拦截器 `get_current_user`。
- `requirements.txt`：`passlib[bcrypt]==1.7.4`、`bcrypt==4.0.1`、`python-jose[cryptography]==3.3.0`
  （⚠️ `bcrypt` 必须锁定 `4.0.1`：`passlib 1.7.4` 与 `bcrypt>=4.1` 存在 `__about__` 兼容性问题，会导致注册/登录时报 `AttributeError`）。

