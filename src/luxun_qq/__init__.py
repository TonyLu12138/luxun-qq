# -*- coding: utf-8 -*-
"""luxun-qq：从 QQ 聊天记录里学「我的聊天风格」，并用鲁迅式锐评腔回话。

包结构：

* ``schema``  —— 数据契约（``Message`` / ``Exchange`` / ``Direction`` / ``MsgKind``）
* ``config``  —— 配置加载（``Config``）
* ``corpus``  —— 语料构建 + 风格统计 + 检索
  （``pair_exchanges`` / ``analyze_style`` / ``build_style_index`` / ``StyleRetriever``）
* ``style``   —— 风格指南渲染（``LUXUN_PERSONA`` / ``build_style_guide``）

导入策略：子模块导入全部包在 ``try/except`` 里，
任何子模块缺失（或环境不完整）都不会让 ``import luxun_qq`` 失败，
也天然规避循环导入。
"""
from __future__ import annotations

__version__ = "0.2.0"

__all__ = [
    # 子模块
    "config",
    "corpus",
    "schema",
    "style",
    # 数据契约
    "Message",
    "Direction",
    "MsgKind",
    "Exchange",
    "Config",
    # 语料 / 检索
    "pair_exchanges",
    "analyze_style",
    "build_style_index",
    "StyleRetriever",
    "to_style_guide",
    "write_style_report",
    # 风格指南
    "build_style_guide",
    "LUXUN_PERSONA",
    # 版本
    "__version__",
]

# ---- 子模块本身（``from luxun_qq import schema`` 一类用法）----
try:
    from . import config, corpus, schema, style  # noqa: F401
except Exception:  # pragma: no cover - 缺子模块也不该炸
    pass

# ---- schema：核心数据契约 ----
try:
    from .schema import (  # noqa: F401
        SCHEMA_VERSION,
        Direction,
        Exchange,
        Message,
        MsgKind,
        read_messages,
        write_jsonl,
    )
except Exception:  # pragma: no cover
    Direction = Exchange = Message = MsgKind = None  # type: ignore[assignment]

# ---- config ----
try:
    from .config import Config  # noqa: F401
except Exception:  # pragma: no cover
    Config = None  # type: ignore[assignment]

# ---- corpus：语料构建与检索 ----
try:
    from .corpus import (  # noqa: F401
        StyleRetriever,
        analyze_style,
        build_style_index,
        detect_emotion,
        extract_keywords,
        pair_exchanges,
        read_style_index,
        to_style_guide,
        write_style_index,
    )
except Exception:  # pragma: no cover
    StyleRetriever = None  # type: ignore[assignment]
    pair_exchanges = analyze_style = build_style_index = None  # type: ignore[assignment]
    to_style_guide = None  # type: ignore[assignment]

# ---- style：风格指南（以 style 层为出口，它内部转发到 corpus 实现）----
try:
    from .style import LUXUN_PERSONA, build_style_guide, write_style_report  # noqa: F401
except Exception:  # pragma: no cover
    LUXUN_PERSONA = ""  # type: ignore[assignment]
    build_style_guide = write_style_report = None  # type: ignore[assignment]
