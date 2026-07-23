<script setup lang="ts">
defineProps<{
  panel: any;
  inventoryClass: (status?: string) => string;
  statusClass: (status?: string) => string;
}>();
const emit = defineEmits<{ (e: 'open-run', runId: string): void }>();
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
    <div class="ops-grid">
      <div v-for="server in (panel?.servers || panel?.inventory?.servers || [])" :key="server.name" class="card">
        <div class="ops-row-head">
          <strong>{{ server.name }}</strong>
          <span class="ops-chip" :class="inventoryClass(server.status)">{{ server.status }}</span>
          <span v-if="server.optional" class="ops-chip">optional</span>
        </div>
        <p class="text-muted text-sm">{{ server.description || '-' }}</p>
        <p class="text-xs">
          transport: {{ server.transport || '-' }}
          · latency: {{ server.latencyMs != null ? server.latencyMs + 'ms' : '-' }}
          · circuit: {{ server.circuitOpen ? 'open' : 'ok' }}
        </p>
        <p v-if="server.error" class="text-muted text-xs">{{ server.error }}</p>
        <div class="ops-chip-row">
          <span v-for="tool in (server.tools || [])" :key="tool" class="ops-chip">{{ tool }}</span>
        </div>
      </div>
    </div>
    <div class="card table-wrap" style="margin-top:12px">
      <h3>MCP Invocations</h3>
      <table class="ops-table">
        <thead>
          <tr>
            <th>Tool</th><th>Server</th><th>Outcome</th><th>Retry/Cache</th>
            <th>Args</th><th>Result/Error</th><th>Run</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, idx) in (panel?.recentCalls || panel?.invocations?.items || [])" :key="idx">
            <td>{{ row.tool || row.toolName }}</td>
            <td>{{ row.server || row.mcpServer || '-' }}</td>
            <td><span class="ops-chip" :class="statusClass(row.outcome || row.status)">{{ row.outcome || row.status }}</span></td>
            <td class="text-xs">retry={{ row.retryCount ?? '-' }} · cache={{ row.cacheHit == null ? '-' : row.cacheHit }}</td>
            <td class="mono text-xs">{{ typeof row.arguments === 'string' ? row.arguments : JSON.stringify(row.arguments || {}).slice(0, 80) }}</td>
            <td class="text-muted text-xs">{{ row.error || (typeof row.resultPreview === 'string' ? row.resultPreview : JSON.stringify(row.resultPreview || {}).slice(0, 80)) || '-' }}</td>
            <td class="mono text-xs ops-link" @click="row.runId && emit('open-run', row.runId)">{{ row.runId }}</td>
          </tr>
          <tr v-if="!(panel?.recentCalls || panel?.invocations?.items || []).length">
            <td colspan="7" class="text-muted">暂无真实 MCP 调用</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.ops-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
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
</style>
