"""
LLM 客户端（DeepSeek 首选，OpenRouter 备用）。

特性：
  - 磁盘缓存（按 provider/model/prompt 哈希），避免重复调用、保证可复现
  - 统计：真实 API 调用次数、缓存命中、token 数、估算成本（美元）
  - 线程安全，支持并发实体抽取
  - 失败重试 + 退避
"""
import hashlib
import json
import os
import threading
import time

import requests

import config


def _key(provider, model, system, user, temperature, max_tokens, json_mode):
    h = hashlib.sha256()
    h.update(("|".join([provider, model, system or "", user, str(temperature),
                        str(max_tokens), str(json_mode)])).encode("utf-8"))
    return h.hexdigest()


class LLMClient:
    def __init__(self, provider="deepseek"):
        self.provider = provider
        if provider == "deepseek":
            self.base_url = config.DEEPSEEK_BASE_URL
            self.model = config.DEEPSEEK_MODEL
            self.api_key = config.DEEPSEEK_API_KEY
        elif provider == "openrouter":
            self.base_url = config.OPENROUTER_BASE_URL
            self.model = config.OPENROUTER_MODEL
            self.api_key = config.OPENROUTER_API_KEY
        else:
            raise ValueError("unknown provider: %s" % provider)

        self._lock = threading.Lock()
        self.cache = {}
        if os.path.exists(config.LLM_CACHE):
            try:
                with open(config.LLM_CACHE, "r", encoding="utf-8") as f:
                    self.cache = json.load(f)
            except Exception:
                self.cache = {}

        self.n_calls = 0          # 真实 API 调用（缓存未命中）
        self.n_cache_hits = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.api_seconds = 0.0

    # ------------------------------------------------------------------
    def chat(self, user, system=None, temperature=0.0, max_tokens=800, json_mode=False):
        k = _key(self.provider, self.model, system, user, temperature, max_tokens, json_mode)
        with self._lock:
            if k in self.cache:
                self.n_cache_hits += 1
                return self.cache[k]["content"]

        content = self._call_api(user, system, temperature, max_tokens, json_mode)

        with self._lock:
            self.cache[k] = {"content": content}
        return content

    def _call_api(self, user, system, temperature, max_tokens, json_mode):
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"}
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        body = {"model": self.model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        last_err = None
        for attempt in range(4):
            try:
                t0 = time.time()
                r = requests.post(url, headers=headers, json=body, timeout=90)
                dt = time.time() - t0
                if r.status_code == 200:
                    data = r.json()
                    usage = data.get("usage", {}) or {}
                    with self._lock:
                        self.n_calls += 1
                        self.api_seconds += dt
                        self.prompt_tokens += usage.get("prompt_tokens", 0)
                        self.completion_tokens += usage.get("completion_tokens", 0)
                    return data["choices"][0]["message"]["content"]
                last_err = "HTTP %d: %s" % (r.status_code, r.text[:200])
            except Exception as e:  # noqa
                last_err = "%s: %s" % (type(e).__name__, str(e)[:200])
            time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("LLM call failed after retries: %s" % last_err)

    # ------------------------------------------------------------------
    def save(self):
        with self._lock:
            tmp = config.LLM_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False)
            os.replace(tmp, config.LLM_CACHE)

    def cost_usd(self):
        return (self.prompt_tokens / 1e6 * config.DEEPSEEK_PRICE_IN_PER_M
                + self.completion_tokens / 1e6 * config.DEEPSEEK_PRICE_OUT_PER_M)

    def stats(self):
        return {
            "provider": self.provider,
            "model": self.model,
            "api_calls": self.n_calls,
            "cache_hits": self.n_cache_hits,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "api_seconds": round(self.api_seconds, 1),
            "est_cost_usd": round(self.cost_usd(), 4),
        }
