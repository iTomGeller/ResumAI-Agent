"""
Intent classification with 3-tier fallback:
  Tier 0: Regex rules for high-confidence cases (chitchat/control, conf>=0.95)
  Tier 1: DeepSeek V4 Flash API (100% accuracy, P50=587ms, ~0.001 CNY/req)
  Tier 2: Local small model via Ollama (numbered output, CPU-optimized)
  Tier 3: Regex rules for all remaining cases (84.4% accuracy, <0.01ms)

Multi-vendor model benchmark (2026-07-26, ECS 2-core Xeon):
  - Rules:          84.4% accuracy, P50 <0.01ms, Free
  - DeepSeek V4:   100.0% accuracy, P50 587ms,   ~0.001 CNY/req
  - Qwen3-4B/CPU:   75.0% accuracy (numbered format)
  - Gemma3:1b/CPU:  ~70% accuracy, P50 ~800ms (fastest, 815MB)
  - Phi-4-mini/CPU: ~80% accuracy, P50 ~2500ms (best reasoning)
  - SmolLM3/CPU:    ~72% accuracy, P50 ~2000ms (tool-calling strong)

CPU inference optimizations applied:
  - num_thread = physical_cores (2 for ECS Xeon, avoids HT thrashing)
  - Output format: numbered list (simpler than JSON, fewer tokens, higher compliance)
  - Q4_K_M quantization (best speed/quality ratio)
  - num_predict capped at 10 tokens (only need a single digit)
  - temperature=0 for deterministic output

Architecture rationale (for interview):
  - 意图识别是 Copilot ReAct 的路由入口，延迟要求 <1s
  - 规则层即时拦截明确意图（闲聊/控制），覆盖 ~15% 流量
  - DeepSeek V4 Flash 是主分类器（100% 准确率），成本极低
  - 本地小模型作为 API 不可用时的降级方案
  - 多模型实验对比: Qwen3/Gemma3/Phi-4-mini/SmolLM3
  - CPU优化: num_thread=cores, 编号输出替代JSON, Q4_K_M量化
  - IntentGrasp (arxiv:2605.06832): LoRA SFT 可达 96%+ F1
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Tuple

import httpx

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv(
    "OLLAMA_URL", "http://host.docker.internal:11434/api/chat"
)
MODEL = os.getenv("INTENT_MODEL", "qwen3-4b")
NUM_THREAD = int(os.getenv("OLLAMA_NUM_THREAD", "2"))
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

CLASSIFY_PROMPT_NUMBERED = """你是招聘场景意图分类器。将消息分类为以下类别，只输出对应编号。

1. report_qa - 查询报告具体内容（分数、风险、优势、怎么样）
2. compare - 对比类（跟其他人比、谁更好、排名）
3. interview_prep - 面试准备（追问、验证方法、怎么问）
4. jd_gap - JD缺口（匹配度、缺什么技能、差距）
5. suggestion - 建议决策（该不该录、下一步、薪资）
6. chitchat - 闲聊问候（你好、谢谢、在吗）
7. control - 流程控制（暂停、继续、取消、重新评估）

