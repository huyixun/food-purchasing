"""应用装配：创建 FastAPI 实例、注册错误处理器与路由、挂载聊天页。

运行：
    python -m prep_assistant.main serve          # 推荐
    uvicorn prep_assistant.api.app:app --reload  # 开发模式
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from prep_assistant.api.errors import AppError, error_body
from prep_assistant.api.routes.admin import router as admin_router
from prep_assistant.api.routes.auth import router as auth_router
from prep_assistant.api.routes.chat import RateLimiter, router as chat_router
from prep_assistant.infra import db as database
from prep_assistant.infra.config import REPO_ROOT, load_config
from prep_assistant.infra.security import seed_users


def create_app(db_path: str | None = None) -> FastAPI:
    """构建应用实例。

    db_path 参数供测试注入临时库；生产/开发从 config/prototype.yaml 读取。
    """
    cfg = load_config()
    chat_cfg = cfg.get("chat", {})
    resolved_db = db_path or str(REPO_ROOT / chat_cfg.get("db_path", "data/chat.db"))
    limits = chat_cfg.get("rate_limit", {})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动时建表（幂等）+ 播种种子账号（首次生成随机管理员密码）；
        # 请求期连接按需打开，避免跨线程共享连接
        conn = database.connect(app.state.db_path)
        database.init_schema(conn)
        seed_users(conn)
        conn.close()
        yield

    app = FastAPI(title="备料助手", version="0.3.0", lifespan=lifespan)
    app.state.db_path = resolved_db
    app.state.cfg = cfg  # 管理路由共用（上传上限、jwt 有效期等都在这里读）
    app.state.index_dirty = False  # 知识库增删后置 True，下次问答强制重建索引
    app.state.rate_limiter = RateLimiter(
        max_requests=int(limits.get("max_requests", 10)),
        window_seconds=int(limits.get("window_seconds", 60)),
    )

    def _db_conn():
        conn = database.connect(app.state.db_path)
        database.init_schema(conn)  # 幂等，测试临时库也能直接用
        return conn

    app.state.db_conn = _db_conn

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=error_body(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=error_body("INVALID_REQUEST"))

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception):
        # 兜底：任何未处理异常都收敛为统一中文错误体，不向客户端泄漏堆栈
        return JSONResponse(status_code=500, content=error_body("INTERNAL_ERROR"))

    @app.get("/")
    async def index():
        """聊天页（原生 HTML+JS 单文件）。"""
        return FileResponse(REPO_ROOT / "static" / "chat.html")

    # 管理后台样式表等静态资源
    app.mount("/static", StaticFiles(directory=REPO_ROOT / "static"), name="static")

    app.include_router(chat_router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    return app


app = create_app()
