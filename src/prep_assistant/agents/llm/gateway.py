"""★① LLM 网关（阶段 2 正式版，自 examples/rag_prototype.py 抽取）。

设计要点：
- 供应商无关：任何实现 OpenAI /chat/completions 协议的服务都可用，
  换供应商 = 改 config 的 base_url 与 model，代码零改动
- 密钥只从环境变量读取，永不落盘、永不写日志
- stream_llm() 优先真流式；供应商不支持流式或建连失败时，
  自动降级为「整段返回 → 切片推送」的伪流式，上层无感
- 错误类型化：LLMTimeoutError / LLMUnavailableError 均继承 LLMConfigError，
  路由层据此映射为 504 / 503 的中文错误码

待办（阶段 3）：重试与熔断、多供应商故障转移、llm_calls 审计落库。
"""

from __future__ import annotations

import json
import os
from typing import Iterator

import httpx


class LLMConfigError(RuntimeError):
    """LLM 配置或调用异常的基类，message 面向管理员，必须是可操作的中文。"""


class LLMUnavailableError(LLMConfigError):
    """供应商不可达 / 无密钥 / 响应异常 → 映射 503 LLM_UNAVAILABLE。"""


class LLMTimeoutError(LLMConfigError):
    """生成超时 → 映射 504 LLM_TIMEOUT。"""


def _api_key(cfg: dict) -> str:
    """从配置指定的环境变量读密钥；缺失时给出可操作的提示。"""
    env_name = cfg["llm"].get("api_key_env", "")
    key = os.environ.get(env_name, "")
    if not key:
        raise LLMUnavailableError(
            f"未设置环境变量 {env_name}，无法调用大模型。"
            f"请管理员配置该环境变量，或在 config/prototype.yaml 更换 api_key_env。"
        )
    return key


def _headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {_api_key(cfg)}"}


def _payload(messages: list[dict], cfg: dict, *, stream: bool) -> dict:
    llm = cfg["llm"]
    return {
        "model": llm["chat_model"],
        "messages": messages,
        "temperature": llm.get("temperature", 0.2),
        "stream": stream,
    }


def call_llm(messages: list[dict], cfg: dict) -> str:
    """整段调用，返回完整回答文本（CLI 与伪流式降级共用）。"""
    llm = cfg["llm"]
    url = llm["base_url"].rstrip("/") + "/chat/completions"
    try:
        resp = httpx.post(
            url,
            json=_payload(messages, cfg, stream=False),
            headers=_headers(cfg),
            timeout=llm.get("timeout_seconds", 60),
        )
        resp.raise_for_status()
    except httpx.TimeoutException as exc:
        raise LLMTimeoutError("生成超时，请稍后重试") from exc
    except httpx.HTTPStatusError as exc:
        raise LLMUnavailableError(
            f"供应商返回 {exc.response.status_code}，请检查 base_url / model / 密钥"
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMUnavailableError(f"无法连接大模型服务：{exc}") from exc

    try:
        return resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMUnavailableError("供应商响应结构异常，无法解析回答") from exc


def stream_llm(messages: list[dict], cfg: dict) -> Iterator[str]:
    """流式调用，逐段 yield 增量文本。

    降级规则（决策：真流式＋降级）：
    - 收到任何增量之后出错 → 不降级（避免重复计费），抛类型化异常
    - 建连 / 首包之前出错或供应商不支持 stream → 退回 call_llm
      整段拿到后按小片 yield，前端观感仍是打字机
    """
    llm = cfg["llm"]
    url = llm["base_url"].rstrip("/") + "/chat/completions"
    received = False
    try:
        with httpx.Client(timeout=llm.get("timeout_seconds", 60)) as client:
            with client.stream(
                "POST", url, json=_payload(messages, cfg, stream=True), headers=_headers(cfg)
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue  # 容忍个别坏行，不让整次回答报废
                    choices = obj.get("choices") or [{}]
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        received = True
                        yield delta
                if received:
                    return
    except httpx.TimeoutException as exc:
        if received:
            raise LLMTimeoutError("生成超时，请稍后重试") from exc
    except httpx.HTTPError as exc:
        if received:
            raise LLMUnavailableError(f"生成过程中连接中断：{exc}") from exc
        # 未收到任何增量 → 走下方伪流式降级

    text = call_llm(messages, cfg)
    step = 6  # 每片 6 个字符，接近真人打字节奏
    for i in range(0, len(text), step):
        yield text[i:i + step]
