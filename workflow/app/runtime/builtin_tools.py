"""Deterministic resume-analysis builtin tools for candidate evaluation.

These are pure in-process functions (stdlib + optional pypdf) with no network
IO. They are the production path for candidate evaluation — NOT a sandbox.
Policy Lab / benchmark / replay may import the same kernels via
``app.policy_lab.tool_kernels`` when Docker isolation is required.
"""
from __future__ import annotations

import base64
import io
import json
import re
from typing import Any, Dict, List, Optional, Tuple

MONTHS_PER_YEAR = 12

RANGE_PATTERN = re.compile(
    r"(20\d{2}|19\d{2})\s*[./年]\s*(\d{1,2})\s*月?\s*[-–—~至到]+\s*"
    r"((20\d{2}|19\d{2})\s*[./年]\s*(\d{1,2})\s*月?|至今|现在|now|present|Present)")

VAGUE_WORDS = [
    "熟悉", "了解", "参与", "协助", "负责", "深入理解", "精通各种", "等等",
    "良好的沟通", "团队合作", "吃苦耐劳", "学习能力强",
]

METRIC_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(%|倍|ms|毫秒|qps|QPS|万|k|K|s|秒|台|人|次)")


def _lines(text: str) -> List[str]:
    return (text or "").splitlines()


def _to_month_index(year: str, month: str) -> int:
    return int(year) * MONTHS_PER_YEAR + (int(month) - 1)


def parse_resume(args: Dict[str, Any]) -> Dict[str, Any]:
    text = args.get("resumeText") or ""
    filename = str(args.get("filename") or "resume.txt")
    pages = None
    if not text and args.get("resumeBase64"):
        raw = base64.b64decode(args["resumeBase64"])
        if filename.lower().endswith(".pdf"):
            try:
                from pypdf import PdfReader

                reader = PdfReader(io.BytesIO(raw))
                pages = len(reader.pages)
                text = "\n".join((page.extract_text() or "") for page in reader.pages)
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "error": f"pdf_parse_failed: {exc}", "pages": pages}
        else:
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "error": f"decode_failed: {exc}"}
    text = (text or "").replace("\x00", " ").strip()
    if not text:
        return {"success": False, "error": "empty_text", "pages": pages}
    if len(text) > 400_000:
        return {"success": False, "error": "text_too_large", "chars": len(text)}

    lines = _lines(text)
    section_titles = {
        "skills": ["技能", "技术栈", "专业技能", "skills"],
        "projects": ["项目", "项目经历", "项目经验", "projects"],
        "experience": ["工作经历", "实习", "工作经验", "experience"],
        "education": ["教育", "学历", "education"],
    }
    sections: Dict[str, List[str]] = {key: [] for key in section_titles}
    current: Optional[str] = None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        matched = None
        for key, titles in section_titles.items():
            if any(t.lower() in stripped.lower() for t in titles) and len(stripped) <= 24:
                matched = key
                break
        if matched:
            current = matched
            continue
        if current:
            sections[current].append(stripped)

    known_skills = [
        "java", "spring", "spring boot", "spring cloud", "mysql", "redis", "kafka",
        "rabbitmq", "rocketmq", "docker", "kubernetes", "python", "go", "mybatis",
        "elasticsearch", "neo4j", "milvus", "langchain", "langgraph", "llm", "agent",
        "rag", "vue", "react", "netty", "grpc", "postgresql", "mongodb", "linux",
        "jvm", "并发", "分布式", "微服务", "prometheus", "grafana",
    ]
    lower = text.lower()
    skills = sorted({s for s in known_skills if s in lower})

    project_names = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^(项目|Project)[:：\s]", stripped) or (
                "项目" in stripped and len(stripped) <= 40 and not stripped.startswith("-")):
            project_names.append(stripped[:80])
    contact = {
        "emails": re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)[:3],
        "githubHandles": re.findall(r"github\.com/([A-Za-z0-9_-]+)", text)[:3],
    }
    timeline = extract_timeline(text)
    return {
        "success": True,
        "chars": len(text),
        "pages": pages,
        "skills": skills,
        "projectNames": project_names[:12],
        "sections": {k: v[:40] for k, v in sections.items()},
        "contact": contact,
        "timelinePeriods": timeline,
        "confidence": 0.9 if (skills or project_names) else 0.4,
    }


