"""Generate a high-quality resume dataset for PDF parsing and RAG evaluation.

The PDFs are intentionally text-based, not scanned images, so PDFBox should
extract text deterministically during backend upload tests.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "testdata" / "resumes"


RESUMES = [
    {
        "id": "java_platform_senior",
        "name": "Alex Chen",
        "role": "Senior Java Platform Engineer",
        "expected": ["Java", "Spring Boot", "Kafka", "Kubernetes", "Redis", "MySQL", "payment platform", "observability"],
        "text": """Alex Chen
Senior Java Platform Engineer | 7 years experience | Shanghai
Email: alex.chen@example.com | GitHub: https://github.com/example-dev

Summary
Senior backend engineer focused on payment platform, distributed systems, observability, and production incident response.

Skills
Java 17, Spring Boot 3, Kafka, Kubernetes, Redis, MySQL, Docker, Prometheus, Grafana, OpenTelemetry, SQL optimization.

Experience
2021.05 - Present  FinTech Payment Platform  Senior Backend Engineer
- Led payment platform refactor from monolith to domain services, including order, settlement, reconciliation, and risk-control modules.
- Designed Kafka-based asynchronous transaction pipeline with idempotency keys, retry topics, and dead-letter handling.
- Reduced P99 payment callback latency from 1.8s to 420ms by SQL index redesign, cache warmup, and connection pool tuning.
- Built observability dashboards for payment success rate, settlement lag, consumer backlog, JVM GC, and API latency.

2018.07 - 2021.04  SaaS Logistics Platform  Backend Engineer
- Built shipment tracking APIs with Spring Boot and MySQL, handled 30M tracking events per day.
- Introduced Redis cache and rate limiting to protect partner integrations.

Projects
Payment Reconciliation Engine
- Implemented daily reconciliation between payment gateway, internal ledger, and settlement files.
- Added audit trace id to every payment state transition for compliance investigation.

Education
2014.09 - 2018.06  East China University of Science and Technology  B.S. Software Engineering
""",
    },
    {
        "id": "ai_agent_backend",
        "name": "Mia Wang",
        "role": "AI Agent Backend Engineer",
        "expected": ["LLM", "RAG", "LangGraph", "MCP", "Milvus", "FastAPI", "agent harness", "tool governance"],
        "text": """Mia Wang
AI Agent Backend Engineer | 5 years backend + 2 years LLM application experience
Email: mia.wang@example.com | GitHub: https://github.com/example-dev

Summary
Backend engineer building production AI agent workflows, retrieval pipelines, and runtime observability.

Skills
Python, Java, FastAPI, Spring Boot, LangGraph, LangChain, MCP, Milvus, Redis, PostgreSQL, Docker, Kubernetes, Langfuse.

Experience
2023.03 - Present  AI Recruiting Platform  Agent Platform Engineer
- Built multi-agent resume evaluation workflow with intent router, parser, JD matcher, technical evaluator, risk evaluator, and report generator.
- Designed agent harness with route plan, tool budget, policy guard, trace replay, and evaluation feedback loop.
- Implemented hybrid RAG with BM25-like lexical retrieval, embedding search, reranking, and fallback visibility.
- Integrated MCP resume evidence server and dynamic skill loading for evidence synthesis.

2020.07 - 2023.02  Cloud Observability Company  Backend Engineer
- Developed metrics ingestion APIs, alert routing, and dashboard query optimization.
- Reduced trace query latency by 45% through index redesign and payload compaction.

Projects
Agentic RAG Evidence Pipeline
- Added query planner, retrieval evaluator, reranker, confidence scoring, and Langfuse tracing for every retrieval step.
- Tracked hit count, top score, fallback rate, latency, and answer faithfulness proxy in Grafana.

Education
2015.09 - 2019.06  Zhejiang University  B.S. Computer Science
""",
    },
    {
        "id": "junior_frontend",
        "name": "Leo Zhang",
        "role": "Junior Frontend Engineer",
        "expected": ["Vue", "TypeScript", "CSS", "dashboard", "internship", "risk"],
        "text": """Leo Zhang
Junior Frontend Engineer | 1.5 years experience
Email: leo.zhang@example.com

Summary
Frontend engineer focused on Vue dashboards, component reuse, and user interaction details.

Skills
Vue 3, TypeScript, Vite, Pinia, CSS, ECharts, REST API integration, basic Node.js.

Experience
2024.02 - Present  Data Analytics Startup  Frontend Engineer
- Built dashboard pages for candidate list, report view, trace timeline, and feedback management.
- Improved loading state and pagination interactions, reducing perceived page stutter.

