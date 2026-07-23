import requests
import json

url = "http://8.138.10.189/api/tasks"
payload = {
    "resumeText": """张三
联系方式: zhangsan@email.com | GitHub: https://github.com/zhangsan-dev

教育背景:
浙江大学 计算机科学与技术 本科 2012-2016

工作经历:
阿里巴巴 高级Java工程师(P7) 2020-至今
- 负责交易中台核心系统重构，TPS从2000提升至8000+
- 设计并实现分布式事务框架，支撑3000万日订单
- 优化Kafka消息队列，日处理消息10亿条
- 技术博客: https://juejin.cn/user/zhangsan

字节跳动 后端工程师 2018-2020
- 负责推荐系统数据pipeline开发
- 使用Flink实时处理千万级DAU用户行为数据
- 参与开源项目ByteHouse贡献

技术栈:
Java/Spring Boot/Spring Cloud, MySQL/Redis/MongoDB, Kafka/RocketMQ, Kubernetes/Docker, Flink/Spark

项目:
1. 交易中台重构 (阿里)
   - 微服务拆分，从单体到200+服务
   - 设计统一支付网关，接入支付宝/微信/银联
   - 实现分布式幂等方案，资损率降低99.9%

2. 实时推荐Pipeline (字节)
   - 基于Flink的实时特征计算
   - 支撑千万DAU的个性化推荐
   - A/B实验平台CTR提升15%

论文:
- 2019 SIGMOD "Efficient Transaction Processing in Distributed Systems"
""",
    "jobDescription": """高级后端工程师 (P7/P8)
要求:
- 5年以上Java开发经验
- 精通Spring Boot/Cloud微服务架构
- 熟悉分布式系统设计（分布式事务、消息队列、缓存）
- 有大规模系统性能优化经验
- 有团队技术方案设计和评审能力
加分项:
- 有电商/交易系统经验
- 有开源项目贡献
- 有论文发表经历
日活/月活用户规模数据（JD要求日活千万级）""",
    "runType": "full_evaluation"
}

headers = {"Content-Type": "application/json"}
resp = requests.post(url, json=payload, headers=headers, timeout=10)
print(f"Status: {resp.status_code}")
print(f"Response: {resp.text[:800]}")
