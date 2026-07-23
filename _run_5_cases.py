"""Run 5 test cases and measure results."""
import requests, time, json, sys

BASE = 'http://8.138.10.189'

CASES = [
    {
        "name": "case1_senior_java",
        "resume": "张明\n高级Java工程师 | 8年\n\n2019-今 阿里P7\n- 交易中台重构 TPS 2万→8万\n- 分布式事务(TCC) 50+微服务\n- K8s容器化 200+应用\n- 全链路压测 修复30+瓶颈\n\n2016-2019 美团\n- 外卖订单 3000万/日\n- 状态机 SLA 99.99%\n- Flink管道 10亿条/日\n\n技术: Java,Spring Boot/Cloud,Dubbo,K8s,Docker,MySQL,Redis,RocketMQ,Kafka,Flink,ES\n教育: 浙大CS本科",
        "jd": "高级Java工程师\n- 5年+Java\n- 精通Spring Boot/Cloud\n- 分布式+高并发\n- K8s容器编排\n- 大厂优先",
    },
    {
        "name": "case2_frontend",
        "resume": "李华\n前端工程师 | 4年\n\n2022-今 字节跳动\n- 抖音直播间互动组件 React+WebSocket\n- 性能优化 FCP 3s→1.2s\n- 微前端架构 qiankun 接入10+子应用\n\n2020-2022 小米\n- MIUI官网重构 Vue3+TypeScript\n- 组件库开发 40+组件 单元测试覆盖85%\n\n技术: React,Vue3,TypeScript,Webpack,Vite,Node.js,Tailwind\n教育: 北邮计算机本科",
        "jd": "前端工程师\n- 3年+前端\n- 精通React或Vue\n- TypeScript\n- 性能优化经验\n- 工程化经验",
    },
    {
        "name": "case3_no_jd",
        "resume": "王强\n后端工程师 | 3年\n\n2023-今 腾讯\n- 微信支付对账系统 Go+MySQL\n- 日处理2亿流水 准确率99.999%\n\n2021-2023 京东\n- 物流调度系统 Java+Redis\n- 路径优化算法 配送效率+15%\n\n技术: Go,Java,MySQL,Redis,Kafka,Docker\n教育: 华科软工硕士",
        "jd": "",
    },
    {
        "name": "case4_weak",
        "resume": "赵六\n实习生\n\n2025 某创业公司实习3个月\n- 帮忙写了一些页面\n- 学习了React\n\n技术: HTML,CSS,JavaScript\n教育: 某三本 信息管理 2025届",
        "jd": "高级全栈工程师\n- 5年+经验\n- 精通React+Node.js\n- 分布式系统设计\n- 带团队经验",
    },
    {
        "name": "case5_with_github",
        "resume": "陈工\nAI工程师 | 5年\nGitHub: https://github.com/chenai\n\n2021-今 商汤科技\n- 目标检测模型优化 mAP提升8%\n- 模型推理加速 TensorRT latency降低60%\n- MLOps平台搭建 模型发布自动化\n\n2019-2021 旷视\n- 人脸识别SDK 移动端部署\n- 模型压缩 体积减少70% 精度损失<1%\n\n技术: Python,PyTorch,TensorFlow,ONNX,TensorRT,Docker,K8s\n教育: 中科大CS硕士\n论文: CVPR 2020 一作",
        "jd": "AI工程师\n- 3年+深度学习\n- 模型训练和部署\n- 有开源贡献优先\n- 论文发表优先",
    },
]

results = []
for case in CASES:
    print(f"\n{'='*50}")
    print(f"Running: {case['name']}")
    files = {'file': (f"{case['name']}.txt", case['resume'].encode('utf-8'), 'text/plain')}
    data = {'jobCategory': 'TECH', 'executionMode': 'DAG_CONCURRENT'}
    if case['jd']:
        data['jobDescription'] = case['jd']
    else:
        data['jobDescription'] = ''
    
    t0 = time.time()
    r = requests.post(f'{BASE}/api/tasks/upload', files=files, data=data, timeout=30)
    if r.status_code != 200:
        print(f'  Upload failed: {r.status_code} {r.text[:200]}')
        results.append({"name": case["name"], "error": f"upload_{r.status_code}"})
        continue
    
    task = r.json()
    trace_id = task.get('traceId') or task.get('data', {}).get('traceId', '')
    print(f'  traceId={trace_id}', flush=True)
    
    if not trace_id:
        print(f'  ERROR: no traceId in response: {json.dumps(task)[:200]}', flush=True)
        results.append({"name": case["name"], "error": "no_trace_id"})
        continue
    
    final_state = None
    for i in range(50):
        time.sleep(3)
        try:
            r2 = requests.get(f'{BASE}/api/tasks/{trace_id}', timeout=10)
        except Exception as ex:
            print(f'  [{i*3}s] poll error: {ex}', flush=True)
            continue
        if r2.status_code != 200:
            continue
        d = r2.json()
        st = d.get('status') or d.get('evaluationState')
        if st in ('SUCCESS', 'PARTIAL_SUCCESS', 'FAILED', 'SYSTEM_FAILED'):
            elapsed = time.time() - t0
            sr = d.get('structuredReport') or {}
            if isinstance(sr, str):
                try: sr = json.loads(sr)
                except: sr = {}
            
            result = {
                "name": case["name"],
                "traceId": trace_id,
                "status": st,
                "duration_s": round(elapsed, 1),
                "overallScore": sr.get("overallScore") if isinstance(sr, dict) else None,
                "recommendation": sr.get("recommendation") if isinstance(sr, dict) else None,
                "dims": len(sr.get("dimensions", [])) if isinstance(sr, dict) else 0,
                "probes": len(sr.get("interviewProbes", [])) if isinstance(sr, dict) else 0,
                "risks": len(sr.get("risks", [])) if isinstance(sr, dict) else 0,
                "strengths": len(sr.get("strengths", [])) if isinstance(sr, dict) else 0,
                "dataQuality": sr.get("dataQuality") if isinstance(sr, dict) else None,
            }
            results.append(result)
            print(f'  status={st} score={result["overallScore"]} rec={result["recommendation"]} '
                  f'dims={result["dims"]} probes={result["probes"]} risks={result["risks"]} '
                  f'duration={result["duration_s"]}s')
            final_state = st
            break
    
    if final_state is None:
        elapsed = time.time() - t0
        results.append({"name": case["name"], "traceId": trace_id, "error": "timeout", "duration_s": round(elapsed, 1)})
        print(f'  TIMEOUT after {elapsed:.0f}s')

print(f"\n{'='*60}")
print("SUMMARY")
print(f"{'='*60}")
durations = []
for r in results:
    if "error" not in r:
        print(f"  {r['name']}: score={r['overallScore']} rec={r['recommendation']} "
              f"dims={r['dims']} probes={r['probes']} risks={r['risks']} "
              f"dur={r['duration_s']}s [{r['status']}]")
        if r['status'] == 'SUCCESS':
            durations.append(r['duration_s'])
    else:
        print(f"  {r['name']}: ERROR={r.get('error')} dur={r.get('duration_s', 'N/A')}s")

if durations:
    durations.sort()
    p50 = durations[len(durations)//2]
    p95 = durations[-1] if len(durations) < 20 else durations[int(len(durations)*0.95)]
    print(f"\n  p50={p50}s  p95={p95}s  (target: p50<30s, p95<45s)")
