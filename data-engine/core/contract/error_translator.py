"""错误翻译器——所有 driver 异常的统一翻译入口

职责：
1. 把 driver 抛出的原始错误翻译为统一中文 + 修复建议
2. 按 driver 类型路由（sqlite/mysql/postgresql/...）
3. 支持新 driver 通过 register() 注册翻译模式（开闭原则）

设计原则：
- 静态方法 + 类级别注册表（线程安全）
- 翻译规则用 (regex, template) 元组，re.search + expand 实现捕获组替换
- 翻译后返回 AppError（保持原异常的 message 和 detail）
- 无法匹配时返回兜底中文错误

从 sqlite_driver._translate_sqlite_error / mysql_driver._translate_mysql_error 提取。
新增 driver 时只需 ErrorTranslator.register("postgresql", [...]) 一行注册。
"""
import re
from typing import List, Tuple

from core.exceptions import AppError


# 翻译规则类型：(pattern, template)
TranslationRule = Tuple[str, str]


class ErrorTranslator:
    """错误翻译器——所有 driver 异常的统一翻译入口"""

    # 注册表：driver_type → [(pattern, translation), ...]
    _TRANSLATIONS = {
        "sqlite": [
            (r"no such table: (.+)", r"表 '\1' 不存在"),
            (r"no such column: (.+)", r"字段 '\1' 不存在"),
            (r"table (.+) already exists", r"表 '\1' 已存在"),
            (r"UNIQUE constraint failed: (\w+)\.(\w+)",
             r"违反唯一性约束：字段 '\2' 的值已存在"),
            (r"NOT NULL constraint failed: (\w+)\.(\w+)",
             r"违反非空约束：字段 '\2' 不能为空"),
            (r"CHECK constraint failed: (.+)", r"违反 CHECK 约束：\1"),
            (r"foreign key mismatch", r"外键类型不匹配：请检查引用列类型是否一致"),
            (r"FOREIGN KEY constraint failed",
             r"外键约束失败：引用的记录不存在或被引用记录仍有依赖"),
            (r"ambiguous column name: (.+)",
             r"字段名歧义：多个表中存在同名字段（），请使用 table.column 格式指定"),
        ],
        "mysql": [
            (r"Table '(\w+\.)?(\w+)' doesn't exist", r"表 '\2' 不存在"),
            (r"Unknown column '(\w+)'", r"字段 '\1' 不存在"),
            (r"Table '(\w+\.)?(\w+)' already exists", r"表 '\2' 已存在"),
            (r"Duplicate entry '([^']+)' for key '(\w+)'",
             r"违反唯一性约束：值 '\1' 已存在（字段 \2）"),
            (r"cannot be null", r"违反非空约束：该字段不能为空"),
            (r"foreign key constraint fails \((.+)\)",
             r"外键约束失败：\1（引用的记录不存在或被引用记录仍有依赖）"),
            (r"Check constraint '(\w+)' is violated",
             r"违反 CHECK 约束：约束 '\1' 失败"),
            (r"Data too long for column '(\w+)'",
             r"字段 '\1' 数据过长，超过字段长度限制"),
            (r"Incorrect (\w+) value: '([^']+)' for column '(\w+)'",
             r"字段 '\3' 类型不匹配：值 '\2' 不是有效的 \1"),
        ],
        # 未来扩展：postgresql、oracle 等
        # 新 driver 注册示例：
        # ErrorTranslator.register("postgresql", [
        #     (r'relation "(\w+)" does not exist', r"表 '\1' 不存在"),
        #     ...
        # ])
    }

    # 修复建议（可选，匹配关键字后追加）
    _SUGGESTIONS = {
        "不存在": "请检查表名/字段名拼写，或确认已创建该表/字段",
        "已存在": "请使用不同的名称，或先删除已存在的对象",
        "违反唯一性约束": "请检查数据是否重复，或使用 INSERT IGNORE / REPLACE",
        "违反非空约束": "请为该字段提供有效值，或将表字段改为允许 NULL",
        "外键约束失败": "请先删除/更新引用该记录的子记录，或检查外键值是否存在",
        "外键类型不匹配": "请确保外键列与被引用列的类型完全一致",
        "违反 CHECK 约束": "请检查数据是否满足 CHECK 表达式条件",
        "数据过长": "请缩短数据长度，或修改字段类型为更大容量的类型",
        "类型不匹配": "请检查数据类型是否与字段定义一致",
    }

    @staticmethod
    def register(driver_type: str, patterns: List[TranslationRule]) -> None:
        r"""注册新的 driver 错误翻译模式（供新 driver 扩展用）

        示例：
            ErrorTranslator.register("postgresql", [
                (r'relation "(\w+)" does not exist', r"表 '\1' 不存在"),
                (r'column "(\w+)" does not exist', r"字段 '\1' 不存在"),
            ])

        Args:
            driver_type: driver 类型名（如 "postgresql"）
            patterns: 翻译规则列表，每项为 (regex_pattern, translation_template)
        """
        ErrorTranslator._TRANSLATIONS[driver_type] = list(patterns)

    @staticmethod
    def add_rules(driver_type: str, patterns: List[TranslationRule]) -> None:
        """为已注册的 driver 追加翻译规则（不覆盖已有规则）

        Args:
            driver_type: driver 类型名
            patterns: 要追加的翻译规则列表
        """
        existing = ErrorTranslator._TRANSLATIONS.get(driver_type, [])
        existing.extend(patterns)
        ErrorTranslator._TRANSLATIONS[driver_type] = existing

    @staticmethod
    def translate(driver_type: str, error: Exception) -> AppError:
        """翻译 driver 异常为友好中文错误

        Args:
            driver_type: "sqlite" / "mysql" / "postgresql" 等
            error: driver 抛出的原始异常

        Returns:
            翻译后的 AppError（detail 字段保留原始错误信息）
        """
        msg = str(error)
        patterns = ErrorTranslator._TRANSLATIONS.get(driver_type, [])

        translated = None
        for pattern, template in patterns:
            try:
                m = re.search(pattern, msg, re.IGNORECASE)
                if m:
                    translated = m.expand(template)
                    break
            except re.error:
                # 模式本身非法，跳过
                continue

        if translated:
            suggestion = ErrorTranslator._match_suggestion(translated)
            message = translated
            if suggestion:
                message = f"{translated}。建议：{suggestion}"
            return AppError(message, detail=msg)

        # 兜底：截断原始错误
        return AppError(f"数据库操作失败: {msg[:200]}", detail=msg)

    @staticmethod
    def _match_suggestion(translated_msg: str) -> str:
        """根据翻译后的消息匹配修复建议

        Args:
            translated_msg: 翻译后的中文消息

        Returns:
            修复建议字符串，无匹配则返回空串
        """
        for keyword, suggestion in ErrorTranslator._SUGGESTIONS.items():
            if keyword in translated_msg:
                return suggestion
        return ""

    @staticmethod
    def get_driver_type(driver) -> str:
        """从 Driver 实例推断 driver_type

        优先用类名推断（SqliteDriver → sqlite，MysqlDriver → mysql），
        兜底返回 "sqlite"。

        Args:
            driver: Driver 实例

        Returns:
            driver_type 字符串
        """
        cls_name = type(driver).__name__.lower()
        if "sqlite" in cls_name:
            return "sqlite"
        if "mysql" in cls_name:
            return "mysql"
        if "postgres" in cls_name or "pg" in cls_name:
            return "postgresql"
        if "oracle" in cls_name:
            return "oracle"
        # 兜底
        return cls_name.replace("driver", "") or "sqlite"
