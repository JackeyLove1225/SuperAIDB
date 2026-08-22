"""注册与元数据——全部 38 个 register_tool 调用集中于此。

注册顺序与拆包前 agent/tools.py 完全一致（顺序影响测试断言），
import 本模块即完成注册表填充 + apply_metadata 标注。
"""
from core.tool_registry import Tool, Param, register_tool
from pipeline.constants import TIER2_BATCH_UNITS

from agent.tools.query import (
    list_databases, describe_schema, _query_with_fallback,
    join_query_tool, aggregate_query_tool, list_selections_tool,
)
from agent.tools.records import (
    insert_data, batch_insert_data, edit_data, delete_data, mutate_data,
)
from agent.tools.ddl import (
    batch_create_tables, create_standard_tables, drop_table_tool,
    add_column_tool, drop_column_tool, modify_column_tool,
    alter_precision_tool, set_not_null_tool,
    add_foreign_key_tool, drop_foreign_key_tool,
    create_index_tool, drop_index_tool,
)
from agent.tools.templates import (
    save_template, list_templates, import_template, drop_template,
    clear_session,
)
from agent.tools.files import (
    process_file, upload_file, search_documents, list_vector_collections,
    export_data_tool,
)
from agent.tools.admin import (
    clear_db, unsupported_op,
    _confirm_action, _escalate_permission, _deescalate_permission,
)

# ====== 工具注册 ======

# 行业特定示例从配置加载（换行业时只需修改 prompts.yml，无需改代码）
def _load_tool_examples() -> dict:
    """从行业配置加载工具描述中的示例"""
    try:
        from industries.base import get_current_industry
        cfg = get_current_industry()
        return cfg.tool_examples or {}
    except Exception:
        return {}

_tool_exs = _load_tool_examples()
_join_ex = _tool_exs.get("join_query", "查A及其B信息")
_agg_ex = _tool_exs.get("aggregate_query", "统计数量、计算平均值")

register_tool(Tool(name="list_databases",description="查询已安装的数据库清单（类型、路径）",handler=list_databases,params=[Param("database","str","数据库名",default="")]))
register_tool(Tool(name="describe_schema",description="查询数据库有哪些表，或查看某张表的字段列表和外键关系。不能用来查询数据内容。",handler=describe_schema,params=[Param("table","str","表名（可选）",default=""),Param("column","str","字段名（可选）",default=""),Param("database","str","数据库名",default="")]))
register_tool(Tool(name="insert_data",description="在表中插入一行数据。data是JSON对象：{\"字段名\":\"值\"}",handler=insert_data,params=[Param("table","str","表名"),Param("data","str","JSON数据"),Param("database","str","数据库名")]))
register_tool(Tool(name="batch_insert_data",description="批量插入多行数据。data是JSON数组：[{\"a1\":5},{\"a1\":6}]",handler=batch_insert_data,params=[Param("table","str","表名"),Param("data","str","JSON数组格式的数据"),Param("database","str","数据库名")]))