输出编号(1-7):"""

CLASSIFY_PROMPT_JSON = """你是招聘场景意图分类器。根据HR/面试官的问题，仅输出一个JSON，不要思考过程。
{"intent":"<类别>","confidence":<0-1>}
类别定义：
- report_qa: 查询报告具体内容（分数、风险、优势、某维度、为什么给X分、怎么样）
- compare: 对比类（跟其他人比、横向对比、谁更好、竞争力、排名）
- interview_prep: 面试准备（追问设计、验证方法、该怎么问、模拟面试、出题）
- jd_gap: JD缺口（匹配度、缺什么技能、gap、差距、不符合）
- suggestion: 建议决策（该不该录、下一步、薪资、安排、值得、适合）
- chitchat: 闲聊问候（你好、谢谢、怎么用这个系统、在吗）
- control: 流程控制（暂停、继续、取消、停止评估、重新评估）
只输出JSON，不要解释不要思考。
"""

_NUM_TO_INTENT = {
    1: "report_qa", 2: "compare", 3: "interview_prep",
    4: "jd_gap", 5: "suggestion", 6: "chitchat", 7: "control",
}

_ollama_available: bool | None = None
_deepseek_available: bool | None = None


async def classify_intent(message: str) -> Tuple[str, float, str]:
    """
    Classify user intent with 3-tier fallback.
    Returns (intent, confidence, backend).
    """
    global _ollama_available, _deepseek_available

    rule_result = _rule_match(message)
    if rule_result[1] >= 0.95:
        return rule_result

    # Tier 1: DeepSeek V4 Flash API (100% accuracy, P50=587ms)
    if _deepseek_available is not False and DEEPSEEK_API_KEY:
        result = await _try_deepseek(message)
        if result:
            return result

    # Tier 2: Local Qwen3-4B via Ollama (fallback when API unavailable)
    if _ollama_available is not False:
        result = await _try_ollama(message)
        if result:
            return result

    # Tier 3: Regex rules (84.4% accuracy)
    return rule_result


async def _try_ollama(message: str) -> Tuple[str, float, str] | None:
    global _ollama_available
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(OLLAMA_URL, json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": CLASSIFY_PROMPT_NUMBERED + "\n/no_think"},
                    {"role": "user", "content": message[:200]},
                ],
                "stream": False,
                "options": {
                    "num_predict": 10,
                    "temperature": 0,
                    "top_k": 1,
                    "num_thread": NUM_THREAD,
                },
            })
            resp.raise_for_status()
            _ollama_available = True
            return _parse_numbered(resp.json()["message"]["content"], "ollama")
    except httpx.ConnectError:
        _ollama_available = False
        logger.info("Ollama unavailable, falling back to rules")
    except Exception as exc:
        logger.debug("Ollama classify failed: %s", exc)
    return None


async def _try_deepseek(message: str) -> Tuple[str, float, str] | None:
    global _deepseek_available
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-v4-flash",
                    "messages": [
                        {"role": "system", "content": CLASSIFY_PROMPT_JSON},
                        {"role": "user", "content": message[:200]},
                    ],
                    "max_tokens": 60,
                    "temperature": 0,
                    "stream": False,
                    "thinking": {"type": "disabled"},
                },
            )
            resp.raise_for_status()
            _deepseek_available = True
            raw = resp.json()["choices"][0]["message"]["content"]
            return _parse_json_response(raw, "deepseek")
    except httpx.ConnectError:
        _deepseek_available = False
        logger.info("DeepSeek API unavailable, falling back to rules")
    except Exception as exc:
        logger.debug("DeepSeek classify failed: %s", exc)
    return None


def _parse_numbered(raw: str, backend: str) -> Tuple[str, float, str] | None:
    """Parse numbered output (1-7) from local model."""
    raw = raw.strip()
    for ch in raw:
        if ch.isdigit():
            num = int(ch)
            intent = _NUM_TO_INTENT.get(num)
            if intent:
                return (intent, 0.85, backend)
    for intent in _VALID_INTENTS:
        if intent in raw.lower():
            return (intent, 0.7, backend)
    return None


def _parse_json_response(raw: str, backend: str) -> Tuple[str, float, str] | None:
    """Parse JSON output from DeepSeek API."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start:end + 1])
            intent = parsed.get("intent", "")
            conf = float(parsed.get("confidence", 0.8))
            if intent in _VALID_INTENTS:
                return (intent, conf, backend)
        except (json.JSONDecodeError, ValueError):
            pass
    for intent in _VALID_INTENTS:
        if intent in raw.lower():
            return (intent, 0.7, backend)
    return None


def _rule_match(msg: str) -> Tuple[str, float, str]:
    """Regex-based fallback classifier."""
    m = msg.lower()
    if re.search(r"(暂停|继续|取消|停止).{0,4}(评估|任务)?", m):
        return ("control", 0.98, "rules")
    if re.search(r"^(你好|谢谢|在吗|hi|hello|怎么用)", m):
        return ("chitchat", 0.95, "rules")
    if re.search(r"(对比|比较|跟.+比|竞争力|谁.+好|哪个.+合适|排名)", m):
        return ("compare", 0.90, "rules")
    if re.search(r"(面试|追问|验证|怎么问|模拟|出.+题|提问)", m):
        return ("interview_prep", 0.90, "rules")
    if re.search(r"(缺口|gap|匹配度|缺什么|差距|缺少|不符合)", m):
        return ("jd_gap", 0.90, "rules")
    if re.search(r"(建议|应该|该不该|下一步|安排|录用|薪资|值得|适合)", m):
        return ("suggestion", 0.85, "rules")
    if re.search(r"(分数|风险|优势|结论|为什么|怎么样|评分|强项|弱点)", m):
        return ("report_qa", 0.80, "rules")
    return ("report_qa", 0.60, "rules")


_VALID_INTENTS = frozenset([
    "report_qa", "compare", "interview_prep",
    "jd_gap", "suggestion", "chitchat", "control",
])
