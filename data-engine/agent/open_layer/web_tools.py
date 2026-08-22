"""Web 工具——供 deep research 的 Researcher 调用

双策略：
- 主选 Tavily（需 TAVILY_API_KEY，AI 优化结果，免费 1000 次/月）
- 兜底 DuckDuckGo（完全免费，无需配置）
- 都不可用时返回提示

设计原则：
- 零配置可用（DuckDuckGo 兜底）
- 配置 Tavily 后自动升级（质量更高）
- 网页抓取有超时和大小限制
"""

import sys
import os
from pathlib import Path

# 确保项目根目录在 sys.path 中
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config.settings import settings


def web_search(query: str, max_results: int = 5) -> str:
    """网络搜索——返回结果列表（标题+摘要+链接）

    策略：
    1. 配置了 TAVILY_API_KEY → 用 Tavily（AI 优化，返回正文摘要）
    2. 未配置 → 用 DuckDuckGo（免费兜底）
    3. 都失败 → 返回错误提示

    Args:
        query: 搜索关键词
        max_results: 最大结果数

    Returns:
        搜索结果文本（格式化的结果列表）
    """
    if not settings.WEB_SEARCH_ENABLED:
        return "Web 搜索已禁用（WEB_SEARCH_ENABLED=false）"

    # 策略1：Tavily（主选）
    if settings.TAVILY_API_KEY:
        result = _tavily_search(query, max_results)
        if result and "搜索失败" not in result:
            return result

    # 策略2：DuckDuckGo（兜底）
    result = _duckduckgo_search(query, max_results)
    if result and "搜索失败" not in result:
        return result

    return f"网络搜索失败。可能原因：\n1. 网络连接问题\n2. 搜索 API 限流\n3. 未配置 TAVILY_API_KEY 且 DuckDuckGo 不可用\n\n建议：\n- 检查网络连接\n- 配置 TAVILY_API_KEY 获得更稳定的搜索服务"


