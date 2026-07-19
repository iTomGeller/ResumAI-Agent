# Docker Sandbox

## 用途
简历解析、时间线、JD 覆盖、证据定位、报告核验、lint、schema、policy 评估
（固定 8 个白名单工具，仅此而已——不是任意代码执行平台）。

## 镜像固定
Worker 镜像 tag 为部署时的 Git SHA：`resumai-sandbox-worker:${SANDBOX_WORKER_TAG}`，
由 `scripts/ecs_safe_deploy.sh` 写入 `.env`；compose 在缺少该变量时拒绝启动，
**禁止 latest**。

## 安全边界
network=none, read-only rootfs, non-root(65534), cap-drop ALL,
no-new-privileges, memory 256–512MB, CPU 0.5, PID limit, tmpfs workspace
配额, stdout cap, 工具超时, TTL 3–5min + 孤儿回收。

调用方（Agent/LLM/用户）永远不能指定：镜像、command、entrypoint、volume、
host path、capability、网络。Manager API 只接受白名单工具名 + JSON 参数；
Docker Socket 只在 Manager 内部（仅 Docker 内网可达，不暴露宿主端口）。

## 威胁模型摘要
- 恶意简历内容 → 在无网络只读容器内解析，最坏破坏本容器（TTL 回收）
- 提示注入让 LLM 请求任意工具 → 白名单 + Agent 级 allowlist 双层拒绝
- Manager 被内网攻破 → 有 Docker Socket 权限；缓解：内部 token、参数
  白名单、固定镜像名来自服务器配置；这是当前单机形态的已知边界

Labels：project=resumai, sandbox=true, runId, conversationId, expireAt
Manager：`sandbox/manager/app.py`；Worker：`sandbox/worker/`；并发 1–2（4C16G）。
