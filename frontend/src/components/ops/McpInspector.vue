<script setup lang="ts">
import { computed, ref } from 'vue';
const props = defineProps<{
  panel: any;
  inventoryClass: (status?: string) => string;
  statusClass: (status?: string) => string;
}>();
const emit = defineEmits<{ (e: 'open-run', runId: string): void }>();

const PAGE_SIZE = 10;
const mcpPage = ref(0);
const allCalls = computed(() => props.panel?.recentCalls || props.panel?.invocations?.items || []);
const endpointStats = computed(() => props.panel?.endpointStats || []);
const cumulativeCalls = computed(() => endpointStats.value.reduce(
  (sum: number, row: any) => sum + Number(row.calls || 0), 0));
const totalPages = computed(() => Math.max(1, Math.ceil(allCalls.value.length / PAGE_SIZE)));
const pagedCalls = computed(() => {
  const start = mcpPage.value * PAGE_SIZE;
  return allCalls.value.slice(start, start + PAGE_SIZE);
});
const activeServers = computed(() => {
  const raw = props.panel?.servers || props.panel?.inventory?.servers || [];
  return raw.filter((s: any) => s.status === 'AVAILABLE' || s.status === 'RATE_LIMITED');
});
const disabledServers = computed(() => {
  const raw = props.panel?.servers || props.panel?.inventory?.servers || [];
  return raw.filter((s: any) => s.status !== 'AVAILABLE' && s.status !== 'RATE_LIMITED');
});
const allServers = computed(() => props.panel?.servers || props.panel?.inventory?.servers || []);
const availableToolCount = computed(() => {
  const explicit = Number(props.panel?.toolCount ?? props.panel?.inventory?.toolCount);
  if (Number.isFinite(explicit)) return explicit;
  return allServers.value.reduce((sum: number, server: any) => sum + (server.tools || []).length, 0);
});
function mcpOutcome(row: any): string {
  const explicit = row.outcome || row.status;
  if (explicit === 'FAILED' || explicit === 'REJECTED') return explicit;
  const preview = row.resultPreview || row.error || '';
  const text = typeof preview === 'string' ? preview : JSON.stringify(preview);
  if (text.includes('"success": false') || text.includes('"success":false')) return 'FAILED';
  return explicit || 'UNKNOWN';
}
function rowTime(row: any): string {
  return row.occurredAt || row.startedAt || row.createdAt || row.createTime || row.time || '';
}
function formatTimestamp(value?: string): string {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const rendered = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date).replaceAll('/', '-');
  return `${rendered}.${String(date.getMilliseconds()).padStart(3, '0')}`;
}
function isoTimestamp(value?: string): string {
  if (!value) return 'timestamp not collected';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toISOString();
}
function durationLabel(row: any): string {
  const explicit = Number(row.durationMs);
  if (Number.isFinite(explicit) && explicit >= 0) return `${Math.round(explicit)}ms`;
  if (row.startedAt && row.endedAt) {
    const elapsed = Date.parse(row.endedAt) - Date.parse(row.startedAt);
    if (Number.isFinite(elapsed) && elapsed >= 0) return `${elapsed}ms`;
  }
  return '未采集';
}
function compactLatency(value?: number | null): string {
  if (value == null) return '-';
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) return '-';
  return numeric >= 1000 ? `${(numeric / 1000).toFixed(2)}s` : `${Math.round(numeric)}ms`;
}
function successRateLabel(value?: number | null): string {
  if (value == null) return '-';
  const numeric = Number(value);
  return Number.isFinite(numeric) ? `${numeric.toFixed(1)}%` : '-';
}
</script>

