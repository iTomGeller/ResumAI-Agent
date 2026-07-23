"""Test a single case with the rawExcerpt fix."""
import requests, time, json

BASE = 'http://8.138.10.189'

resume = ("张明\n高级Java工程师 | 8年\n\n2019-今 阿里P7\n"
          "- 交易中台重构 TPS 2万到8万\n"
          "- 分布式事务(TCC) 50+微服务\n"
          "- K8s容器化 200+应用\n"
          "- 全链路压测 修复30+瓶颈\n\n"
          "2016-2019 美团\n"
          "- 外卖订单 3000万/日\n"
          "- 状态机 SLA 99.99%\n"
          "- Flink管道 10亿条/日\n\n"
          "技术: Java,Spring Boot/Cloud,Dubbo,K8s,Docker,MySQL,Redis,RocketMQ,Kafka,Flink,ES\n"
          "教育: 浙大CS本科")

jd = ("高级Java工程师\n- 5年+Java\n- 精通Spring Boot/Cloud\n"
      "- 分布式+高并发\n- K8s容器编排\n- 大厂优先")

files = {'file': ('test_raw_fix.txt', resume.encode('utf-8'), 'text/plain')}
data = {'jobDescription': jd, 'jobCategory': 'BACKEND', 'executionMode': 'DAG_CONCURRENT'}

t0 = time.time()
r = requests.post(f'{BASE}/api/tasks/upload', files=files, data=data, timeout=30)
print(f'Upload: {r.status_code}')
task = r.json()
trace_id = task.get('traceId', '')
print(f'traceId: {trace_id}')

for i in range(50):
    time.sleep(3)
    try:
        r2 = requests.get(f'{BASE}/api/tasks/{trace_id}', timeout=10)
    except:
        continue
    if r2.status_code != 200:
        continue
    d = r2.json()
    st = d.get('evaluationState') or d.get('status')
    if st in ('SUCCESS', 'PARTIAL_SUCCESS', 'COMPLETED', 'FAILED', 'SYSTEM_FAILED'):
        elapsed = time.time() - t0
        print(f'\nCompleted in {elapsed:.1f}s  status={st}')
        sr = d.get('structuredReport') or {}
        if isinstance(sr, str):
            try: sr = json.loads(sr)
            except: sr = {}
        if isinstance(sr, dict):
            print(f'overallScore: {sr.get("overallScore")}')
            print(f'recommendation: {sr.get("recommendation")}')
            print(f'dataQuality: {sr.get("dataQuality")}')
            dims = sr.get('dimensions', [])
            print(f'\nDimensions ({len(dims)}):')
            for dd in dims:
                print(f'  {dd.get("name")}: score={dd.get("score")} [{dd.get("status")}]')
            print(f'\nstrengths: {len(sr.get("strengths", []))}')
            print(f'risks: {len(sr.get("risks", []))}')
            print(f'probes: {len(sr.get("interviewProbes", []))}')
        break
else:
    print(f'\nTIMEOUT after {time.time()-t0:.0f}s')

# Also check the agent execution tree
r3 = requests.get(f'{BASE}/api/tasks/{trace_id}/agent-execution', timeout=10)
if r3.status_code == 200:
    data = r3.json()
    tree = data.get('executionTree', [])
    print(f'\nAgent Execution ({len(tree)} agents):')
    for node in tree:
        print(f'  Phase{node.get("phase")} {node.get("name")}: '
              f'{node.get("durationMs")}ms llm={node.get("llmCalls")} '
              f'tool={node.get("toolCalls")} [{node.get("status")}]')
