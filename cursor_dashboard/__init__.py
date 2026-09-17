"""查询多个 Cursor 账号的订阅套餐和额度余量。

两个入口共用同一套取数逻辑：
    cursor-quota   终端 CLI      -> cursor_dashboard.cli
    cursor-panel   Web 面板      -> cursor_dashboard.server
"""

from .client import ENDPOINTS, AuthExpired, CursorClient, RateLimited, fetch_one
from .usage import assemble, collect

__version__ = "0.0.9"

__all__ = [
    "AuthExpired",
    "RateLimited",
    "CursorClient",
    "ENDPOINTS",
    "fetch_one",
    "assemble",
    "collect",
]