register_tool(Tool(name="query",description="查询表中的数据。支持条件筛选和分页。默认每页100条，可通过page参数翻页。",handler=_query_with_fallback,params=[Param("query","str","查询内容（自然语言描述）",required=True),Param("table","str","表名（可选）"),Param("column","str","字段名（可选）"),Param("conditions","str","筛选条件 JSON（可选）"),Param("page","int","页码（1-indexed，默认1）",default=1),Param("page_size","int","每页条数（默认100，最大500）",default=100),Param("database","str","数据库名（可选）")]))
register_tool(Tool(name="join_query",description=f"多表联合查询。通过外键关系自动推断ON条件，也支持on_condition手动指定ON条件，支持INNER/LEFT/RIGHT JOIN。适用于跨表查询场景（如{_join_ex}）。",handler=join_query_tool,params=[Param("main_table","str","主表名",required=True),Param("join_tables","str","要关联的表名，逗号分隔",required=True),Param("select_fields","str","查询字段，默认*",default="*"),Param("where","str","WHERE条件（支持table.column格式）",default=""),Param("join_type","str","JOIN类型：INNER、LEFT或RIGHT，默认LEFT（明细联查以主表为准）",default="LEFT"),Param("on_condition","str","手动ON条件（如 a.id = b.a_id，多个用AND连接），给出时优先于外键自动推断",default=""),Param("database","str","数据库名")]))
register_tool(Tool(name="aggregate_query",description=f"聚合统计查询。支持COUNT/SUM/AVG/MIN/MAX，可配合GROUP BY分组和HAVING过滤。适用于统计场景（如{_agg_ex}）。",handler=aggregate_query_tool,params=[Param("table","str","表名",required=True),Param("agg_func","str","聚合函数：COUNT/SUM/AVG/MIN/MAX",required=True),Param("agg_field","str","聚合字段（COUNT时用*），默认*",default="*"),Param("group_by","str","分组字段，逗号分隔",default=""),Param("having","str","HAVING条件（如COUNT(*) > 5）",default=""),Param("where","str","WHERE条件",default=""),Param("database","str","数据库名")]))
register_tool(Tool(name="list_selections",description="列出所有查询结果暂存的选择集。支持指定ID查询单个选择集。",handler=list_selections_tool,params=[Param("as_json","bool","是否返回JSON格式（给AI用）",default=False)]))
register_tool(Tool(name="edit_data",description="通过选择集编号修改数据。需先查询创建选择集。set_data格式如 a2=888 或 a2=888,b1='xx'。",handler=edit_data,params=[Param("selection_id","int","选择集编号",required=True),Param("set_data","str","修改内容（如 a2=888 或 a2=888,b1='xx'）",required=True),Param("table","str","表名"),Param("database","str","数据库名")]))
register_tool(Tool(name="delete_data",description="通过选择集编号删除数据。需先查询创建选择集。",handler=delete_data,params=[Param("selection_id","int","选择集编号",required=True),Param("table","str","表名"),Param("database","str","数据库名")]))
register_tool(Tool(name="mutate_data",description="修改或删除记录的统一入口（人审闸内置：候选0条如实报、1条直接执行、多条挂起等用户确认、超100条要求缩小范围——批量写任何人不得绕过）。指令需含表名、筛选条件、要改的内容（删除不用）。",handler=mutate_data,params=[Param("instruction","str","自然语言指令，如：把目标表中编号为 901 的记录的 price 改成 200；删除 code 为 X 的记录",required=True),Param("action","str","UPDATE 或 DELETE（留空自动判断）",default=""),Param("database","str","数据库名",default="")]))
register_tool(Tool(name="unsupported_op",description="暂不支持的操作（决策树中无对应工具的意图组合统一路由到这里）",handler=unsupported_op,params=[Param("operation","str","操作描述")]))
# 表结构定义的 JSON Schema——从根源约束 AI 输出格式，无需归一化
_TABLE_DEFINITION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "表名，必须是英文 snake_case"},
            "business_name": {"type": "string", "description": "业务名称（中文）"},
            "columns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "字段名，必须是英文 snake_case（如 name/capacity/location），严禁中文；中文业务名放 business_name"},
                        "type": {"type": "string", "enum": ["VARCHAR", "INTEGER", "FLOAT", "TEXT"]},
                        "business_name": {"type": "string", "description": "中文业务名（如 名称/容量/位置）"},
                        "not_null": {"type": "boolean", "description": "是否非空"}
                    },
                    "required": ["name", "type"]
                }
            },
            "foreign_keys": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "columns": {"type": "array", "items": {"type": "string"}, "description": "本表外键字段名"},
                        "references": {"type": "string", "description": "引用表名"},
                        "ref_columns": {"type": "array", "items": {"type": "string"}, "description": "引用表字段名，默认id"}
                    },
                    "required": ["columns", "references", "ref_columns"]
                }
            },
            "indexes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "索引名"},
                        "columns": {"type": "array", "items": {"type": "string"}, "description": "索引字段"},
                        "unique": {"type": "boolean", "description": "是否唯一索引，默认true"}
                    },
                    "required": ["name", "columns"]
                }
            }
        },
        "required": ["name", "columns"]
    }
}

