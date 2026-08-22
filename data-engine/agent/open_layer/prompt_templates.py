"""Prompt 模板文本——decompose / schema 设计两大模板（P2-2 拆分自 prompts.py）

约定：
- 模板内示例一律行业无关（格式说明而非领域词），领域知识由行业配置
  （decompose_examples/tool_examples/terminology）在构建时注入
- 修改模板文案前先跑 quick 回归（文案即协议历史教训：P1-1 已把行为判定
  收敛到 code，文案本身不再影响行为，但示例词会直接影响 AI 输出质量）
"""

DECOMPOSE_PROMPT_TEMPLATE = """分析用户意图，判断是简单任务还是复杂任务，并拆解子任务。
\r
请返回 JSON：
{{
    "is_complex": true/false,
    "task_type": "basic|agent_query|deep_research",
    "sub_tasks": [
        {{
            "type": "db",
            "query": "标准化指令（使用标准行为动词+标准对象词，保留所有具体数据）",
            "behavior_key": "查|改|增|删|导入|上传|导出",
            "db_category_key": "记录|表|结构|字段|外键|索引|类型|精度|关联|统计|数据库|模板|会话|选择集|文件",
            "constraint": "约束值（可选，如非空/主键/批量/单条等）",
            "structured_args": {{
                "table": "表名（必须来自上方可用表清单）",
                "column": "字段名（该表的真实字段）",
                "col_type": "字段类型（如FLOAT/TEXT/INTEGER/VARCHAR，仅DDL时填）",
                "data": {{"字段名": "值"}},
                "conditions": [{{"field": "字段名", "op": "=|!=|<|>|<=|>=|LIKE", "value": "值"}}],
                "main_table": "主表名（JOIN查询时填）",
                "join_tables": "关联表名，逗号分隔（JOIN查询时填）",
                "select_fields": "查询字段，默认*（JOIN查询时填）",
                "agg_func": "COUNT|SUM|AVG|MIN|MAX|DISTINCT（聚合查询时填）",
                "agg_field": "聚合字段，COUNT时用*（聚合查询时填）",
                "group_by": "分组字段，逗号分隔（聚合查询时填）",
                "where": "WHERE条件字符串（JOIN/聚合查询时填，如 field='value'）",
                "set_data": "修改内容（如a2=888，编辑数据时填）",
                "ref_table": "被引用表名（加外键时填）",
                "precision": "精度值（如10,2，修改精度时填）",
                "new_type": "新类型（修改类型时填）",
                "not_null": false,
                "unique": true,
                "force": false,
                "definitions": [{{"name":"英文表名","business_name":"中文业务名","columns":[{{"name":"英文snake_case字段名","type":"类型","business_name":"中文字段名"}}]}}],
                "filepath": "文件路径（文件操作时填）",
                "page_start": 1,
                "page_end": 0,
                "selection_id": 0,
                "name": "模板名/文件名"
            }}
        }},
        {{
            "type": "rag",
            "query": "文档检索查询"
        }},
        {{
            "type": "file_query",
            "query": "查看文件内容",
            "path": "文件路径（必须与文件清单中的 path 完全一致）"
        }}
    ]
}}
\r
规则：
- is_complex=false 时，sub_tasks 只有一个元素
- is_complex=true 时，sub_tasks 包含按顺序执行的多个子任务
- task_type="agent_query"：纯查询类指令——查记录/看表结构/统计/关联查询/文档检索/库清单，
  无论单步还是多步探索，只要不含任何写操作都归此类（查询由智能体自主规划工具完成，sub_tasks 留空数组即可）。
  注意：需要读取工作区文件具体内容回答的问题（如"这个文件讲了什么"）不属于此类，走 basic 的 file_query
- task_type="basic"：含写操作的指令——增/改/删记录、建改表结构、加删字段、导入/上传/导出文件等
- task_type="deep_research"：分析/评估/预测/建议/投资价值/趋势/对比分析/洞察等高阶模糊指令（需要多轮探索+推理）
- task_type="build_db"：用户要求把上传的文件"建成数据库/自动建库/把文件夹变成可查询的数据"时。
  触发后系统执行：读取文件样本 → 设计表结构 → 请用户确认 → 建表（到此为止）。
  仅在文件清单非空且用户明确要求"建库/设计表结构"时使用；sub_tasks 留空数组即可。
  注意：用户说"入库/录入/把文件导入"是录数据请求，不是建库——应走 basic（导入+文件），不要使用 build_db。
type=db 必须填写 behavior_key、db_category_key 和 structured_args
- type=rag 不需要填写 behavior_key/db_category_key/structured_args
- type=file_query：查看工作区已加载文件的具体内容。当用户问题需要基于上传文件内容回答时使用（如"分析这个文件"、"这个文件讲了什么"、"对比这两个文件"）。path 必须与上方文件清单中的 path 完全一致。
  - 如果用户只是问"有哪些文件"、"加载了什么"、"文件列表"，直接基于文件清单回答，**不要生成 file_query**。
  - 如果用户的问题不需要文件内容（如数据库查询、通用知识），不要生成 file_query。
- query 字段必须使用标准化表述（用标准行为动词+真实表名，不用口语化表达）
- **query 标准化时不得丢掉原句的关键对象词**（如"有哪些表"改写后必须保留"表"字样，不得写成"查询数据库"）
- **db_category_key 判定**：问"有哪些表/几张表/有什么表"选 表（即使句中出现"数据库"字样）；只有问数据库本身的信息（有哪些数据库、库的类型、路径）才选 数据库
- **删/改记录的安全规则**：凡是"删除/修改某些记录"的意图，必须拆成两个子任务——
  先【查】（用真实条件查出目标记录，建立选择集），再【删/改】（对选择集执行）。
  禁止直接产出不带前置查询的裸删/裸改任务（否则系统会误删上次查询的选择集）。
- **关键：query 必须保留用户指令中的所有具体数据！**（如ID、姓名、字段值、条件等）
- **关键：structured_args 必须包含执行该指令所需的全部参数！**
  - 字段名/表名必须使用真实英文名，不能用中文名
  - data 是 JSON 对象，如 {{"字段名":"值"}}
  - conditions 是 JSON 数组，如 [{{"field":"字段名","op":"=","value":"值"}}]
  - 没有用到的字段留空字符串""或省略
- 最多 {max_tasks} 个子任务
\r
数据库当前可用表（真实表名，structured_args 中的表名必须从这里选择，禁止编造）：
{tables_section}
\r
{terminology_section}
\r
可用文档集合：{collections}
\r
{tail_sections}用户指令：{instruction}
"""


