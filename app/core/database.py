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
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

_DB_PATH: str = ""


def _resolve_sqlite_path() -> Path:
    """解析 SQLite 文件路径,默认持久化到 ./data/data.db。"""
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
        """)

        try:
            conn.execute("ALTER TABLE projects ADD COLUMN video_engine TEXT")
            logger.info("[DB] 迁移: 已添加 video_engine 列")
        except Exception:
            pass
    logger.info("[DB] SQLite 初始化完成: %s", _get_db_path())

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
) -> None:
    """插入或更新项目记录(UPSERT)。"""
    ts = created_at or datetime.utcnow().isoformat()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO projects
                (task_id, product_id, status, progress, language, vibe,
                 visual_style, scenes_data, final_video_url, video_engine, error_message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                error_message   = excluded.error_message
            """,
            (project_id, product_id, status, progress, language, vibe,
             visual_style, scenes_data, final_video_url, video_engine, error_message, ts),
        )
    logger.debug("[DB] 项目已同步: %s (status=%s, progress=%.2f)", project_id, status, progress)


def sync_project_from_model(
    project: Any,
    product_id: Optional[str] = None,
) -> None:
    """从 VideoProject 模型同步到数据库(便捷封装)。"""
    try:
        scenes_data = project.model_dump_json() if hasattr(project, "model_dump_json") else json.dumps(project.dict(), ensure_ascii=False, default=str)
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
            created_at=project.created_at.isoformat() if hasattr(project.created_at, "isoformat") else str(project.created_at),
        )
    except Exception as exc:
        logger.error("[DB] 同步项目失败 %s: %s", getattr(project, "project_id", "?"), exc)


def list_projects(page: int = 1, size: int = 20) -> dict:
    """分页返回项目列表(不含 scenes_data)。"""
    offset = (page - 1) * size
    with _get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        rows = conn.execute(
            """
            SELECT task_id, product_id, status, progress, language, vibe,
                   visual_style, final_video_url, video_engine, error_message, created_at
            FROM projects
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (size, offset),
        ).fetchall()
    return {
        "total": total,
        "page": page,
        "size": size,
        "items": [dict(r) for r in rows],
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
