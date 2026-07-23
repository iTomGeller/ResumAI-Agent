"""Post-deployment verification: trigger a new evaluation and check results."""
import requests
import time
import json

BASE = "http://8.138.10.189"

resume_text = """张三 | 高级Java工程师 | 8年经验
联系: zhangsan@example.com | 13800138000

技术栈:
- Java (8/11/17), Spring Boot, Spring Cloud, MyBatis Plus
- MySQL (分库分表), Redis (集群), RocketMQ, Kafka
- Kubernetes, Docker, Prometheus, Grafana, ELK
- 分布式事务(TCC/SAGA), 限流熔断(Sentinel), 链路追踪(SkyWalking)

工作经历:
2019.03 - 至今 | 阿里巴巴 | P7 高级Java工程师 | 交易中台团队
项目1: 交易中台重构
- 负责核心交易链路重构，TPS从2万提升到8万(4倍提升)
- 设计分布式事务TCC方案，覆盖50+微服务，事务成功率99.99%
- 引入Flink实时风控，延迟从200ms降至50ms
- 主导全链路压测体系建设，发现并修复30+性能瓶颈
项目2: 实时数据管道
- Flink实时管道处理10亿条/日，端到端延迟<1s
- 设计数据质量监控，异常发现时间从小时级降至分钟级
- 推动团队从Storm迁移到Flink，处理能力提升5倍

2016.07 - 2019.02 | 美团 | 高级工程师 | 外卖订单团队
项目: 订单履约系统
- 日订单3000万，系统SLA 99.99%
- 设计订单状态机，支持15种状态流转，异常自动补偿
- K8s容器化200+应用，资源利用率提升40%
- 主导MySQL分库分表(256库)，单表QPS从5000提升到5万

教育:
2012 - 2016 | 浙江大学 | 计算机科学与技术 | 本科
- GPA 3.8/4.0, ACM银牌

开源:
- GitHub: https://github.com/zhangsan-example (500+ stars)
"""

jd = """高级Java工程师 (P7)
要求:
- 5年以上Java开发经验
- 精通Spring Boot/Cloud微服务架构
- 熟悉分布式系统设计(分布式事务、消息队列、缓存)
- 有大规模高并发系统设计经验(日活千万级)
- 熟悉Kubernetes容器编排
- 有全链路压测、性能优化经验优先
- 大厂经验优先
"""

print("=" * 60)
print("POST-DEPLOYMENT VERIFICATION")
print("=" * 60)

# 1. Upload
print("\n[1] Uploading resume...")
files = {"file": ("verify_deploy.txt", resume_text.encode("utf-8"), "text/plain")}
data = {"jobDescription": jd, "jobCategory": "BACKEND", "executionMode": "DAG_CONCURRENT"}
try:
    r = requests.post(f"{BASE}/api/tasks/upload", files=files, data=data, timeout=30)
    print(f"    Status: {r.status_code}")
    if r.status_code != 200:
        print(f"    Body: {r.text[:500]}")
        exit(1)
    task = r.json()
    trace_id = task.get("traceId", "")
    print(f"    TraceId: {trace_id}")
except Exception as e:
    print(f"    ERROR: {e}")
    exit(1)

# 2. Poll for completion
print(f"\n[2] Polling for completion (max 150s)...")
final_data = None
for i in range(30):
    time.sleep(5)
    elapsed = (i + 1) * 5
    try:
        resp = requests.get(f"{BASE}/api/tasks/{trace_id}", timeout=10)
        if resp.status_code == 200:
            d = resp.json()
            status = d.get("status", "")
            score = d.get("overallScore")
            rec = d.get("recommendation", "")
            print(f"    [{elapsed:3d}s] status={status} score={score} rec={rec}")
            if status in ("SUCCESS", "PARTIAL_SUCCESS", "FAILED", "SYSTEM_FAILED"):
                final_data = d
                break
    except Exception as e:
        print(f"    [{elapsed:3d}s] poll error: {e}")

if not final_data:
    print("    TIMEOUT: evaluation did not complete in 150s")
    exit(1)

# 3. Analyze results
print(f"\n[3] Results Analysis")
print(f"    Status: {final_data.get('status')}")
print(f"    Score: {final_data.get('overallScore')}")
print(f"    Recommendation: {final_data.get('recommendation')}")
print(f"    DataQuality: {final_data.get('dataQuality')}")
duration_ms = final_data.get("durationMs", 0)
print(f"    Duration: {duration_ms/1000:.1f}s")

sr = final_data.get("structuredReport") or {}
dims = sr.get("dimensions") or []
strengths = sr.get("strengths") or []
risks = sr.get("risks") or []
probes = sr.get("interviewProbes") or sr.get("interviewQuestions") or []
must_have = sr.get("mustHaveCoverage") or []
missing = sr.get("missingEvidence") or []
summary = sr.get("summary", "")

print(f"\n    Dimensions: {len(dims)}")
for d in dims:
    print(f"      - {d.get('id','?')}: score={d.get('score')} status={d.get('status')}")
print(f"    Strengths: {len(strengths)}")
print(f"    Risks: {len(risks)}")
for r in risks:
    if isinstance(r, dict):
        print(f"      - [{r.get('severity','?')}] {r.get('risk','')[:60]}")
print(f"    InterviewProbes: {len(probes)}")
for p in probes[:3]:
    if isinstance(p, dict):
        print(f"      - {p.get('question','')[:80]}")
print(f"    MustHaveCoverage: {len(must_have)}")
print(f"    MissingEvidence: {len(missing)}")
print(f"    Summary: {summary[:200]}")

# 4. Check run events
print(f"\n[4] Run Events...")
try:
    resp = requests.get(f"{BASE}/api/runs/by-trace/{trace_id}", timeout=10)
    if resp.status_code == 200:
        runs = resp.json() if isinstance(resp.json(), list) else [resp.json()]
        for run in runs[:1]:
            run_id = run.get("runId", "")
            print(f"    RunId: {run_id}")
            # Get events
            ev_resp = requests.get(f"{BASE}/api/runs/{run_id}/events", timeout=10)
            if ev_resp.status_code == 200:
                events = ev_resp.json() if isinstance(ev_resp.json(), list) else []
                categories = {}
                for ev in events:
                    cat = ev.get("category", "UNKNOWN")
                    categories[cat] = categories.get(cat, 0) + 1
                print(f"    Event categories: {json.dumps(categories, indent=6)}")
                print(f"    Total events: {len(events)}")
except Exception as e:
    print(f"    Events error: {e}")

# 5. Verdict
print(f"\n{'=' * 60}")
print("VERIFICATION VERDICT")
print(f"{'=' * 60}")
issues = []
if not final_data.get("overallScore"):
    issues.append("overallScore is empty")
if not final_data.get("recommendation"):
    issues.append("recommendation is empty")
if len(dims) < 4:
    issues.append(f"dimensions only {len(dims)}, need >= 4")
if len(strengths) < 2:
    issues.append(f"strengths only {len(strengths)}, need >= 2")
if len(risks) < 1:
    issues.append(f"risks only {len(risks)}, need >= 1")
if len(probes) < 6:
    issues.append(f"interviewProbes only {len(probes)}, need >= 6 for rich resume")
if duration_ms > 45000:
    issues.append(f"duration {duration_ms/1000:.1f}s > 45s target")
if "没有简历" in summary or "共享状态为空" in summary:
    issues.append("report claims no resume")

if issues:
    print("ISSUES FOUND:")
    for iss in issues:
        print(f"  - {iss}")
else:
    print("ALL CHECKS PASSED")

print(f"\nTrace URL: {BASE}/#/task/{trace_id}")
