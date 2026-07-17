"""认证 API (V17.0 极简注册登录系统)。

提供:
    POST /api/auth/register  注册(用户名/密码/显示名 → bcrypt 哈希存储)
    POST /api/auth/login     登录(校验密码 → 签发 JWT 并写入 HttpOnly Cookie)
    GET  /api/auth/me        获取当前用户(从 Cookie 解析 JWT)
    POST /api/auth/logout    退出登录(清除 Cookie)

全局拦截器 get_current_user 供业务路由(projects/history/products/dashboard/chat)
通过 Depends 注入, 未登录直接拒绝访问(401)。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.core.database import create_user, get_user_by_id, get_user_by_username
from app.core.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "access_token"


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------
class UserRegister(BaseModel):
    username: str = Field(
        ..., min_length=3, max_length=32,
        pattern=r"^[A-Za-z0-9_\-]+$",
        description="用户名(3-32位, 仅允许字母/数字/_/-)",
    )
    password: str = Field(..., min_length=6, max_length=128, description="密码(至少6位)")
    display_name: str = Field("", max_length=64, description="显示名称(留空则默认同用户名)")


class UserLogin(BaseModel):
    username: str = Field(..., min_length=1, max_length=32)
    password: str = Field(..., min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# 当前用户依赖(全局拦截器)
# ---------------------------------------------------------------------------
def get_current_user(access_token: Optional[str] = Cookie(default=None)) -> dict:
    """从 HttpOnly Cookie 解析 JWT, 返回当前登录用户信息(dict)。

    未登录 / 凭证无效 / 用户不存在 → 抛出 401。
    业务路由通过 Depends(get_current_user) 注入, 未登录直接拒绝访问。
    """
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或会话已过期,请先登录",
        )
    payload = decode_access_token(access_token)
    if not payload or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录凭证无效或已过期",
        )
    user = get_user_by_id(int(payload["sub"]))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在,请重新登录",
        )
    return user


# ---------------------------------------------------------------------------
# 内部辅助: Cookie 写入 / 清除
# ---------------------------------------------------------------------------
def _set_auth_cookie(response: Response, token: str) -> None:
    """将 JWT 写入 HttpOnly Cookie(httponly + samesite=lax, 防 XSS 读取)。"""
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.post("/register")
async def register(payload: UserRegister, response: Response) -> dict:
    """注册新用户: 校验用户名唯一 → bcrypt 哈希 → 写入 users 表 → 自动登录。"""
    if get_user_by_username(payload.username):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该用户名已被占用,请换一个",
        )
    display_name = payload.display_name.strip() or payload.username
    user = create_user(
        username=payload.username,
        hashed_password=hash_password(payload.password),
        display_name=display_name,
    )
    token = create_access_token(user["username"], user["id"])
    _set_auth_cookie(response, token)
    logger.info("[Auth] 新用户注册成功: %s (id=%s)", user["username"], user["id"])
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
    }


@router.post("/login")
async def login(payload: UserLogin, response: Response) -> dict:
    """登录: 校验用户名 + bcrypt 密码 → 签发 JWT 并写入 HttpOnly Cookie。"""
    user = get_user_by_username(payload.username)
    # 用户名或密码错误统一返回 401, 不区分以避免用户枚举
    if not user or not verify_password(payload.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )
    token = create_access_token(user["username"], user["id"])
    _set_auth_cookie(response, token)
    logger.info("[Auth] 用户登录成功: %s (id=%s)", user["username"], user["id"])
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
    }


@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)) -> dict:
    """获取当前登录用户信息(供前端全局路由守卫调用)。"""
    return {
        "id": current_user["id"],
        "username": current_user["username"],
        "display_name": current_user["display_name"],
    }


@router.post("/logout")
async def logout(response: Response) -> dict:
    """退出登录: 清除 access_token Cookie。"""
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"detail": "已退出登录"}