<template>
  <div class="ops-panel-inner">
    <div class="card ops-note">
      <strong>source: {{ panel?.source || panel?.inventory?.source || '—' }}</strong>
      <p>
        {{ panel?.note }}
        · lastProbe={{ panel?.lastProbeAt || panel?.inventory?.lastProbeAt || '-' }}
        · reachable={{ (panel?.runtimeReachable ?? panel?.inventory?.runtimeReachable) ? 'yes' : 'no' }}
      </p>
      <p v-if="panel?.runtimeError" class="text-muted text-sm">{{ panel.runtimeError }}</p>
    </div>
    <div class="ops-metrics">
      <div><span>MCP Servers</span><strong>{{ allServers.length }}</strong></div>
      <div><span>Available tools</span><strong>{{ availableToolCount }}</strong></div>
      <div><span>累计真实调用</span><strong>{{ cumulativeCalls }}</strong></div>
      <div><span>Recent real calls</span><strong>{{ allCalls.length }}</strong></div>
    </div>
    <div class="ops-grid">
      <div v-for="server in activeServers" :key="server.name" class="card card-active">
        <div class="ops-row-head">
          <strong>{{ server.name }}</strong>
          <span class="ops-chip ok">{{ server.status }}</span>
        </div>
        <p class="text-muted text-sm">{{ server.description || '-' }}</p>
        <p class="text-xs">
          transport: {{ server.transport || '-' }}
          · latency: {{ server.latencyMs != null ? server.latencyMs + 'ms' : '-' }}
          · circuit: {{ server.circuitOpen ? 'open' : 'ok' }}
        </p>
        <div class="ops-chip-row">
          <span v-for="tool in (server.tools || [])" :key="tool" class="ops-chip">{{ tool }}</span>
        </div>
      </div>
    </div>
    <details v-if="disabledServers.length" class="disabled-section">
      <summary class="text-muted text-sm">{{ disabledServers.length }} 个已禁用/不可用 MCP（点击展开）</summary>
      <div class="ops-grid" style="margin-top:8px;opacity:0.5">
        <div v-for="server in disabledServers" :key="server.name" class="card">
          <div class="ops-row-head">
            <strong>{{ server.name }}</strong>
            <span class="ops-chip bad">{{ server.status }}</span>
          </div>
          <p class="text-muted text-xs">{{ server.description || '-' }}</p>
          <p v-if="server.error" class="text-muted text-xs">{{ server.error }}</p>
        </div>
      </div>
    </details>
    <div class="card table-wrap" style="margin-top:12px">
      <h3>MCP Endpoint 总体调用统计</h3>
      <p class="text-muted text-sm endpoint-note">按当前 Registry 的 Server / Tool 汇总历史真实调用；从未调用的 endpoint 也显示为 0。</p>
      <table class="ops-table endpoint-table">
        <thead>
          <tr>
            <th>Server</th><th>Endpoint / Tool</th><th>调用</th><th>成功</th><th>失败</th>
            <th>拒绝</th><th>运行中</th><th>成功率</th><th>Avg</th><th>P50</th><th>P90</th><th>Max</th><th>最近调用</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in endpointStats" :key="`${row.server}:${row.endpoint}`">
            <td><strong>{{ row.server }}</strong></td>
            <td class="mono">{{ row.endpoint }}</td>
            <td>{{ row.calls ?? 0 }}</td>
            <td class="stat-success">{{ row.success ?? 0 }}</td>
            <td class="stat-failed">{{ row.failed ?? 0 }}</td>
            <td>{{ row.rejected ?? 0 }}</td>
            <td>{{ row.running ?? 0 }}</td>
            <td>{{ successRateLabel(row.successRate) }}</td>
            <td>{{ compactLatency(row.averageMs) }}</td>
            <td>{{ compactLatency(row.p50Ms) }}</td>
            <td>{{ compactLatency(row.p90Ms) }}</td>
            <td>{{ compactLatency(row.maxMs) }}</td>
            <td class="mono text-xs time-cell">
              <time v-if="row.lastCalledAt" :datetime="row.lastCalledAt" :title="isoTimestamp(row.lastCalledAt)">
                {{ formatTimestamp(row.lastCalledAt) }}
              </time>
              <span v-else>-</span>
            </td>
          </tr>
          <tr v-if="!endpointStats.length">
            <td colspan="13" class="text-muted">暂无 Endpoint 统计</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="card table-wrap" style="margin-top:12px">
      <h3>MCP Invocations</h3>
      <table class="ops-table">
        <thead>
          <tr>
            <th>Time（北京时间）</th><th>Tool</th><th>Server</th><th>Stage / Outcome</th><th>耗时</th><th>Retry/Cache</th>
            <th>Args</th><th>Result/Error</th><th>Run</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, idx) in pagedCalls"
              :key="`${row.toolCallId || 'no-call'}-${row.lifecycleStage || row.outcome || 'event'}-${row.occurredAt || row.createTime || idx}`">
            <td class="mono text-xs time-cell">
              <time :datetime="rowTime(row)" :title="isoTimestamp(rowTime(row))">{{ formatTimestamp(rowTime(row)) }}</time>
              <span v-if="row.endedAt" :title="isoTimestamp(row.endedAt)">→ {{ formatTimestamp(row.endedAt) }}</span>
            </td>
            <td>{{ row.tool || row.toolName }}</td>
            <td>{{ row.server || row.mcpServer || '-' }}</td>
            <td>
              <span v-if="row.lifecycleStage" class="ops-chip neutral">{{ row.lifecycleStage }}</span>
              <span class="ops-chip" :class="statusClass(mcpOutcome(row))">{{ mcpOutcome(row) }}</span>
            </td>
            <td class="mono text-xs">{{ durationLabel(row) }}</td>
            <td class="text-xs">retry={{ row.retryCount ?? '-' }} · cache={{ row.cacheHit == null ? '-' : row.cacheHit }}</td>
            <td class="mono text-xs">{{ typeof row.arguments === 'string' ? row.arguments : JSON.stringify(row.arguments || {}).slice(0, 80) }}</td>
            <td class="text-muted text-xs">{{ row.error || (typeof row.resultPreview === 'string' ? row.resultPreview : JSON.stringify(row.resultPreview || {}).slice(0, 80)) || '-' }}</td>
            <td class="mono text-xs ops-link" @click="row.runId && emit('open-run', row.runId)">{{ row.runId }}</td>
          </tr>
          <tr v-if="!allCalls.length">
            <td colspan="9" class="text-muted">暂无真实 MCP 调用</td>
          </tr>
        </tbody>
      </table>
      <div v-if="totalPages > 1" class="pagination">
        <button :disabled="mcpPage <= 0" @click="mcpPage--">&lt;</button>
        <span>{{ mcpPage + 1 }} / {{ totalPages }}（共 {{ allCalls.length }} 条）</span>
        <button :disabled="mcpPage >= totalPages - 1" @click="mcpPage++">&gt;</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ops-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
