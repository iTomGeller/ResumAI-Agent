<script setup lang="ts">
defineProps<{ plan?: any; budget?: any }>();
</script>

<template>
  <div class="card">
    <h3>Plan / Budget</h3>
    <template v-if="plan?.present">
      <p class="text-sm">{{ plan.reason || 'Coordinator plan' }}</p>
      <div class="ops-chip-row">
        <span v-for="a in (plan.plan || [])" :key="a" class="ops-chip">{{ a }}</span>
      </div>
      <p class="text-muted text-xs" style="margin-top:8px">
        selected: {{ Object.keys(plan.selectedBecause || {}).join(', ') || '-' }}
        · skipped: {{ Object.keys(plan.skippedBecause || {}).join(', ') || '-' }}
      </p>
      <h4 class="ops-subhead">Budget planned vs actual</h4>
      <pre class="ops-pre">{{ JSON.stringify({ planned: plan.budgetPlan || budget?.planned, actual: budget?.actualMetrics || budget }, null, 2) }}</pre>
    </template>
    <p v-else class="text-muted text-sm">尚无 Coordinator plan（无 agent.selected）</p>
  </div>
</template>

<style scoped>
.ops-pre {
  margin: 6px 0 0; padding: 8px; font-size: 11px; overflow: auto; max-height: 180px;
  background: var(--color-bg); border-radius: 6px; border: 1px solid var(--color-border-light);
}
.ops-chip-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.ops-chip {
  display: inline-flex; padding: 2px 7px; border-radius: 999px;
  background: var(--color-bg); border: 1px solid var(--color-border); font-size: 11px;
}
.ops-subhead { margin: 12px 0 6px; font-size: 13px; color: var(--color-text-secondary); }
</style>
