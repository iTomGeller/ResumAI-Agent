<script setup lang="ts">
import type { ConversationRevision } from '../../composables/useConversation';

defineProps<{
  revisions: ConversationRevision[];
  activeTraceId: string;
  viewedTraceId: string;
}>();

const emit = defineEmits<{
  select: [traceId: string];
}>();

const statusLabels: Record<string, string> = {
  QUEUED: '排队',
  RETRYING: '待重试',
  RUNNING: '运行中',
  PAUSING: '暂停中',
  RESUMING: '恢复中',
  PAUSED: '已暂停',
  SUCCESS: '已完成',
  PARTIAL_SUCCESS: '部分完成',
  FAILED: '失败',
  CANCELLED: '已取消',
  SUPERSEDED: '已被替代',
};

function statusLabel(status?: string): string {
  return statusLabels[status || ''] || status || '未知';
}

function revisionTitle(revision: ConversationRevision): string {
  const parts = [`v${revision.revision}`, statusLabel(revision.status)];
  if (revision.evaluationBrief) parts.push(revision.evaluationBrief);
  return parts.join(' · ');
}
</script>

<template>
  <div class="revision-switcher" aria-label="评估版本">
    <div class="revision-switcher-head">
      <span>Revision</span>
      <small v-if="revisions.length">共 {{ revisions.length }} 个版本</small>
    </div>
    <div v-if="revisions.length" class="revision-list">
      <button
        v-for="revision in revisions"
        :key="revision.traceId"
        type="button"
        class="revision-chip"
        :class="{
          'is-current': revision.traceId === activeTraceId,
          'is-viewed': revision.traceId === viewedTraceId,
          'is-superseded': revision.status === 'SUPERSEDED',
        }"
        :title="revisionTitle(revision)"
        @click="emit('select', revision.traceId)"
      >
        <strong>v{{ revision.revision }}</strong>
        <span>{{ statusLabel(revision.status) }}</span>
        <i v-if="revision.traceId === activeTraceId">当前</i>
        <i v-else-if="revision.traceId === viewedTraceId">查看中</i>
      </button>
    </div>
    <span v-else class="revision-empty">暂无 revision 数据</span>
  </div>
</template>

<style scoped>
.revision-switcher { padding: 12px 14px; }

.revision-switcher-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  color: var(--color-text-secondary);
  font-size: 11px;
  font-weight: 600;
}

.revision-switcher-head small { color: var(--color-text-muted); font-size: 10px; font-weight: 400; }

.revision-list { display: flex; gap: 6px; overflow-x: auto; padding-bottom: 2px; }

.revision-chip {
  position: relative;
  flex: 0 0 auto;
  display: grid;
  gap: 1px;
  min-width: 62px;
  padding: 6px 8px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  text-align: left;
}

.revision-chip strong { font-size: 11px; }
.revision-chip span { color: var(--color-text-secondary); font-size: 9px; }
.revision-chip i { color: var(--color-primary); font-size: 9px; font-style: normal; }
.revision-chip.is-viewed { border-color: var(--color-primary); box-shadow: 0 0 0 1px rgba(37, 99, 235, 0.1); }
.revision-chip.is-current { background: #eff6ff; }
.revision-chip.is-superseded { opacity: 0.7; background: #f8fafc; }
.revision-empty { color: var(--color-text-muted); font-size: 11px; }
</style>