2023.06 - 2023.12  E-commerce Company  Frontend Intern
- Implemented product search filters and order detail pages.
- Wrote unit tests for reusable components.

Projects
Trace Timeline UI
- Designed grouped phase view with expandable agent rounds and raw protocol debug section.

Education
2019.09 - 2023.06  Nanjing University of Posts and Telecommunications  B.S. Software Engineering
""",
    },
    {
        "id": "product_manager_llm",
        "name": "Nora Li",
        "role": "AI Product Manager",
        "expected": ["product", "LLM", "RAG", "metrics", "workflow", "stakeholder"],
        "text": """Nora Li
AI Product Manager | 6 years product experience
Email: nora.li@example.com

Summary
Product manager experienced in B2B workflow products, LLM features, and data-driven iteration.

Skills
PRD, user research, funnel metrics, LLM/RAG basics, dashboard design, stakeholder management, SQL basics.

Experience
2022.01 - Present  HR SaaS Company  Senior Product Manager
- Owned AI resume screening workflow from upload, parsing, evaluation, report review, to HR feedback.
- Defined metrics including time-to-screen, recommendation acceptance, manual override rate, and evidence coverage.
- Coordinated engineering, data, and customer success teams for enterprise rollout.

2018.07 - 2021.12  Collaboration Tools Company  Product Manager
- Built workflow automation and approval modules for mid-market customers.

Projects
LLM Report Quality Loop
- Designed feedback taxonomy and evaluation dashboard for AI-generated hiring reports.

Education
2014.09 - 2018.06  Fudan University  B.A. Management
""",
    },
    {
        "id": "risk_sparse_resume",
        "name": "Sam Short",
        "role": "Backend Engineer",
        "expected": ["sparse", "risk", "missing timeline", "missing project detail"],
        "text": """Sam Short
Backend Engineer
Skills: Java, Spring Boot, Kafka.
Project: payment system refactor.
GitHub: https://github.com/example-dev
""",
    },
]


def expanded_resumes(target_count: int = 300) -> list[dict]:
    extras = []
    templates = [
        ("backend_observability", "Backend Observability Engineer", ["Java", "OpenTelemetry", "Prometheus", "Grafana", "incident"], "Built tracing, metrics, alert routing, JVM dashboards, and incident playbooks."),
        ("data_platform", "Data Platform Engineer", ["Python", "Flink", "Kafka", "Spark", "warehouse"], "Owned streaming ETL, Kafka ingestion, Flink jobs, and data quality monitoring."),
        ("devops_k8s", "DevOps Kubernetes Engineer", ["Kubernetes", "Docker", "Helm", "CI/CD", "SRE"], "Managed Kubernetes clusters, Helm charts, blue-green deployment, and SLO monitoring."),
        ("redis_mysql_backend", "Backend Storage Engineer", ["MySQL", "Redis", "Java", "SQL", "cache"], "Optimized MySQL indexes, Redis cache strategy, distributed locks, and hot-key protection."),
        ("security_backend", "Security Backend Engineer", ["Java", "OAuth", "audit", "risk", "compliance"], "Implemented OAuth2, audit logs, permission models, and payment risk controls."),
        ("qa_automation", "QA Automation Engineer", ["Python", "Selenium", "CI", "test", "quality"], "Built UI/API automation suites, flaky test detection, and release quality dashboards."),
        ("mobile_engineer", "Mobile Engineer", ["Android", "Kotlin", "performance", "crash"], "Optimized Android startup, crash monitoring, and payment SDK integration."),
        ("frontend_senior", "Senior Frontend Engineer", ["Vue", "React", "TypeScript", "performance", "dashboard"], "Built complex dashboards, trace UI, virtual lists, and frontend performance monitoring."),
        ("ml_platform", "ML Platform Engineer", ["Python", "MLflow", "Kubernetes", "model serving"], "Built model serving platform, feature pipelines, and experiment tracking."),
        ("llm_rag_engineer", "LLM RAG Engineer", ["RAG", "Milvus", "rerank", "Langfuse", "evaluation"], "Implemented query rewriting, hybrid retrieval, reranking, RAGAS evaluation, and trace analysis."),
        ("product_growth", "Growth Product Manager", ["product", "metrics", "A/B", "funnel"], "Owned activation funnel, A/B experiments, and growth analytics."),
        ("ops_customer_success", "Customer Success Ops", ["operations", "SLA", "customer", "workflow"], "Managed enterprise onboarding, issue triage, SLA tracking, and workflow automation."),
        ("new_grad_java", "New Graduate Java Engineer", ["Java", "Spring", "internship", "project"], "Completed internship building Spring Boot APIs and campus project management system."),
        ("career_gap_risk", "Backend Engineer With Career Gap", ["Java", "risk", "gap", "timeline"], "Worked in Java backend, had a 10 month career gap, project details are limited."),
        ("fake_project_risk", "Questionable Project Resume", ["risk", "GitHub", "project", "verification"], "Lists many buzzwords but no company, no dates, no measurable project outcomes."),
    ]
    idx = 1
    while len(RESUMES) + len(extras) < target_count:
        rid, role, expected, focus = templates[(idx - 1) % len(templates)]
        batch = (idx - 1) // len(templates) + 1
        years = 2 + idx % 8
        company = ["Example Technology", "Northstar Cloud", "Harbor Data", "Bluefin AI", "Cedar Finance"][idx % 5]
        metric = ["P99 latency 38%", "error rate 27%", "release cycle 40%", "query cost 31%", "on-call MTTR 45%"][idx % 5]
        github_line = f" | GitHub: https://github.com/example-dev" if idx % 3 != 0 else ""
        text = f"""{role} Candidate {idx}
{role} | {years} years experience
Email: candidate{idx}@example.com{github_line}

