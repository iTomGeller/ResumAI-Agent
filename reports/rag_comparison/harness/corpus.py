"""
语料加载与分词。

- 读取 manifest.json
- .txt 直接 UTF-8 读取；.pdf 用 PyMuPDF(fitz) 抽取文本
- 中英混合分词：英文/技术词用正则，中文用 jieba
- 结果缓存到 cache/corpus.json，保证可复现
"""
import json
import os
import re

import config

_CJK = re.compile(r"[\u4e00-\u9fff]")
# 英文 / 数字 / 技术词（保留 ci/cd, node.js, c++, oauth2, a/b 等形态）
_EN_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#.\-/]*")

_jieba = None


def _get_jieba():
    global _jieba
    if _jieba is None:
        import jieba
        jieba.initialize()
        _jieba = jieba
    return _jieba


def tokenize(text):
    """中英混合分词，返回 token 列表（小写）。供 BM25 / TF-IDF 共用。"""
    if not text:
        return []
    low = text.lower()
    tokens = _EN_TOKEN.findall(low)
    jb = _get_jieba()
    for tok in jb.cut(low):
        tok = tok.strip()
        if tok and _CJK.search(tok):
            tokens.append(tok)
    return tokens


def extract_text(path):
    """读取单份简历正文。"""
    abspath = path if os.path.isabs(path) else os.path.join(config.REPO_ROOT, path)
    ext = os.path.splitext(abspath)[1].lower()
    if ext == ".txt":
        with open(abspath, "r", encoding="utf-8") as f:
            return f.read()
    if ext == ".pdf":
        import fitz  # PyMuPDF
        doc = fitz.open(abspath)
        try:
            return "\n".join(page.get_text() for page in doc)
        finally:
            doc.close()
    raise ValueError("unsupported file type: %s" % abspath)


def load_manifest():
    with open(config.MANIFEST_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build_corpus(force=False):
    """构建语料并缓存。返回 list[dict]，每条含 id/role/fileType/expectedSkills/text/tokens。"""
    if os.path.exists(config.CORPUS_CACHE) and not force:
        with open(config.CORPUS_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)

    manifest = load_manifest()
    corpus = []
    for entry in manifest:
        text = extract_text(entry["path"])
        text = re.sub(r"[ \t]+", " ", text).strip()
        corpus.append({
            "id": entry["id"],
            "name": entry.get("name", ""),
            "role": entry["role"],
            "fileType": entry["fileType"],
            "hasGithub": entry.get("hasGithub", False),
            "expectedSkills": entry["expectedSkills"],
            "charLen": len(text),
            "text": text,
            "tokens": tokenize(text),
        })

    with open(config.CORPUS_CACHE, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False)
    return corpus


if __name__ == "__main__":
    c = build_corpus(force=True)
    n_pdf = sum(1 for d in c if d["fileType"] == "pdf")
    n_txt = sum(1 for d in c if d["fileType"] == "txt")
    avg_tok = sum(len(d["tokens"]) for d in c) / len(c)
    print("corpus built: %d docs (%d pdf, %d txt)" % (len(c), n_pdf, n_txt))
    print("avg tokens/doc: %.0f" % avg_tok)
    print("sample tokens[senior_backend_003]:",
          [d["tokens"][:25] for d in c if d["id"] == "senior_backend_003"][0])