.ops-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin-bottom: 12px; }
.ops-metrics > div { border: 1px solid var(--color-border); border-radius: 10px; padding: 10px 12px; background: var(--color-surface); }
.ops-metrics span { display: block; font-size: 12px; color: var(--color-text-secondary); }
.ops-metrics strong { display: block; margin-top: 2px; font-size: 20px; }
.ops-row-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.ops-chip-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.ops-chip {
  display: inline-flex; padding: 2px 7px; border-radius: 999px;
  background: var(--color-bg); border: 1px solid var(--color-border); font-size: 11px;
}
.ops-chip.ok { background: var(--color-success-light); color: var(--color-success); border-color: transparent; }
.ops-chip.warn { background: var(--color-warning-light); color: var(--color-warning); border-color: transparent; }
.ops-chip.bad { background: var(--color-danger-light); color: var(--color-danger); border-color: transparent; }
.ops-chip.neutral { background: #eef2f7; color: #475569; border-color: transparent; }
.ops-note { padding: 12px 14px; margin-bottom: 12px; }
.ops-note p { margin: 4px 0 0; color: var(--color-text-secondary); font-size: 13px; }
.ops-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ops-table th, .ops-table td { text-align: left; padding: 8px; border-bottom: 1px solid var(--color-border-light); vertical-align: top; }
.ops-link { cursor: pointer; color: var(--color-primary); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
.time-cell { min-width: 168px; font-variant-numeric: tabular-nums; }
.time-cell span { display: block; margin-top: 2px; color: var(--color-text-secondary); }
.endpoint-note { margin: -4px 0 10px; }
.endpoint-table { min-width: 1220px; }
.stat-success { color: var(--color-success); font-weight: 600; }
.stat-failed { color: var(--color-danger); font-weight: 600; }
.pagination { display: flex; align-items: center; justify-content: center; gap: 12px; padding: 10px 0; font-size: 13px; }
.pagination button { border: 1px solid var(--color-border); border-radius: 6px; padding: 4px 10px; cursor: pointer; background: var(--color-surface); }
.pagination button:disabled { opacity: 0.4; cursor: not-allowed; }
</style>
