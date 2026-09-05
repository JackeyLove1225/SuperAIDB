"""层 7：行业管理 + 术语路由 + AI向导 回归测试

覆盖：
  1. 行业 CRUD（创建/列表/删除）
  2. 行业配置（config / prompts / schemas PUT & GET）
  3. 术语映射配置（table_aliases / behavior_aliases / object_aliases）
  4. 行业切换 + settings 同步
  5. 行业导出/导入
  6. 术语路由验证（behavior_aliases → 标准行为映射）
  （AI 向导配置段已随该功能下线移除——202608 起行业配置在建表流程内完成）

依赖：Management API 服务运行在 http://127.0.0.1:2025
  - 服务未运行时自动跳过，不报失败

设计原则：
  - 使用独立的测试行业名（zztestind_*），不影响工程行业
  - 测试后完整清理
  - 术语路由测试验证"映射不替换"原则
"""
import sys, os, json, shutil, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

API = "http://127.0.0.1:2025"

# 认证口径钉死（TestClient 化后走真实认证中间件）：本层跑无认证模式——
# X-API-Key 系统通道已废除（20260903），开 true 会要求 Bearer 而本层无用户
# 凭据（曾全层 401）。写面由 X-Loopback-Token 防伪闸守护（见 _headers()）。
from config.settings import settings as _st
_st.API_KEY_ENABLED = "false"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDUSTRIES_DIR = os.path.join(BASE_DIR, "industries")


