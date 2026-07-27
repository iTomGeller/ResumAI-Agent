<script setup lang="ts">
import { ref, computed } from 'vue';
const props = defineProps<{
  panel: any;
  inventoryClass: (status?: string) => string;
}>();
const showAllEvents = ref(false);
const EVENT_PREVIEW = 8;
const allEvents = computed(() => props.panel?.events || props.panel?.selectedApplied || []);
const visibleEvents = computed(() =>
  showAllEvents.value ? allEvents.value : allEvents.value.slice(0, EVENT_PREVIEW));
function eventLabel(eventType?: string): string {
  return (eventType || 'unknown').replace(/^skill\./, '');
}
function eventTone(eventType?: string): string {
  if (eventType === 'skill.applied') return 'applied';
  if (eventType === 'skill.failed') return 'bad';
  if (eventType === 'skill.skipped') return 'warn';
  if (eventType === 'skill.loaded') return 'loaded';
  return 'neutral';
}
function eventTime(row: any): string {
  return row.occurredAt || row.lastOccurredAt || row.lastAt
    || row.startedAt || row.createdAt || row.createTime || '';
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
</script>

<template>
  <div class="ops-panel-inner">
    <div class="card ops-note">
      <strong>source: {{ panel?.source || '—' }}</strong>
      <p>{{ panel?.note }} · root={{ panel?.root || '-' }}</p>
    </div>
    <div class="ops-metrics">
      <div><span>ACTIVE</span><strong>{{ panel?.activeCount ?? panel?.count ?? 0 }}</strong></div>
      <div><span>Deprecated</span><strong>{{ panel?.deprecatedCount ?? 0 }}</strong></div>
    </div>
    <div class="ops-columns two">
      <div class="card">
        <h3>Runtime Manifest</h3>
        <ul class="ops-list">
          <li v-for="skill in (panel?.skills || [])" :key="skill.skillId || skill.name">
            <div class="ops-row-head">
              <strong>{{ skill.skillId || skill.name }}</strong>
              <span class="ops-chip">v{{ skill.version }}</span>
              <span class="mono text-xs">{{ skill.hash }}</span>
              <span class="ops-chip" :class="inventoryClass(skill.status)">{{ skill.status }}</span>
              <span class="ops-chip neutral">{{ skill.disclosureState || 'METADATA' }}</span>
            </div>
            <p class="text-muted text-sm">{{ skill.description }}</p>
            <div class="ops-chip-row">
              <span v-for="tool in (skill.requiredTools || skill.allowedTools || [])" :key="tool" class="ops-chip">{{ tool }}</span>
            </div>
          </li>
        </ul>
      </div>
      <div class="card table-wrap">
        <h3>Catalog / Selected / Loaded / Applied / Skipped / Failed</h3>
        <table class="ops-table">
          <thead>
            <tr><th>Skill</th><th>Catalog</th><th>Selected</th><th>Loaded</th><th>Applied</th><th>Skipped</th><th>Failed</th><th>Last Time</th><th>Last Run</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in (panel?.usageBySkill || [])" :key="row.skillId">
              <td>{{ row.skillId }}</td>
              <td>{{ row.catalog ?? 0 }}</td>
              <td>{{ row.selected ?? 0 }}</td>
              <td>{{ row.loaded ?? 0 }}</td>
              <td>{{ row.applied ?? row.completed ?? 0 }}</td>
              <td>{{ row.skipped ?? 0 }}</td>
              <td>{{ row.failed ?? 0 }}</td>
              <td class="mono text-xs time-cell"><time :datetime="eventTime(row)" :title="isoTimestamp(eventTime(row))">{{ formatTimestamp(eventTime(row)) }}</time></td>
              <td class="mono text-xs">{{ row.lastRunId || '-' }}</td>
            </tr>
            <tr v-if="!(panel?.usageBySkill || []).length">
              <td colspan="9" class="text-muted">尚无 skill catalog / selected / loaded / applied 事件</td>
            </tr>
          </tbody>
        </table>
        <h4 class="ops-subhead">Skill Events (关联 Run / Agent) <span class="text-muted text-xs">共 {{ allEvents.length }} 条</span></h4>
        <table class="ops-table compact-table">
          <thead>
            <tr><th>Time（北京时间）</th><th>Event</th><th>Skill</th><th>Agent</th><th>Run</th><th>Trigger / Reason</th></tr>
          </thead>
          <tbody>
            <tr v-for="(ev, idx) in visibleEvents" :key="idx"
                :class="{ 'row-applied': ev.eventType === 'skill.applied' }">
              <td class="mono text-xs time-cell">
                <time :datetime="eventTime(ev)" :title="isoTimestamp(eventTime(ev))">{{ formatTimestamp(eventTime(ev)) }}</time>
                <span v-if="ev.endedAt" :title="isoTimestamp(ev.endedAt)">→ {{ formatTimestamp(ev.endedAt) }}</span>
              </td>
              <td><span class="ops-chip" :class="eventTone(ev.eventType)">{{ eventLabel(ev.eventType) }}</span></td>
              <td><strong>{{ ev.skillId || ev.toolName }}</strong></td>
              <td class="text-xs">{{ ev.agentId || '-' }}</td>
              <td class="mono text-xs">{{ (ev.runId || '-').slice(0, 16) }}</td>
              <td class="text-xs">{{ ev.triggerReason || ev.reason || ev.lifecycleStage || '-' }}</td>
            </tr>
          </tbody>
        </table>
        <button v-if="allEvents.length > EVENT_PREVIEW && !showAllEvents"
                class="expand-btn" @click="showAllEvents = true">
          展开全部 {{ allEvents.length }} 条 ▾
        </button>
        <button v-if="showAllEvents && allEvents.length > EVENT_PREVIEW"
                class="expand-btn" @click="showAllEvents = false">
          收起 ▴
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ops-metrics {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; margin-bottom: 12px;
}
.ops-metrics > div {
  border: 1px solid var(--color-border); border-radius: 10px; padding: 10px 12px; background: var(--color-surface);
}
.ops-metrics span { display: block; font-size: 12px; color: var(--color-text-secondary); }
.ops-columns { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.ops-row-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.ops-chip-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.ops-chip {
  display: inline-flex; padding: 2px 7px; border-radius: 999px;
  background: var(--color-bg); border: 1px solid var(--color-border); font-size: 11px;
}
.ops-chip.applied { background: rgba(34, 197, 94, 0.12); color: #16a34a; border-color: transparent; font-weight: 600; }
.ops-chip.loaded { background: #e0e7ff; color: #4338ca; border-color: transparent; font-weight: 600; }
.ops-chip.warn { background: var(--color-warning-light); color: var(--color-warning); border-color: transparent; }
.ops-chip.bad { background: var(--color-danger-light); color: var(--color-danger); border-color: transparent; }
.ops-chip.neutral { background: #eef2f7; color: #475569; border-color: transparent; }
.ops-note { padding: 12px 14px; margin-bottom: 12px; }
.ops-subhead { margin: 12px 0 6px; font-size: 13px; color: var(--color-text-secondary); }
.ops-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ops-table th, .ops-table td { text-align: left; padding: 8px; border-bottom: 1px solid var(--color-border-light); }
.compact-table td { padding: 5px 8px; font-size: 12px; }
.compact-table th { padding: 5px 8px; font-size: 11px; text-transform: uppercase; color: var(--color-text-secondary); }
.row-applied { background: rgba(34, 197, 94, 0.04); }
.expand-btn {
  display: block; width: 100%; margin-top: 6px; padding: 6px;
  border: 1px dashed var(--color-border); border-radius: 6px;
  background: transparent; cursor: pointer; font-size: 12px;
  color: var(--color-text-secondary);
}
.expand-btn:hover { background: var(--color-surface); }
.ops-list { list-style: none; display: grid; gap: 10px; margin: 0; padding: 0; }
.ops-list li { border: 1px solid var(--color-border-light); border-radius: 8px; padding: 10px; background: var(--color-bg); }
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
.time-cell { min-width: 168px; font-variant-numeric: tabular-nums; }
.time-cell span { display: block; margin-top: 2px; color: var(--color-text-secondary); }
@media (max-width: 900px) { .ops-columns { grid-template-columns: 1fr; } }
</style>
