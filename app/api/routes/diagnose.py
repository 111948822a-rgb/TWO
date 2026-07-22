"""一键诊断接口:排查"视频无法生成"的环境与产物问题(V-XRAY #4)。

GET /api/diagnose 返回:
  - 存储根目录(DATA_ROOT / STORAGE_ROOT)的存在性 + 读写权限
  - storage 目录下所有文件(递归)的绝对路径 + 精确字节数
  - ffmpeg 可执行路径 + `ffmpeg -version` 首行输出
  - 最近一次 ffmpeg 体检测试视频的状态

该接口公开(无鉴权),仅返回非敏感的运维信息,供快速定位断点。
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter

from app.core.config import settings
from app.services.compositor import _get_ffmpeg_exe

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["diagnose"])


def _dir_info(path: str) -> dict:
    """返回单个目录的存在性 / 类型 / 读写权限 / (可选)文件清单。"""
    info = {
        "path": path,
        "exists": os.path.exists(path),
        "is_dir": os.path.isdir(path) if os.path.exists(path) else None,
        "readable": os.access(path, os.R_OK) if os.path.exists(path) else False,
        "writable": os.access(path, os.W_OK) if os.path.exists(path) else False,
    }
    return info


def _list_files(root: str, max_entries: int = 200) -> list[dict]:
    """递归列出 root 下所有文件(路径 + 字节数),上限 max_entries 防止输出爆炸。"""
    out: list[dict] = []
    if not root or not os.path.isdir(root):
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(fp)
            except OSError:
                size = -1
            out.append({"path": fp, "size": size})
            if len(out) >= max_entries:
                out.append({"path": f"{root} (已达到 {max_entries} 条上限, 截断)", "size": 0})
                return out
    # 按路径排序,便于阅读
    out.sort(key=lambda x: x["path"])
    return out


def _ffmpeg_version() -> dict:
    """获取 ffmpeg 可执行路径与 -version 首行。"""
    try:
        exe = _get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        return {"exe": None, "ok": False, "error": str(exc), "version": None}
    try:
        proc = subprocess.run(
            [exe, "-version"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
            stdin=subprocess.DEVNULL,
        )
        first_line = proc.stdout.splitlines()[0] if proc.stdout else ""
        return {
            "exe": exe,
            "ok": proc.returncode == 0,
            "version": first_line or None,
            "error": proc.stderr[:500] if proc.returncode != 0 else None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"exe": exe, "ok": False, "error": str(exc), "version": None}


@router.get("/diagnose")
async def diagnose() -> dict:
    """全链路一键诊断。"""
    storage_root = settings.STORAGE_ROOT
    data_root = settings.DATA_ROOT

    # storage 下子目录概览(便于一眼看出 temp/outputs/audios 是否为空)
    subdir_summary = {}
    if storage_root and os.path.isdir(storage_root):
        for sub in ("temp", "outputs", "audios", "assets", "uploads"):
            sp = os.path.join(storage_root, sub)
            if os.path.isdir(sp):
                try:
                    n = sum(len(fs) for _, _, fs in os.walk(sp))
                    total = sum(
                        (os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(sp) for f in fs)
                    )
                except OSError:
                    n, total = -1, -1
                subdir_summary[sub] = {"file_count": n, "total_bytes": total}

    # 最近一次 ffmpeg 体检测试视频状态
    ffmpeg_test = os.path.join(storage_root, "ffmpeg_test.mp4")
    ffmpeg_test_info = None
    if os.path.exists(ffmpeg_test):
        ffmpeg_test_info = {
            "path": ffmpeg_test,
            "exists": True,
            "size": os.path.getsize(ffmpeg_test),
        }
    else:
        ffmpeg_test_info = {"path": ffmpeg_test, "exists": False, "size": 0}

    return {
        "storage_root": _dir_info(storage_root),
        "data_root": _dir_info(data_root),
        "subdir_summary": subdir_summary,
        "ffmpeg_test_video": ffmpeg_test_info,
        "ffmpeg": _ffmpeg_version(),
        "storage_files": _list_files(storage_root),
        "note": (
            "查看全链路日志:在 Render 控制台 / 日志面板 搜索关键词 "
            "'[全链路自检]' 可追踪每个分镜 图/视频/音频 的本地落盘路径与字节数;"
            "搜索 '[FFmpeg 体检]' 查看合成前环境体检结果;"
            "搜索 '[Download]' 查看视频片段下载明细。"
        ),
    }