register_tool(Tool(name="batch_create_tables",description="创建新表。每张表自动添加id主键，外键只能指向引用表的id。",handler=batch_create_tables,params=[Param("definitions","str","表结构定义数组",required=True,schema=_TABLE_DEFINITION_SCHEMA),Param("database","str","数据库名")]))
register_tool(Tool(name="create_standard_tables",description="根据当前行业模板自动创建所有标准表",handler=create_standard_tables,params=[Param("database","str","数据库名")]))
register_tool(Tool(name="drop_table",description="删除表。使用参数控制是删单表还是删全部。",handler=drop_table_tool,params=[Param("table","str","表名"),Param("all","bool","是否删除全部表",default=False),Param("database","str","数据库名")]))
register_tool(Tool(name="add_column",description="在表中新增字段",handler=add_column_tool,params=[Param("table","str","表名"),Param("column","str","字段名"),Param("col_type","str","字段类型",default="TEXT"),Param("not_null","bool","是否非空",default=False),Param("database","str","数据库名")]))
register_tool(Tool(name="drop_column",description="删除表中的字段",handler=drop_column_tool,params=[Param("table","str","表名"),Param("column","str","字段名"),Param("force","bool","是否强制执行（跳过安全检查）",default=False),Param("database","str","数据库名")]))
register_tool(Tool(name="modify_column",description="修改字段的数据类型",handler=modify_column_tool,params=[Param("table","str","表名"),Param("column","str","字段名"),Param("new_type","str","新数据类型",default="TEXT"),Param("force","bool","是否强制执行",default=False),Param("database","str","数据库名")]))
register_tool(Tool(name="alter_precision",description="修改字段精度（如DECIMAL(12,2)）",handler=alter_precision_tool,params=[Param("table","str","表名"),Param("column","str","字段名"),Param("precision","str","精度（如: 10,2）",required=True),Param("force","bool","是否强制执行（跳过精度收紧检查）",default=False),Param("database","str","数据库名")]))
register_tool(Tool(name="set_not_null",description="将字段设置为非空（NOT NULL）",handler=set_not_null_tool,params=[Param("table","str","表名"),Param("column","str","字段名"),Param("database","str","数据库名")]))
register_tool(Tool(name="add_foreign_key",description="为表添加外键，外键始终指向目标表的主键id",handler=add_foreign_key_tool,params=[Param("table","str","当前表名"),Param("column","str","外键字段名"),Param("ref_table","str","被引用的表名"),Param("force","bool","类型不一致时是否强制执行",default=False),Param("database","str","数据库名")]))
register_tool(Tool(name="drop_foreign_key",description="删除表的外键约束",handler=drop_foreign_key_tool,params=[Param("table","str","表名"),Param("column","str","外键字段名"),Param("force","bool","是否强制执行（连字段一起删除）",default=False),Param("database","str","数据库名")]))
register_tool(Tool(name="create_index",description="为表的字段创建索引（默认唯一索引）",handler=create_index_tool,params=[Param("table","str","表名"),Param("column","str","字段名"),Param("unique","bool","是否唯一索引（默认True）",default=True),Param("database","str","数据库名")]))
register_tool(Tool(name="drop_index",description="删除表的索引",handler=drop_index_tool,params=[Param("table","str","表名"),Param("column","str","索引列名"),Param("database","str","数据库名")]))
register_tool(Tool(name="save_template",description="将当前表结构保存为模板",handler=save_template,params=[Param("name","str","模板名称",required=True),Param("database","str","数据库名")]))
register_tool(Tool(name="list_templates",description="列出所有可用模板",handler=list_templates,params=[Param("database","str","数据库名")]))
register_tool(Tool(name="import_template",description="从模板导入表结构",handler=import_template,params=[Param("name","str","模板名称",required=True),Param("table","str","目标表名"),Param("database","str","数据库名")]))
register_tool(Tool(name="drop_template",description="删除模板",handler=drop_template,params=[Param("name","str","模板名称",required=True),Param("database","str","数据库名")]))
register_tool(Tool(name="clear_db",description="清空数据库（删除所有表和数据）",handler=clear_db,params=[Param("drop_tables","bool","删除表结构",default=False),Param("database","str","数据库名")]))
register_tool(Tool(name="clear_session",description="清除对话历史记录",handler=clear_session,params=[Param("database","str","数据库名")]))
register_tool(Tool(name="process_file",description="文件入库：将上传的文件（PDF/Excel/Word/图片png,jpg,bmp,webp/扫描PDF）指定范围的表格数据提取并录入数据库（图片与无文字层的扫描页自动走 PaddleOCR 识别为文字后提取）。PDF按页码范围，Excel按流单元序号范围（每单元≤500行，大sheet自动切块），Word按逻辑段批次。不指定页码/序号时默认处理整份文件（自动分批，无需人工干预）。文件大小限制50MB。filepath为空时自动使用最近上传的文件。流单元序号为1-indexed。tables参数可限定提取目标表（逗号分隔，默认按行业全部标准表）。",handler=process_file,params=[Param("filepath","str","文件路径（为空时用最近上传的文件）"),Param("page_start","int","起始流单元序号（1-indexed；PDF页/Excel行块/Word段批）"),Param("page_end","int","结束流单元序号（1-indexed；不指定则处理到文件末尾）"),Param("overwrite","bool","是否覆盖已有数据",default=False),Param("batch_size","int","每批处理的流单元数（PDF页/Excel行块/Word段批）",default=TIER2_BATCH_UNITS),Param("tables","str","限定提取目标表，逗号分隔（默认按行业全部标准表）"),Param("database","str","数据库名")]))
register_tool(Tool(name="upload_file",description="上传本地文件到uploads目录（支持批量，逗号分隔多个路径）",handler=upload_file,params=[Param("filepath","str","本地文件路径（多个用逗号分隔）",required=True),Param("batch","bool","是否批量上传",default=False),Param("database","str","数据库名")]))
register_tool(Tool(name="search_documents",description="搜索向量数据库中的文字内容（从已处理的PDF/Excel中检索段落）",handler=search_documents,params=[Param("query","str","搜索关键词或问题",required=True),Param("collection","str","文件名（可选，不指定则搜索全部）",default=""),Param("top_k","int","返回结果数",default=5),Param("database","str","数据库名")]))
register_tool(Tool(name="list_vector_collections",description="列出向量数据库中的所有文件集合",handler=list_vector_collections,params=[Param("database","str","数据库名")]))
register_tool(Tool(name="export_data",description="导出数据为CSV或Excel文件。可按表名导出全表数据，或按选择集导出查询结果（选择集暂只支持CSV）。",handler=export_data_tool,params=[Param("table","str","要导出的表名"),Param("selection_id","int","选择集编号（导出选择集数据时使用）",default=0),Param("where","str","WHERE条件（可选，筛选导出数据）",default=""),Param("format","str","导出格式：csv 或 excel，默认 csv",default="csv"),Param("database","str","数据库名")]))

