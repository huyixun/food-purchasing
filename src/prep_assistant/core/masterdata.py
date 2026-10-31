"""F10 主数据与口径字典版本。

决策 B3.1：三套外部系统编码不一致，助手自建品类 / 批次 / 门店三张码表并维护映射。

约束：
- 未映射的品类不出建议（宁可空白，不可瞎算）
- 字典按「品类 + 版本」取用，新版本只增不改，历史建议的追溯指针不变
- 映射歧义记录进人工修复队列，不进入自动推荐样本

实现说明：
- 字典是**带版本的只读对象**，不是配置常量——换算要能指回当时的版本逐位重建（f45）。
- 出成率属商业秘密（PRD 8.2），字典只在服务端被确定性代码读取，不进任何模型上下文。
- 本模块只读，字典变更走「新增版本文件 + 配置指向」，不做运行时写。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from prep_assistant.infra.config import REPO_ROOT

DICT_PATH = REPO_ROOT / "config" / "dict.yaml"


@dataclass(frozen=True)
class Material:
    """单个品类的字典条目（不可变，防止被上层就地修改）。"""

    code: str
    name: str
    category: str
    denominator_g: int      # 1 份成品所需净料克数
    base_yield: float       # 字典基准出成率
    process_hours: float    # 从解冻到加工的占用时长


@dataclass(frozen=True)
class Dictionary:
    """一个版本的完整字典。version 会写进每张建议单，用于追溯。"""

    version: str
    effective_from: str
    materials: dict[str, Material]
    buffer_hours: float
    warn_ratio: float

    def get(self, code: str) -> Material | None:
        return self.materials.get(code)


_cache: dict[str, Dictionary] = {}


def load_dict(path: Path | None = None) -> Dictionary:
    """加载字典；同一路径只读一次（字典不可变，进程内缓存安全）。

    测试可传自定义 path，不依赖全局状态。
    """
    target = path or DICT_PATH
    key = str(target)
    if key in _cache:
        return _cache[key]

    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    shelf = raw.get("shelf_life", {})
    dictionary = Dictionary(
        version=str(raw["dict_version"]),
        effective_from=str(raw.get("effective_from", "")),
        materials={
            item["code"]: Material(
                code=item["code"],
                name=item["name"],
                category=item["category"],
                denominator_g=int(item["denominator_g"]),
                base_yield=float(item["base_yield"]),
                process_hours=float(item["process_hours"]),
            )
            for item in raw["materials"]
        },
        buffer_hours=float(shelf.get("buffer_hours", 1.5)),
        warn_ratio=float(shelf.get("warn_ratio", 0.8)),
    )
    _cache[key] = dictionary
    return dictionary
