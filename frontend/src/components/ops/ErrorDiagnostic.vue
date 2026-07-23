<script setup lang="ts">
import { computed, ref, watch } from 'vue';

const props = defineProps<{
  errors: any[];
  runStatus?: string;
  errorCode?: string;
  errorMessage?: string;
}>();

const open = ref(false);
const isFailed = computed(() => {
  const s = (props.runStatus || '').toUpperCase();
  return s === 'FAILED' || s === 'TIMED_OUT' || s === 'CANCELLED';
});

watch([isFailed, () => props.errors], () => {
  if (isFailed.value || (props.errors || []).length) open.value = true;
}, { immediate: true });
</script>

<template>
  <div class="card">
    <div class="ops-row-head" style="cursor:pointer" @click="open = !open">
      <h3 style="margin:0">Error Diagnostic</h3>
      <span class="ops-chip" :class="isFailed ? 'bad' : ''">{{ runStatus || '—' }}</span>
      <span class="text-muted text-xs">{{ open ? '收起' : '展开' }}</span>
    </div>
    <div v-if="open">
      <p v-if="errorCode || errorMessage" class="text-sm" style="margin-top:8px">
        <strong>{{ errorCode || 'ERROR' }}</strong> — {{ errorMessage || '-' }}
      </p>
      <ul class="ops-list compact" style="margin-top:8px">
        <li v-for="(err, idx) in errors" :key="idx">
          <div class="ops-row-head">
            <span class="ops-chip bad">{{ err.eventType }}</span>
            <span v-if="err.rootCause" class="ops-chip warn">root cause</span>
            <span class="text-xs">{{ err.agentId || '-' }}</span>
          </div>
          <p class="text-sm">{{ err.message || err.errorCode || '-' }}</p>
          <pre v-if="err.payload" class="ops-pre">{{ JSON.stringify(err.payload, null, 2) }}</pre>
        </li>
        <li v-if="!errors?.length" class="text-muted text-sm">无错误事件</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.ops-list { list-style: none; display: grid; gap: 8px; margin: 0; padding: 0; }
.ops-list.compact { gap: 6px; }
.ops-list li { border: 1px solid var(--color-border-light); border-radius: 8px; padding: 8px; background: var(--color-bg); }
.ops-row-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.ops-chip {
  display: inline-flex; padding: 2px 7px; border-radius: 999px;
  background: var(--color-bg); border: 1px solid var(--color-border); font-size: 11px;
}
.ops-chip.bad { background: var(--color-danger-light); color: var(--color-danger); border-color: transparent; }
.ops-chip.warn { background: var(--color-warning-light); color: var(--color-warning); border-color: transparent; }
.ops-pre {
  margin: 6px 0 0; padding: 8px; font-size: 11px; overflow: auto; max-height: 180px;
  background: var(--color-surface); border-radius: 6px; border: 1px solid var(--color-border-light);
}
</style>