SCHEMA_DESIGN_TEMPLATE = """你是数据库结构设计专家。用户上传了一批文件，希望把它们组织成一个可查询的关系数据库。

文件清单：
{manifest_text}

代表性文件内容样本：
{samples_text}
{feedback_section}
请设计一套合理的表结构，要求：
1. 从文件内容中识别出核心业务实体，每个实体一张表
2. 表名用英文 snake_case，业务名用中文
3. 字段类型只用：TEXT（文本）/ INTEGER（整数）/ FLOAT（小数）；不要 id 字段（系统会自动添加主键）
4. 表之间的关联用外键表达：外键字段指向引用表的 id
5. 外键字段必须是 INTEGER 类型且只能引用引用表的 id 字段，禁止引用业务编码列；字段名用 {{引用表}}_id 形式
6. 不要设计冗余表：清单类/说明类文件不需要建表（它们会进入文档检索库）
7. 表数量控制在 2-8 张，宁精勿滥

只返回 JSON：
{{
    "tables": [
        {{
            "name": "英文表名",
            "business_name": "中文业务名",
            "description": "表的业务描述（一句话：存什么数据、和其他表什么关系）",
            "columns": [
                {{"name": "字段名", "type": "TEXT|INTEGER|FLOAT", "business_name": "中文字段名"}}
            ],
            "foreign_keys": [
                {{"columns": ["外键字段"], "references": "引用表名", "ref_columns": ["id"]}}
            ]
        }}
    ],
    "rationale": "设计理由（一段话，说明每张表的用途和表间关系，给用户确认用）"
}}"""
