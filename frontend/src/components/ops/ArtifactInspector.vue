<script setup lang="ts">
defineProps<{ artifacts?: any }>();
</script>

<template>
  <div class="card">
    <h3>Artifacts</h3>
    <p class="text-sm">
      present: {{ (artifacts?.presentKeys || []).join(', ') || '（无）' }}
    </p>
    <p class="text-muted text-xs" v-if="(artifacts?.missingKeys || []).length">
      missing required: {{ artifacts.missingKeys.join(', ') }}
    </p>
    <pre class="ops-pre">{{ JSON.stringify({
      keys: artifacts?.presentKeys,
      required: artifacts?.requiredKeys,
      edges: artifacts?.edges,
    }, null, 2) }}</pre>
  </div>
</template>

<style scoped>
.ops-pre {
  margin: 6px 0 0; padding: 8px; font-size: 11px; overflow: auto; max-height: 200px;
  background: var(--color-bg); border-radius: 6px; border: 1px solid var(--color-border-light);
}
</style>
