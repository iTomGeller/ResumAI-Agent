<script setup lang="ts">
defineProps<{
  items: any[];
  selectedRunId?: string;
  statusClass: (status?: string) => string;
}>();
const emit = defineEmits<{ (e: 'select', runId: string): void }>();
</script>

<template>
  <div class="card table-wrap">
    <h3>Recent Runs</h3>
    <table class="ops-table">
      <thead>
        <tr><th>Run</th><th>Status</th><th>Type</th><th>Agent</th><th>Trace</th><th>Error</th></tr>
      </thead>
      <tbody>
        <tr
          v-for="row in items"
          :key="row.runId"
          class="ops-click-row"
          :class="{ active: selectedRunId === row.runId }"
          @click="emit('select', row.runId)"
        >
          <td class="mono">{{ row.runId }}</td>
          <td><span class="ops-chip" :class="statusClass(row.status)">{{ row.status }}</span></td>
          <td>{{ row.runType || '-' }}</td>
          <td>{{ row.currentAgent || '-' }}</td>
          <td class="mono text-xs">{{ row.traceId || row.sourceTaskTraceId || '-' }}</td>
          <td class="text-muted text-xs">{{ row.errorCode || row.errorMessage || '-' }}</td>
        </tr>
        <tr v-if="!items.length">
          <td colspan="6" class="text-muted">暂无 run</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.ops-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ops-table th, .ops-table td { text-align: left; padding: 8px; border-bottom: 1px solid var(--color-border-light); vertical-align: top; }
.ops-click-row { cursor: pointer; }
.ops-click-row:hover, .ops-click-row.active { background: var(--color-primary-light, #eef2ff); }
.ops-chip {
  display: inline-flex; padding: 2px 7px; border-radius: 999px;
  background: var(--color-bg); border: 1px solid var(--color-border); font-size: 11px;
}
.ops-chip.ok { background: var(--color-success-light); color: var(--color-success); border-color: transparent; }
.ops-chip.warn { background: var(--color-warning-light); color: var(--color-warning); border-color: transparent; }
.ops-chip.bad { background: var(--color-danger-light); color: var(--color-danger); border-color: transparent; }
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
</style>