Summary
{focus} Batch {batch}, domain scenario {idx % 9}.

Skills
{', '.join(expected)}, REST API, documentation, monitoring, cross-team collaboration.

Experience
2021.01 - Present  {company}  {role}
- Delivered production features related to {focus}
- Collaborated with backend, product, QA, and operations teams.
- Improved reliability, observability, and delivery quality with measurable engineering practices.
- Measured outcome: improved {metric} through design review, rollout control, dashboards, and incident retrospectives.

2019.07 - 2020.12  Prior Systems  Engineer
- Maintained legacy modules, wrote migration plans, reviewed pull requests, and handled production defects.
- Worked with customer success and security teams to resolve escalations.

Project
Domain Platform Improvement {idx}
- Designed implementation plan, handled rollout, monitored metrics, and produced post-launch review.
- Evidence includes dashboards, logs, pull requests, design docs, and incident notes.

Side Project
Knowledge Base Evaluation {idx}
- Built retrieval test cases, labeled relevant snippets, compared lexical, embedding, and hybrid search quality.

Education
2016.09 - 2020.06  Example University  B.S. Computer Science
"""
        extras.append({"id": f"{rid}_{idx:03d}", "name": f"Candidate {idx}", "role": role, "expected": expected, "text": text})
        idx += 1
    return RESUMES + extras


def pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_simple_pdf(path: Path, text: str) -> None:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) > 105:
            while len(line) > 105:
                lines.append(line[:105])
                line = line[105:]
        lines.append(line)

    pages = []
    for i in range(0, len(lines), 42):
        pages.append(lines[i:i + 42])
    if not pages:
        pages = [["Empty"]]

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    for idx, page_lines in enumerate(pages):
        page_obj = 3 + idx * 2
        content_obj = page_obj + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> "
            f"/Contents {content_obj} 0 R >>".encode()
        )
        stream_lines = ["BT", "/F1 10 Tf", "50 790 Td", "14 TL"]
        for line in page_lines:
            stream_lines.append(f"({pdf_escape(line)}) Tj")
            stream_lines.append("T*")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("latin-1", errors="replace")
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")

    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{i} 0 obj\n".encode())
        data.extend(obj)
        data.extend(b"\nendobj\n")
    xref_offset = len(data)
    data.extend(f"xref\n0 {len(objects)+1}\n".encode())
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode())
    path.write_bytes(data)


def main() -> None:
    target_count = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    metadata = []
    resumes = expanded_resumes(target_count)
    for item in resumes:
        base = OUT / item["id"]
        txt = base.with_suffix(".txt")
        pdf = base.with_suffix(".pdf")
        txt.write_text(item["text"], encoding="utf-8")
        write_simple_pdf(pdf, item["text"])
        metadata.append({
            "id": item["id"],
            "name": item["name"],
            "role": item["role"],
            "expected": item["expected"],
            "txt": str(txt.relative_to(ROOT)),
            "pdf": str(pdf.relative_to(ROOT)),
            "textLength": len(item["text"]),
        })
    (OUT / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] generated {len(metadata)} resumes under {OUT}")


if __name__ == "__main__":
    main()