register_tool(Tool(name="confirm_action",description="查看当前待批准的高危操作（只读说明）。高危操作的批准/拒绝只能在 Web 管理台审批中心由管理员进行（20260822 起本工具不再结算，防 AI 自助结算人审闸）。",handler=_confirm_action,params=[Param("token","str","（已废弃参数）",default=""),Param("approve","bool","（已废弃参数）",default=True)]))
register_tool(Tool(name="escalate_permission",description="临时提权为管理员（sudo）：默认 AI 以 MCP_ROLE 身份操作，需管理员全权限时调用本工具触发人审，用户批准后临时 admin（TTL 到期自动降回）。仅 MCP 通道使用。",handler=_escalate_permission,params=[Param("role","str","提权目标角色，当前仅支持 admin",default="admin"),Param("ttl","int","有效期秒数（60-3600，默认 600）",default=600)]))
register_tool(Tool(name="deescalate_permission",description="立即撤销临时提权，恢复默认角色（MCP_ROLE）。",handler=_deescalate_permission,params=[]))

# ── 工具元数据（20260805，差距 1/3.3 地基）──
# 集中声明，apply_metadata 一次性写入注册表。纯声明式，不改运行时行为。
# risk_level: readonly（只读）| record_write（记录级写）| ddl（结构变更）| file（文件）| admin（管理）
# gate: nuke（核武人审闸）| selection（需选择集）| none（无特殊闸）
# requires_table（3.3 20260806）: 执行需确定目标表（单表/多表；参数直给或选择集回退）。
#   建新表（batch_create_tables/create_standard_tables）与模板/会话/库级/文件工具=False。
from core.tool_registry import apply_metadata as _apply_tool_meta

