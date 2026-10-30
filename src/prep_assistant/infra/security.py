"""登录鉴权：PBKDF2 密码哈希 + JWT（HttpOnly Cookie）+ 种子账号播种。

设计要点：
- 密码哈希用标准库 hashlib.pbkdf2_hmac，不引入 bcrypt 依赖（私有化环境友好）；
  格式 `pbkdf2_sha256$迭代次数$盐(hex)$哈希(hex)`
- JWT 用 HS256，密钥首次启动时随机生成并持久化到 data/.jwt_secret，
  服务重启后已登录会话不失效；该文件已进 .gitignore，绝不进版本库
- 种子账号（决策：A）：admin 的随机密码打印到控制台并写入
  data/.admin_credentials（同样已 gitignore，提示管理员尽快修改——本期不提供改密功能）
- 身份从 Cookie 解析；员工端聊天页本期仍保持匿名可用（历史决策），只有
  管理后台 /admin/* 强制登录，且 role=admin 才放行
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt

from prep_assistant.infra.config import REPO_ROOT
from prep_assistant.infra.db import create_user, get_user

JWT_ALGORITHM = "HS256"
_ITERATIONS = 120_000  # PBKDF2 迭代次数：OWASP 2023 建议 60 万，这里在原型性能与安全间取中


# ── 密码哈希 ────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """明文 → PBKDF2-SHA256 哈希（自带随机盐）。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    """校验密码：用存储串里的盐和迭代次数重算一遍，恒定时间比较。"""
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", plain.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return secrets.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False  # 存储串损坏一律视为校验失败，不抛异常


# ── JWT ─────────────────────────────────────────────────────

def _jwt_secret() -> bytes:
    """读（首次则生成并落盘）JWT 签名密钥。"""
    path = REPO_ROOT / "data" / ".jwt_secret"
    if path.exists():
        return path.read_bytes().strip()
    secret = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret, encoding="utf-8")
    return secret


def issue_token(employee_no: str, role: str, hours: int = 12) -> str:
    """签发 JWT；有效期默认 12 小时，可由 config 的 admin.jwt_hours 调整。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": employee_no,
        "role": role,
        "iat": now,
        "exp": now + timedelta(hours=hours),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def parse_token(token: str) -> dict | None:
    """解析 JWT；过期 / 篡改 / 格式错误一律返回 None（调用方转为 401/重定向）。"""
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


# ── 请求身份提取 ────────────────────────────────────────────

def current_user(request) -> dict | None:
    """从请求 Cookie 解出当前登录身份；未登录返回 None。

    Cookie 名固定 pa_token；管理员页面与 API 共用本函数。
    """
    token = request.cookies.get("pa_token")
    if not token:
        return None
    payload = parse_token(token)
    if not payload:
        return None
    return {"employee_no": payload.get("sub", ""), "role": payload.get("role", "")}


def require_admin(request) -> dict:
    """管理接口守卫：未登录/过期 → 401，非管理员 → 403。

    页面路由捕获 UNAUTHORIZED 做重定向，API 路由直接交给异常处理器。
    """
    from prep_assistant.api.errors import AppError

    user = current_user(request)
    if not user:
        raise AppError("UNAUTHORIZED")
    if user["role"] != "admin":
        raise AppError("FORBIDDEN")
    return user


# ── 种子账号 ────────────────────────────────────────────────

def seed_users(conn: sqlite3.Connection) -> str | None:
    """首次启动播种账号（幂等）：admin + 员工演示账号 demo。

    返回 admin 初始密码（仅首次生成时非 None，供入口打印）；已存在则返回 None。
    """
    if get_user(conn, "admin") is not None:
        return None

    admin_password = secrets.token_urlsafe(12)
    create_user(conn, "admin", hash_password(admin_password), "admin")
    # 员工演示账号：与聊天页缺省身份 demo 对齐，密码就用 demo（仅原型环境）
    if get_user(conn, "demo") is None:
        create_user(conn, "demo", hash_password("demo"), "employee")

    # 随机密码既打印（前台可见）也落盘（后台启动也能拿到），两处都在提示修改
    cred_path = REPO_ROOT / "data" / ".admin_credentials"
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    cred_path.write_text(
        f"工号: admin\n初始密码: {admin_password}\n"
        f"（生成于 {datetime.now().isoformat(timespec='seconds')}，请尽快修改——本期未提供改密功能）\n",
        encoding="utf-8",
    )
    print(f"[种子账号] admin 初始密码: {admin_password}（已写入 {cred_path}，请尽快修改）")
    return admin_password