def extract_timeline(text: str) -> List[Dict[str, Any]]:
    periods: List[Dict[str, Any]] = []
    for lineno, line in enumerate(_lines(text), start=1):
        for match in RANGE_PATTERN.finditer(line):
            start = _to_month_index(match.group(1), match.group(2))
            if match.group(4):
                end = _to_month_index(match.group(4), match.group(5))
                open_ended = False
            else:
                end = None
                open_ended = True
            periods.append({
                "raw": match.group(0),
                "line": lineno,
                "context": line.strip()[:120],
                "startMonth": start,
                "endMonth": end,
                "openEnded": open_ended,
            })
    return periods


def check_timeline(args: Dict[str, Any]) -> Dict[str, Any]:
    text = args.get("resumeText") or ""
    now_month = int(args.get("nowYear", 2026)) * MONTHS_PER_YEAR + int(args.get("nowMonth", 7)) - 1
    periods = extract_timeline(text)
    issues: List[Dict[str, Any]] = []
    normalized: List[Tuple[int, int, Dict[str, Any]]] = []
    for period in periods:
        end = period["endMonth"] if period["endMonth"] is not None else now_month
        if end < period["startMonth"]:
            issues.append({"type": "inverted_range", "detail": period["raw"],
                           "line": period["line"], "severity": "high"})
            continue
        if period["startMonth"] > now_month:
            issues.append({"type": "future_start", "detail": period["raw"],
                           "line": period["line"], "severity": "high"})
        if period["endMonth"] is not None and period["endMonth"] > now_month + 6:
            issues.append({"type": "future_end", "detail": period["raw"],
                           "line": period["line"], "severity": "medium"})
        normalized.append((period["startMonth"], end, period))

    normalized.sort(key=lambda item: item[0])
    overlaps: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []
    for i in range(1, len(normalized)):
        prev_start, prev_end, prev = normalized[i - 1]
        cur_start, cur_end, cur = normalized[i]
        if cur_start < prev_end:
            months = prev_end - cur_start
            overlaps.append({
                "months": months,
                "a": prev["context"], "b": cur["context"],
                "aLine": prev["line"], "bLine": cur["line"],
                "severity": "high" if months >= 6 else "low",
            })
        elif cur_start - prev_end >= 6:
            gaps.append({
                "months": cur_start - prev_end,
                "after": prev["context"], "before": cur["context"],
            })
    for overlap in overlaps:
        issues.append({"type": "overlap", "detail": f"{overlap['a']} <-> {overlap['b']}",
                       "months": overlap["months"], "severity": overlap["severity"]})
    for gap in gaps:
        issues.append({"type": "gap", "detail": f"{gap['after']} -> {gap['before']}",
                       "months": gap["months"], "severity": "info"})
    return {
        "success": True,
        "periodCount": len(periods),
        "periods": periods,
        "overlaps": overlaps,
        "gaps": gaps,
        "issues": issues,
        "hasHighRisk": any(i["severity"] == "high" for i in issues),
    }


def _extract_requirements(jd_text: str) -> List[str]:
    requirements: List[str] = []
    for line in _lines(jd_text):
        stripped = line.strip().lstrip("·•-*0123456789.、) （(").strip()
        if len(stripped) >= 6 and any(
                k in stripped for k in ["熟悉", "掌握", "经验", "了解", "精通", "能力",
                                        "优先", "要求", "负责", "familiar", "experience"]):
            requirements.append(stripped[:120])
    if not requirements:
        requirements = [l.strip()[:120] for l in _lines(jd_text) if len(l.strip()) >= 10][:10]
    return requirements[:20]


