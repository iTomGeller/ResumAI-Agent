<script setup lang="ts">
import { computed, ref, watch } from 'vue';

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
const entryPage = ref(0);
const typeFilter = ref('ALL');
const PAGE_SIZE = 10;

const allEntries = computed(() => props.panel?.entries || []);
function canonicalType(row: any): string {
  return String(row?.taxonomy || row?.memoryType || row?.type || 'UNKNOWN').toUpperCase();
}
const filteredEntries = computed(() => {
  if (typeFilter.value === 'ALL') return allEntries.value;
  return allEntries.value.filter((e: any) => canonicalType(e) === typeFilter.value);
});
const availableTypes = computed(() => {
  const types = new Set<string>();
  for (const e of allEntries.value) { types.add(canonicalType(e)); }
  return ['ALL', ...Array.from(types).sort()];
});
const usageTypeCounts = computed(() => {
  const counts: Record<string, number> = {
    SEMANTIC: 0, EPISODIC: 0, PROCEDURAL: 0, WORKING: 0,
  };
  for (const row of (props.panel?.usage || [])) {
    const type = canonicalType(row);
    counts[type] = (counts[type] || 0) + 1;
  }
  return counts;
});
const ttlSummary = computed(() => {
  const counts: Record<string, number> = {
    ACTIVE: 0, EXPIRING_SOON: 0, EXPIRED: 0, ARCHIVED: 0, NO_EXPIRY: 0,
  };
  for (const row of allEntries.value) {
    const state = String(row?.ttl?.state || 'NO_EXPIRY').toUpperCase();
    counts[state] = (counts[state] || 0) + 1;
  }
  return counts;
});
const ttlDefaults = computed(() => props.panel?.defaults?.ttl?.typeDefaultDays || {});
const totalPages = computed(() => Math.max(1, Math.ceil(filteredEntries.value.length / PAGE_SIZE)));
const pagedEntries = computed(() => {
  const start = entryPage.value * PAGE_SIZE;
  return filteredEntries.value.slice(start, start + PAGE_SIZE);
});

watch(() => props.initialRunId, (v) => { if (v != null) runId.value = v; });
watch(() => props.initialDecision, (v) => { if (v != null) decision.value = v; });

