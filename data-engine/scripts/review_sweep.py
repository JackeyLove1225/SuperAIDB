"""修复断链防御——同病全仓扫

每类历史修复登记其"同类写法"模式与作用域/白名单，一键全仓扫。
修复完跑一遍：修 A 忘了 A' 的断链类问题直接现形。

用法：python scripts/review_sweep.py        # 有残留 exit 1
"""
import os
import re
import sys
from pathlib import Path

# Windows 控制台默认 cp1252/GBK，中文输出会直接 UnicodeEncodeError——自防御
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent
# 仓根上上级目录名动态锚（开发目录名——零硬编码字面量，从本文件位置推导）。
# 生效门槛（CI 拓扑实证）：CI checkout 路径形如 D:\a\<repo>\<repo>，
# 推导出的"目录名"是单字母 a 或仓名本身——a/ 会误伤一切 a/ 前缀路径、
# 仓名会误伤文档占位路径，这两种推导结果都不是"个人目录"语义。
# 仅当推导名像真实个人目录（≥4 字符且不等于仓名/父级名）时启用锚，
# 否则退化为无锚（结构形态 Users/ 仍在岗，发布闸真针兜底）。
_gp_name = BASE.parent.parent.name
_GENERIC = {"", "a", "users", "home", "data", "workspace", "work",
            BASE.name.lower(), BASE.parent.name.lower()}
_PERSONAL_DIR_RE = (
    re.escape(_gp_name) + r"[/\\]"
    if len(_gp_name) >= 4 and _gp_name.lower() not in _GENERIC
    else r"(?!x)x"  # 永不匹配（无锚退化，fail-open 方向如实记录）
)
if _PERSONAL_DIR_RE == r"(?!x)x":
    print(f"    [info] 个人目录锚未启用（推导名 {_gp_name!r} 不像个人目录——"
          f"CI 拓扑下锚会自爆，结构形态与发布针仍在岗）")
# CI 里前端仓以第二 checkout 落在工作区内（SWEEP_UI_DIR 指定）——
# 前端病型规则在 CI 经 SWEEP_UI_DIR 生效，不因"兄弟仓缺席"空转
UI = os.environ.get("SWEEP_UI_DIR") and Path(os.environ["SWEEP_UI_DIR"]) or BASE.parent / "agent-chat-ui"

# （名称， 作用域根， glob， 正则， 允许出现的文件名片段， 严重度）
CHECKS = [
    # 非恒定时间比较 API key
    ("API key 非恒定比较", BASE, "*.py", r"api_key\s*==\s*settings\.API_KEY", [], "high"),
    # os.kill(pid, 0) 存活探测（只抓真实调用行——行尾即调用结束；
    # 解释性注释/docstring 提及不抓）
    ("os.kill(pid,0) 存活探测", BASE, "*.py", r"os\.kill\(\w+,\s*0\)\s*$",
     ["review_sweep"], "high"),
    # 朴素逗号切分解析——现有的全是人工复核过的"纯标识符清单"
    #（join/select/group/索引列/模板数值，无引号字面值参与）。本项改公告级：
    # 不阻断，只展示清单供人工核对——新增 SET/值字面量解析要过这里
    ("朴素 split(',') 解析清单（公告级，人工核对）", BASE / "core", "*.py",
     r"\.split\([\"'],[\"']\)",
     ["extract_", "split_top", "data_ops", "condition_parser", "check_templates",
      "crud", "db_chat", "base", "mysql_driver", "sqlite_driver", "join_executor",
      "types"],
     "info"),
    # 选择集 ids str(int()) 直拼
    ("id 列表 int 直拼残留", BASE, "*.py", r"str\(int\(\w+\)\)",
     ["check_templates"], "medium"),  # check_templates 是模板数值归一，非 id 列表
    # 前端：裸 fetch 绕过 apiFetch（收敛点/代理/健康检查豁免）
    ("裸 fetch 残留", UI / "src", "*.ts*",
     r"(?<![\w.])fetch\(", ["api-fetch", "route.ts", "StartupOverlay", "media-sign"], "high"),
    # 过程性叙事词（净化后不得复活）
    ("退役叙事词复活", BASE, "*.py", r"已退役|LangGraph :2024", ["review_sweep"], "medium"),
    ("neo4j 残留复活", BASE, "*.py", r"neo4j_store|Neo4jStore", ["review_sweep"], "medium"),
    # 密钥/个人痕迹（永不进仓）。注意：模式必须是结构形态，不得内嵌任何真实密钥
    # 字面值——否则本脚本自己就成为泄漏源（发布器的密钥扫描会拦下它，真有此事）。
    # (?i) 全串忽略大小写：sk- 模型 key 形态 / 48 位 hex token 形态 /
    # key|secret|token 后接 32+ 位高熵串的硬编码赋值 / 开发机绝对路径形态。
    ("密钥/个人路径泄漏", BASE, "*.*",
     (r"(?i)sk-[0-9a-f]{32,}|(?<![0-9a-f])[0-9a-f]{48}(?![0-9a-f])"
      r"|(?:api[_-]?key|secret|token)[\"'\s]*[:=]\s*[\"'][A-Za-z0-9_\-]{32,}"
      r"|[A-Za-z]:[/\\]+Users[/\\][\w.\\@-]*|" + _PERSONAL_DIR_RE),  # 个人路径：结构形态 + 仓根目录名动态锚（正/反斜杠都算）
     ["review_sweep", "archive"], "high"),  # archive: 冻结历史文档，不进发布面（sync_showcase 排除）
    # 表名捕获的旧病型形态（固定 \w+ 不含点号/空白容忍）——schema 前缀穿透的
    # 结构签名，全仓唯一正解是 TABLE_REF_FRAGMENT（security_contract）
    ("表名捕获旧病型复活", BASE / "core", "*.py",
     r"\[`\"\\\]\\?\(\\w\+\)", ["review_sweep"], "high"),
]

