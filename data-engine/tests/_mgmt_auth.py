"""Management API 认证头辅助——API_KEY_ENABLED=true 时测试脚本需携带 X-API-Key

从 config/.env 读取 API_KEY_ENABLED / API_KEY：
- 认证关闭或未配置 → 返回空 dict（兼容本地开发模式）
- 认证开启 → 返回 {"X-API-Key": <key>}
"""

import os

_ENV_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", ".env"
)


def _read_env(key: str) -> str:
    try:
        with open(_ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip().lower() == key.lower():
                    return v.strip()
    except Exception:
        pass
    return ""


def auth_headers() -> dict:
    """返回需要附加到 Management API 请求上的认证头"""
    enabled = _read_env("API_KEY_ENABLED").lower()
    if enabled not in ("true", "1", "yes"):
        return {}
    key = _read_env("API_KEY")
    return {"X-API-Key": key} if key else {}
