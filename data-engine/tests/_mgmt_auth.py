"""Management API 测试专用用户通道（20260903，X-API-Key 废除后的替代）

背景：X-API-Key 系统通道已废除（server.py 中间件注释）。它曾让持有
API_KEY 的脚本直接获得 system 角色（≈admin 等效、跳过全部角色/用户/
自助规则），是绕过用户权限体系的旁门。

替代方案：测试/脚本走真实用户身份——登录换取 Bearer token：
- 认证关闭（API_KEY_ENABLED != true，本地开发模式）→ 返回空 dict
- 认证开启 → 用测试专用账号登录（凭据由 MGMT_TEST_USER / MGMT_TEST_PASS
  环境变量提供），token 进程内缓存

测试专用账号的创建（管理员一次性操作，自助注册通道强制 user 角色）：
- 管理员登录 → /dashboard/users 或 POST /api/auth/users 建号（可指定角色）
- 建议建 test_bot（admin，探针/集成测试用）与 test_reader（readonly，
  只读路径测试用），密码强随机，仅存本机密码管理器/环境变量

适用：跑在真实服务上的探针（scripts/debug/_perm_matrix_check.py 等）。
隔离库测试（test_37 等 TestClient 场景）请自建临时用户库，勿用本模块。
"""

import json
import os
import urllib.request

_ENV_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", ".env"
)

_token_cache: dict = {"token": ""}


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


def _post(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def auth_headers(mgmt: str = "http://127.0.0.1:2025",
                 force_refresh: bool = False) -> dict:
    """返回附加到 Management API 请求上的认证头（Bearer，测试专用用户通道）"""
    enabled = _read_env("API_KEY_ENABLED").lower()
    if enabled not in ("true", "1", "yes"):
        return {}  # 本地开发模式（认证关闭）无需头
    if not force_refresh and _token_cache["token"]:
        return {"Authorization": f"Bearer {_token_cache['token']}"}
    username = os.environ.get("MGMT_TEST_USER", "")
    password = os.environ.get("MGMT_TEST_PASS", "")
    if not username or not password:
        raise SystemExit(
            "X-API-Key 已废除（20260903），测试须走真实用户通道："
            "请设置 MGMT_TEST_USER / MGMT_TEST_PASS 环境变量"
            "（测试专用账号，由管理员预先创建，见本文件头部说明）")
    status, r = _post(f"{mgmt}/api/auth/login",
                      {"username": username, "password": password})
    if status != 200 or not r.get("token"):
        raise SystemExit(f"测试专用账号登录失败: {status} {r}")
    _token_cache["token"] = r["token"]
    return {"Authorization": f"Bearer {r['token']}"}
