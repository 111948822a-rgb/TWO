"""V17.0 安全工具: 密码哈希(bcrypt) + JWT 签发/校验。

设计要点:
    - 密码一律使用 bcrypt 哈希存储, 绝不保存明文(见 database.create_user)。
    - JWT 用于无状态会话: 登录后写入 HttpOnly Cookie, 业务路由通过
      get_current_user 解析校验(见 app/api/routes/auth.py)。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

# 内部系统: JWT 密钥优先取环境变量 JWT_SECRET_KEY(生产由 Render 注入并持久化),
# 本地开发回退到固定值(非对外公网机密系统, 仅用于会话标识)。
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "dev-internal-auth-secret-change-in-prod")
ALGORITHM = "HS256"
# 默认 7 天(分钟), 可通过环境变量覆盖
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "10080"))

# bcrypt 哈希上下文(禁止明文存储)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """对明文密码进行 bcrypt 哈希, 返回可安全存储的哈希串。"""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """校验明文密码与存储的 bcrypt 哈希是否匹配。"""
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception:
        # 哈希串格式异常等一律视为校验失败, 不向外暴露细节
        return False


def create_access_token(username: str, user_id: int) -> str:
    """签发 JWT, payload 含 sub=user_id, username, exp。"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode = {"sub": str(user_id), "username": username, "exp": expire}
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """校验 JWT 并返回 payload(dict); 无效或过期返回 None。"""
    try:
        return jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
