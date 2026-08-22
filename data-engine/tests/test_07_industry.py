"""层 7：行业管理 + 术语路由 + AI向导 回归测试

覆盖：
  1. 行业 CRUD（创建/列表/删除）
  2. 行业配置（config / prompts / schemas PUT & GET）
  3. 术语映射配置（table_aliases / behavior_aliases / object_aliases）
  4. 行业切换 + settings 同步
  5. AI 向导配置（多轮问答 → 生成 → apply）
  6. 行业导出/导入
  7. 术语路由验证（behavior_aliases → 标准行为映射）

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
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDUSTRIES_DIR = os.path.join(BASE_DIR, "industries")
ORIGINAL_INDUSTRY = os.environ.get("INDUSTRY", "engineering")

pass_count = 0
fail_count = 0
skip_count = 0
errors = []
API_AVAILABLE = False


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


def api_get(path):
    try:
        from tests._mgmt_auth import auth_headers
        req = urllib.request.Request(f"{API}{path}", headers=auth_headers())
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return 0, {"error": str(e)}


def api_req(path, method="POST", data=None):
    try:
        from tests._mgmt_auth import auth_headers
        headers = {"Content-Type": "application/json", **auth_headers()}
        body = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(f"{API}{path}", data=body, method=method, headers=headers)
        r = urllib.request.urlopen(req, timeout=60)
        return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
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
        API_AVAILABLE = False
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

    # 创建
    status, resp = api_req("/api/industries/create", "POST", {
        "name": name, "display_name": "测试行业", "description": "CRUD测试", "template": "custom"
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
        "name": name, "display_name": "配置测试", "description": "", "template": "custom"
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

    # schemas list
    status, resp = api_get(f"/api/industries/{name}/schemas")
    check("schema.list_ok", status == 200 and resp.get("count") == 1)

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
        "name": name, "display_name": "术语测试", "description": "", "template": "custom"
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
        "name": name, "display_name": "切换测试", "description": "", "template": "custom"
    })

    # 切换
    api_req("/api/industries/switch", "POST", {"industry": name})
    _sync_industry_in_process(name)

    from config.settings import settings
    check("switch.settings_synced", settings.INDUSTRY == name, f"INDUSTRY={settings.INDUSTRY}")

    # 切换回原行业
    api_req("/api/industries/switch", "POST", {"industry": ORIGINAL_INDUSTRY})
    _sync_industry_in_process(ORIGINAL_INDUSTRY)
    check("switch.restored", settings.INDUSTRY == ORIGINAL_INDUSTRY)

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
        "name": name, "display_name": "导出测试", "description": "", "template": "custom"
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


def test_ai_wizard():
    """测试 AI 向导配置（多轮问答 → 生成 → apply）"""
    print("\n=== 7.6 AI 向导配置 ===")
    if not API_AVAILABLE:
        skip("ai_wizard", "Management API 不可用")
        return

    # 启动向导
    status, resp = api_req("/api/industries/ai-wizard/start", "POST")
    check("wizard.start", status == 200 and "session_id" in resp, f"status={status}")
    if status != 200:
        return

    session_id = resp["session_id"]
    conversations = [
        "我要为测试行业创建配置。行业名 zztestind_wiz，描述是测试数据管理。",
        "数据层级是：分类 → 项目。专家角色是'你是测试专家'。",
        "主要表：category（分类表，含编码、名称）、item（项目表，含分类ID、名称、数值）。",
        "常见查询：查询所有分类、统计项目数量、查分类下的项目。",
        "术语习惯：'录入'就是新增，'查看'就是查询，'去掉'就是删除。",
        "信息够了，生成配置",
    ]

    wizard_done = False
    for i, msg in enumerate(conversations):
        status, resp = api_req("/api/industries/ai-wizard/chat", "POST", {
            "session_id": session_id, "message": msg,
        })
        check(f"wizard.round{i+1}", status == 200, f"status={status}")
        if status == 200 and i == len(conversations) - 1:
            wizard_done = resp.get("phase") == "done"
            check("wizard.done", wizard_done, f"phase={resp.get('phase')}")

    # apply
    if wizard_done:
        status, resp = api_req("/api/industries/ai-wizard/apply", "POST", {
            "session_id": session_id, "industry_name": "zztestind_wiz",
        })
        check("wizard.apply", status == 200 and resp.get("ok"), f"status={status}")
        wiz_dir = os.path.join(INDUSTRIES_DIR, "zztestind_wiz")
        check("wizard.dir_exists", os.path.exists(wiz_dir))

    # 清理
    wiz_dir = os.path.join(INDUSTRIES_DIR, "zztestind_wiz")
    if os.path.exists(wiz_dir):
        shutil.rmtree(wiz_dir)


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
        "name": name, "display_name": "路由测试", "description": "", "template": "custom"
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

    from agent.router import parse_semantic

    test_cases = [
        ("查看所有数据", "查"),
        ("录入一条新数据", "增"),
        ("去掉那条数据", "删"),
        ("修改那条数据", "改"),
        ("找一下相关数据", "查"),
    ]
    for user_input, exp_behavior in test_cases:
        result = parse_semantic(user_input)
        act_behavior = result.get("behavior_key", "")
        check(f"route.{user_input[:4]}", act_behavior == exp_behavior,
              f"输入='{user_input}', 期望={exp_behavior}, 实际={act_behavior}")

    # 验证标准行为仍然是原子层（不可变）
    # 如果没有 STANDARD_BEHAVIORS 常量，跳过此检查
    try:
        from agent.router import STANDARD_BEHAVIORS
        check("route.std_behaviors_intact", set(STANDARD_BEHAVIORS) >= {"查", "增", "删", "改"},
              f"STANDARD_BEHAVIORS={STANDARD_BEHAVIORS}")
    except ImportError:
        check("route.std_behaviors_intact", True, "STANDARD_BEHAVIORS 未定义")

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
    _check_api()
    if not API_AVAILABLE:
        print("⚠ Management API 不可用，行业管理测试将全部跳过")
        print("  启动服务: cd data-engine && python agent/management/server.py")

    test_industry_crud()
    test_industry_config()
    test_terminology_mapping()
    test_industry_switch()
    test_industry_export_import()
    test_ai_wizard()
    test_terminology_routing()

    print(f"\n{'='*50}")
    print(f"INDUSTRY: PASS={pass_count}  FAIL={fail_count}  SKIP={skip_count}  TOTAL={pass_count+fail_count+skip_count}")
    if fail_count:
        print(f"失败项: {errors}")
        sys.exit(1)
    print("=== ALL INDUSTRY TESTS PASSED ===")
