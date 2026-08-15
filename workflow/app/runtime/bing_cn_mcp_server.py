from __future__ import annotations

"""Keyless MCP web search backed by Bing's mainland China RSS endpoint.

The process speaks standard MCP over stdio.  It deliberately exposes search
only; exact page retrieval remains the responsibility of the separately
governed ``fetch`` MCP server.
"""

import html
import re
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx
from mcp.server.fastmcp import FastMCP


SEARCH_ENDPOINT = "https://cn.bing.com/search"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/124.0 Safari/537.36 ResumAI-MCP/1.0"
)
DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)
TAG_RE = re.compile(r"<[^>]+>")

mcp = FastMCP("resumai-bing-cn-search")


def _plain_text(value: str) -> str:
    value = html.unescape(TAG_RE.sub(" ", value or ""))
    return re.sub(r"\s+", " ", value).strip()


def _valid_result_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _matches_site(value: str, site: str) -> bool:
    if not site:
        return True
    hostname = (urlparse(value).hostname or "").lower().rstrip(".")
    return hostname == site or hostname.endswith(f".{site}")


@mcp.tool()
async def web_search(query: str, max_results: int = 5, site: str = "") -> dict[str, Any]:
    """Search the public web from the mainland ECS.

    Use only to discover public pages related to a candidate-declared name,
    repository, domain, or URL.  Search hits are discovery leads, not verified
    candidate facts; use the fetch MCP tool on a selected URL before citing it.

    Args:
        query: Focused search query containing the declared candidate identifier.
        max_results: Number of results to return, from 1 to 10.
        site: Optional exact domain restriction such as github.com.
    """
    normalized_query = re.sub(r"\s+", " ", (query or "")).strip()
    if not normalized_query or len(normalized_query) > 300:
        raise ValueError("query must contain 1-300 characters")

    normalized_site = (site or "").strip().lower().rstrip(".")
    if normalized_site:
        if not DOMAIN_RE.fullmatch(normalized_site):
            raise ValueError("site must be a plain DNS domain")
        normalized_query = f"{normalized_query} site:{normalized_site}"

    limit = max(1, min(int(max_results), 10))
    params = {
        "q": normalized_query,
        "format": "rss",
        "cc": "CN",
        "mkt": "zh-CN",
        "setlang": "zh-Hans",
        "count": str(limit),
    }
    timeout = httpx.Timeout(12.0, connect=5.0)
    async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"}) as client:
        response = await client.get(SEARCH_ENDPOINT, params=params)
        response.raise_for_status()

    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError as exc:
        raise RuntimeError("bing-cn returned malformed RSS") from exc

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in root.findall(".//item"):
        title = _plain_text(item.findtext("title") or "")
        url = (item.findtext("link") or "").strip()
        snippet = _plain_text(item.findtext("description") or "")
        if not _valid_result_url(url) or not _matches_site(url, normalized_site) or url in seen:
            continue
        seen.add(url)
        results.append({
            "title": title,
            "url": url,
            "snippet": snippet,
            "source": "bing-cn-rss",
        })
        if len(results) >= limit:
            break

    return {
        "query": query,
        "effectiveQuery": normalized_query,
        "provider": "bing-cn-rss",
        "resultCount": len(results),
        "results": results,
        "verificationRequired": True,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
