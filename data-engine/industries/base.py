"""行业配置基类——所有行业配置继承此类"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class IndustryConfig:
    """行业配置"""
    name: str                               # 行业名称
    description: str = ""                   # 行业描述（展示给用户）
    expert_role: str = ""                   # AI 专家角色设定
    hierarchy_desc: str = ""                # 行业数据的层级分类描述
    default_table_name: str = "data"        # 默认表名
    classification_hints: str = ""          # AI 分类提示词补充
    schema_hints: str = ""                  # AI 模式提取提示词补充
    custom_prompts: dict = field(default_factory=dict)  # 自定义提示词覆盖
    tables: list = field(default_factory=list)  # 标准表结构定义
    focus_fields: list = None                   # 字段聚焦（指令级提取字段子集；
    # 声明字段——dataclasses.replace 自动携带，下游裁剪/prompt 直接读）
    field_dict: dict = field(default_factory=dict)  # 标准字段字典
    # AI 提示词配置化——以下字段从 prompts.yml 加载，实现换行业零代码改动
    decompose_examples: list = field(default_factory=list)  # 任务拆解 few-shot 示例
    router_examples: list = field(default_factory=list)     # 语义路由 few-shot 示例
    tool_examples: dict = field(default_factory=dict)       # 工具描述中的行业示例
    # 术语适配配置——让 AI 理解行业术语和个人表达方式
    terminology: dict = field(default_factory=dict)         # 术语映射（表别名/行为别名/对象别名/值域别名）

    @classmethod
    def load(cls, industry_dir: str) -> "IndustryConfig":
        """从行业目录加载配置"""
        base = Path(industry_dir)
        config_path = base / "config" / "config.yml"
        schema_dir = base / "schemas"
        fields_path = base / "fields" / "fields.yml"
        prompts_path = base / "prompts" / "prompts.yml"

        config_data = {}
        prompts_data = {}

        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                config_data = yaml.safe_load(f) or {}

        if prompts_path.exists():
            with open(prompts_path, encoding="utf-8") as f:
                prompts_data = yaml.safe_load(f) or {}

        # 读取所有 schemas/*.yaml → tables
        tables = []
        if schema_dir.exists():
            for p in sorted(schema_dir.glob("*.yaml")):
                try:
                    tables.append(yaml.safe_load(p.read_text(encoding="utf-8")))
                except Exception:
                    continue

        # 读取 fields.yaml → field_dict
        field_dict = {}
        if fields_path.exists():
            field_dict = yaml.safe_load(fields_path.read_text(encoding="utf-8")) or {}

        decompose_examples = prompts_data.get("decompose_examples", [])
        router_examples = prompts_data.get("router_examples", [])
        # few-shot 示例必填字段加载期校验（配置即代码，缺字段加载即报错）
        for i, ex in enumerate(decompose_examples):
            if not isinstance(ex, dict) or not ex.get("query") or "sub_tasks" not in ex:
                raise ValueError(
                    f"行业 {base.name} 的 decompose_examples[{i}] 缺 query/sub_tasks 必填字段")
        for i, ex in enumerate(router_examples):
            if not isinstance(ex, dict) or not ex.get("input")                     or not ex.get("behavior_key") or not ex.get("db_category_key"):
                raise ValueError(
                    f"行业 {base.name} 的 router_examples[{i}] 缺 input/behavior_key/db_category_key 必填字段")

        return cls(
            name=config_data.get("name", base.name),
            description=config_data.get("description", ""),
            expert_role=config_data.get("expert_role", ""),
            hierarchy_desc=config_data.get("hierarchy_desc", ""),
            default_table_name=config_data.get("default_table_name", "data"),
            classification_hints=prompts_data.get("classification_hints", ""),
            schema_hints=prompts_data.get("schema_hints", ""),
            custom_prompts=prompts_data.get("custom_prompts", {}),
            tables=tables,
            field_dict=field_dict,
            decompose_examples=decompose_examples,
            router_examples=router_examples,
            tool_examples=prompts_data.get("tool_examples", {}),
            terminology=prompts_data.get("terminology", {}),
        )

    def get_classification_prompt(self) -> str:
        """获取分类提示词"""
        return self.classification_hints or ""

    def get_schema_prompt(self) -> str:
        """获取模式提取提示词"""
        return self.schema_hints or ""


# 行业注册表（ConfigHub 语义：目录签名新鲜度——任何 yml 变更即重载，免重启）
_industries: dict[str, IndustryConfig] = {}
_industry_mtimes: dict[str, float] = {}


def register_industry(name: str, config: IndustryConfig):
    _industries[name] = config


def _dir_mtime(base: Path) -> float:
    """行业目录签名：config/prompts/schemas/fields 下所有 yml 的最新 mtime"""
    mt = 0.0
    for pat in ("config/*.yml", "prompts/*.yml", "schemas/*.yaml", "fields/*.yml"):
        for p in base.glob(pat):
            try:
                mt = max(mt, p.stat().st_mtime)
            except OSError:
                pass  # 单文件 mtime 取不到则按未变（保守不刷新）
    return mt


def _load_fresh(name: str) -> Optional[IndustryConfig]:
    """按目录签名新鲜度加载行业配置（文件即契约：改了 yml 下次取用即新值）"""
    base = Path(__file__).parent / name
    if not base.is_dir():
        return _industries.get(name)
    m = _dir_mtime(base)
    if _industries.get(name) is None or _industry_mtimes.get(name) != m:
        try:
            cfg = IndustryConfig.load(str(base))
        except Exception:
            return _industries.get(name)  # 加载失败沿用旧值（last_good）
        _industries[name] = cfg
        _industry_mtimes[name] = m
    return _industries.get(name)


def get_industry(name: str) -> Optional[IndustryConfig]:
    return _load_fresh(name)


def discover_industries(base_dir: str = None):
    """自动发现 industries/ 下的所有行业目录（首次批量注册用；日常取用走 _load_fresh）"""
    if base_dir is None:
        base_dir = Path(__file__).parent
    else:
        base_dir = Path(base_dir)

    for entry in base_dir.iterdir():
        if entry.is_dir() and not entry.name.startswith("_"):
            try:
                config = IndustryConfig.load(str(entry))
                register_industry(config.name, config)
                _industry_mtimes[config.name] = _dir_mtime(entry)
            except Exception:
                pass  # 不是有效的行业目录则跳过


def get_current_industry() -> IndustryConfig:
    """获取当前行业的配置（从 settings.INDUSTRY 读取行业名）

    行业目录内容随取随新（目录签名）；找不到时返回一个空的默认配置。
    """
    try:
        from config.settings import settings
        name = settings.INDUSTRY
    except Exception:
        name = "construction_engineering"

    cfg = _load_fresh(name)
    if cfg is None:
        # 找不到行业配置，返回默认空配置（避免阻断业务）
        return IndustryConfig(name=name)
    return cfg


def reset_registry() -> None:
    """行业注册表公开重置入口（行业切换经 registry 调用；
    替代外部直戳 _industries 私有全局——状态卫生）"""
    _industries.clear()


# 自注册到重置注册表
from core.registry import register_reset

register_reset("industry_registry", reset_registry)