_EXT = {".py", ".md", ".ts", ".tsx", ".yml", ".yaml", ".toml", ".js", ".ps1", ".bat", ".txt", ".json"}
_SKIP_DIRS = {".git", "node_modules", ".next", "__pycache__", ".libs", "vendor", "exports",
              "uploads", "logs", "db", "cache", ".dev_export", ".ui_export",
              "runtime",  # config/runtime/ 是 gitignore 的守护进程/会话暂态（含 daemon token），永不进发布面
              }
_SKIP_FILES = {"pnpm-lock.yaml"}


def _iter_files(root: Path, glob_pat: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in filenames:
            if f in _SKIP_FILES:
                continue
            p = Path(dirpath) / f
            if glob_pat == "*.*" and p.suffix not in _EXT:
                continue
            if glob_pat != "*.*" and p.suffix != glob_pat.lstrip("*"):
                continue
            yield p


def main() -> int:
    problems = 0
    for name, root, glob_pat, pattern, allow, sev in CHECKS:
        if not root.exists():
            # 扫描器不吱声等于没扫（自己的纪律自己遵守）——
            # SWEEP_UI_DIR 显式设置而目录缺失（CI 布局漂移）即 fail-closed
            if os.environ.get("SWEEP_UI_DIR") and "agent-chat-ui" in str(root):
                print(f"  [FAIL] SWEEP_UI_DIR 显式设置但目录缺失: {root}（前端病型扫描面 fail-closed）")
                sys.exit(1)
            print(f"  [skip] 作用域不存在，本环境跳过: {root}")
            continue
        rx = re.compile(pattern)
        hits = []
        for p in _iter_files(root, glob_pat):
            # 注意 UI 作用域在 BASE 之外（兄弟仓 agent-chat-ui）：
            # 匹配/展示一律用 relpath/绝对路径，relative_to 会直接 ValueError
            # 白名单精确匹配（词边界）：宽子串（如 "base"/"types"）会把未来的
            # prototypes.py 这类文件静默豁免出扫描面——只认文件名/词干/目录段
            if any(a == p.stem or a == p.name or a in p.parts for a in allow):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                # 扫描器读不动就必须吱声——静默跳过的文件等于没扫
                print(f"    [warn] 读取失败跳过扫描: {p}: {e}")
                continue
            disp = os.path.relpath(p, BASE)
            for ln, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"    {disp}:{ln}: {line.strip()[:90]}")
        if hits and sev != "info":
            problems += 1
            print(f"[{sev}] ✗ {name}（{len(hits)} 处残留）:")
            print("\n".join(hits[:8]))
        elif hits:
            print(f"[{sev}] ℹ {name}（{len(hits)} 处，人工复核清单）:")
            print("\n".join(hits[:8]))
        else:
            print(f"[{sev}] ✓ {name}")
    print()
    if problems:
        print(f"发现 {problems} 类残留——修复断链，先扫同类再提交")
        return 1
    print("全部病型无残留 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
