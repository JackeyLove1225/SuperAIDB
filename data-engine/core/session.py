"""会话管理模块——持久化对话历史，原子化操作"""
import json
from pathlib import Path
from datetime import date, datetime


def get_history_path() -> Path:
    """按日期返回当前对话 JSON 文件路径"""
    today = date.today().isoformat()
    return Path(__file__).resolve().parent.parent / "db" / "json" / f"conversation_{today}.json"


def load_history(max_turns: int = 500) -> list[str]:
    path = get_history_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data[-max_turns:] if len(data) > max_turns else data
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history: list[str]):
    path = get_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    _save_md(history)


def _save_md(history: list[str]):
    """将对话历史输出为 Markdown 文件"""
    today = date.today().isoformat()
    md_path = Path(__file__).resolve().parent.parent / "db" / "md" / f"conversation_{today}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 对话历史\n\n"]
    for entry in history:
        if entry.startswith("用户"):
            ts = _ts(entry)
            # 兼容旧格式 "用户:" 和新格式 "用户 [ts]:"
            content = entry.split("]: ", 1)[-1] if "]:" in entry else entry.split(":", 1)[-1]
            lines.append(f"> **用户** ({ts}):\n\n{content.strip()}\n\n---\n\n")
        elif entry.startswith("结果:"):
            content = entry[3:].strip()
            lines.append(f"**系统**: {content}\n\n---\n\n")
        elif entry.strip():
            lines.append(f"{entry}\n\n---\n\n")
    md_path.write_text("".join(lines), encoding="utf-8")


def _ts(entry: str) -> str:
    """从 entry 中提取时间戳，无法提取时返回空字符串"""
    import re
    m = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', entry)
    return m.group(1) if m else ""


def clear_history():
    path = get_history_path()
    if path.exists():
        path.unlink()


def add_turn(history: list[str], user_input: str, result: str, max_turns: int = 500) -> list[str]:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    history.append(f"用户 [{ts}]: {user_input}")
    if result:
        history.append(f"结果: {result[:200]}")
    trimmed = history[-(max_turns * 2):] if len(history) > max_turns * 2 else history
    save_history(trimmed)
    return trimmed


# 注意：clear_session 工具统一在 agent/tools.py 注册（唯一实现方，handler 调用本模块的 clear_history）。
# 本模块不再自行注册，避免同名工具双注册。