function apply() {
  emit('filter', { runId: runId.value.trim(), decision: decision.value.trim() });
}
function rowTime(row: any): string {
  return row.occurredAt || row.usedAt || row.retrievedAt || row.createdAt || row.createTime || '';
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
function formatDuration(seconds?: number | null): string {
  if (seconds == null) return '永久';
  if (seconds <= 0) return '已到期';
  const days = Math.floor(seconds / 86400);
  if (days >= 1) return `${days}天`;
  const hours = Math.floor(seconds / 3600);
  if (hours >= 1) return `${hours}小时`;
  return `${Math.max(1, Math.floor(seconds / 60))}分钟`;
}
function effectiveTtl(ttl: any): string {
  if (!ttl || ttl.effectiveTtlSeconds == null) return '-';
  const days = ttl.effectiveTtlSeconds / 86400;
  return `${Number.isInteger(days) ? days : days.toFixed(1)}天`;
}
function ttlStateText(state?: string): string {
  const labels: Record<string, string> = {
    ACTIVE: '有效', EXPIRING_SOON: '即将到期', EXPIRED: '已到期',
    ARCHIVED: '已归档', DELETED: '已删除', NO_EXPIRY: '无到期时间',
  };
  return labels[String(state || '').toUpperCase()] || state || '-';
}
function ttlChipClass(state?: string): string {
  const value = String(state || '').toUpperCase();
  if (value === 'ACTIVE') return 'ok';
  if (value === 'EXPIRING_SOON') return 'warn';
  return value === 'EXPIRED' || value === 'DELETED' ? 'danger' : '';
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
      <div><span>Semantic used</span><strong>{{ usageTypeCounts.SEMANTIC }}</strong></div>
      <div><span>Episodic used</span><strong>{{ usageTypeCounts.EPISODIC }}</strong></div>
      <div><span>Procedural used</span><strong>{{ usageTypeCounts.PROCEDURAL }}</strong></div>
      <div><span>Working used</span><strong>{{ usageTypeCounts.WORKING }}</strong></div>
      <div><span>TTL 有效</span><strong>{{ ttlSummary.ACTIVE }}</strong></div>
      <div><span>7天内到期</span><strong>{{ ttlSummary.EXPIRING_SOON }}</strong></div>
      <div><span>已到期</span><strong>{{ ttlSummary.EXPIRED }}</strong></div>
    </div>
    <div class="ttl-policy card">
      <strong>TTL 策略：</strong>绝对过期，召回不会续期；
      默认 Working {{ ttlDefaults.WORKING ?? '-' }}天、Semantic {{ ttlDefaults.SEMANTIC ?? '-' }}天、
      Episodic {{ ttlDefaults.EPISODIC ?? '-' }}天、Procedural {{ ttlDefaults.PROCEDURAL ?? '-' }}天。
      单条记录若写入了不同 TTL，会标记“写入覆盖”。
    </div>
    <div class="card table-wrap">
      <h3>Memory Usage（USED / IGNORED）</h3>
      <table class="ops-table">
        <thead>
          <tr>
            <th>Time（北京时间）</th><th>Memory</th><th>Decision</th><th>Consumer</th>
            <th>Scores</th><th>Reason</th><th>Run</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in (panel?.usage || [])" :key="row.id || `${row.memoryId}-${row.rankNo}`">
            <td class="mono text-xs time-cell"><time :datetime="rowTime(row)" :title="rowTime(row)">{{ formatTimestamp(rowTime(row)) }}</time></td>
            <td>
              <div class="mono text-xs">{{ row.memoryId }}</div>
              <div class="text-muted text-xs">{{ canonicalType(row) }} · {{ row.ownerScope }} · {{ row.source }}</div>
              <div v-if="row.producerVersion || row.consumerVersion" class="text-muted text-xs mono">
                producer={{ row.producerVersion || 'legacy' }} · consumer={{ row.consumerVersion || 'legacy' }}
              </div>
              <div v-if="row.ttl" class="ttl-line text-xs">
                <span class="ops-chip" :class="ttlChipClass(row.ttl.state)">{{ ttlStateText(row.ttl.state) }}</span>
                剩余 {{ formatDuration(row.ttl.remainingTtlSeconds) }} · 到期 {{ formatTimestamp(row.ttl.expiresAt) }}
              </div>
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
            <td colspan="7" class="text-muted">暂无 usage 记录（检索后才会写入）</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="card" style="margin-top:12px">
      <h3>Entries（{{ filteredEntries.length }} / {{ allEntries.length }} 条）</h3>
      <div class="type-filters" style="margin-bottom:8px;display:flex;gap:6px;flex-wrap:wrap">
        <button v-for="t in availableTypes" :key="t"
          :class="['btn', 'btn-xs', typeFilter === t ? 'btn-primary' : 'btn-ghost']"
          @click="typeFilter = t; entryPage = 0">{{ t }}</button>
      </div>
      <ul class="ops-list">
        <li v-for="item in pagedEntries" :key="item.memoryId">
          <div class="ops-row-head">
            <span class="ops-chip" :class="canonicalType(item) === 'EPISODIC' ? '' : 'ok'">{{ canonicalType(item) }}</span>
            <span v-if="item.ttl" class="ops-chip" :class="ttlChipClass(item.ttl.state)">{{ ttlStateText(item.ttl.state) }}</span>
            <span class="text-muted text-xs">scope:{{ item.ownerScope }} · src:{{ item.source }} · {{ item.runId || '-' }}</span>
            <time class="mono text-xs entry-time" :datetime="rowTime(item)" :title="rowTime(item)">{{ formatTimestamp(rowTime(item)) }}</time>
          </div>
          <div v-if="item.ttl" class="ttl-detail text-xs">
            TTL {{ effectiveTtl(item.ttl) }} · 剩余 {{ formatDuration(item.ttl.remainingTtlSeconds) }}
            （{{ item.ttl.remainingPercent ?? 0 }}%）· 到期 {{ formatTimestamp(item.ttl.expiresAt) }}
            · 类型默认 {{ item.ttl.typeDefaultDays }}天
            <span v-if="item.ttl.overrideDetected" class="ttl-override">· 写入覆盖</span>
            · 不随召回续期
          </div>
          <p>{{ item.content }}</p>
        </li>
      </ul>
      <div v-if="totalPages > 1" class="pagination">
        <button :disabled="entryPage <= 0" @click="entryPage--">&lt;</button>
        <span>{{ entryPage + 1 }} / {{ totalPages }}</span>
        <button :disabled="entryPage >= totalPages - 1" @click="entryPage++">&gt;</button>
      </div>
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
.ttl-policy { margin-bottom: 12px; padding: 10px 12px; color: var(--color-text-secondary); font-size: 13px; }
.ttl-line { display: flex; align-items: center; gap: 5px; margin: 4px 0; color: var(--color-text-secondary); }
.ttl-detail { margin-top: 6px; color: var(--color-text-secondary); }
.ttl-override { color: var(--color-warning); font-weight: 600; }
.ops-list { list-style: none; display: grid; gap: 10px; margin: 0; padding: 0; }
.ops-list li { border: 1px solid var(--color-border-light); border-radius: 8px; padding: 10px; background: var(--color-bg); }
.ops-row-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.ops-chip {
  display: inline-flex; padding: 2px 7px; border-radius: 999px;
  background: var(--color-bg); border: 1px solid var(--color-border); font-size: 11px;
}
.ops-chip.ok { background: var(--color-success-light); color: var(--color-success); border-color: transparent; }
.ops-chip.warn { background: var(--color-warning-light); color: var(--color-warning); border-color: transparent; }
.ops-chip.danger { background: var(--color-danger-light, #fee2e2); color: var(--color-danger, #dc2626); border-color: transparent; }
.ops-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ops-table th, .ops-table td { text-align: left; padding: 8px; border-bottom: 1px solid var(--color-border-light); vertical-align: top; }
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
.time-cell { min-width: 168px; font-variant-numeric: tabular-nums; }
.entry-time { margin-left: auto; font-variant-numeric: tabular-nums; }
.pagination { display: flex; align-items: center; justify-content: center; gap: 12px; padding: 10px 0; font-size: 13px; }
.pagination button { border: 1px solid var(--color-border); border-radius: 6px; padding: 4px 10px; cursor: pointer; background: var(--color-surface); }
.pagination button:disabled { opacity: 0.4; cursor: not-allowed; }
</style>
