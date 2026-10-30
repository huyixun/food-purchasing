"""管理员/员工登录：表单提交 → 校验密码 → 签发 JWT 写入 HttpOnly Cookie。

登录页是服务端渲染表单；校验失败带错误信息重渲染（不暴露「用户不存在/
密码错」的区分，统一提示，防工号枚举）。
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from prep_assistant.infra.config import REPO_ROOT
from prep_assistant.infra.db import get_user
from prep_assistant.infra.security import issue_token, verify_password

router = APIRouter()
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))

LOGIN_PAGE = "login.html"


@router.get("/admin/login")
def login_page(request: Request):
    """登录页；已登录的管理员直接进后台首页。"""
    from prep_assistant.infra.security import current_user

    user = current_user(request)
    if user and user["role"] == "admin":
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(request, LOGIN_PAGE, {"error": None})


@router.post("/admin/login")
def login(request: Request, employee_no: str = Form(...), password: str = Form(...)):
    """校验工号密码，成功则种 Cookie 并跳转。"""
    conn = request.app.state.db_conn()
    user = get_user(conn, employee_no.strip())
    if user is None or not verify_password(password, user["password_hash"]):
        # 统一错误信息，不区分「账号不存在」与「密码错误」
        return templates.TemplateResponse(
            request, LOGIN_PAGE, {"error": "工号或密码不正确"}, status_code=401
        )

    jwt_hours = 12
    try:
        jwt_hours = int(request.app.state.cfg.get("admin", {}).get("jwt_hours", 12))
    except (TypeError, ValueError):
        pass
    token = issue_token(user["employee_no"], user["role"], hours=jwt_hours)
    resp = RedirectResponse("/admin", status_code=303)
    # HttpOnly：JS 读不到，防 XSS 偷 token；SameSite=Lax：防 CSRF 基础防护
    resp.set_cookie(
        "pa_token", token, max_age=jwt_hours * 3600, httponly=True, samesite="lax"
    )
    return resp


@router.post("/admin/logout")
def logout():
    """登出：清 Cookie 回登录页。"""
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie("pa_token")
    return resp
