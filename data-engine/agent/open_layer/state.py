"""AgentState 定义——开放式 AI 层的状态结构"""

from typing import TypedDict, Annotated, Sequence, List, Dict, Any
from langgraph.graph.message import add_messages


class FileManifestEntry(TypedDict, total=False):
    """文件清单单条——文件元信息（不含内容，体积小，注入 prompt）"""
    path: str          # 相对路径（唯一键）
    filename: str      # 文件名
    category: str      # 文件类别（image/pdf/document/spreadsheet/markdown/text/code/other）
    language: str      # 代码语言标签（如 python/typescript，仅代码类有）
    size: int          # 文件大小（字节）
    line_count: int    # 行数（仅文本类有）
    server_path: str   # 落盘后的服务器绝对路径（经 /api/files/upload 上传后由前端填入）


class FileContext(TypedDict, total=False):
    """runtime-scoped context——文件清单与内容分离架构

    context 不进 checkpoint，每次 run 独立，不污染 messages 历史、不膨胀 state（符合内存优先原则）。
    - file_manifest：文件清单（小），注入 understand_and_decompose 的 prompt，AI 直接看到
    - file_contents：文件内容映射（大），AI 通过 type=file_query 子任务按需读取
    - artifact：兼容原有 artifactContext
    - user_token：认证 Bearer token（20260804 认证接入）——前端登录后随 context 传入，
      graph 节点开头验签注入权限角色（同步节点跑线程池，contextvar 须在节点内注入）
    """
    file_manifest: List[FileManifestEntry]
    file_contents: Dict[str, str]
    artifact: Dict[str, Any]
    user_token: str


class AgentState(TypedDict):
    """开放式 AI Agent 的状态

    Attributes:
        messages: 对话历史（LangGraph 标准消息列表）
        sub_tasks: 拆解出的子任务列表，每个子任务带类型
            [{"type": "db", "query": "查一下A1-6的价格"},
             {"type": "rag", "query": "什么是小型空心砖块"}]
        current_step: 当前执行的子任务索引
        results: 每个子任务的执行结果
        is_complex: 是否为复杂意图（需要多步编排）
        iteration: 当前迭代次数（防止无限循环）
        failed_tasks: 失败的子任务记录
            [{"step": 0, "query": "...", "error": "...", "attempts": 2}]
        task_type: 指令类型——"basic"=基础操作（查/改/增/删），
                   "deep_research"=深度研究（分析/评估/预测/建议等高阶指令）

    体系B新增字段：
        research_plan: 研究计划
            {"understanding": "...", "research_mode": "web|local|hybrid",
             "goals": [{"id": 1, "goal": "...", "status": "pending|in_progress|completed|blocked",
                        "findings": "...", "tool_type": "web|local|hybrid"}]}
        ooda_history: OODA循环历史
            [{"goal_id": 1, "round": 0, "thought": "...", "action": {...}, "observation": "..."}]
        waiting_for_user: 暂停状态（等待用户输入）
            {"type": "ask_user|blocked", "question": "...", "reason": "...", "suggestion": "..."}
        user_clarification: 用户对提问/卡壳的回答
        research_mode: 研究模式 web|local|hybrid
    """
    messages: Annotated[Sequence, add_messages]
    sub_tasks: list[dict]
    current_step: int
    results: list[str]
    is_complex: bool
    iteration: int
    failed_tasks: list[dict]
    task_type: str
    # 体系B新增
    research_plan: dict
    ooda_history: list
    waiting_for_user: dict
    user_clarification: str
    research_mode: str
    research_context_cache: dict  # OODA 循环内复用的上下文缓存（原 _cached_context 黑钥匙正名，P2-4）
    # 新行业注册（已并入建库流程，替代原独立行业向导）
    industry_intent: bool     # 用户意图涉及创建新行业（路由确定性判定）
    industry_pack: dict       # 待确认/已确认的行业注册包（config + prompts 词典/路由样例）
    # 全自动建库流程（task_type=build_db）新增
    build_samples: list       # 探索阶段读取的文件样本 [{path, category, sample}]
    build_manifest: list      # 文件清单快照（checkpoint 持久化：interrupt 恢复后 context 丢失，
                              # 入库阶段必须依赖 state 中的清单而非 runtime.context）
    proposed_schema: dict     # AI 设计的库结构（definitions 格式 + rationale）
    schema_feedback: str      # 人工确认环节拒绝时给出的调整意见
    schema_confirm_rounds: int  # schema 被人工拒绝的轮次（上限 2 轮后放弃）
    # 失败重规划回路（方案C，20260806）
    agent_status: str       # agent_run 循环结局：ok / exhausted（步数耗尽=replan 触发信号）
    agent_trace_note: str   # 循环轨迹摘要（"query → mutate_data"），replan 时作失败证据
    replan_count: int       # 已重规划次数（route_after_agent 上限 1，防死循环）
    replan_note: str        # 失败证据摘要，understand 消费进拆解 prompt 后即清
