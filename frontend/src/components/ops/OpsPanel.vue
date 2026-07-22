<script setup lang="ts">
import { onMounted, ref } from 'vue';

type OpsTab = 'memory' | 'sandbox' | 'policy' | 'mcp' | 'skills';

const props = defineProps<{ initialTab?: OpsTab }>();
const activeTab = ref<OpsTab>(props.initialTab || 'memory');
const loading = ref(false);
const error = ref('');
const data = ref<any>(null);

const tabs: Array<{ id: OpsTab; label: string }> = [
  { id: 'memory', label: 'Memory' },
  { id: 'sandbox', label: 'Sandbox' },
  { id: 'policy', label: '策略学习' },
  { id: 'mcp', label: 'MCP' },
  { id: 'skills', label: 'Skills' },
];

async function loadOps() {
  loading.value = true;
  error.value = '';
  try {
    const res = await fetch('/api/ops');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data.value = await res.json();
  } catch (e: any) {
    error.value = e?.message || 'Ops 数据读取失败';
  } finally {
    loading.value = false;
  }
}

function statusClass(status?: string) {
  const s = (status || '').toUpperCase();
  if (s === 'AVAILABLE' || s === 'SUCCESS' || s === 'READY' || s === 'ACTIVE') return 'ok';
  if (s === 'RATE_LIMITED' || s === 'DEGRADED' || s === 'PENDING') return 'warn';
  if (s === 'AUTH_REQUIRED' || s === 'DOWN' || s === 'FAILED') return 'bad';
  return '';
}

onMounted(loadOps);
</script>