_TOOL_METADATA = {
    # 只读
    "query":            {"risk_level": "readonly", "intent_tags": ["查", "记录"], "gate": "none", "requires_table": True},
    "describe_schema":  {"risk_level": "readonly", "intent_tags": ["查", "结构", "表", "字段"], "gate": "none", "requires_table": True},
    "join_query":       {"risk_level": "readonly", "intent_tags": ["查", "关联", "记录"], "gate": "none", "requires_table": True},
    "aggregate_query":  {"risk_level": "readonly", "intent_tags": ["查", "统计", "记录"], "gate": "none", "requires_table": True},
    "list_databases":   {"risk_level": "readonly", "intent_tags": ["查", "数据库"], "gate": "none", "requires_table": False},
    "list_selections":  {"risk_level": "readonly", "intent_tags": ["查", "选择集"], "gate": "none", "requires_table": False},
    "search_documents": {"risk_level": "readonly", "intent_tags": ["查", "文件"], "gate": "none", "requires_table": False},
    "list_vector_collections": {"risk_level": "readonly", "intent_tags": ["查", "文件"], "gate": "none", "requires_table": False},
    "list_templates":   {"risk_level": "readonly", "intent_tags": ["查", "模板"], "gate": "none", "requires_table": False},
    # 记录级写
    "insert_data":      {"risk_level": "record_write", "intent_tags": ["增", "记录"], "gate": "none", "requires_table": True},
    "batch_insert_data": {"risk_level": "record_write", "intent_tags": ["增", "记录", "批量"], "gate": "none", "requires_table": True},
    "edit_data":        {"risk_level": "record_write", "intent_tags": ["改", "记录"], "gate": "nuke", "requires_table": True},
    "delete_data":      {"risk_level": "record_write", "intent_tags": ["删", "记录"], "gate": "nuke", "requires_table": True},
    "mutate_data":      {"risk_level": "record_write", "intent_tags": ["改", "删", "记录"], "gate": "nuke", "requires_table": True},
    # DDL
    "batch_create_tables": {"risk_level": "ddl", "intent_tags": ["增", "表"], "gate": "none", "requires_table": False},
    "create_standard_tables": {"risk_level": "ddl", "intent_tags": ["增", "表", "模板"], "gate": "none", "requires_table": False},
    "drop_table":       {"risk_level": "ddl", "intent_tags": ["删", "表"], "gate": "nuke", "requires_table": True},
    "add_column":       {"risk_level": "ddl", "intent_tags": ["增", "字段"], "gate": "none", "requires_table": True},
    "drop_column":      {"risk_level": "ddl", "intent_tags": ["删", "字段"], "gate": "nuke", "requires_table": True},
    "modify_column":    {"risk_level": "ddl", "intent_tags": ["改", "字段", "类型"], "gate": "nuke", "requires_table": True},
    "alter_precision":  {"risk_level": "ddl", "intent_tags": ["改", "字段", "精度"], "gate": "nuke", "requires_table": True},
    "set_not_null":     {"risk_level": "ddl", "intent_tags": ["改", "字段", "非空"], "gate": "nuke", "requires_table": True},
    "add_foreign_key":  {"risk_level": "ddl", "intent_tags": ["增", "外键"], "gate": "none", "requires_table": True},
    "drop_foreign_key": {"risk_level": "ddl", "intent_tags": ["删", "外键"], "gate": "nuke", "requires_table": True},
    "create_index":     {"risk_level": "ddl", "intent_tags": ["增", "索引"], "gate": "none", "requires_table": True},
    "drop_index":       {"risk_level": "ddl", "intent_tags": ["删", "索引"], "gate": "nuke", "requires_table": True},
    # 模板
    "save_template":    {"risk_level": "ddl", "intent_tags": ["增", "模板"], "gate": "none", "requires_table": False},
    "import_template":  {"risk_level": "ddl", "intent_tags": ["导入", "模板"], "gate": "none", "requires_table": False},
    "drop_template":    {"risk_level": "ddl", "intent_tags": ["删", "模板"], "gate": "nuke", "requires_table": False},
    # 文件
    "process_file":     {"risk_level": "file", "intent_tags": ["导入", "文件"], "gate": "none", "requires_table": False},
    "upload_file":      {"risk_level": "file", "intent_tags": ["上传", "文件"], "gate": "none", "requires_table": False},
    "export_data":      {"risk_level": "file", "intent_tags": ["导出", "文件", "记录"], "gate": "none", "requires_table": True},
    # 管理
    "clear_db":         {"risk_level": "admin", "intent_tags": ["删", "数据库"], "gate": "nuke", "requires_table": False},
    "clear_session":    {"risk_level": "admin", "intent_tags": ["删", "会话"], "gate": "none", "requires_table": False},
    "confirm_action":   {"risk_level": "admin", "intent_tags": [], "gate": "none", "requires_table": False},
    # 其他
    "unsupported_op":   {"risk_level": "", "intent_tags": [], "gate": "none", "requires_table": False},
}
_apply_tool_meta(_TOOL_METADATA)
