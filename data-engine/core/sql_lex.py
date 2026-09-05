"""SQL 词法器：全仓唯一的引号/注释/方言词法模型

统一建模引号/注释/方言的全部形态——schema 前缀、引用片段亚型、列级闸、
注释截断、AND 拆分误伤字面量、MySQL 方言（# 注释 / \\' 转义 / $ 标识符）。
共享词法器保证全仓口径一致，避免各调用点各自手搓引号状态机。

本模块提供三个原语，全部引号感知（doubling 转义处理）：
- strip_comments(sql, dialect)：剥注释（-- 行、/* */ 块、MySQL # 行）
- split_top_level(text, sep)：顶层分隔符切分（字面量内的分隔符不切）
- split_top_and_or(where)：顶层 AND 切分（字面量内的 AND 不切）

引号模型：' " ` 与方括号 [ ]（]] 为括号内转义）——与 TABLE_REF_FRAGMENT
的标识符形态全集一致（两层词法模型不再分叉）。

方言模型：
- sqlite：引号 ' " ` []，doubling 转义，注释 -- 与 /* */
- mysql：引号 ' " `，doubling + 反斜杠转义，注释 --、/* */、#
标识符字符集含 $（MySQL 形态；SQLite 下不出现，向后兼容）。
"""
import re

DIALECT_SQLITE = "sqlite"
DIALECT_MYSQL = "mysql"

_QUOTES = ("'", '"', '`', '[')  # '[' 的闭合符是 ']'
_CLOSE = {"'": "'", '"': '"', '`': '`', '[': ']'}


def _walk(sql: str, dialect: str):
    """逐字符产生 (ch, in_quote)——三原语共用的引号状态机（含方括号）。

    in_quote：None=引号外；否则为当前引号的开字符（' " ` [ 之一）。
    转义正确归一：''/""/``/]] 为 doubling；mysql 方言另认 \\' 反斜杠转义。
    """
    i, n = 0, len(sql)
    quote = None
    while i < n:
        ch = sql[i]
        if quote:
            closer = _CLOSE[quote]
            if (dialect == DIALECT_MYSQL and ch == "\\"
                    and i + 1 < n and sql[i + 1] == closer):
                yield ch, quote
                yield sql[i + 1], quote  # 反斜杠转义：不闭合
                i += 2
                continue
            if ch == closer:
                if i + 1 < n and sql[i + 1] == closer:
                    yield ch, quote
                    yield sql[i + 1], quote  # doubling 转义
                    i += 2
                    continue
                quote = None
                yield ch, None
                i += 1
                continue
            yield ch, quote
            i += 1
            continue
        if ch in _QUOTES:
            quote = ch
            yield ch, quote
            i += 1
            continue
        yield ch, None
        i += 1


def strip_comments(sql: str, dialect: str = DIALECT_SQLITE) -> str:
    """剥除 SQL 注释——引号感知；字面量内的 -- /* # 不剥（数据不是注释）。

    块注释位补空白，防两侧词粘连（FROM/**/users → FROM users）。
    dialect=mysql 时额外处理 # 行注释与 \\' 反斜杠转义。
    """
    out, i, n = [], 0, len(sql)
    quote = None
    while i < n:
        ch = sql[i]
        if quote:
            closer = _CLOSE[quote]
            out.append(ch)
            if (dialect == DIALECT_MYSQL and ch == "\\"
                    and i + 1 < n and sql[i + 1] == closer):
                out.append(closer)  # 反斜杠转义：\' 不闭合引号（MySQL）
                i += 2
                continue
            if ch == closer:
                if i + 1 < n and sql[i + 1] == closer:
                    out.append(closer)  # doubling 转义
                    i += 1
                else:
                    quote = None
            i += 1
            continue
        if ch in _QUOTES:
            quote = ch
            out.append(ch)
            i += 1
            continue
        if sql.startswith('--', i):
            j = sql.find('\n', i)
            i = n if j < 0 else j
            continue
        if sql.startswith('/*', i):
            j = sql.find('*/', i + 2)
            i = n if j < 0 else j + 2
            out.append(' ')
            continue
        if (dialect == DIALECT_MYSQL and ch == '#'
                and (not out or (out and str(out[-1]).isspace()))):
            j = sql.find('\n', i)
            i = n if j < 0 else j
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def split_top_level(text: str, sep: str) -> list:
    """顶层分隔符切分——引号/括号感知：字面量内与括号内的分隔符不切
    （sep 限单字符——本仓全部逗号场景；多字符分隔请用 split_top_and_or 族）"""
    parts, buf = [], []
    depth = 0
    i = 0
    for ch, in_quote in _walk(text, DIALECT_SQLITE):
        if in_quote is not None:
            buf.append(ch)
        elif ch == '(':
            depth += 1
            buf.append(ch)
        elif ch == ')':
            depth = max(0, depth - 1)
            buf.append(ch)
        elif depth == 0 and text.startswith(sep, i):
            parts.append(''.join(buf))
            buf = []
            i += len(sep) - 1
        else:
            buf.append(ch)
        i += 1
    parts.append(''.join(buf))
    return parts


_AND_RE = re.compile(r'\s+AND\s+', re.IGNORECASE)


def split_top_and_or(where: str) -> list:
    """顶层 AND 切分——引号感知：字面量内的 AND 不切（`name = 'R AND D'`
    曾按正则直接拆成两个假条件）。

    只切 AND（OR 语义不支持拆分，调用方按既有口径处理）。
    """
    if not where:
        return []
    parts, buf = [], []
    i = 0
    it = _walk(where, DIALECT_SQLITE)
    for ch, in_quote in it:
        if in_quote is not None:
            buf.append(ch)
            i += 1
            continue
        m = _AND_RE.match(where, i)
        if m:
            parts.append(''.join(buf))
            buf = []
            # 生成器按字消费：AND 区间的剩余字符必须从迭代器里排空，
            # 否则会被下一迭代重新拼回（'t.a = [R AND D] AND ...' 的
            # 第二段变 'AND t.b...'）
            for _ in range(m.end() - i - 1):
                next(it, None)
            i = m.end()
            continue
        buf.append(ch)
        i += 1
    parts.append(''.join(buf))
    return [p for p in (x.strip() for x in parts) if p]
