"""SQLite 持久化层(V8.0)。

使用 sqlite3 标准库,轻量无依赖。
两张表:
    products  — 产品库(name, selling_points, image_urls)
    projects  — 历史记录(task_id, status, scenes_data JSON, final_video_url)

每次调用创建独立连接,避免 asyncio 多线程问题。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

_DB_PATH: str = ""


def _resolve_sqlite_path() -> Path:
    """解析 SQLite 文件路径,默认持久化到 /data/db/data.db。"""
    db_url = (settings.DATABASE_URL or "").strip()
    if db_url.startswith("sqlite:///"):
        raw_path = db_url[len("sqlite:///"):]
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path

    data_root = Path(settings.DATA_ROOT)
    if not data_root.is_absolute():
        data_root = Path.cwd() / data_root
    return data_root / "data.db"


def _get_db_path() -> str:
    global _DB_PATH
    if not _DB_PATH:
        db_path = _resolve_sqlite_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _DB_PATH = str(db_path)
    return _DB_PATH


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_get_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """初始化数据库(幂等,安全重复调用)。"""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                selling_points  TEXT DEFAULT '',
                image_urls      TEXT DEFAULT '[]',
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT NOT NULL UNIQUE,
                hashed_password TEXT NOT NULL,
                display_name    TEXT NOT NULL,
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                task_id           TEXT PRIMARY KEY,
                product_id        TEXT,
                status            TEXT DEFAULT 'pending',
                progress          REAL DEFAULT 0.0,
                language          TEXT DEFAULT 'en',
                vibe              TEXT DEFAULT 'upbeat',
                visual_style      TEXT DEFAULT 'photorealistic',
                scenes_data       TEXT DEFAULT '{}',
                final_video_url   TEXT,
                video_engine      TEXT,
                error_message     TEXT,
                created_at        TEXT NOT NULL,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_projects_created_at
                ON projects(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_users_username
                ON users(username);
        """)

        try:
            conn.execute("ALTER TABLE projects ADD COLUMN video_engine TEXT")
            logger.info("[DB] 迁移: 已添加 video_engine 列")
        except Exception:
            pass

        # V17.0: 标记视频创建者(全员共享历史, 但记录是谁生成的)
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN creator_name TEXT")
            logger.info("[DB] 迁移: 已添加 creator_name 列")
        except Exception:
            pass

        # V17.3: 执行时间打点(耗时计算 / ETA 估算)
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN started_at TEXT")
            logger.info("[DB] 迁移: 已添加 started_at 列")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN completed_at TEXT")
            logger.info("[DB] 迁移: 已添加 completed_at 列")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN logs TEXT")
            logger.info("[DB] 迁移: 已添加 logs 列")
        except Exception:
            pass

        # 自愈: updated_at 心跳,每次状态同步都会刷新,用于判断任务是否卡死
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN updated_at TEXT")
            logger.info("[DB] 迁移: 已添加 updated_at 列")
        except Exception:
            pass
    logger.info("[DB] SQLite 初始化完成: %s", _get_db_path())


# ---------------------------------------------------------------------------
# V17.0 用户系统(极简注册登录)
# ---------------------------------------------------------------------------
def create_user(username: str, hashed_password: str, display_name: str) -> dict:
    """新增用户, 返回用户基础信息(dict, 不含密码哈希)。"""
    ts = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, hashed_password, display_name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, hashed_password, display_name, ts),
        )
        uid = cur.lastrowid
    return {"id": uid, "username": username, "display_name": display_name}


def get_user_by_username(username: str) -> Optional[dict]:
    """按用户名查询用户(含 hashed_password), 不存在返回 None。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    """按主键查询用户(含 hashed_password), 不存在返回 None。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None