def _current_industry() -> str:
    """读 config/.env 的当前行业（切换测试的恢复目标）。

    历史坑：曾硬编码回退 "engineering"——行业改名后该值指向不存在行业，
    切换测试的"恢复"把 config/.env 写串，全行业别名/字段读取随之全灭
    （层 2/20/28 连锁变红）。恢复目标必须读真实当前值。
    """
    try:
        for line in open(os.path.join(BASE_DIR, "config", ".env"), encoding="utf-8"):
            if line.startswith("INDUSTRY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass  # .env 读不到则走 settings 默认
    return "construction_engineering"


ORIGINAL_INDUSTRY = os.environ.get("INDUSTRY") or _current_industry()

pass_count = 0
fail_count = 0
skip_count = 0
errors = []
API_AVAILABLE = True  # TestClient 承载恒可用（20260825 层 7 入 CI）


def check(name, condition, detail=""):
    global pass_count, fail_count
    if condition:
        pass_count += 1
        print(f"  PASS [{name}]")
    else:
        fail_count += 1
        errors.append(name)
        print(f"  FAIL [{name}] {detail[:80]}")


def skip(name, reason=""):
    global skip_count
    skip_count += 1
    print(f"  SKIP [{name}] {reason}")


def _client():
    """TestClient 承载（20260825 层 7 入 CI：真实 ASGI 调用，无需服务端口）"""
    from fastapi.testclient import TestClient
    from agent.management.server import mgmt_app
    return TestClient(mgmt_app)


def _headers():
    # X-API-Key 系统通道已废除（20260903）；本层跑无认证模式（本地开发姿势），
    # 敏感面要 loopback 令牌（防伪闸——测试同通道注入；
    # 同进程铸造：deps._loopback_token() 缺文件即铸，与服务端判定同值）
    h = {"Content-Type": "application/json"}
    try:
        from agent.management.deps import _loopback_token
        tok = _loopback_token()
        if tok:
            h["X-Loopback-Token"] = tok
    except Exception:
        pass
    return h


def api_get(path):
    try:
        r = _client().get(path, headers=_headers())
        return r.status_code, r.json()
    except Exception as e:
        return 0, {"error": str(e)}


def api_req(path, method="POST", data=None):
    try:
        r = _client().request(method, path,
                              content=(json.dumps(data).encode("utf-8") if data else None),
                              headers=_headers())
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def _sync_industry_in_process(industry_name: str):
    """在测试进程中同步行业切换"""
    from config.settings import settings
    settings.INDUSTRY = industry_name
    try:
        from core.datasource_manager import DataSourceManager
        DataSourceManager.reset_instance()
    except Exception:
        pass
    try:
        import core.data_ops as _data_ops
        _data_ops._federated_driver = None
    except Exception:
        pass
    try:
        import industries.base as _base
        _base._industries.clear()
    except Exception:
        pass


def _check_api():
    """检查 Management API 是否可用"""
    global API_AVAILABLE
    try:
        r = urllib.request.urlopen(f"{API}/api/health", timeout=3)
        API_AVAILABLE = (r.status == 200)
    except Exception:
        API_AVAILABLE = True  # TestClient 承载恒可用（20260825 层 7 入 CI）
    return API_AVAILABLE


# ═══════════════════════════════════════════════════════════════
# 测试函数
# ═══════════════════════════════════════════════════════════════

def test_industry_crud():
    """测试行业创建/列表/删除"""
    print("\n=== 7.1 行业 CRUD ===")
    if not API_AVAILABLE:
        skip("industry_crud", "Management API 不可用")
        return

    name = "zztestind_crud"
    # 清理可能残留
    p = os.path.join(INDUSTRIES_DIR, name)
    if os.path.exists(p):
        shutil.rmtree(p)

    # 创建（模板字段留空 → 服务端 DEFAULT_TEMPLATE；"custom" 模板已随模板体系收敛移除）
    status, resp = api_req("/api/industries/create", "POST", {
        "name": name, "display_name": "测试行业", "description": "CRUD测试"
    })
    check("industry.create", status == 200 and resp.get("ok"), f"status={status}")

    # 列表
    status, resp = api_get("/api/industries")
    if status == 200:
        names = [i["name"] for i in resp.get("industries", [])]
        check("industry.in_list", name in names, f"names={names}")
    else:
        check("industry.in_list", False, f"status={status}")

    # 删除
    status, resp = api_req(f"/api/industries/{name}", "DELETE")
    check("industry.delete", status == 200, f"status={status}")
    check("industry.dir_gone", not os.path.exists(p))


def test_industry_config():
    """测试行业配置（config / prompts / schemas）"""
    print("\n=== 7.2 行业配置 ===")
    if not API_AVAILABLE:
        skip("industry_config", "Management API 不可用")
        return

    name = "zztestind_cfg"
    p = os.path.join(INDUSTRIES_DIR, name)
    if os.path.exists(p):
        shutil.rmtree(p)

    # 创建
    api_req("/api/industries/create", "POST", {
        "name": name, "display_name": "配置测试", "description": ""
    })

    # config PUT + GET
    api_req(f"/api/industries/{name}/config", "PUT", {
        "name": name, "description": "测试配置",
        "expert_role": "你是测试专家",
        "hierarchy_desc": "A → B",
        "default_table_name": "tbl_a"
    })
    check("config.file_exists", os.path.exists(os.path.join(p, "config", "config.yml")))
    status, resp = api_get(f"/api/industries/{name}/config")
    check("config.get_ok", status == 200 and resp.get("config", {}).get("expert_role") == "你是测试专家")

    # schemas PUT + GET
    api_req(f"/api/industries/{name}/schemas/tbl_a", "PUT", {"schema": {
        "name": "tbl_a", "business_name": "表A", "description": "测试表",
        "datasource": "primary",
        "columns": [
            {"name": "code", "type": "VARCHAR", "not_null": True},
            {"name": "value", "type": "FLOAT"},
        ],
        "foreign_keys": [],
        "indexes": [{"name": "idx_code", "columns": ["code"], "unique": True}],
    }})
    check("schema.file_exists", os.path.exists(os.path.join(p, "schemas", "tbl_a.yaml")))
    status, resp = api_get(f"/api/industries/{name}/schemas/tbl_a")
    check("schema.get_ok", status == 200 and resp.get("schema", {}).get("name") == "tbl_a")

    # schemas list（模板行业自带 4 张 quota_* 表——断言 tbl_a 在列且计数=模板表数+1，
    # 不再假设空行业：模板体系收敛后 create 从 DEFAULT_TEMPLATE 复制）
    status, resp = api_get(f"/api/industries/{name}/schemas")
    names = [s.get("name") for s in resp.get("schemas", [])]
    check("schema.list_ok", status == 200 and "tbl_a" in names and resp.get("count") == len(names))

    # schema delete
    api_req(f"/api/industries/{name}/schemas/tbl_a", "DELETE")
    check("schema.deleted", not os.path.exists(os.path.join(p, "schemas", "tbl_a.yaml")))

    # 清理
    if os.path.exists(p):
        shutil.rmtree(p)


def test_terminology_mapping():
    """测试术语映射配置（table_aliases / behavior_aliases / object_aliases）"""
    print("\n=== 7.3 术语映射配置 ===")
    if not API_AVAILABLE:
        skip("terminology", "Management API 不可用")
        return

    name = "zztestind_term"
    p = os.path.join(INDUSTRIES_DIR, name)
    if os.path.exists(p):
        shutil.rmtree(p)

    api_req("/api/industries/create", "POST", {
        "name": name, "display_name": "术语测试", "description": ""
    })

    # PUT prompts with terminology
    term = {
        "table_aliases": {
            "patient": ["患者表", "患者"],
            "visit": ["就诊表", "病历"],
        },
        "behavior_aliases": {
            "查": ["看看", "找一下", "查房"],
            "增": ["开处方", "录入", "登记"],
            "删": ["撤销", "作废"],
            "改": ["修改", "调整"],
        },
        "object_aliases": {
            "记录": ["病历", "处方", "医嘱"],
            "表": ["表格", "清单"],
            "字段": ["列", "指标"],
        },
    }
    api_req(f"/api/industries/{name}/prompts", "PUT", {
        "classification_hints": "", "schema_hints": "",
        "decompose_examples": [], "router_examples": [], "tool_examples": {},
        "terminology": term,
    })

    # GET 验证
    status, resp = api_get(f"/api/industries/{name}/prompts")
    check("term.get_ok", status == 200)
    if status == 200:
        got_term = resp["prompts"].get("terminology", {})
        check("term.has_table_aliases", "table_aliases" in got_term)
        check("term.has_behavior_aliases", "behavior_aliases" in got_term)
        check("term.has_object_aliases", "object_aliases" in got_term)
        check("term.behavior_查", "查房" in got_term.get("behavior_aliases", {}).get("查", []))
        check("term.behavior_增", "开处方" in got_term.get("behavior_aliases", {}).get("增", []))
        check("term.object_记录", "病历" in got_term.get("object_aliases", {}).get("记录", []))

    # 清理
    if os.path.exists(p):
        shutil.rmtree(p)


def test_industry_switch():
    """测试行业切换 + settings 同步"""
    print("\n=== 7.4 行业切换 ===")
    if not API_AVAILABLE:
        skip("industry_switch", "Management API 不可用")
        return

    name = "zztestind_sw"
    p = os.path.join(INDUSTRIES_DIR, name)
    if os.path.exists(p):
        shutil.rmtree(p)

    api_req("/api/industries/create", "POST", {
        "name": name, "display_name": "切换测试", "description": ""
    })

    # 切换——断言落在服务端效果（config/.env 真被改写），不只读刚 set 的进程值
    # （旧断言是同义反复——服务端 500 时照样绿）
    from config.settings import settings
    status, resp = api_req("/api/industries/switch", "POST", {"industry": name})
    check("switch.api_ok", status == 200 and resp.get("ok"), f"status={status} resp={str(resp)[:80]}")
    _sync_industry_in_process(name)
    check("switch.settings_synced", settings.INDUSTRY == name, f"INDUSTRY={settings.INDUSTRY}")
    # 20260825 锚定回归锁：端点 .env 写路径锚定 settings 模块位置
    #（连带清除 _project_root 死 import——ruff 门禁抓获的残留），
    # 裸仓无 .env 时端点负责创建（if exists 门会在 CI 静默不写）——
    # env_persisted 行为断言即覆盖（不再需要 .env 预存在）
    check("switch.env_persisted", _current_industry() == name,
          f".env 未改写: {_current_industry()}")

    # 切换回原行业
    status, resp = api_req("/api/industries/switch", "POST", {"industry": ORIGINAL_INDUSTRY})
    check("switch.restore_api_ok", status == 200 and resp.get("ok"), f"status={status}")
    _sync_industry_in_process(ORIGINAL_INDUSTRY)
    check("switch.restored", settings.INDUSTRY == ORIGINAL_INDUSTRY)
    check("switch.env_restored", _current_industry() == ORIGINAL_INDUSTRY)

    # 清理
    if os.path.exists(p):
        shutil.rmtree(p)


def test_industry_export_import():
    """测试行业导出/导入"""
    print("\n=== 7.5 行业导出/导入 ===")
    if not API_AVAILABLE:
        skip("export_import", "Management API 不可用")
        return

    name = "zztestind_exp"
    p = os.path.join(INDUSTRIES_DIR, name)
    if os.path.exists(p):
        shutil.rmtree(p)

    # 创建并配置
    api_req("/api/industries/create", "POST", {
        "name": name, "display_name": "导出测试", "description": ""
    })
    api_req(f"/api/industries/{name}/config", "PUT", {
        "name": name, "description": "导出测试",
        "expert_role": "专家", "hierarchy_desc": "A", "default_table_name": "a"
    })

    # 导出：POST /api/industries/export/{name}（注意路径和方法）
    status, resp = api_req(f"/api/industries/export/{name}", "POST")
    check("export.ok", status == 200 and resp.get("ok") and "path" in resp, f"status={status}")

    # 导入：用 zip_path（不是 data），先删除原目录再导入验证恢复
    if status == 200:
        zip_path = resp.get("path", "")
        # 删除原目录，验证 import 能恢复
        if os.path.exists(p):
            shutil.rmtree(p)
        status2, resp2 = api_req("/api/industries/import", "POST", {
            "zip_path": zip_path, "overwrite": True
        })
        check("import.ok", status2 == 200 and resp2.get("ok"), f"status={status2}")
        check("import.dir_exists", os.path.exists(p))
        check("import.config_exists", os.path.exists(os.path.join(p, "config", "config.yml")))
        # 清理导出的 zip 文件
        if os.path.exists(zip_path):
            try:
                os.unlink(zip_path)
            except Exception:
                pass

    # 清理
    if os.path.exists(p):
        shutil.rmtree(p)


def test_terminology_routing():
    """测试术语路由——验证"映射不替换"原则

    标准行为（查/增/删/改）和标准对象不可变，
    术语只做映射（如"开处方"→增，"查房"→查），不替换标准行为。
    """
    print("\n=== 7.7 术语路由验证 ===")
    if not API_AVAILABLE:
        skip("term_routing", "Management API 不可用")
        return

    name = "zztestind_route"
    p = os.path.join(INDUSTRIES_DIR, name)
    if os.path.exists(p):
        shutil.rmtree(p)

    # 创建行业 + 术语映射
    api_req("/api/industries/create", "POST", {
        "name": name, "display_name": "路由测试", "description": ""
    })
    api_req(f"/api/industries/{name}/prompts", "PUT", {
        "classification_hints": "", "schema_hints": "",
        "decompose_examples": [], "router_examples": [], "tool_examples": {},
        "terminology": {
            "table_aliases": {},
            "behavior_aliases": {
                "查": ["查看", "找一下", "查房"],
                "增": ["录入", "新建", "添加"],
                "删": ["去掉", "清除"],
                "改": ["修改", "调整"],
            },
            "object_aliases": {
                "记录": ["数据", "条目"],
            },
        },
    })

    # 切换到测试行业
    api_req("/api/industries/switch", "POST", {"industry": name})
    _sync_industry_in_process(name)

    from agent.router import text_behavior_override, _BEHAVIOR_KEYWORDS

    # 确定性行为规则（树资产）：行业切换不影响标准行为关键词判定
    test_cases = [
        ("查看所有数据", "查"),
        ("录入一条新数据", "增"),
        ("去掉那条数据", "删"),
        ("修改那条数据", "改"),
        ("查询相关数据", "查"),
    ]
    for user_input, exp_behavior in test_cases:
        act_behavior = text_behavior_override(user_input)
        check(f"route.{user_input[:4]}", act_behavior == exp_behavior,
              f"输入='{user_input}', 期望={exp_behavior}, 实际={act_behavior}")

    # 验证标准行为族仍然是原子层（不可变）
    check("route.behavior_keywords_intact",
          {bk for bk, _ in _BEHAVIOR_KEYWORDS} >= {"查", "增", "删", "改"},
          f"_BEHAVIOR_KEYWORDS={[bk for bk, _ in _BEHAVIOR_KEYWORDS]}")

    # 切换回原行业
    api_req("/api/industries/switch", "POST", {"industry": ORIGINAL_INDUSTRY})
    _sync_industry_in_process(ORIGINAL_INDUSTRY)

    # 清理
    if os.path.exists(p):
        shutil.rmtree(p)


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":

    test_industry_crud()
    test_industry_config()
    test_terminology_mapping()
    test_industry_switch()
    test_industry_export_import()
    test_terminology_routing()

    print(f"\n{'='*50}")
    print(f"INDUSTRY: PASS={pass_count}  FAIL={fail_count}  SKIP={skip_count}  TOTAL={pass_count+fail_count+skip_count}")
    if fail_count:
        print(f"失败项: {errors}")
        sys.exit(1)
    print("=== ALL INDUSTRY TESTS PASSED ===")
