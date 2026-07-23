<script setup lang="ts">
import { computed, ref, watch } from 'vue';

const props = defineProps<{
  timeline: any[];
  highlightSeq?: number | null;
  statusClass: (status?: string) => string;
}>();

const expanded = ref<Record<string, boolean>>({});

watch(() => props.highlightSeq, (seq) => {
  if (seq != null) expanded.value[`s${seq}`] = true;
}, { immediate: true });

const rows = computed(() => props.timeline || []);

function outcomeClass(outcome?: string, eventType?: string) {
  const o = (outcome || '').toUpperCase();
  if (o === 'FAILED') return 'bad';
  if (o === 'SUCCESS') return 'ok';
  if (o === 'RUNNING') return 'warn';
  if ((eventType || '').includes('failed')) return 'bad';
  return '';
}
</script>

<template>
  <div class="card">
    <h3>Timeline</h3>
    <ul class="ops-list compact">
      <li v-for="ev in rows" :key="`${ev.seq}-${ev.eventType}`" :class="{ highlight: highlightSeq === ev.seq }">
        <div class="ops-row-head" @click="expanded[`s${ev.seq}`] = !expanded[`s${ev.seq}`]" style="cursor:pointer">
          <span class="ops-chip">#{{ ev.seq }}</span>
          <span class="ops-chip" :class="outcomeClass(ev.outcome, ev.eventType)">{{ ev.outcome || ev.eventType }}</span>
          <span class="text-sm">{{ ev.eventType }}</span>
          <span class="text-muted text-xs">{{ ev.agentId || '-' }} · {{ ev.toolName || '-' }}</span>
        </div>
        <pre v-if="expanded[`s${ev.seq}`]" class="ops-pre">{{ JSON.stringify(ev.payload || {}, null, 2) }}</pre>
      </li>
      <li v-if="!rows.length" class="text-muted text-sm">无事件</li>
    </ul>
  </div>
</template>

<style scoped>
.highlight { outline: 1px solid var(--color-primary, #3b82f6); }
.ops-pre {
  margin: 6px 0 0; padding: 8px; font-size: 11px; overflow: auto; max-height: 220px;
  background: var(--color-bg); border-radius: 6px; border: 1px solid var(--color-border-light);
}
</style>
