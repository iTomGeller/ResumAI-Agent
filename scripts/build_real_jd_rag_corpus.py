#!/usr/bin/env python3
"""Build the JD retrieval corpus from an Apache-2.0 Chinese job dataset.

The JD rows are original public-dataset records.  Only XML removal, whitespace
normalization, deduplication and category mapping are performed.  Retrieval
queries are generated separately and are explicitly marked synthetic.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import html
import io
import json
import re
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "testdata" / "rag_three_stage"
DATASET = "wangzihaogithub/job-educational-parser-dataset-08-0-0805"
LICENSE = "apache-2.0"
DATASET_URL = f"https://huggingface.co/datasets/{DATASET}"
TRAIN_CSV_URL = (
    f"{DATASET_URL}/resolve/main/default/train/19w_0701.csv?download=true"
)

LENGTH_CATEGORY_QUOTAS = {
    "short": {
        "BACKEND": 5, "FRONTEND": 5, "DATA": 5, "ALGORITHM_AI": 5,
        "PRODUCT": 5, "INFRA": 5, "QA_SECURITY": 5, "MOBILE_EMBEDDED": 5,
    },
    "medium": {
        "BACKEND": 5, "FRONTEND": 5, "DATA": 5, "ALGORITHM_AI": 5,
        "PRODUCT": 5, "INFRA": 5, "QA_SECURITY": 5, "MOBILE_EMBEDDED": 5,
    },
    "long": {
        "BACKEND": 8, "FRONTEND": 4, "DATA": 5, "ALGORITHM_AI": 9,
        "PRODUCT": 2, "INFRA": 3, "QA_SECURITY": 3, "MOBILE_EMBEDDED": 6,
    },
}

CATEGORY_PATTERNS = [
    ("MOBILE_EMBEDDED", re.compile(r"嵌入式|Android|iOS|客户端|驱动软件", re.I)),
    ("FRONTEND", re.compile(r"前端|Web前端|小程序|React|Vue", re.I)),
    ("DATA", re.compile(r"数据分析|数据开发|ETL|数仓|大数据|数据工程|数据仓库", re.I)),
    ("ALGORITHM_AI", re.compile(r"算法|大模型|AIGC|人工智能|机器学习|深度学习|NLP|自然语言|视觉|图像", re.I)),
    ("PRODUCT", re.compile(r"产品经理", re.I)),
    ("INFRA", re.compile(r"运维|SRE|云计算|基础架构|DevOps", re.I)),
    ("QA_SECURITY", re.compile(r"测试开发|测试工程|安全工程|网络安全|信息安全", re.I)),
    ("BACKEND", re.compile(r"Java|Python开发|Golang|Go开发|C\+\+开发|后端|后台|服务端|软件开发|软件工程师|系统开发", re.I)),
]

TECH_TERMS = [
    "Java", "Python", "Go", "Golang", "C++", "Spring", "Spring Boot", "MySQL",
    "PostgreSQL", "Redis", "Kafka", "Flink", "Spark", "Hive", "ClickHouse",
    "React", "Vue", "TypeScript", "JavaScript", "Android", "iOS", "Kotlin", "Swift",
    "Docker", "Kubernetes", "K8s", "Linux", "Prometheus", "微服务", "分布式",
    "机器学习", "深度学习", "大模型", "LLM", "RAG", "Agent", "PyTorch", "TensorFlow",
    "数据分析", "数仓", "算法", "产品设计", "A/B测试", "自动化测试", "网络安全",
]


def fetch_page(offset: int, length: int = 100) -> list[dict[str, Any]]:
    url = "https://datasets-server.huggingface.co/rows?" + urllib.parse.urlencode({
        "dataset": DATASET, "config": "default", "split": "train",
        "offset": offset, "length": length,
    })
    last: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))["rows"]
        except Exception as exc:
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"fetch offset={offset} failed: {last}")


def clean_fragment(value: str) -> str:
    value = html.unescape(value or "").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\t\u00a0 ]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def classify_format(description: str) -> str:
    if re.search(r"(?m)^#{1,6}\s+", description):
        return "markdown"
    if "\n" in description and re.search(
            r"(?:岗位职责|任职要求|职位要求|岗位要求)[：:]", description):
        return "plain_labeled_multiline"
    if "\n" in description:
        return "plain_multiline"
    return "plain_compact"


def parse_row(item: dict[str, Any]) -> dict[str, Any] | None:
    raw = str((item.get("row") or {}).get("user") or "")
    title_match = re.search(r"<岗位名称>\s*(.*?)\s*</岗位名称>", raw, re.S)
    descriptions = re.findall(r"<岗位描述>\s*(.*?)\s*</岗位描述>", raw, re.S)
    if not title_match or not descriptions:
        return None
    title = clean_fragment(title_match.group(1))
    description = "\n".join(filter(None, (clean_fragment(value) for value in descriptions)))
    category = next((name for name, pattern in CATEGORY_PATTERNS if pattern.search(title)), None)
    if not category or not (120 <= len(description) <= 3000):
        return None
    format_cohort = classify_format(description)
    level = ("intern" if re.search(r"实习|校招|应届", title)
             else "senior" if re.search(r"高级|资深|专家|负责人|架构", title)
             else "experienced")
    return {
        "sourceRowId": int(item["row_idx"]),
        "sourceRecordId": item.get("source_record_id"),
        "title": title,
        "category": category,
        "description": description,
        "formatCohort": format_cohort,
        "level": level,
    }


def length_cohort(description: str) -> str:
    length = len(description)
    if length <= 600:
        return "short"
    if length <= 1200:
        return "medium"
    return "long"


def deterministic_order(row: dict[str, Any]) -> tuple[int, int]:
    # Prefer the centre of each declared length cohort while keeping selection
    # stable. This avoids silently collapsing the benchmark around 600 chars.
    target = {"short": 420, "medium": 900, "long": 1800}[
        length_cohort(row["description"])
    ]
    length_penalty = abs(len(row["description"]) - target)
    intern_penalty = 500 if row["level"] == "intern" else 0
    digest = int(hashlib.sha256(str(row["sourceRowId"]).encode()).hexdigest()[:8], 16)
    return intern_penalty + length_penalty, digest


def sample_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    seen_description: set[str] = set()
    title_counts: Counter[str] = Counter()
    for row in sorted(rows, key=lambda item: (item["sourceRowId"], deterministic_order(item))):
        if row["formatCohort"] == "markdown":
            continue
        content_hash = hashlib.sha256(row["description"].encode("utf-8")).hexdigest()
        normalized_title = re.sub(r"[（(].*?[）)]|\s+|招聘|急招|诚聘", "", row["title"]).lower()
        if content_hash in seen_description or title_counts[normalized_title] >= 3:
            continue
        seen_description.add(content_hash)
        title_counts[normalized_title] += 1
        cohort = length_cohort(row["description"])
        row["lengthCohort"] = cohort
        buckets[(cohort, row["category"])].append(row)
    selected = []
    for cohort, category_quotas in LENGTH_CATEGORY_QUOTAS.items():
        for category, quota in category_quotas.items():
            bucket = sorted(buckets[(cohort, category)], key=deterministic_order)
            if len(bucket) < quota:
                raise RuntimeError(
                    f"cohort/category {cohort}/{category} has {len(bucket)}, needs {quota}"
                )
            selected.extend(bucket[:quota])
    selected.sort(key=lambda row: (
        list(LENGTH_CATEGORY_QUOTAS).index(row["lengthCohort"]),
        list(LENGTH_CATEGORY_QUOTAS[row["lengthCohort"]]).index(row["category"]),
        deterministic_order(row),
    ))
    return selected


def load_full_train_csv() -> tuple[list[dict[str, Any]], int]:
    parsed: list[dict[str, Any]] = []
    scanned = 0
    with urllib.request.urlopen(TRAIN_CSV_URL, timeout=600) as response:
        text_stream = io.TextIOWrapper(response, encoding="utf-8-sig", newline="")
        for index, row in enumerate(csv.DictReader(text_stream)):
            scanned += 1
            item = {
                "row_idx": index,
                "source_record_id": row.get("job_id"),
                "row": {"user": row.get("user") or ""},
            }
            parsed_row = parse_row(item)
            if parsed_row is not None:
                parsed.append(parsed_row)
    return parsed, scanned


def title_terms(title: str) -> set[str]:
    cleaned = re.sub(r"[（(].*?[）)]|\d+届|招聘|急招|诚聘|实习生?|校招", "", title).lower()
    chars = re.findall(r"[\u3400-\u9fff]", cleaned)
    bigrams = {"".join(chars[i:i + 2]) for i in range(len(chars) - 1)}
    latin = set(re.findall(r"[a-z][a-z0-9+#.-]+", cleaned))
    return bigrams | latin


def extract_techs(text: str) -> list[str]:
    found = [term for term in TECH_TERMS if re.search(re.escape(term), text, re.I)]
    return list(dict.fromkeys(found))[:8]


def resume_query(target: dict[str, Any], decoy: dict[str, Any], position: str,
                 case_type: str) -> str:
    techs = extract_techs(target["title"] + " " + target["description"])
    decoy_techs = extract_techs(decoy["title"] + " " + decoy["description"])
    target_signal = (
        (f"最近目标方向为{target['title']}，" if case_type == "lexical" else "最近两年承担目标方向的核心交付，")
        + (f"实际使用{'、'.join(techs[:5])}。" if techs else "能说明核心技能、项目边界和交付结果。")
        + "项目包含生产发布、监控告警、故障复盘和量化结果，能够解释个人贡献。"
    )
    decoy_signal = (
        f"早期参与{decoy['title']}相关工作，接触{'、'.join(decoy_techs[:4]) if decoy_techs else '常用工程工具'}，"
        "主要是协助交付，不是最近的主责方向。"
    )
    generic = [
        "工作经历：参与需求评审、开发测试、灰度上线和值班复盘，能够区分个人产出、团队产出和平台能力。",
        "项目经历：处理过依赖抖动、请求堆积和数据校验问题，记录基线、机器规格、压测时长、错误率和恢复时间。",
        "工程实践：使用版本控制、代码评审、单元测试、发布清单和监控面板，经历过失败方案和回滚。",
        decoy_signal,
    ]
    filler = [block.replace("经历", f"经历{cycle}") for cycle in range(1, 12) for block in generic]
    if position == "early":
        ordered = [target_signal, *filler]
    elif position == "middle":
        midpoint = len(filler) // 2
        ordered = [*filler[:midpoint], target_signal, *filler[midpoint:]]
    else:
        ordered = [*filler, target_signal]
    return "\n\n".join(ordered)


def build_queries(jds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queries = []
    for index, target in enumerate(jds):
        target_techs = extract_techs(target["title"] + " " + target["description"])
        evidence_techs = extract_techs(target["description"])
        target_terms = title_terms(target["title"])
        candidates = [row for row in jds if row["jdId"] != target["jdId"]]
        candidates.sort(key=lambda row: (
            row["category"] != target["category"],
            -len(target_terms & title_terms(row["title"])), row["jdId"]))
        decoy = candidates[0]
        similar = [row for row in candidates
                   if row["category"] == target["category"]
                   and target_terms & title_terms(row["title"])][:3]
        relevance = {target["jdId"]: 3}
        for row in similar:
            relevance[row["jdId"]] = 1
        case_type = ("lexical", "semantic_paraphrase", "hard_negative")[index % 3]
        position = ("early", "middle", "late")[index % 3]
        queries.append({
            "caseId": f"jdq-real-{index + 1:03d}",
            "stage": "jd_recall",
            "caseType": case_type,
            "querySource": "coordinator_auto_resume_text",
            "query": resume_query(target, decoy, position, case_type),
            "goldId": target["jdId"],
            "family": target["category"],
            "level": target["level"],
            "formatCohort": target["formatCohort"],
            "signalPosition": position,
            "lengthCohort": target["lengthCohort"],
            "benchmarkSplit": "heldout" if index % 3 == 2 else "calibration",
            "relevance": relevance,
            "hardNegativeIds": [row["jdId"] for row in similar],
            "labelProvenance": "exact source JD; adjacent labels use same-category title-bigram overlap",
            "goldEvidenceTerms": evidence_techs[:5],
        })
    return queries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--reuse-existing", action="store_true",
                        help="reclassify/rebuild queries from the frozen real JD file without network")
    parser.add_argument("--full-train", action="store_true",
                        help="stream the full public train CSV and build the length-stratified corpus")
    args = parser.parse_args()
    if args.reuse_existing:
        path = args.out / "jd_catalog.json"
        jds = json.loads(path.read_text(encoding="utf-8"))
        for row in jds:
            row["formatCohort"] = classify_format(row["description"])
        queries = build_queries(jds)
        path.write_text(json.dumps(jds, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.out / "jd_queries.json").write_text(
            json.dumps(queries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"selected": len(jds),
                          "formatCounts": Counter(row["formatCohort"] for row in jds)},
                         ensure_ascii=False, indent=2, default=dict))
        return 0
    if args.full_train:
        parsed, rows_scanned = load_full_train_csv()
        offsets: list[int] = []
    else:
        offsets = list(range(0, 198000, 4000))
        raw_rows = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for page in pool.map(fetch_page, offsets):
                raw_rows.extend(page)
        rows_scanned = len(raw_rows)
        parsed = [row for item in raw_rows if (row := parse_row(item)) is not None]
    selected = sample_rows(parsed)
    jds = []
    for index, row in enumerate(selected, start=1):
        jds.append({
            "jdId": f"exp-real-jd-{index:03d}",
            "title": row["title"],
            "category": row["category"],
            "description": row["description"],
            "level": row["level"],
            "formatCohort": row["formatCohort"],
            "lengthCohort": row["lengthCohort"],
            "source": {
                "dataset": DATASET,
                "split": "train",
                "rowId": row["sourceRowId"],
                "recordId": row.get("sourceRecordId"),
                "license": LICENSE,
                "datasetUrl": DATASET_URL,
                "transformations": ["remove XML wrappers", "normalize whitespace", "deduplicate", "map category"],
            },
        })
    queries = build_queries(jds)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "jd_catalog.json").write_text(
        json.dumps(jds, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out / "jd_queries.json").write_text(
        json.dumps(queries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt = {
        "dataset": DATASET, "license": LICENSE, "datasetUrl": DATASET_URL,
        "pages": len(offsets), "rowsScanned": rows_scanned, "eligibleRows": len(parsed),
        "selected": len(jds), "categoryCounts": Counter(row["category"] for row in jds),
        "lengthCohortCounts": Counter(row["lengthCohort"] for row in jds),
        "formatCounts": Counter(row["formatCohort"] for row in jds),
        "levelCounts": Counter(row["level"] for row in jds),
        "descriptionChars": {
            "min": min(len(row["description"]) for row in jds),
            "mean": round(sum(len(row["description"]) for row in jds) / len(jds), 2),
            "max": max(len(row["description"]) for row in jds),
        },
    }
    (args.out / "jd_source_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, default=dict) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2, default=dict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
