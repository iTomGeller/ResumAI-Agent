<script setup lang="ts">
defineProps<{
  panel: any;
  inventoryClass: (status?: string) => string;
}>();
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
            </div>
            <p class="text-muted text-sm">{{ skill.description }}</p>
            <div class="ops-chip-row">
              <span v-for="tool in (skill.requiredTools || skill.allowedTools || [])" :key="tool" class="ops-chip">{{ tool }}</span>
            </div>
          </li>
        </ul>
      </div>
      <div class="card table-wrap">
        <h3>Selected / Applied / Failed</h3>
        <table class="ops-table">
          <thead>
            <tr><th>Skill</th><th>Selected</th><th>Applied</th><th>Failed</th><th>Last Run</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in (panel?.usageBySkill || [])" :key="row.skillId">
              <td>{{ row.skillId }}</td>
              <td>{{ row.selected ?? 0 }}</td>
              <td>{{ row.applied ?? row.completed ?? 0 }}</td>
              <td>{{ row.failed ?? 0 }}</td>
              <td class="mono text-xs">{{ row.lastRunId || '-' }}</td>
            </tr>
            <tr v-if="!(panel?.usageBySkill || []).length">
              <td colspan="5" class="text-muted">尚无 skill.selected / applied 事件</td>
            </tr>
          </tbody>
        </table>
        <h4 class="ops-subhead">Trigger / Hash Drift / Required MCP</h4>
        <ul class="ops-list compact">
          <li v-for="(ev, idx) in (panel?.selectedApplied || []).slice(0, 30)" :key="idx">
            <div class="ops-row-head">
              <span class="ops-chip">{{ ev.eventType }}</span>
              <strong>{{ ev.skillId || ev.toolName }}</strong>
              <span class="text-xs">{{ ev.triggerReason || '-' }}</span>
              <span v-if="ev.hashDrift" class="ops-chip warn">hash drift</span>
            </div>
            <p class="text-muted text-xs">
              required: {{ (ev.requiredMcp || []).join(', ') || '-' }}
              · hash={{ ev.skillHash || ev.runHash || '-' }}
            </p>
          </li>
        </ul>
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
.ops-list { list-style: none; display: grid; gap: 10px; margin: 0; padding: 0; }
.ops-list.compact { gap: 6px; }
.ops-list li { border: 1px solid var(--color-border-light); border-radius: 8px; padding: 10px; background: var(--color-bg); }
.ops-row-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.ops-chip-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.ops-chip {
  display: inline-flex; padding: 2px 7px; border-radius: 999px;
  background: var(--color-bg); border: 1px solid var(--color-border); font-size: 11px;
}
.ops-chip.warn { background: var(--color-warning-light); color: var(--color-warning); border-color: transparent; }
.ops-chip.neutral { background: #eef2f7; color: #475569; border-color: transparent; }
.ops-note { padding: 12px 14px; margin-bottom: 12px; }
.ops-subhead { margin: 12px 0 6px; font-size: 13px; color: var(--color-text-secondary); }
.ops-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ops-table th, .ops-table td { text-align: left; padding: 8px; border-bottom: 1px solid var(--color-border-light); }
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
@media (max-width: 900px) { .ops-columns { grid-template-columns: 1fr; } }
</style>
