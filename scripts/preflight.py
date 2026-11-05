"""演示前自检：10 秒内告诉你「今天这场演示能演到哪一幕」。

为什么要单独写一个：
    演示最怕的不是功能没做完，是开场 30 秒才发现没配 API key / 库里没有批次 /
    管理员密码文件丢了。这些都要在观众进场前暴露，不能在投屏上暴露。

跑法：
    .venv/Scripts/python.exe scripts/preflight.py

退出码：
    0 = 可完整演示（含真实 LLM）
    1 = 只能演示离线部分（闸门 / 阻断 / 审计留痕），口播需提前改口径

注意：本脚本只读，不建表、不写库、不发网络请求。
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

OK, WARN, FAIL = "[ OK ]", "[WARN]", "[FAIL]"
_failures: list[str] = []
_warnings: list[str] = []


def _say(tag: str, title: str, detail: str = "") -> None:
    line = f"{tag} {title}"
    if detail:
        line += f" — {detail}"
    print(line)


def check_python() -> None:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 11):
        _say(OK, "Python 版本", f"{major}.{minor}")
    else:
        _failures.append("Python 版本低于 3.11")
        _say(FAIL, "Python 版本", f"{major}.{minor}（需要 >= 3.11）")


def check_deps() -> None:
    required = ["fastapi", "uvicorn", "pydantic", "httpx", "yaml", "jwt",
                "jinja2", "pypdf", "numpy", "faiss", "rank_bm25", "jieba"]
    missing = []
    for name in required:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 缺依赖就是缺，不区分原因
            missing.append(name)
    if missing:
        _failures.append(f"缺依赖：{', '.join(missing)}")
        _say(FAIL, "依赖", f"缺少 {', '.join(missing)}（pip install -r requirements.txt）")
    else:
        _say(OK, "依赖", f"{len(required)} 个运行时包齐备")


def check_llm_key() -> None:
    key_env = "DEEPSEEK_API_KEY"
    cfg_path = REPO_ROOT / "config" / "prototype.yaml"
    if cfg_path.exists():
        try:
            import yaml
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            key_env = cfg.get("llm", {}).get("api_key_env", key_env)
        except Exception:  # noqa: BLE001 配置读不到就用默认变量名
            pass
    if os.getenv(key_env):
        _say(OK, "LLM 密钥", f"{key_env} 已设置")
    else:
        _warnings.append("无 LLM 密钥：只能演离线幕（闸门 / 阻断 / 留痕）")
        _say(WARN, "LLM 密钥", f"{key_env} 未设置 → 真实问答会返回 503，改走离线演示")


def _db_path() -> Path:
    cfg_path = REPO_ROOT / "config" / "prototype.yaml"
    if cfg_path.exists():
        try:
            import yaml
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            rel = cfg.get("chat", {}).get("db_path", "data/chat.db")
            return REPO_ROOT / rel
        except Exception:  # noqa: BLE001
            pass
    return REPO_ROOT / "data" / "chat.db"


def check_data() -> None:
    db = _db_path()
    if not db.exists():
        _failures.append(f"数据库不存在：{db}")
        _say(FAIL, "数据库", f"{db} 不存在，先跑一次 serve 让它建表")
        return
    _say(OK, "数据库", str(db.relative_to(REPO_ROOT)))

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        def count(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0])

        batches = count("SELECT COUNT(*) FROM batches")
        pending = count("SELECT COUNT(*) FROM batches WHERE remaining_shelf_life_hours IS NULL")
        no_yield = count("SELECT COUNT(*) FROM batches WHERE measured_yield IS NULL")
        sources = count("SELECT COUNT(*) FROM knowledge_sources")
        templates = count("SELECT COUNT(*) FROM prompt_templates WHERE is_active = 1")
        messages = count("SELECT COUNT(*) FROM messages")

        if batches >= 1:
            _say(OK, "批次台账", f"{batches} 条（缺效期 {pending} 条 / 缺出成率 {no_yield} 条，可演失败分支）")
        else:
            _failures.append("批次台账为空，第二幕无数据可演")
            _say(FAIL, "批次台账", "0 条，需要先播种或扫码录入")

        if sources >= 1:
            _say(OK, "知识来源", f"{sources} 个")
        else:
            _failures.append("知识库为空，第一幕无法演")
            _say(FAIL, "知识来源", "0 个")

        _say(OK if templates else WARN, "生效提示词", f"{templates} 个版本生效" if templates else "未设置，用出厂默认")

        _say(OK if messages else WARN, "会话记录",
             f"{messages} 条" if messages else "无历史，审计页会是空的（演示前先聊两轮）")
        if not messages:
            _warnings.append("无历史会话，第五幕（审计留痕）需要现场先产生数据")
    finally:
        conn.close()


def check_secrets() -> None:
    creds = REPO_ROOT / "data" / ".admin_credentials"
    if creds.exists():
        _say(OK, "管理员凭证", "data/.admin_credentials 存在")
    else:
        _warnings.append("管理员凭证文件不存在：登录页密码需现场生成（首次启动会打印）")
        _say(WARN, "管理员凭证", "文件不存在，首次启动时看控制台")

    for name in (".jwt_secret", ".hmac_secret"):
        p = REPO_ROOT / "data" / name
        _say(OK if p.exists() else WARN, f"密钥文件 {name}", "存在" if p.exists() else "未生成（首启自动生成）")


def check_known_gaps() -> None:
    """把已知缺口当众说，比被观众问出来强。"""
    gaps = []
    if not (REPO_ROOT / "templates" / "draft.html").exists():
        gaps.append("确认卡片页缺失 → 第二幕只能停在「卡片已发出」，确认走接口确认而非页面点击")
    if not (REPO_ROOT / "src" / "prep_assistant" / "api" / "routes" / "prep.py").exists():
        gaps.append("/prep/drafts/{id} 无页面 → 铁律里给的链接点开是 404，别现场点")
    if gaps:
        for g in gaps:
            _say(WARN, "已知缺口", g)
        _warnings.append("存在 2 处确认入口缺口，第二幕必须提前改口播")


def main() -> int:
    print("=" * 68)
    print("备料助手 · 演示前自检")
    print("=" * 68)
    check_python()
    check_deps()
    check_llm_key()
    check_data()
    check_secrets()
    check_known_gaps()

    print("-" * 68)
    if _failures:
        print("结论：不能演示。先修：")
        for item in _failures:
            print(f"  - {item}")
        return 1
    if _warnings:
        print("结论：可演示，但口播要改口径：")
        for item in _warnings:
            print(f"  - {item}")
        print("建议走 docs/demo-script.md 的「离线版」轨迹。")
        return 1
    print("结论：可完整演示（真实 LLM）。按 docs/demo-script.md 十二分钟版走。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
