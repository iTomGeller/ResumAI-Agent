"""
全局配置：路径、API Key、评测超参数。

所有脚本都从这里读取配置，保证可复现。API Key 仅从环境变量读取，
仓库不提供任何默认凭据。
"""
import os

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.dirname(HARNESS_DIR)                 # reports/rag_comparison
REPO_ROOT = os.path.dirname(os.path.dirname(OUT_DIR))  # 仓库根目录

STRESS_DIR = os.path.join(REPO_ROOT, "testdata", "stress_resumes")
MANIFEST_PATH = os.path.join(STRESS_DIR, "manifest.json")

CACHE_DIR = os.path.join(OUT_DIR, "cache")
FIG_DIR = os.path.join(OUT_DIR, "figures")
for _d in (CACHE_DIR, FIG_DIR):
    os.makedirs(_d, exist_ok=True)

CORPUS_CACHE = os.path.join(CACHE_DIR, "corpus.json")
GRAPH_CACHE = os.path.join(OUT_DIR, "graph_cache.json")
LLM_CACHE = os.path.join(CACHE_DIR, "llm_cache.json")

QUERIES_PATH = os.path.join(OUT_DIR, "queries.json")
GROUND_TRUTH_PATH = os.path.join(OUT_DIR, "ground_truth.json")
METRICS_PATH = os.path.join(OUT_DIR, "metrics.json")
RUNS_PATH = os.path.join(OUT_DIR, "runs.json")  # 每方案每查询的检索明细

# ---------------------------------------------------------------------------
# 环境（离线/代理/HF 缓存）——必须在 import sentence_transformers 之前生效
# ---------------------------------------------------------------------------
# 本机系统代理(127.0.0.1:7999)会触发 Python3.8 的 SSL bug，统一直连。
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")
os.environ.setdefault("HF_HOME", os.path.join(CACHE_DIR, "hf"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# ---------------------------------------------------------------------------
# Embedding 模型（与项目 EMBEDDING_MODEL 对齐）
# ---------------------------------------------------------------------------
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# ---------------------------------------------------------------------------
# LLM（DeepSeek 首选，OpenRouter 备用）
# ---------------------------------------------------------------------------
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# DeepSeek 计价（美元 / 1M token，官方 deepseek-chat 标准价；用于估算成本）
DEEPSEEK_PRICE_IN_PER_M = 0.27   # 输入（cache miss）
DEEPSEEK_PRICE_OUT_PER_M = 1.10  # 输出

# ---------------------------------------------------------------------------
# 评测超参数
# ---------------------------------------------------------------------------
RANK_DEPTH = 20          # 每方案对每查询返回的排序深度
RRF_K = 60               # Reciprocal Rank Fusion 常数
AGENTIC_CANDIDATES = 15  # Agentic 重排时送入 LLM 的候选数
GRAPH_HOP_DECAY = 0.4    # GraphRAG 一跳扩展的权重衰减
DEFAULT_MIN_OVERLAP = 2  # ground truth 默认技能重叠阈值

METRIC_KS = {"precision": 5, "recall": 10, "ndcg": 10}

# 并发（DeepSeek 实体抽取）
LLM_WORKERS = 8
SEED = 42