def _ssrf_check(url: str) -> str | None:
    """SSRF 闸（评审五轮 A5）：环回/内网/链路本地/保留地址一律拒。
    返回 None=放行，否则拒绝文案。"""
    import ipaddress
    import socket as _socket
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return f"无效的 URL：{url}"
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return f"⛔ 目标地址在环回/内网范围，已拒绝（SSRF 防护）：{host}"
    try:
        ips = {i[4][0] for i in _socket.getaddrinfo(host, None)}
    except Exception:
        return f"无法解析目标主机：{host}"
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return f"目标地址非法: {ip}"
        if (addr.is_loopback or addr.is_private or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
            return f"⛔ 目标地址在环回/内网/保留范围，已拒绝（SSRF 防护）：{host} -> {ip}"
    return None


def web_fetch(url: str) -> str:
    """抓取网页内容——转为 markdown 格式

    Args:
        url: 网页 URL

    Returns:
        网页内容（markdown 格式，截断到 WEB_FETCH_MAX_CHARS）

    内存优化：
    - 检查 Content-Length，>5MB 直接拒绝
    - 流式读取 + 即时截断，避免大页面撑爆内存
    - HTML 转 markdown 后立即释放原始 HTML

    安全：SSRF 闸（环回/内网/链路本地/保留地址拒绝）+ 重定向逐跳复检
    （每跳重新过闸——防 A 站跳内网，评审五轮 A5）
    """
    if not url or not url.startswith(("http://", "https://")):
        return f"无效的 URL：{url}"

    try:
        import httpx
    except ImportError:
        return "缺少 httpx 依赖，请运行：pip install httpx"

    # 大小上限：超过此值直接拒绝（防止 OOM）
    MAX_CONTENT_BYTES = 5 * 1024 * 1024  # 5MB
    # 流式读取的块大小
    CHUNK_SIZE = 32 * 1024  # 32KB
    # 中间 HTML 缓冲上限（防止恶意大页面）
    MAX_HTML_BUFFER = 2 * 1024 * 1024  # 2MB

    from urllib.parse import urljoin
    current = url
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        with httpx.Client(timeout=settings.WEB_FETCH_TIMEOUT, follow_redirects=False) as client:
            html_bytes = b""
            for _hop in range(5):  # 重定向逐跳复检
                err = _ssrf_check(current)
                if err:
                    return err
                # 用 stream 流式请求，先看 Content-Length
                with client.stream("GET", current, headers=headers) as resp:
                    if resp.is_redirect:
                        loc = resp.headers.get("location", "")
                        if not loc:
                            return f"重定向缺少 location：{current}"
                        current = urljoin(current, loc)
                        continue
                    resp.raise_for_status()

                    content_type = resp.headers.get("content-type", "")
                    content_length = resp.headers.get("content-length")

                    # Content-Length 预检
                    if content_length and int(content_length) > MAX_CONTENT_BYTES:
                        return f"网页过大（{int(content_length) // 1024}KB > 5MB 上限），拒绝抓取：{current}"

                    # 非 HTML 内容直接返回摘要
                    if "text/html" not in content_type and "text/plain" not in content_type:
                        return f"非文本内容（Content-Type: {content_type}），大小：{content_length or '未知'} bytes"

                    # 流式读取 HTML，超过 MAX_HTML_BUFFER 立即停止
                    html_parts = []
                    total = 0
                    for chunk in resp.iter_bytes(chunk_size=CHUNK_SIZE):
                        total += len(chunk)
                        if total > MAX_HTML_BUFFER:
                            html_parts.append("\n<!-- 内容过大，已截断 -->".encode("utf-8"))
                            break
                        html_parts.append(chunk)
                    html_bytes = b"".join(html_parts)
                    # 立即释放分块列表
                    del html_parts
                    break
            else:
                return f"重定向次数过多（>5）：{url}"

            # 解码 HTML
            html = html_bytes.decode("utf-8", errors="ignore")
            del html_bytes

        # 转 markdown
        try:
            import markdownify
            md = markdownify.markdownify(html, heading_style="ATX")
        except ImportError:
            # 无 markdownify 时，简单清理 HTML 标签
            import re
            md = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
            md = re.sub(r'<style[^>]*>.*?</style>', '', md, flags=re.DOTALL | re.IGNORECASE)
            md = re.sub(r'<[^>]+>', ' ', md)
            md = re.sub(r'\s+', ' ', md).strip()

        # 释放原始 HTML
        del html

        # 截断
        max_chars = settings.WEB_FETCH_MAX_CHARS
        if len(md) > max_chars:
            head = md[:int(max_chars * 0.7)]
            tail = md[-int(max_chars * 0.2):]
            omitted = len(md) - len(head) - len(tail)
            md = f"{head}\n\n...（已省略约 {omitted} 字符）...\n\n{tail}"

        return f"来源：{url}\n\n{md}"

    except httpx.TimeoutException:
        return f"抓取超时（{settings.WEB_FETCH_TIMEOUT}秒）：{url}"
    except httpx.HTTPStatusError as e:
        return f"HTTP 错误 {e.response.status_code}：{url}"
    except Exception as e:
        return f"抓取失败：{e}"


# === 内部实现 ===

def _tavily_search(query: str, max_results: int) -> str:
    """Tavily 搜索（AI 优化，返回正文摘要）"""
    try:
        from langchain_tavily import TavilySearch
    except ImportError:
        return "搜索失败：未安装 langchain-tavily（pip install langchain-tavily）"

    try:
        tool = TavilySearch(
            max_results=max_results,
            api_key=settings.TAVILY_API_KEY,
            search_depth="advanced",  # 深度搜索，返回更多内容
        )
        results = tool.invoke(query)

        # 格式化结果
        if isinstance(results, list):
            lines = [f"Tavily 搜索结果（{len(results)} 条）：\n"]
            for i, r in enumerate(results, 1):
                title = r.get("title", "无标题")
                url = r.get("url", "")
                content = r.get("content", "")[:500]
                score = r.get("score", 0)
                lines.append(f"--- 结果 {i}（相关度: {score:.2f}）---")
                lines.append(f"标题: {title}")
                lines.append(f"链接: {url}")
                lines.append(f"摘要: {content}")
                lines.append("")
            return "\n".join(lines)
        elif isinstance(results, dict):
            # Tavily 有时返回 dict 格式
            items = results.get("results", [])
            lines = [f"Tavily 搜索结果（{len(items)} 条）：\n"]
            for i, r in enumerate(items, 1):
                title = r.get("title", "无标题")
                url = r.get("url", "")
                content = r.get("content", "")[:500]
                lines.append(f"--- 结果 {i} ---")
                lines.append(f"标题: {title}")
                lines.append(f"链接: {url}")
                lines.append(f"摘要: {content}")
                lines.append("")
            return "\n".join(lines)
        else:
            return f"搜索失败：Tavily 返回未知格式：{str(results)[:200]}"
    except Exception as e:
        return f"搜索失败：Tavily 错误：{e}"


def _duckduckgo_search(query: str, max_results: int) -> str:
    """DuckDuckGo 搜索（免费兜底）"""
    try:
        from langchain_community.tools import DuckDuckGoSearchResults
    except ImportError:
        return "搜索失败：未安装 duckduckgo-search（pip install duckduckgo-search）"

    try:
        tool = DuckDuckGoSearchResults(num_results=max_results)
        results_str = tool.invoke(query)

        # DuckDuckGoSearchResults 返回字符串格式
        if isinstance(results_str, str):
            # 解析格式：[snippet] (title) (link)
            import re
            lines = [f"DuckDuckGo 搜索结果：\n"]

            # 尝试解析结构化结果
            entries = re.findall(r'\[([^\]]+)\]\s*\(([^)]+)\)\s*\(([^)]+)\)', results_str)
            if entries:
                for i, (snippet, title, link) in enumerate(entries, 1):
                    lines.append(f"--- 结果 {i} ---")
                    lines.append(f"标题: {title}")
                    lines.append(f"链接: {link}")
                    lines.append(f"摘要: {snippet[:500]}")
                    lines.append("")
            else:
                # 直接返回原始字符串
                lines.append(results_str)
            return "\n".join(lines)
        else:
            return f"搜索失败：DuckDuckGo 返回未知格式：{str(results_str)[:200]}"
    except Exception as e:
        return f"搜索失败：DuckDuckGo 错误：{e}"


# === 工具描述（供 AI 调用时参考）===

WEB_TOOLS_DESCRIPTION = {
    "web_search": {
        "description": "网络搜索，返回结果列表（标题+摘要+链接）",
        "params": {"query": "搜索关键词", "max_results": "最大结果数（默认5）"},
        "returns": "搜索结果文本"
    },
    "web_fetch": {
        "description": "抓取网页内容，转为 markdown 格式",
        "params": {"url": "网页 URL"},
        "returns": "网页内容文本"
    }
}
