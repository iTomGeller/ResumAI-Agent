<script setup lang="ts">
import { ref, watch } from 'vue';

const props = defineProps<{
  panel: any;
  initialRunId?: string;
  initialDecision?: string;
}>();
const emit = defineEmits<{
  (e: 'filter', payload: { runId: string; decision: string }): void;
}>();

const runId = ref(props.initialRunId || '');
const decision = ref(props.initialDecision || '');

watch(() => props.initialRunId, (v) => { if (v != null) runId.value = v; });
watch(() => props.initialDecision, (v) => { if (v != null) decision.value = v; });

function apply() {
  emit('filter', { runId: runId.value.trim(), decision: decision.value.trim() });
}
</script>

<template>
  <div class="ops-panel-inner">
    <div class="ops-filters card">
      <input v-model="runId" placeholder="runId" @keyup.enter="apply" />
      <select v-model="decision">
        <option value="">decision (all)</option>
        <option value="USED">USED</option>
        <option value="IGNORED">IGNORED</option>
      </select>
      <button class="btn btn-ghost" @click="apply">筛选</button>
    </div>
    <div class="ops-metrics">
      <div><span>条目</span><strong>{{ panel?.count ?? 0 }}</strong></div>
      <div><span>已隐藏</span><strong>{{ panel?.skipped ?? 0 }}</strong></div>
      <div><span>usage</span><strong>{{ (panel?.usage || []).length }}</strong></div>
    </div>
    <div class="card table-wrap">
      <h3>Memory Usage（USED / IGNORED）</h3>
      <table class="ops-table">
        <thead>
          <tr>
            <th>Memory</th><th>Decision</th><th>Consumer</th>
            <th>Scores</th><th>Reason</th><th>Run</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in (panel?.usage || [])" :key="row.id || `${row.memoryId}-${row.rankNo}`">
            <td>
              <div class="mono text-xs">{{ row.memoryId }}</div>
              <div class="text-muted text-xs">{{ row.type }} · {{ row.ownerScope }} · {{ row.source }}</div>
              <div class="text-sm">{{ row.contentPreview || '-' }}</div>
            </td>
            <td><span class="ops-chip" :class="row.decision === 'USED' ? 'ok' : 'warn'">{{ row.decision }}</span></td>
            <td>{{ row.consumerAgent }}</td>
            <td class="text-xs">
              v={{ row.vectorScore ?? '-' }}
              · l={{ row.lexicalScore ?? '-' }}
              · r={{ row.recencyScore ?? '-' }}
              · f={{ row.finalScore ?? '-' }}
            </td>
            <td class="text-muted text-xs">{{ row.ignoredReason || '-' }}</td>
            <td class="mono text-xs">{{ row.runId }}</td>
          </tr>
          <tr v-if="!(panel?.usage || []).length">
            <td colspan="6" class="text-muted">暂无 usage 记录（检索后才会写入）</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="card" style="margin-top:12px">
      <h3>Entries</h3>
      <ul class="ops-list">
        <li v-for="item in (panel?.entries || []).slice(0, 40)" :key="item.memoryId">
          <div class="ops-row-head">
            <span class="ops-chip">{{ item.type }}</span>
            <span class="ops-chip">{{ item.ownerScope }}</span>
            <span class="text-muted text-xs">{{ item.source }} · {{ item.runId || '-' }}</span>
          </div>
          <p>{{ item.content }}</p>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.ops-filters { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; padding: 10px; }
.ops-filters input, .ops-filters select {
  border: 1px solid var(--color-border); border-radius: 8px; padding: 6px 10px; min-width: 140px; font-size: 13px;
}
.ops-metrics {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; margin-bottom: 12px;
}
.ops-metrics > div {
  border: 1px solid var(--color-border); border-radius: 10px; padding: 10px 12px; background: var(--color-surface);
}
.ops-metrics span { display: block; font-size: 12px; color: var(--color-text-secondary); }
.ops-list { list-style: none; display: grid; gap: 10px; margin: 0; padding: 0; }
.ops-list li { border: 1px solid var(--color-border-light); border-radius: 8px; padding: 10px; background: var(--color-bg); }
.ops-row-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.ops-chip {
  display: inline-flex; padding: 2px 7px; border-radius: 999px;
  background: var(--color-bg); border: 1px solid var(--color-border); font-size: 11px;
}
.ops-chip.ok { background: var(--color-success-light); color: var(--color-success); border-color: transparent; }
.ops-chip.warn { background: var(--color-warning-light); color: var(--color-warning); border-color: transparent; }
.ops-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ops-table th, .ops-table td { text-align: left; padding: 8px; border-bottom: 1px solid var(--color-border-light); vertical-align: top; }
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
</style>
