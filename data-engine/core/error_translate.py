"""用户可见错误口语化翻译层

把技术错误文本翻译为用户能懂的话，技术细节收进 detail 供日志/排查。
白盒原则：翻译只做"换说法"，不改变错误分类与处理逻辑——
返回 (friendly, detail)；friendly 给用户看，detail 进日志。
"""
import re

# (模式, 口语化说法)——按命中顺序匹配
_PATTERNS = [
    (r"database is locked", "数据库正忙（有写入在进行中），请稍等几秒再试"),
    (r"Thinking mode does not support", "AI 服务当前模型与该调用方式不兼容，请联系管理员调整模型配置"),
    (r"Error code: 401|invalid_api_key|Incorrect API key", "AI 服务密钥失效，请联系管理员更新 API Key"),
    (r"Error code: 429|Rate limit|rate_limit", "AI 服务调用太频繁，被限流了，请稍后再试"),
    (r"Error code: 400", "AI 服务拒绝了本次请求（参数不被接受），请换个说法再试"),
    (r"timeout|timed out|ReadTimeout", "AI 服务响应超时，请稍后再试"),
    (r"Connection.*refused|ECONNREFUSED", "后端服务未启动或不可达，请确认服务已启动"),
    (r"FC AI 调用异常", "AI 服务暂时不可用，请稍后重试"),
    (r"未找到文件", "没有找到对应的文件，请确认文件已加载或已上传"),
    (r"database is not initialized|向量数据库未初始化", "文档检索服务未就绪，请联系管理员检查向量库配置"),
    (r"磁盘空间不足|No space left", "服务器磁盘空间不足，请联系管理员清理"),
]


def translate_error(text: str) -> tuple:
    """技术错误 → (用户口语化说法, 技术细节)

    未命中模式时返回 (精简后的原文, 原文)。friendly 永远以中文人话开头；
    原文过长时截断到 120 字，避免用户被错误墙淹没。
    """
    if not text:
        return "操作失败，原因未知", ""
    for pattern, friendly in _PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return friendly, text[:500]
    # 未命中：保留原文但截断（防止错误墙）
    brief = text.strip()
    if len(brief) > 120:
        brief = brief[:120].rstrip() + "…"
    return f"操作失败：{brief}", text[:500]
