"""定向提取的目标表解析——把用户说的业务词确定性映射为当前行业的实际表名

白盒原则：不猜、不静默。命中靠 表英文名/中文业务名/描述/术语词典别名 的确定性匹配；
未命中的词如实返回，由调用方明确告知用户（绝不含糊地带到无关表）。
"""
from core.logger import get_logger

logger = get_logger(__name__)


def _load_industry_terms() -> tuple[list, dict]:
    """返回 (schemas, table_aliases)

    schemas 来自唯一加载入口 schema_matcher.load_schemas；
    table_aliases 来自行业 prompts.yml 的 terminology.table_aliases。
    """
    from config.settings import settings
    from core.schema_matcher import load_schemas
    schemas = load_schemas()
    aliases = {}
    try:
        from industries.base import discover_industries, get_industry
        discover_industries()
        cfg = get_industry(settings.INDUSTRY)
        aliases = ((cfg.terminology or {}).get("table_aliases") or {})
    except Exception as e:
        logger.warning("术语词典加载失败（仅用表名/业务名匹配）: %s", e)
    return schemas, aliases


def resolve_tables_by_terms(terms: list[str]) -> tuple[list[str], list[str]]:
    """用户目标词 → 当前行业表名

    匹配优先级：英文名精确 = 业务名精确 > 别名精确 > 业务名子串 > 别名子串。
    返回 (命中表名列表（保序去重）, 未命中词列表)。
    """
    schemas, aliases = _load_industry_terms()
    targets: list[str] = []
    unmatched: list[str] = []

    def _add(name: str) -> None:
        if name and name not in targets:
            targets.append(name)

    for raw in terms:
        t = (raw or "").strip()
        if len(t) < 2:
            continue
        hit = ""
        # 1) 精确匹配：英文名 / 业务名 / 别名
        for s in schemas:
            if t == s.get("name") or t == s.get("business_name") or t in (aliases.get(s.get("name"), []) or []):
                hit = s.get("name", "")
                break
        # 2) 业务名子串（用户词 ⊂ 业务名，或业务名 ⊂ 用户词——如"供应商"⊂"供应商信息"）
        if not hit:
            for s in schemas:
                bn = s.get("business_name", "")
                if bn and (t in bn or bn in t):
                    hit = s.get("name", "")
                    break
        # 3) 别名子串
        if not hit:
            for s in schemas:
                for a in (aliases.get(s.get("name"), []) or []):
                    if len(a) >= 2 and (t in a or a in t):
                        hit = s.get("name", "")
                        break
                if hit:
                    break
        # 4) 子词兜底：用户词逐步缩短（≥2字），取最长且在表业务名中唯一命中的子词
        # （'材料价格' → '材料' ⊂ '定额材料明细表'；多表同长命中=歧义不猜，如实未命中）
        if not hit and len(t) > 2:
            for ln in range(len(t) - 1, 1, -1):
                subs = {t[i:i + ln] for i in range(0, len(t) - ln + 1)}
                sub_hits = {s.get("name", "") for s in schemas
                            if s.get("business_name", "")
                            and any(sub in s["business_name"] for sub in subs)}
                if len(sub_hits) == 1:
                    hit = sub_hits.pop()
                    logger.info("目标词 %r 子词兜底命中表 %s", t, hit)
                    break
                if len(sub_hits) > 1:
                    break
        if hit:
            _add(hit)
        else:
            unmatched.append(t)
    return targets, unmatched
