# Docker Sandbox

## 用途
简历解析、时间线、JD 覆盖、证据定位、报告核验、lint、schema、policy 评估。

## 安全
network=none, read-only rootfs, non-root, cap-drop ALL, no-new-privileges,
memory 256–512MB, CPU 0.5, PID limit, TTL 3–5min, stdout cap。

禁止：任意镜像/Volume/Host Path/Shell/网络；容器内无 Secret/Docker Socket。

Labels：project=resumai, sandbox=true, runId, conversationId, expireAt

Manager：`sandbox/manager/app.py`；Worker：`sandbox/worker/`
并发 1–2（4C16G）。
