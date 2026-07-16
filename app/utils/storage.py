"""
存储抽象层。

当前实现为本地文件系统存储(LocalStorage),
未来可无缝切换到阿里云 OSS:只需新增 OSSStorage 子类并在
get_storage() 工厂中切换,业务代码无需改动。

接口契约:
    - upload(local_path, key) -> url       上传本地文件,返回可访问 URL
    - get_url(key)            -> url       获取已上传文件的 URL
    - save_bytes(data, key)   -> local_path  保存二进制到存储

目录结构(LocalStorage):
    {STORAGE_ROOT}/
        uploads/   用户上传的白底图等
        images/    生成的关键帧
        videos/    生成的视频片段
        audios/    生成的配音
        outputs/   最终合成视频
        temp/      临时文件
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from app.core.config import settings


class IStorage(ABC):
    """存储抽象基类。"""

    @abstractmethod
    def upload(self, local_path: str, key: str) -> str:
        """上传本地文件到存储,返回可访问 URL。"""

    @abstractmethod
    def get_url(self, key: str) -> str:
        """根据 key 获取可访问 URL。"""

    @abstractmethod
    def save_bytes(self, data: bytes, key: str) -> str:
        """保存二进制数据,返回本地路径或 URL。"""


class LocalStorage(IStorage):
    """本地文件系统存储。"""

    def __init__(self, root: Optional[str] = None) -> None:
        self.root = Path(root or settings.STORAGE_ROOT).resolve()
        # 初始化子目录
        for sub in ("uploads", "images", "videos", "audios", "outputs", "temp"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def _full_path(self, key: str) -> Path:
        """根据 key 计算绝对路径,防止越权访问。

        key 形如 "images/scene_001.jpg",会拼接到 root 之下。
        """
        path = (self.root / key).resolve()
        # 安全检查:必须在 root 之下,防止 ../ 越权
        if not str(path).startswith(str(self.root)):
            raise ValueError(f"非法 key 越权访问: {key}")
        return path

    def upload(self, local_path: str, key: str) -> str:
        """将本地文件复制到 storage 对应 key 位置,返回 URL。"""
        src = Path(local_path)
        if not src.exists():
            raise FileNotFoundError(f"源文件不存在: {local_path}")

        dst = self._full_path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return self.get_url(key)

    def get_url(self, key: str) -> str:
        """返回可访问 URL(开发期为绝对路径)。"""
        return str(self._full_path(key))

    def save_bytes(self, data: bytes, key: str) -> str:
        """将二进制保存到 storage,返回本地路径。"""
        dst = self._full_path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        return str(dst)


# ---------------------------------------------------------------------------
# 工厂:后续可在此根据 settings.STORAGE_BACKEND 切换实现
# ---------------------------------------------------------------------------

_storage_instance: Optional[IStorage] = None


def get_storage() -> IStorage:
    """获取存储实例(单例)。

    未来扩展示例:
        if settings.STORAGE_BACKEND == "oss":
            return OSSStorage(...)
        return LocalStorage()
    """
    global _storage_instance
    if _storage_instance is None:
        _storage_instance = LocalStorage()
    return _storage_instance
