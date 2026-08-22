"""层 25：3.2 角色化模型配置（config/llm_roles.yml + graph._resolve_role_model）

选模与落账同一角色体系（core/llm_usage.set_role）：角色 → 档位 → 模型两级配置，
默认分档依据 3.0 报告 TIER_MAP（规划档吃 pro、机械档吃 flash）。

覆盖：

1. 默认配置解析：9 个角色按 yml 默认映射到正确档位（planning→AI_MODEL_PLANNING
   回退 AI_MODEL；mechanical→AI_MODEL）
2. planning 参数兼容：role="" 时与 3.2 之前行为完全一致（旧调用点零回归）
3. 角色级模型覆盖：roles[x] 写具体模型名 → 绕过档位直接用（细调通道）
4. 档位级模型覆盖：tiers.planning 写模型名 → 全规划档角色统一吃该模型
5. 未知角色：未配置角色一律机械档（控成本保守默认）
6. yml 缺失/损坏：fail-open 回 planning 参数语义（与 3.2 之前行为一致）
7. _get_llm 集成：role 传参选模正确，缓存按模型名分键（不联网，纯实例化）
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.open_layer import graph as g
from config.settings import settings


class _YmlFixture:
    """临时 yml + 缓存重置 + settings 模型遮蔽的上下文（用例间零污染）"""

    def __init__(self, content: str | None, ai_model="m-flash", ai_planning="m-pro"):
        self.content = content
        self.ai_model, self.ai_planning = ai_model, ai_planning
        self._tmp = None

    def __enter__(self):
        if self.content is not None:
            self._tmp = tempfile.NamedTemporaryFile(
                "w", suffix=".yml", delete=False, encoding="utf-8")
            self._tmp.write(self.content)
            self._tmp.close()
            self._patcher = patch.object(g, "_LLM_ROLES_PATH", Path(self._tmp.name))
        else:  # None = 文件不存在场景
            self._patcher = patch.object(
                g, "_LLM_ROLES_PATH", Path(tempfile.mkdtemp()) / "nonexistent.yml")
        self._patcher.start()
        g._reset_llm_roles_cache()
        self._old_model, self._old_planning = settings.AI_MODEL, settings.AI_MODEL_PLANNING
        settings.AI_MODEL, settings.AI_MODEL_PLANNING = self.ai_model, self.ai_planning
        return self

    def __exit__(self, *exc):
        settings.AI_MODEL, settings.AI_MODEL_PLANNING = self._old_model, self._old_planning
        self._patcher.stop()
        g._reset_llm_roles_cache()
        if self._tmp:
            os.unlink(self._tmp.name)
        return False


def test_default_roles_mapping():
    """默认 yml：规划档 6 角色吃 pro、机械档 4 角色吃 flash"""
    planning_roles = ("decompose", "synthesize", "agent_loop",
                      "research", "review", "schema_design")
    mechanical_roles = ("extract_param", "extract_file", "ooda_correct", "other")
    # 用真实 config/llm_roles.yml（不 patch 路径），只遮蔽 settings 模型名
    g._reset_llm_roles_cache()
    old = settings.AI_MODEL, settings.AI_MODEL_PLANNING
    settings.AI_MODEL, settings.AI_MODEL_PLANNING = "m-flash", "m-pro"
    try:
        for r in planning_roles:
            assert g._resolve_role_model(r) == "m-pro", f"{r} 应吃规划档"
        for r in mechanical_roles:
            assert g._resolve_role_model(r) == "m-flash", f"{r} 应吃机械档"
        # 规划档模型未配置（空）时回退 AI_MODEL
        settings.AI_MODEL_PLANNING = ""
        for r in planning_roles:
            assert g._resolve_role_model(r) == "m-flash", f"{r} 空规划档应回退 flash"
    finally:
        settings.AI_MODEL, settings.AI_MODEL_PLANNING = old
        g._reset_llm_roles_cache()
    print(f"OK - 默认配置：规划档 {len(planning_roles)} 角色 / 机械档 {len(mechanical_roles)} 角色分档正确")


def test_planning_param_compat():
    """planning 参数兼容：role="" 时行为与 3.2 之前一致"""
    with _YmlFixture(None):  # yml 缺失也走同一兼容路径
        assert g._resolve_role_model("", planning=True) == "m-pro"
        assert g._resolve_role_model("", planning=False) == "m-flash"
        assert g._resolve_role_model() == "m-flash"
        settings.AI_MODEL_PLANNING = ""
        assert g._resolve_role_model("", planning=True) == "m-flash"  # 空配置回退
    print("OK - 兼容态：planning 参数语义零回归（含 yml 缺失 fail-open）")


def test_role_level_model_override():
    """角色级覆盖：roles[x] 写具体模型名 → 绕过档位直接用"""
    yml = (
        "tiers:\n  planning: ''\n  mechanical: ''\n"
        "roles:\n  agent_loop: deepseek-v4-flash   # 循环降档省钱场景\n"
        "  decompose: planning\n"
    )
    with _YmlFixture(yml):
        assert g._resolve_role_model("agent_loop") == "deepseek-v4-flash"
        assert g._resolve_role_model("decompose") == "m-pro"  # 档位路径不受影响
    print("OK - 角色级模型覆盖：具体模型名绕过档位生效")


def test_tier_level_model_override():
    """档位级覆盖：tiers 写模型名 → 该档全部角色统一吃该模型"""
    yml = (
        "tiers:\n  planning: glm-4.6\n  mechanical: ''\n"
        "roles:\n  decompose: planning\n  review: planning\n  other: mechanical\n"
    )
    with _YmlFixture(yml):
        assert g._resolve_role_model("decompose") == "glm-4.6"
        assert g._resolve_role_model("review") == "glm-4.6"
        assert g._resolve_role_model("other") == "m-flash"
    print("OK - 档位级模型覆盖：规划档统一吃配置模型")


def test_unknown_role_conservative():
    """未知角色：未配置一律机械档（控成本保守默认）；roles 空表也不崩"""
    yml = "tiers:\n  planning: ''\n  mechanical: ''\nroles:\n  decompose: planning\n"
    with _YmlFixture(yml):
        assert g._resolve_role_model("future_new_role") == "m-flash"
        assert g._resolve_role_model("future_new_role", planning=True) == "m-pro"  # 尊重 planning 兜底
    print("OK - 未知角色：机械档保守默认，planning 参数仍作兜底")


def test_broken_yml_fail_open():
    """yml 损坏：fail-open 回 planning 参数语义，不抛异常"""
    with _YmlFixture("tiers: [这不是合法映射\nroles: {{"):
        assert g._resolve_role_model("decompose", planning=True) == "m-pro"
        assert g._resolve_role_model("decompose") == "m-flash"
    print("OK - 损坏 yml：fail-open 回 planning 语义")


def test_get_llm_integration():
    """_get_llm 集成：role 选模正确 + 缓存按模型名分键

    mock langchain_openai 模块：被测逻辑是"role→模型名→缓存分键"，
    ChatOpenAI 实例化由 test_13 单例测试覆盖；真导入会连带 transformers→torch
    （~15s+），把 quick 层拖过 run_all 超时（est_time×3）。
    """
    import types
    fake_mod = types.ModuleType("langchain_openai")
    instances = {}

    def _fake_chat_openai(model=None, **kwargs):
        inst = MagicMock(name=f"ChatOpenAI({model})")
        inst.model_name = model
        instances[model] = inst
        return inst

    fake_mod.ChatOpenAI = _fake_chat_openai
    g._reset_llm_roles_cache()
    old = settings.AI_MODEL, settings.AI_MODEL_PLANNING
    settings.AI_MODEL, settings.AI_MODEL_PLANNING = "m-flash", "m-pro"
    g._llm_cache.clear()
    try:
        with patch.dict(sys.modules, {"langchain_openai": fake_mod}):
            a = g._get_llm(role="decompose")
            b = g._get_llm(role="extract_param")
            c = g._get_llm(role="synthesize")
            assert a is c, "同档角色应共享同模型单例"
            assert a is not b, "不同档角色应不同实例"
            assert set(g._llm_cache) == {"m-pro", "m-flash"}, "缓存按模型名分键"
            # role 为空回退 planning 语义
            d = g._get_llm(planning=True)
            assert d is a
        assert set(instances) == {"m-pro", "m-flash"}, "实例化模型名与分档一致"
    finally:
        settings.AI_MODEL, settings.AI_MODEL_PLANNING = old
        g._llm_cache.clear()
        g._reset_llm_roles_cache()
    print("OK - _get_llm 集成：角色选模 + 模型名分键缓存正确")


if __name__ == "__main__":
    test_default_roles_mapping()
    test_planning_param_compat()
    test_role_level_model_override()
    test_tier_level_model_override()
    test_unknown_role_conservative()
    test_broken_yml_fail_open()
    test_get_llm_integration()
    print("\n✅ 层 25 全部通过：3.2 角色化模型配置（默认分档/兼容/角色级/档位级/"
          "未知角色/损坏 fail-open/集成缓存）")
