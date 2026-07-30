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
const usageRows = computed(() => {
  const usage = props.panel?.usageBySkill || [];
  const byId = new Map(usage.map((row: any) => [row.skillId, row]));
  const rows = (props.panel?.skills || []).map((skill: any) => {
    const skillId = skill.skillId || skill.name;
    return byId.get(skillId) || {
      skillId,
      catalog: 0, selected: 0, loaded: 0, applied: 0, skipped: 0, failed: 0,
      lastHash: skill.hash, lastVersion: skill.version,
    };
  });
  const catalogIds = new Set(rows.map((row: any) => row.skillId));
  return [...rows, ...usage.filter((row: any) => !catalogIds.has(row.skillId))];
});
const triggeredSkillCount = computed(() => usageRows.value.filter((row: any) =>
  Number(row.selected || 0) + Number(row.loaded || 0) + Number(row.applied || row.completed || 0) > 0).length);
const appliedCount = computed(() => usageRows.value.reduce((sum: number, row: any) =>
  sum + Number(row.applied ?? row.completed ?? 0), 0));
function isUnusedSkill(row: any): boolean {
  return Number(row.catalog || 0) + Number(row.selected || 0) + Number(row.loaded || 0)
    + Number(row.applied ?? row.completed ?? 0) + Number(row.skipped || 0) + Number(row.failed || 0) === 0;
}
function shortRunId(runId?: string): string {
  if (!runId) return '-';
  return runId.length > 20 ? `${runId.slice(0, 20)}…` : runId;
}
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
      <div><span>已触发 Skill</span><strong>{{ triggeredSkillCount }}</strong></div>
      <div><span>累计 Applied</span><strong>{{ appliedCount }}</strong></div>
    </div>
    <section class="card section-card manifest-section">
      <div class="section-head">
        <div>
          <h3>Runtime Skill 目录</h3>
          <p class="text-muted text-sm">展示当前可被动态选择的 Skill 元数据与允许使用的工具，不代表本次已调用。</p>
        </div>
        <span class="section-count">{{ panel?.skills?.length || 0 }} 个 ACTIVE</span>
      </div>
      <div class="manifest-grid">
        <article v-for="skill in (panel?.skills || [])" :key="skill.skillId || skill.name" class="skill-card">
          <div class="skill-title-row">
            <strong class="skill-name">{{ skill.skillId || skill.name }}</strong>
            <span class="ops-chip" :class="inventoryClass(skill.status)">{{ skill.status }}</span>
          </div>
          <div class="skill-meta-row">
            <span class="ops-chip">{{ skill.version || 'unversioned' }}</span>
            <span class="ops-chip neutral">{{ skill.disclosureState || 'METADATA' }}</span>
            <span v-if="skill.hash && skill.hash !== 'not-loaded'" class="mono hash" :title="skill.hash">
              {{ skill.hash.slice(0, 12) }}
            </span>
            <span v-else class="ops-chip neutral">指令未加载</span>
          </div>
          <p class="text-muted text-sm skill-description">{{ skill.description }}</p>
          <div class="tool-block">
            <span class="tool-label">允许工具</span>
            <div v-if="(skill.requiredTools || skill.allowedTools || []).length" class="ops-chip-row">
              <span v-for="tool in (skill.requiredTools || skill.allowedTools || [])" :key="tool" class="ops-chip tool-chip">{{ tool }}</span>
            </div>
            <span v-else class="text-muted text-xs">无绑定工具</span>
          </div>
        </article>
      </div>
    </section>

    <section class="card section-card usage-section">
      <div class="section-head">
        <div>
          <h3>Skill 调用汇总</h3>
          <p class="text-muted text-sm">按真实 run_event 汇总；全为 0 表示该 Skill 尚未被工作流触发。</p>
        </div>
      </div>
      <div class="table-scroll">
        <table class="ops-table usage-table">
          <thead>
            <tr><th>Skill</th><th>目录曝光</th><th>选中</th><th>加载</th><th>应用</th><th>跳过</th><th>失败</th><th>最近时间</th><th>最近 Run</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in usageRows" :key="row.skillId" :class="{ 'unused-row': isUnusedSkill(row) }">
              <td class="skill-name-cell">
                <strong>{{ row.skillId }}</strong>
                <span v-if="isUnusedSkill(row)" class="ops-chip neutral">未触发</span>
              </td>
              <td class="numeric-cell">{{ row.catalog ?? 0 }}</td>
              <td class="numeric-cell">{{ row.selected ?? 0 }}</td>
              <td class="numeric-cell">{{ row.loaded ?? 0 }}</td>
              <td class="numeric-cell applied-number">{{ row.applied ?? row.completed ?? 0 }}</td>
              <td class="numeric-cell">{{ row.skipped ?? 0 }}</td>
              <td class="numeric-cell failed-number">{{ row.failed ?? 0 }}</td>
              <td class="mono text-xs time-cell"><time :datetime="eventTime(row)" :title="isoTimestamp(eventTime(row))">{{ formatTimestamp(eventTime(row)) }}</time></td>
              <td class="mono text-xs run-cell" :title="row.lastRunId || ''">{{ shortRunId(row.lastRunId) }}</td>
            </tr>
            <tr v-if="!usageRows.length">
              <td colspan="9" class="text-muted">尚无 skill catalog / selected / loaded / applied 事件</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="card section-card events-section">
      <div class="section-head">
        <div>
          <h3>最近 Skill 事件</h3>
          <p class="text-muted text-sm">查看 Skill 在哪个 Run、哪个 Agent 中被曝光、选择、加载或应用。</p>
        </div>
        <span class="section-count">共 {{ allEvents.length }} 条</span>
      </div>
      <div class="table-scroll">
        <table class="ops-table compact-table events-table">
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
              <td class="mono text-xs run-cell" :title="ev.runId || ''">{{ shortRunId(ev.runId) }}</td>
              <td class="text-xs">{{ ev.triggerReason || ev.reason || ev.lifecycleStage || '-' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <button v-if="allEvents.length > EVENT_PREVIEW && !showAllEvents"
              class="expand-btn" @click="showAllEvents = true">
        展开全部 {{ allEvents.length }} 条 ▾
      </button>
      <button v-if="showAllEvents && allEvents.length > EVENT_PREVIEW"
              class="expand-btn" @click="showAllEvents = false">
        收起 ▴
      </button>
    </section>
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
.section-card { margin-bottom: 12px; padding: 16px; }
.section-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.section-head h3 { margin: 0 0 4px; }
.section-head p { margin: 0; }
.section-count {
  flex: 0 0 auto; padding: 4px 10px; border-radius: 999px;
  background: #eef4ff; color: var(--color-primary); font-size: 12px; font-weight: 600;
}
.manifest-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 10px; }
.skill-card { border: 1px solid var(--color-border-light); border-radius: 10px; padding: 12px; background: var(--color-bg); }
.skill-title-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.skill-name { min-width: 0; overflow-wrap: anywhere; color: var(--color-text); }
.skill-meta-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 7px; }
.skill-description { margin: 10px 0 0; line-height: 1.65; }
.hash { color: var(--color-text-secondary); }
.tool-block { margin-top: 10px; padding-top: 9px; border-top: 1px dashed var(--color-border-light); }
.tool-label { display: block; margin-bottom: 6px; color: var(--color-text-secondary); font-size: 11px; font-weight: 600; }
.tool-chip { max-width: 100%; overflow-wrap: anywhere; }
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
.table-scroll { width: 100%; overflow-x: auto; }
.ops-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ops-table th, .ops-table td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--color-border-light); vertical-align: middle; }
.ops-table th { color: var(--color-text-secondary); font-size: 11px; font-weight: 600; white-space: nowrap; }
.usage-table { min-width: 1040px; }
.skill-name-cell { min-width: 235px; white-space: nowrap; }
.skill-name-cell .ops-chip { margin-left: 7px; vertical-align: middle; }
.numeric-cell { width: 64px; text-align: center !important; font-variant-numeric: tabular-nums; }
.applied-number { color: #16a34a; font-weight: 700; }
.failed-number { color: var(--color-danger); font-weight: 700; }
.unused-row { color: var(--color-text-secondary); background: rgba(148, 163, 184, 0.05); }
.run-cell { min-width: 154px; white-space: nowrap; }
.events-table { min-width: 940px; }
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
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
.time-cell { min-width: 168px; font-variant-numeric: tabular-nums; }
.time-cell span { display: block; margin-top: 2px; color: var(--color-text-secondary); }
@media (max-width: 900px) {
  .manifest-grid { grid-template-columns: 1fr; }
  .section-head { align-items: flex-start; }
}
</style>
