# -*- coding: utf-8 -*-
"""管理台的底层适配器包。

壳（8767 管理台）只许导入本包。本文件只转出协议、错误与登记，不导入任何具体适配器：
具体适配器由 ``registry.adapter()`` 按需延迟加载。
"""
from .base import (  # noqa: F401
    AdapterError,
    AdapterUnavailable,
    BackendAdapter,
    BOT_ID_MESSAGE,
    LifecycleError,
    SecretToken,
    UNAVAILABLE_TEXT,
    capability_on,
    check_notify,
    require_capability,
    valid_bot_id,
)
from . import registry  # noqa: F401