<template>
  <section class="ops-page">
    <div class="ops-hero">
      <div>
        <h1>Agent Ops</h1>
        <p>只读观测 Memory、Sandbox、无 GPU 策略学习、MCP 与 Skills。不改写运行态。</p>
      </div>
      <button class="btn btn-ghost" :disabled="loading" @click="loadOps">
        {{ loading ? '刷新中...' : '刷新' }}
      </button>
    </div>

    <div v-if="error" class="trace-health-warning">{{ error }}</div>

    <div class="ops-tabs">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        :class="{ active: activeTab === tab.id }"
        @click="activeTab = tab.id"
      >{{ tab.label }}</button>
    </div>

    <div v-if="loading && !data" class="empty-state"><p>加载中...</p></div>

    <template v-else-if="data">
      <!-- Memory -->
      <div v-if="activeTab === 'memory'" class="ops-panel">
        <div class="ops-metrics">
          <div><span>条目</span><strong>{{ data.memory?.count ?? 0 }}</strong></div>
          <div v-for="(count, type) in (data.memory?.byType || {})" :key="type">
            <span>{{ type }}</span><strong>{{ count }}</strong>
          </div>
        </div>
        <div class="ops-columns">
          <div class="card">
            <h3>会话偏好</h3>
            <ul class="ops-list">
              <li v-for="item in (data.memory?.preferences || []).slice(0, 12)" :key="item.memoryId">
                <div class="ops-row-head">
                  <span class="ops-chip">{{ item.source || 'preference' }}</span>
                  <span class="text-muted text-xs">置信 {{ item.confidence ?? '-' }}</span>
                </div>
                <p>{{ item.content }}</p>
              </li>
              <li v-if="!(data.memory?.preferences || []).length" class="text-muted text-sm">暂无偏好记忆</li>
            </ul>
          </div>
          <div class="card">
            <h3>摘要 / 情节</h3>
            <ul class="ops-list">
              <li v-for="item in (data.memory?.summaries || []).slice(0, 12)" :key="item.memoryId">
                <div class="ops-row-head">
                  <span class="ops-chip">{{ item.type }}</span>
                  <span class="text-muted text-xs">{{ item.conversationId || item.runId || '-' }}</span>
                </div>
                <p>{{ item.content }}</p>
              </li>
              <li v-if="!(data.memory?.summaries || []).length" class="text-muted text-sm">暂无摘要</li>
            </ul>
          </div>
          <div class="card">
            <h3>命中 / 失败提示</h3>
            <ul class="ops-list">
              <li v-for="item in (data.memory?.hits || []).slice(0, 12)" :key="item.memoryId">
                <div class="ops-row-head">
                  <span class="ops-chip">{{ item.type }}</span>
                  <span class="text-muted text-xs">{{ item.source || '-' }}</span>
                </div>
                <p>{{ item.content }}</p>
              </li>
              <li v-if="!(data.memory?.hits || []).length" class="text-muted text-sm">暂无命中记录</li>
            </ul>
          </div>
        </div>
      </div>

      <!-- Sandbox -->
      <div v-else-if="activeTab === 'sandbox'" class="ops-panel">
        <div class="ops-metrics">
          <div><span>执行记录</span><strong>{{ data.sandbox?.count ?? 0 }}</strong></div>
          <div v-for="(count, status) in (data.sandbox?.byStatus || {})" :key="status">
            <span>{{ status }}</span><strong>{{ count }}</strong>
          </div>
        </div>
        <div class="card table-wrap">
          <table class="ops-table">
            <thead>
              <tr>
                <th>工具</th><th>状态</th><th>隔离</th><th>耗时</th><th>Run</th><th>错误</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in (data.sandbox?.executions || [])" :key="row.sandboxId + row.createTime">
                <td>{{ row.toolName || '-' }}</td>
                <td><span class="ops-chip" :class="statusClass(row.status)">{{ row.status || '-' }}</span></td>
                <td>{{ row.isolationMode || '-' }}</td>
                <td>{{ row.durationMs != null ? row.durationMs + 'ms' : '-' }}</td>
                <td class="mono">{{ row.runId || '-' }}</td>
                <td class="text-muted text-xs">{{ row.error || row.stderrTail || '-' }}</td>
              </tr>
              <tr v-if="!(data.sandbox?.executions || []).length">
                <td colspan="6" class="text-muted">暂无 Sandbox 执行记录</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Policy -->
      <div v-else-if="activeTab === 'policy'" class="ops-panel">
        <div class="card ops-note">
          <strong>{{ data.policy?.mode || 'rule_reward_no_gpu' }}</strong>
          <p>{{ data.policy?.description }}</p>
        </div>
        <div class="ops-columns two">
          <div class="card">
            <h3>策略包</h3>
            <ul class="ops-list">
              <li v-for="b in (data.policy?.bundles || [])" :key="b.policyId">
                <div class="ops-row-head">
                  <strong>{{ b.name || b.policyId }}</strong>
                  <span v-if="b.isChampion" class="ops-chip ok">champion</span>
                  <span class="ops-chip">g{{ b.generation ?? 0 }} · v{{ b.version ?? 1 }}</span>
                </div>
                <p class="text-muted text-sm">{{ b.description || b.policyId }}</p>
              </li>
            </ul>
          </div>
          <div class="card">
            <h3>最近 Reward / Feedback</h3>
            <ul class="ops-list">
              <li v-for="r in (data.policy?.recentRewards || []).slice(0, 20)" :key="r.id">
                <div class="ops-row-head">
                  <span class="ops-chip">{{ r.source }}</span>
                  <strong>{{ Number(r.totalReward ?? 0).toFixed(3) }}</strong>
                  <span class="text-muted text-xs">{{ r.policyId }}</span>
                </div>
                <p class="text-muted text-xs">{{ r.taskCategory }} · {{ r.runId }}</p>
              </li>
              <li v-if="!(data.policy?.recentRewards || []).length" class="text-muted text-sm">暂无 reward 记录</li>
            </ul>
          </div>
        </div>
        <div class="card table-wrap">
          <h3>策略统计</h3>
          <table class="ops-table">
            <thead>
              <tr><th>Policy</th><th>类别</th><th>Runs</th><th>Rewards</th><th>Avg</th><th>成功/失败</th></tr>
            </thead>
            <tbody>
              <tr v-for="s in (data.policy?.statistics || [])" :key="s.policyId + s.taskCategory">
                <td class="mono">{{ s.policyId }}</td>
                <td>{{ s.taskCategory }}</td>
                <td>{{ s.runCount ?? 0 }}</td>
                <td>{{ s.rewardCount ?? 0 }}</td>
                <td>{{ Number(s.avgReward ?? 0).toFixed(3) }}</td>
                <td>{{ s.successCount ?? 0 }} / {{ s.failureCount ?? 0 }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- MCP -->
      <div v-else-if="activeTab === 'mcp'" class="ops-panel">
        <div class="ops-metrics">
          <div v-for="status in (data.mcp?.statusEnum || [])" :key="status">
            <span>{{ status }}</span>
            <strong>{{ (data.mcp?.servers || []).filter((s: any) => s.status === status).length }}</strong>
          </div>
        </div>
        <div class="ops-grid">
          <div v-for="server in (data.mcp?.servers || [])" :key="server.name" class="card">
            <div class="ops-row-head">
              <strong>{{ server.name }}</strong>
              <span class="ops-chip" :class="statusClass(server.status)">{{ server.status }}</span>
              <span v-if="server.optional" class="ops-chip">optional</span>
            </div>
            <p class="text-muted text-sm">{{ server.description || '-' }}</p>
            <p class="text-xs">transport: {{ server.transport || '-' }} · enabled: {{ server.enabled ? 'yes' : 'no' }}</p>
            <div class="ops-chip-row">
              <span v-for="tool in (server.tools || [])" :key="tool" class="ops-chip">{{ tool }}</span>
              <span v-if="!(server.tools || []).length" class="text-muted text-xs">tools list 未声明</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Skills -->
      <div v-else-if="activeTab === 'skills'" class="ops-panel">
        <div class="ops-metrics">
          <div><span>已安装</span><strong>{{ data.skills?.count ?? 0 }}</strong></div>
          <div><span>广告工具</span><strong>{{ (data.skills?.advertisedTools || []).join(', ') || '-' }}</strong></div>
        </div>
        <div class="ops-columns two">
          <div class="card">
            <h3>Manifest / 版本</h3>
            <ul class="ops-list">
              <li v-for="skill in (data.skills?.skills || [])" :key="skill.name">
                <div class="ops-row-head">
                  <strong>{{ skill.name }}</strong>
                  <span class="ops-chip">v{{ skill.version }}</span>
                  <span class="mono text-xs">{{ skill.hash }}</span>
                </div>
                <p class="text-muted text-sm">{{ skill.description }}</p>
                <div class="ops-chip-row">
                  <span v-for="tool in (skill.allowedTools || [])" :key="tool" class="ops-chip">{{ tool }}</span>
                </div>
              </li>
            </ul>
          </div>
          <div class="card table-wrap">
            <h3>触发矩阵</h3>
            <table class="ops-table">
              <thead>
                <tr><th>Skill</th><th>Triggers</th><th>Agents</th><th>Phases</th></tr>
              </thead>
              <tbody>
                <tr v-for="row in (data.skills?.triggerMatrix || [])" :key="row.skill">
                  <td>{{ row.skill }}</td>
                  <td>{{ (row.triggers || []).join(', ') || '-' }}</td>
                  <td>{{ (row.agents || []).join(', ') || '-' }}</td>
                  <td>{{ (row.phases || []).join(', ') || '-' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.ops-page { display: flex; flex-direction: column; gap: var(--space-lg); }
.ops-hero {
  display: flex; justify-content: space-between; gap: var(--space-md); align-items: flex-start;
  padding: var(--space-xl); border-radius: var(--radius-lg);
  background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%); color: #fff;
}
.ops-hero h1 { margin: 0 0 6px; font-size: 1.5rem; }
.ops-hero p { margin: 0; color: rgba(255,255,255,0.78); font-size: 14px; max-width: 640px; }
.ops-hero .btn-ghost { color: #fff; border-color: rgba(255,255,255,0.35); }
.ops-tabs { display: flex; flex-wrap: wrap; gap: 8px; }
.ops-tabs button {
  border: 1px solid var(--color-border); background: var(--color-surface);
  border-radius: 999px; padding: 6px 12px; font-size: 13px; cursor: pointer;
}
.ops-tabs button.active { border-color: var(--color-primary); color: var(--color-primary); background: var(--color-primary-light, #eef2ff); }
.ops-metrics {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; margin-bottom: 12px;
}
.ops-metrics > div {
  border: 1px solid var(--color-border); border-radius: 10px; padding: 10px 12px; background: var(--color-surface);
}
.ops-metrics span { display: block; font-size: 12px; color: var(--color-text-secondary); }
.ops-metrics strong { font-size: 16px; word-break: break-word; }
.ops-columns { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.ops-columns.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.ops-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
.ops-list { list-style: none; display: grid; gap: 10px; margin: 0; padding: 0; }
.ops-list li { border: 1px solid var(--color-border-light); border-radius: 8px; padding: 10px; background: var(--color-bg); }
.ops-list p { margin: 6px 0 0; font-size: 13px; line-height: 1.5; }
.ops-row-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.ops-chip {
  display: inline-flex; align-items: center; padding: 2px 7px; border-radius: 999px;
  background: var(--color-bg); border: 1px solid var(--color-border); font-size: 11px;
}
.ops-chip.ok { background: var(--color-success-light); color: var(--color-success); border-color: transparent; }
.ops-chip.warn { background: var(--color-warning-light); color: var(--color-warning); border-color: transparent; }
.ops-chip.bad { background: var(--color-danger-light); color: var(--color-danger); border-color: transparent; }
.ops-chip-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.ops-note { padding: 12px 14px; margin-bottom: 12px; }
.ops-note p { margin: 4px 0 0; color: var(--color-text-secondary); font-size: 13px; }
.table-wrap { overflow-x: auto; }
.ops-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ops-table th, .ops-table td { text-align: left; padding: 8px; border-bottom: 1px solid var(--color-border-light); vertical-align: top; }
.ops-table th { color: var(--color-text-secondary); font-size: 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
@media (max-width: 900px) {
  .ops-columns, .ops-columns.two { grid-template-columns: 1fr; }
}
</style>
