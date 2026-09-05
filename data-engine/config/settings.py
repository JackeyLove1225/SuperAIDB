"""全局配置管理"""

import os
from pathlib import Path

from dotenv import load_dotenv


# 加载 .env 文件
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

# .env 新鲜读取（mtime 键缓存）：运行期改 .env（如行业切换）下次访问即生效，
# 不用重启进程（ConfigHub 语义在 dotenv 上的实现）
_env_cache: dict = {"mtime": 0.0, "data": {}}


def _read_env_fresh() -> dict:
    try:
        m = _env_path.stat().st_mtime
    except OSError:
        # 文件被删=配置回默认——删除也是变更，mtime 通道必须感知（20260822 修复：
        # 此前返回陈旧缓存，删 .env 后 INDUSTRY 等热键仍读旧值）
        _env_cache.update(mtime=0.0, data={})
        return _env_cache["data"]
    if m != _env_cache["mtime"]:
        data = {}
        for line in _env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
        _env_cache.update(mtime=m, data=data)
    return _env_cache["data"]


class Settings:
    """全局配置——所有配置从环境变量读取，支持运行时覆盖"""

    # AI 配置
    AI_API_KEY: str = os.getenv("AI_API_KEY", "")
    AI_BASE_URL: str = os.getenv("AI_BASE_URL", "https://api.deepseek.com")
    AI_MODEL: str = os.getenv("AI_MODEL", "deepseek-v4-flash")
    # 规划/思考类步骤（拆解/综合/设计/研究）用的更强模型；空=回退 AI_MODEL。
    # 分级逻辑：机械高频小调用（FC 参数提取等）吃 flash 控成本，
    # 决定"聪不聪明"的规划步吃 pro——成本花在刀刃上。
    AI_MODEL_PLANNING: str = os.getenv("AI_MODEL_PLANNING", "")

    # 数据库加密（默认开）：明文库接入时自动迁移为密文（SQLCipher），
    # 密钥由 OS 凭据管理器保管（keyring）。显式 false 才回退明文（不推荐）
    DB_ENCRYPT: str = os.getenv("DB_ENCRYPT", "true")
    _db_encrypt_override: str = None  # 进程内覆盖优先（测试/切换用），空=读 .env

    # 数据守护进程（运行时隔离，二期）：true=全部驱动调用经 daemon 进程
    #（密钥只驻 daemon 内存，本进程只见 IPC 令牌）；false=进程内直连（调试用）
    DAEMON_MODE: str = os.getenv("DAEMON_MODE", "true")
    _daemon_mode_override: str = None

    @property
    def DAEMON_MODE_EFFECTIVE(self) -> str:
        """DAEMON_MODE 生效值：进程内覆盖 > 环境变量 > .env > 默认 false"""
        if self._daemon_mode_override:
            return self._daemon_mode_override
        import os as _os
        return _os.getenv("DAEMON_MODE") or _read_env_fresh().get("DAEMON_MODE", "true")

    @property
    def DB_ENCRYPT_EFFECTIVE(self) -> str:
        """DB_ENCRYPT 生效值：进程内覆盖 > 环境变量 > .env 文件 > 默认 true"""
        if self._db_encrypt_override:
            return self._db_encrypt_override
        import os as _os
        return _os.getenv("DB_ENCRYPT") or _read_env_fresh().get("DB_ENCRYPT", "true")

    # 数据库类型
    _DB_TYPE: str = os.getenv("DB_TYPE", "sqlite")

    def set_db_type(self, db_type: str):
        self._DB_TYPE = db_type

    @property
    def db_type(self) -> str:
        return self._DB_TYPE

    # SQLite 存储路径
    SQLITE_DB_PATH: str = os.getenv("SQLITE_DB_PATH", "./db/data_engine.db")

    # MySQL 配置（DB_TYPE=mysql 时使用）
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "localhost")
    MYSQL_PORT: str = os.getenv("MYSQL_PORT", "3306")
    MYSQL_USER: str = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD: str = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DATABASE: str = os.getenv("MYSQL_DATABASE", "superaidb")

    # 行业模式（热键：.env 变了下次访问即新值——行业切换免重启；
    # 兼容旧契约：代码直接 settings.INDUSTRY = x 赋值为进程内覆盖，优先于文件）
    _industry_override: str = None

    @property
    def INDUSTRY(self) -> str:
        if self._industry_override:
            return self._industry_override
        return _read_env_fresh().get("INDUSTRY", "construction_engineering")

    @INDUSTRY.setter
    def INDUSTRY(self, value: str):
        self._industry_override = value or None

    # P1 语义解析 AI 开关：True=启用 P1 的 AI 语义解析；
    # False=默认关闭，意图标签走调用方结构化透传（跳过 P1 的 AI 解析）
    P1_AI_ENABLED: bool = os.getenv("P1_AI_ENABLED", "false").lower() == "true"

    # FC（Function Calling）AI 开关：True=启用 FC AI 从自然语言提取参数；
    # False=默认关闭，按调用方给出的 structured_args JSON 直接构造参数，跳过 FC AI
    FC_AI_ENABLED: bool = os.getenv("FC_AI_ENABLED", "false").lower() == "true"

    # 3.1 规划层确定性短路开关：True=简单问题跳过 LLM 拆解直达工具（省 token 省时），
    # False=回退完整 LLM 拆解。fail-open 设计：短路未命中自动落回 LLM，与开关无关。
    SHORTCIRCUIT_ENABLED: bool = os.getenv("SHORTCIRCUIT_ENABLED", "true").lower() == "true"

    # 当前可用文件（前端上传后设置）
    _CURRENT_FILE: str = ""

    def set_current_file(self, path: str):
        self._CURRENT_FILE = path

    @property
    def current_file(self) -> str:
        return self._CURRENT_FILE

    # 存储路径
    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", "./uploads")

    # 向量数据库配置
    VECTOR_STORE_TYPE: str = os.getenv("VECTOR_STORE_TYPE", "chroma")
    CHROMA_PATH: str = os.getenv("CHROMA_PATH", "./db/chroma")

    # Embedding 模型（默认空 = 使用 Chroma 内置 onnxruntime 版 all-MiniLM-L6-v2，行为与历史一致）
    # 配置任意 sentence-transformers 模型名可替换 embedding 模型（中文场景推荐 BAAI/bge-small-zh-v1.5），
    # 需先 pip install sentence-transformers（可选依赖，未安装时自动降级回内置默认模型）。
    # 注意：无论内置模型还是自定义模型，首次使用都需联网下载模型权重；
    # 离线/客户环境需预置模型缓存（内置模型缓存于 ~/.cache/chroma/onnx_models，
    # sentence-transformers 模型缓存于 HF 缓存目录，可用 HF_HOME 指定）。
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "")
    # Embedding 运行设备（可选，如 cpu / cuda；留空由 sentence-transformers 自动选择）
    EMBEDDING_DEVICE: str = os.getenv("EMBEDDING_DEVICE", "")

    # API 认证（默认关闭，本地使用；部署到服务器时开启）
    API_KEY_ENABLED: str = os.getenv("API_KEY_ENABLED", "false")
    API_KEY: str = os.getenv("API_KEY", "")
    # 开放注册（默认开，桌面端自助建号；部署到服务器/暴露网络时必须 false，
    # 关闭后仅 admin 可经用户管理端点创建账号）
    AUTH_REGISTER_ENABLED: str = os.getenv("AUTH_REGISTER_ENABLED", "true")

    # === 服务端口 / host / CORS ===
    MGMT_HOST: str = os.getenv("MGMT_HOST", "127.0.0.1")
    MGMT_PORT: int = int(os.getenv("MGMT_PORT", "2025"))
    LANGGRAPH_PORT: int = int(os.getenv("LANGGRAPH_PORT", "2024"))
    FRONTEND_PORT: int = int(os.getenv("FRONTEND_PORT", "3000"))
    MGMT_CORS_ORIGINS: str = os.getenv(
        "MGMT_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")

    @property
    def mgmt_cors_origin_list(self) -> list:
        """CORS 允许来源（逗号分隔配置 → 列表）"""
        return [o.strip() for o in self.MGMT_CORS_ORIGINS.split(",") if o.strip()]

    # === 体系B：深度研究配置 ===
    # Web搜索
    WEB_SEARCH_ENABLED: bool = os.getenv("WEB_SEARCH_ENABLED", "true").lower() == "true"
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")

    # OCR 云服务（图片/扫描件文字识别，PaddleOCR；无 token 时扫描件如实降级）
    OCR_API_URL: str = os.getenv("OCR_API_URL", "")
    OCR_API_TOKEN: str = os.getenv("OCR_API_TOKEN", "")
    OCR_MODEL: str = os.getenv("OCR_MODEL", "PaddleOCR-VL-1.6")
    OCR_TIMEOUT: str = os.getenv("OCR_TIMEOUT", "300")
    WEB_FETCH_TIMEOUT: int = int(os.getenv("WEB_FETCH_TIMEOUT", "15"))
    WEB_FETCH_MAX_CHARS: int = int(os.getenv("WEB_FETCH_MAX_CHARS", "4000"))

    # 文件工具
    FILE_TOOLS_ENABLED: bool = os.getenv("FILE_TOOLS_ENABLED", "true").lower() == "true"
    # 写文件权限——永久关闭，预防日后需求（代码保留但不启用）
    FILE_WRITE_ENABLED: bool = os.getenv("FILE_WRITE_ENABLED", "false").lower() == "true"
    FILE_ACCESS_ROOT: str = os.getenv("FILE_ACCESS_ROOT", str(Path(__file__).resolve().parent.parent))

    # OODA循环参数
    OODA_MAX_ROUNDS_PER_GOAL: int = int(os.getenv("OODA_MAX_ROUNDS_PER_GOAL", "5"))
    OODA_MAX_GOALS: int = int(os.getenv("OODA_MAX_GOALS", "8"))
    OODA_MAX_TOTAL_ROUNDS: int = int(os.getenv("OODA_MAX_TOTAL_ROUNDS", "20"))

    # === Ladybug 图数据库配置（表关系可视化图层）===
    # LadybugDB 是嵌入式图数据库（Kuzu 继任者）：进程内运行、零 JVM、零独立服务。
    LADYBUG_ENABLED: bool = os.getenv("LADYBUG_ENABLED", "true").lower() == "true"
    # Ladybug 数据库文件路径（空则默认 data-engine/db/schema_graph.lbdb）
    LADYBUG_DB_PATH: str = os.getenv("LADYBUG_DB_PATH", "")

    def check_neo4j_config(self) -> str:
        """兼容接口：返回空串（无配置问题）。"""
        return ""


settings = Settings()