_TERM_SPLIT = re.compile(r"[\s,，。;；、/|()（）:：]+")


def _key_terms(requirement: str) -> List[str]:
    terms = []
    for token in _TERM_SPLIT.split(requirement.lower()):
        token = token.strip()
        if len(token) >= 2 and token not in {"熟悉", "掌握", "经验", "了解", "优先", "要求"}:
            terms.append(token)
    ascii_terms = re.findall(r"[a-zA-Z][a-zA-Z0-9.+#]{1,20}", requirement.lower())
    return list(dict.fromkeys(ascii_terms + terms))[:8]


def calculate_jd_coverage(args: Dict[str, Any]) -> Dict[str, Any]:
    resume = (args.get("resumeText") or "").lower()
    jd_text = args.get("jdText") or ""
    provided = args.get("requirements")
    requirements = [str(r) for r in provided] if provided else _extract_requirements(jd_text)
    if not requirements:
        return {"success": False, "error": "no_requirements_extracted"}
    per_requirement = []
    covered = 0
    for requirement in requirements:
        terms = _key_terms(requirement)
        hits = [t for t in terms if t and t in resume]
        ratio = len(hits) / max(1, len(terms))
        is_covered = ratio >= 0.34 and bool(hits)
        if is_covered:
            covered += 1
        per_requirement.append({
            "requirement": requirement,
            "covered": is_covered,
            "matchedTerms": hits[:6],
            "matchRatio": round(ratio, 3),
        })
    return {
        "success": True,
        "requirementCount": len(requirements),
        "coveredCount": covered,
        "coverage": round(covered / len(requirements), 3),
        "perRequirement": per_requirement,
        "missing": [p["requirement"] for p in per_requirement if not p["covered"]][:10],
    }


def locate_evidence(args: Dict[str, Any]) -> Dict[str, Any]:
    resume = args.get("resumeText") or ""
    claims = [str(c) for c in (args.get("claims") or [])][:30]
    if not claims:
        return {"success": False, "error": "no_claims"}
    lines = _lines(resume)
    results = []
    for claim in claims:
        terms = _key_terms(claim)
        best_line, best_score = None, 0.0
        for lineno, line in enumerate(lines, start=1):
            lower = line.lower()
            if not lower.strip():
                continue
            hits = sum(1 for t in terms if t in lower)
            score = hits / max(1, len(terms))
            if score > best_score:
                best_score = score
                best_line = (lineno, line.strip()[:160])
        found = best_score >= 0.4 and best_line is not None
        results.append({
            "claim": claim[:200],
            "found": found,
            "line": best_line[0] if best_line else None,
            "snippet": best_line[1] if best_line else None,
            "matchScore": round(best_score, 3),
        })
    found_count = sum(1 for r in results if r["found"])
    return {
        "success": True,
        "claims": results,
        "foundCount": found_count,
        "supportRatio": round(found_count / len(results), 3),
    }