def upsert_project(
    project_id: str,
    status: str,
    progress: float,
    language: str,
    vibe: str,
    visual_style: str,
    scenes_data: str,
    final_video_url: Optional[str],
    error_message: Optional[str],
    product_id: Optional[str] = None,
    created_at: Optional[str] = None,
    video_engine: Optional[str] = None,
    creator_name: Optional[str] = None,
    started_at: Optional[str] = None,
    completed_at: Optional[str] = None,
    logs_json: Optional[str] = None,
) -> None:
    """插入或更新项目记录(UPSERT)。

    creator_name 仅在首次 INSERT 时写入; ON CONFLICT 更新的 SET 子句刻意
    不包含 creator_name, 以保证后续状态同步(如阶段推进/重试)不会覆盖创建者标记。

    started_at / completed_at / logs 随每次同步落盘(幂等),供前端耗时/ETA/日志展示。
    """
    ts = created_at or datetime.utcnow().isoformat()
    # 心跳:每次同步都刷新 updated_at,供自愈逻辑判断任务是否卡死
    updated_at = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO projects
                (task_id, product_id, status, progress, language, vibe,
                 visual_style, scenes_data, final_video_url, video_engine,
                 error_message, creator_name, started_at, completed_at, logs,
                 updated_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                product_id      = excluded.product_id,
                status          = excluded.status,
                progress        = excluded.progress,
                language        = excluded.language,
                vibe            = excluded.vibe,
                visual_style    = excluded.visual_style,
                scenes_data     = excluded.scenes_data,
                final_video_url = excluded.final_video_url,
                video_engine    = excluded.video_engine,
                error_message   = excluded.error_message,
                started_at      = excluded.started_at,
                completed_at    = excluded.completed_at,
                logs            = excluded.logs,
                updated_at      = excluded.updated_at
            """,
            (project_id, product_id, status, progress, language, vibe,
             visual_style, scenes_data, final_video_url, video_engine,
             error_message, creator_name, started_at, completed_at, logs_json,
             updated_at, ts),
        )
    logger.debug("[DB] 项目已同步: %s (status=%s, progress=%.2f, creator=%s)",
                 project_id, status, progress, creator_name)


def sync_project_from_model(
    project: Any,
    product_id: Optional[str] = None,
    creator_name: Optional[str] = None,
) -> None:
    """从 VideoProject 模型同步到数据库(便捷封装)。

    creator_name: 创建任务时由业务路由传入当前登录用户的显示名;
                  后续同步(阶段推进/重试)不传(None), 因 ON CONFLICT 更新
                  不会触碰该列, 创建者标记得以保留。
    """
    try:
        scenes_data = project.model_dump_json() if hasattr(project, "model_dump_json") else json.dumps(project.dict(), ensure_ascii=False, default=str)
        logs = getattr(project, "logs", None) or []
        logs_json = json.dumps(
            [l.model_dump() if hasattr(l, "model_dump") else l for l in logs],
            ensure_ascii=False,
        )
        upsert_project(
            project_id=project.project_id,
            status=project.status.value if hasattr(project.status, "value") else str(project.status),
            progress=project.progress,
            language=project.input.language,
            vibe=project.input.vibe,
            visual_style=project.input.visual_style,
            scenes_data=scenes_data,
            final_video_url=project.output.final_video_url,
            error_message=project.error,
            video_engine=getattr(project.output, "video_engine", None),
            product_id=product_id,
            creator_name=creator_name,
            started_at=project.started_at.isoformat() if getattr(project, "started_at", None) else None,
            completed_at=project.completed_at.isoformat() if getattr(project, "completed_at", None) else None,
            logs_json=logs_json,
            created_at=project.created_at.isoformat() if hasattr(project.created_at, "isoformat") else str(project.created_at),
        )
    except Exception as exc:
        logger.error("[DB] 同步项目失败 %s: %s", getattr(project, "project_id", "?"), exc)


def _utc_iso_z(value):
    """将数据库中的裸 UTC 时间字符串规范化为带 'Z' 的 ISO 8601, 供前端按 UTC 正确解析。

    后端全程用 datetime.utcnow() 存储, 但 .isoformat() 不带时区; 前端 new Date(裸串)
    在 UTC+8 环境下会被当作本地时间解析, 导致"已耗时 / 预计剩余"偏差 8 小时。补 'Z'
    即声明其为 UTC。已是带时区(含 Z / +hh:mm)或空值则原样返回(向后兼容旧数据)。
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s or s[-1] in ('Z', 'z'):
        return value
    # 已带时区偏移(如 +08:00 / +0800 / -05:00)
    if ('+' in s[10:]) or (s[10:].startswith('-') and 'T' in s):
        return value
    if 'T' in s:
        return s + 'Z'
    return value


def list_projects(page: int = 1, size: int = 20) -> dict:
    """分页返回项目列表(不含 scenes_data)。"""
    offset = (page - 1) * size
    with _get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        rows = conn.execute(
            """
            SELECT task_id, product_id, status, progress, language, vibe,
                   visual_style, final_video_url, video_engine, error_message,
                   creator_name, started_at, completed_at, created_at
            FROM projects
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (size, offset),
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        # 时间字段补 'Z', 统一为带时区的 ISO 8601, 修复前端 8 小时时区偏差
        for _k in ("started_at", "completed_at", "created_at"):
            if _k in d:
                d[_k] = _utc_iso_z(d[_k])
        items.append(d)
    return {
        "total": total,
        "page": page,
        "size": size,
        "items": items,
    }


def get_project_detail(task_id: str) -> Optional[dict]:
    """返回单个项目的完整详情(含 scenes_data JSON)。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    # 时间字段补 'Z', 统一为带时区的 ISO 8601, 修复前端 8 小时时区偏差
    for _k in ("started_at", "completed_at", "created_at"):
        if _k in result:
            result[_k] = _utc_iso_z(result[_k])
    try:
        result["scenes_data"] = json.loads(result.get("scenes_data") or "{}")
    except (json.JSONDecodeError, TypeError):
        result["scenes_data"] = {}
    return result

def upsert_product(
    name: str,
    selling_points: str,
    image_urls: list[str],
    product_id: Optional[str] = None,
) -> str:
    """新增或更新产品,返回产品 ID。"""
    pid = product_id or uuid.uuid4().hex[:12]
    urls_json = json.dumps(image_urls, ensure_ascii=False)
    ts = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        existing = conn.execute("SELECT id FROM products WHERE id = ?", (pid,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE products SET name=?, selling_points=?, image_urls=? WHERE id=?",
                (name, selling_points, urls_json, pid),
            )
        else:
            conn.execute(
                """
                INSERT INTO products (id, name, selling_points, image_urls, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (pid, name, selling_points, urls_json, ts),
            )
    logger.info("[DB] 产品已保存: %s (%s)", pid, name)
    return pid


def list_products() -> list[dict]:
    """返回全部产品列表。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, selling_points, image_urls, created_at FROM products ORDER BY created_at DESC"
        ).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        try:
            item["image_urls"] = json.loads(item.get("image_urls") or "[]")
        except (json.JSONDecodeError, TypeError):
            item["image_urls"] = []
        result.append(item)
    return result


def get_product(product_id: str) -> Optional[dict]:
    """返回单个产品。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, selling_points, image_urls, created_at FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["image_urls"] = json.loads(item.get("image_urls") or "[]")
    except (json.JSONDecodeError, TypeError):
        item["image_urls"] = []
    return item


def delete_product(product_id: str) -> bool:
    """删除产品,返回是否成功。"""
    with _get_conn() as conn:
        cursor = conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        deleted = cursor.rowcount > 0
    if deleted:
        logger.info("[DB] 产品已删除: %s", product_id)
    return deleted


def delete_project(task_id: str) -> bool:
    """V14.0: 删除项目记录,返回是否成功。"""
    with _get_conn() as conn:
        cursor = conn.execute("DELETE FROM projects WHERE task_id = ?", (task_id,))
        deleted = cursor.rowcount > 0
    if deleted:
        logger.info("[DB] 项目已删除: %s", task_id)
    return deleted


def get_active_tasks(
    stale_seconds: Optional[int] = None, status: Optional[str] = None
) -> list[tuple]:
    """返回处于活跃(未终态)的任务 [(task_id, status), ...]。

    stale_seconds 指定时,仅返回 updated_at 早于 cutoff 的任务(用于看门狗判断卡死)。
    未指定时返回全部活跃任务(用于进程启动时的孤儿任务自愈)。
    status 指定时仅筛选该状态(用于看门狗按状态分别设置不同陈旧阈值)。
    """
    placeholders = ",".join("?" * len(_ACTIVE_STATUSES))
    with _get_conn() as conn:
        if stale_seconds is None:
            if status:
                rows = conn.execute(
                    "SELECT task_id, status FROM projects WHERE status = ?",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT task_id, status FROM projects "
                    f"WHERE status IN ({placeholders})",
                    _ACTIVE_STATUSES,
                ).fetchall()
        else:
            cutoff = (datetime.utcnow() - timedelta(seconds=stale_seconds)).isoformat()
            if status:
                rows = conn.execute(
                    "SELECT task_id, status FROM projects "
                    "WHERE status = ? AND updated_at < ?",
                    (status, cutoff),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT task_id, status FROM projects "
                    f"WHERE status IN ({placeholders}) AND updated_at < ?",
                    (*_ACTIVE_STATUSES, cutoff),
                ).fetchall()
    return [(r["task_id"], r["status"]) for r in rows]


# ---------------------------------------------------------------------------
# V16.0 Dashboard 统计
# ---------------------------------------------------------------------------
# 视为「正在处理中」的任务状态集合:
#   排队(pending) + 各生成阶段,不含 awaiting_selection(等待用户选择)/completed/failed
_ACTIVE_STATUSES = (
    "pending", "scripting", "img_gen", "vid_gen", "audio_gen", "compositing",
)


def get_dashboard_stats() -> dict:
    """V16.0: 返回首页看板聚合统计(只读查询,不触碰生成流水线)。

    说明:
        - total_videos / today_videos 以 final_video_url 非空作为「成功产出」判定。
        - running_tasks 统计仍处于活跃生成阶段的任务(pending 起至 compositing)。
        - 系统未做鉴权,此处为全局统计;接入 user_id 后需追加 WHERE 隔离条件。
    """
    with _get_conn() as conn:
        total_products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

        total_videos = conn.execute(
            "SELECT COUNT(*) FROM projects "
            "WHERE final_video_url IS NOT NULL AND final_video_url != ''"
        ).fetchone()[0]

        today_videos = conn.execute(
            "SELECT COUNT(*) FROM projects "
            "WHERE final_video_url IS NOT NULL AND final_video_url != '' "
            "AND substr(created_at, 1, 10) = date('now')"
        ).fetchone()[0]

        placeholders = ",".join("?" * len(_ACTIVE_STATUSES))
        running_tasks = conn.execute(
            f"SELECT COUNT(*) FROM projects WHERE status IN ({placeholders})",
            _ACTIVE_STATUSES,
        ).fetchone()[0]

    return {
        "total_products": total_products,
        "total_videos": total_videos,
        "today_videos": today_videos,
        "running_tasks": running_tasks,
    }