def verify_report_evidence(args: Dict[str, Any]) -> Dict[str, Any]:
    resume = args.get("resumeText") or ""
    jd_text = args.get("jdText") or ""
    claims = args.get("claims") or []
    external_evidence = args.get("externalEvidence") or []
    successful_sources: List[str] = []
    for item in external_evidence:
        if not isinstance(item, dict):
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        status = str(item.get("status") or "").upper()
        success = result.get("success") is True or status == "SUCCEEDED"
        if not success:
            continue
        for source_url in item.get("sourceUrls") or []:
            url = str(source_url or "").strip()
            if url and url not in successful_sources:
                successful_sources.append(url)
    normalized: List[Dict[str, str]] = []
    for claim in claims[:40]:
        if isinstance(claim, dict):
            normalized.append({"text": str(claim.get("text") or claim.get("claim") or ""),
                               "evidence": str(claim.get("evidence") or "")})
        else:
            normalized.append({"text": str(claim), "evidence": ""})
    normalized = [c for c in normalized if c["text"].strip()]
    if not normalized:
        return {"success": False, "error": "no_claims"}
    corpus = (resume + "\n" + jd_text).lower()
    lines = _lines(resume)
    supported, unsupported = [], []
    for claim in normalized:
        claim_text = claim["text"]
        claim_lower = claim_text.lower()
        matched_sources = [
            url for url in successful_sources
            if url.lower() in claim_lower
            or any(host in claim_lower and host in url.lower()
                   for host in ("csdn", "gitee", "github", "gitcode",
                                "cnblogs", "juejin", "zhihu"))
        ]
        fetch_failure_markers = (
            "无法抓取", "抓取失败", "无法访问", "链接失效", "需登录",
            "未成功抓取", "fetch failed", "unavailable",
        )
        fetch_success_markers = (
            "成功抓取", "已成功抓取", "页面内容已取回", "内容已取回",
            "fetch succeeded",
        )
        if matched_sources and any(marker in claim_lower
                                   for marker in fetch_failure_markers):
            unsupported.append({
                "claim": claim_text[:200],
                "matchRatio": 0.0,
                "location": {"sourceUrl": matched_sources[0]},
                "reason": "contradicted_by_successful_external_fetch",
            })
            continue
        if matched_sources and any(marker in claim_lower
                                   for marker in fetch_success_markers):
            supported.append({
                "claim": claim_text[:200],
                "matchRatio": 1.0,
                "location": {"sourceUrl": matched_sources[0]},
            })
            continue
        basis = claim["evidence"] or claim["text"]
        terms = _key_terms(basis)
        hits = [t for t in terms if t in corpus]
        ratio = len(hits) / max(1, len(terms))
        located = None
        for lineno, line in enumerate(lines, start=1):
            lower = line.lower()
            if hits and sum(1 for t in hits if t in lower) >= max(1, len(hits) // 2):
                located = {"line": lineno, "snippet": line.strip()[:160]}
                break
        entry = {"claim": claim["text"][:200], "matchRatio": round(ratio, 3),
                 "location": located}
        # 数字型断言必须在原文找到同样的数字，防止编造指标
        numbers = re.findall(r"\d+(?:\.\d+)?", claim["text"])
        fabricated_number = any(
            len(n) >= 2 and n not in resume for n in numbers)
        if ratio >= 0.4 and located and not fabricated_number:
            supported.append(entry)
        else:
            if fabricated_number:
                entry["reason"] = "numeric_claim_not_in_source"
            elif not located:
                entry["reason"] = "no_source_line"
            else:
                entry["reason"] = "weak_term_overlap"
            unsupported.append(entry)
    total = len(supported) + len(unsupported)
    return {
        "success": True,
        "supported": supported,
        "unsupported": unsupported,
        "supportRatio": round(len(supported) / total, 3) if total else 0.0,
        "unsupportedCount": len(unsupported),
    }


def resume_lint(args: Dict[str, Any]) -> Dict[str, Any]:
    text = args.get("resumeText") or args.get("rewrittenText") or ""
    if not text.strip():
        return {"success": False, "error": "empty_text"}
    issues = []
    for lineno, line in enumerate(_lines(text), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        for vague in VAGUE_WORDS:
            if vague in stripped and not METRIC_PATTERN.search(stripped):
                issues.append({"line": lineno, "type": "vague_wording",
                               "term": vague, "snippet": stripped[:100]})
                break
        if len(stripped) > 120 and (stripped.startswith("-") or stripped.startswith("·")):
            issues.append({"line": lineno, "type": "bullet_too_long",
                           "snippet": stripped[:100]})
    bullet_lines = [l for l in _lines(text)
                    if l.strip().startswith(("-", "·", "•"))]
    metric_bullets = [l for l in bullet_lines if METRIC_PATTERN.search(l)]
    metric_ratio = len(metric_bullets) / max(1, len(bullet_lines))
    if bullet_lines and metric_ratio < 0.3:
        issues.append({"type": "few_quantified_results",
                       "detail": f"量化描述比例 {metric_ratio:.0%}，建议补充数字指标"})
    score = max(0.0, 1.0 - 0.08 * len(issues))
    return {
        "success": True,
        "issues": issues[:30],
        "issueCount": len(issues),
        "quantifiedBulletRatio": round(metric_ratio, 3),
        "lintScore": round(score, 3),
    }


REPORT_SCHEMA_REQUIRED = ["overallScore", "recommendation", "strengths", "risks", "summary"]


def validate_report_schema(args: Dict[str, Any]) -> Dict[str, Any]:
    report = args.get("report")
    if isinstance(report, str):
        try:
            report = json.loads(report)
        except json.JSONDecodeError:
            return {"success": True, "valid": False,
                    "errors": ["report is not valid JSON"]}
    if not isinstance(report, dict):
        return {"success": True, "valid": False, "errors": ["report must be an object"]}
    errors = []
    for field in args.get("requiredFields") or REPORT_SCHEMA_REQUIRED:
        if field not in report or report.get(field) in (None, "", []):
            errors.append(f"missing field: {field}")
    score = report.get("overallScore")
    if score is not None:
        try:
            value = float(score)
            if not 0 <= value <= 100:
                errors.append("overallScore out of range 0-100")
        except (TypeError, ValueError):
            errors.append("overallScore not numeric")
    recommendation = str(report.get("recommendation") or "")
    if recommendation and recommendation not in (
            "STRONG_HIRE", "HIRE", "CONSIDER", "HOLD", "NO_HIRE", "REJECT"):
        errors.append(f"unknown recommendation: {recommendation}")
    return {"success": True, "valid": not errors, "errors": errors}


def evaluate_policy_output(args: Dict[str, Any]) -> Dict[str, Any]:
    """Benchmark evaluator: does the answer find what it must find, avoid
    what it must not claim, and stay grounded in the resume?"""
    answer = str(args.get("answer") or "")
    resume = str(args.get("resumeText") or "")
    must_find = [str(m) for m in (args.get("mustFind") or [])]
    must_not_claim = [str(m) for m in (args.get("mustNotClaim") or [])]
    lower_answer = answer.lower()
    found, missing = [], []
    for item in must_find:
        terms = _key_terms(item)
        hit_ratio = sum(1 for t in terms if t in lower_answer) / max(1, len(terms))
        (found if hit_ratio >= 0.5 else missing).append(
            {"item": item, "hitRatio": round(hit_ratio, 3)})
    violations = []
    for item in must_not_claim:
        terms = _key_terms(item)
        hit_ratio = sum(1 for t in terms if t in lower_answer) / max(1, len(terms))
        if hit_ratio >= 0.75:
            violations.append({"item": item, "hitRatio": round(hit_ratio, 3)})
    numbers = re.findall(r"\d{2,}(?:\.\d+)?", answer)
    fabricated = [n for n in numbers if n not in resume and n not in ("100", "2026", "2025")]
    must_find_score = len(found) / max(1, len(must_find)) if must_find else 1.0
    violation_penalty = len(violations) / max(1, len(must_not_claim)) if must_not_claim else 0.0
    return {
        "success": True,
        "found": found,
        "missing": missing,
        "violations": violations,
        "fabricatedNumbers": fabricated[:10],
        "mustFindScore": round(must_find_score, 3),
        "violationPenalty": round(violation_penalty, 3),
        "score": round(max(0.0, must_find_score - violation_penalty
                           - 0.05 * min(len(fabricated), 4)), 3),
    }


def web_search_cn(args: Dict[str, Any]) -> Dict[str, Any]:
    """Search via Bing China (accessible within mainland China, no API key)."""
    import urllib.parse
    query = str(args.get("query") or "").strip()
    if not query:
        return {"success": False, "error": "query is required"}
    max_results = min(int(args.get("maxResults") or 5), 8)
    try:
        import httpx
        url = f"https://cn.bing.com/search?q={urllib.parse.quote(query)}&count={max_results}"
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })
        if resp.status_code >= 400:
            return {"success": False, "error": f"HTTP {resp.status_code}"}
        results = []
        import re as _re
        for match in _re.finditer(
                r'<li class="b_algo"[^>]*>.*?<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>'
                r'.*?<p[^>]*>(.*?)</p>', resp.text, _re.DOTALL):
            title = _re.sub(r'<[^>]+>', '', match.group(2)).strip()
            snippet = _re.sub(r'<[^>]+>', '', match.group(3)).strip()
            results.append({"title": title, "url": match.group(1), "snippet": snippet[:200]})
            if len(results) >= max_results:
                break
        if not results:
            for match in _re.finditer(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                                      resp.text):
                text = _re.sub(r'<[^>]+>', '', match.group(2)).strip()
                if text and len(text) > 10 and "bing.com" not in match.group(1):
                    results.append({"title": text[:100], "url": match.group(1), "snippet": ""})
                    if len(results) >= max_results:
                        break
        return {"success": True, "results": results, "resultCount": len(results),
                "query": query, "source": "bing_cn"}
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def fetch_url_cn(args: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch a URL directly via httpx (works for GitHub, domestic sites)."""
    url = str(args.get("url") or "").strip()
    if not url:
        return {"success": False, "error": "url is required"}
    max_length = min(int(args.get("maxLength") or 6000), 12000)
    allowed_prefixes = (
        "https://github.com/", "https://api.github.com/",
        "https://gitee.com/", "https://blog.csdn.net/",
        "https://juejin.cn/", "https://www.zhihu.com/",
        "https://segmentfault.com/", "https://leetcode.cn/",
    )
    if not any(url.startswith(p) for p in allowed_prefixes):
        return {"success": False, "error": f"URL not in allowed list for direct fetch: {url[:80]}",
                "allowedPrefixes": list(allowed_prefixes)}
    try:
        import httpx
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            resp = client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; ResumAI-Bot/1.0)",
                "Accept": "text/html,application/json,text/plain",
            })
        if resp.status_code >= 400:
            return {"success": False, "error": f"HTTP {resp.status_code}", "url": url}
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            text = resp.text[:max_length]
        else:
            import re as _re
            text = _re.sub(r'<script[^>]*>.*?</script>', '', resp.text, flags=_re.DOTALL)
            text = _re.sub(r'<style[^>]*>.*?</style>', '', text, flags=_re.DOTALL)
            text = _re.sub(r'<[^>]+>', ' ', text)
            text = _re.sub(r'\s+', ' ', text).strip()[:max_length]
        return {"success": True, "text": text, "url": url,
                "contentLength": len(text), "statusCode": resp.status_code}
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}", "url": url}


TOOL_IMPLS = {
    "parse_resume": parse_resume,
    "check_timeline": check_timeline,
    "calculate_jd_coverage": calculate_jd_coverage,
    "locate_evidence": locate_evidence,
    "verify_report_evidence": verify_report_evidence,
    "resume_lint": resume_lint,
    "validate_report_schema": validate_report_schema,
    "evaluate_policy_output": evaluate_policy_output,
}


def run_tool(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    impl = TOOL_IMPLS.get(tool)
    if impl is None:
        return {"success": False, "error": f"unknown tool: {tool}"}
    try:
        return impl(args or {})
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


BUILTIN_TOOLS = set(TOOL_IMPLS.keys())


class BuiltinToolRegistry:
    """In-process registry for production candidate-evaluation tools.

    Candidate runtime must never depend on SandboxClient / LocalSandboxFallback.
    """

    def __init__(self) -> None:
        self._impl = TOOL_IMPLS

    async def invoke(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool not in self._impl:
            raise ValueError(f"tool not in builtin allowlist: {tool}")
        return run_tool(tool, args)

    def known(self, tool: str) -> bool:
        return tool in self._impl
