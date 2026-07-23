<script setup lang="ts">
import { computed, watch } from 'vue';
import ConversationPanel from './ConversationPanel.vue';
import { usePageContextStore } from '../../stores/pageContext';
import type { ConversationTurnResponse, TaskControlResponse } from '../../composables/useConversation';

const emit = defineEmits<{
  revisionCreated: [response: ConversationTurnResponse];
  controlTurn: [response: ConversationTurnResponse];
  selectRevision: [traceId: string];
  statusChange: [response: TaskControlResponse];
}>();

const pageContext = usePageContextStore();

const conversationId = computed(() => pageContext.conversationId || pageContext.traceId || '');
const traceId = computed(() => pageContext.traceId || pageContext.conversationId || '');
/** Dashboard / Ops without an open candidate still need a chat target. */
const bootstrapConversationId = computed(
  () => conversationId.value || `global-copilot-${pageContext.workspace || 'hr'}`,
);

watch(
  () => pageContext.copilotFullscreen,
  (full) => document.body.classList.toggle('copilot-fullscreen-lock', !!full),
  { immediate: true },
);
</script>

<template>
  <div
    class="system-copilot floating"
    :class="{
      open: pageContext.copilotOpen,
      fullscreen: pageContext.copilotFullscreen,
    }"
  >
    <button
      v-if="!pageContext.copilotOpen"
      type="button"
      class="system-copilot-fab"
      title="打开系统 Copilot"
      @click="pageContext.toggleCopilot()"
    >
      Copilot
    </button>

    <template v-else>
      <ConversationPanel
        :conversation-id="bootstrapConversationId"
        :trace-id="traceId || bootstrapConversationId"
        :revision-no="pageContext.revisionNo"
        :task-status="pageContext.taskStatus || 'SUCCESS'"
        :overall-score="pageContext.overallScore"
        :recommendation="pageContext.recommendation"
        :run-id="pageContext.runId"
        :extra-context-refs="pageContext.refs"
        :width="pageContext.copilotWidth"
        :fullscreen="pageContext.copilotFullscreen"
        @update:width="pageContext.setCopilotWidth($event)"
        @update:fullscreen="pageContext.setCopilotFullscreen($event)"
        @revision-created="emit('revisionCreated', $event)"
        @control-turn="emit('controlTurn', $event)"
        @select-revision="emit('selectRevision', $event)"
        @status-change="emit('statusChange', $event)"
      />

      <button
        v-if="!pageContext.copilotFullscreen"
        type="button"
        class="system-copilot-collapse"
        title="收起 Copilot"
        @click="pageContext.toggleCopilot()"
      >
        收起
      </button>
    </template>
  </div>
</template>

<style scoped>
.system-copilot {
  z-index: 40;
}

.system-copilot.floating {
  position: fixed;
  right: 20px;
  bottom: 20px;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
}

.system-copilot.floating.open:not(.fullscreen) {
  right: 16px;
  bottom: 16px;
  top: 72px;
  align-items: stretch;
}

.system-copilot.floating.open:not(.fullscreen) :deep(.conversation-panel) {
  height: calc(100vh - 96px);
  max-height: calc(100vh - 96px);
  box-shadow: 0 18px 48px rgba(15, 23, 42, 0.18);
  border-radius: 14px;
}

.system-copilot.fullscreen {
  position: fixed;
  inset: 0;
  z-index: 80;
  background: rgba(15, 23, 42, 0.28);
  display: flex;
  align-items: stretch;
  justify-content: flex-end;
  padding: 0;
}

.system-copilot-fab,
.system-copilot-collapse,
.system-copilot-close {
  border: 1px solid var(--color-border, #d7dde5);
  background: var(--color-surface, #fff);
  color: var(--color-text, #0f172a);
  border-radius: 999px;
  padding: 10px 16px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 10px 28px rgba(15, 23, 42, 0.12);
}

.system-copilot-collapse,
.system-copilot-close {
  align-self: flex-end;
  padding: 6px 12px;
  box-shadow: none;
}

.system-copilot-empty {
  width: min(420px, calc(100vw - 32px));
  background: var(--color-surface, #fff);
  border: 1px solid var(--color-border, #d7dde5);
  border-radius: 14px;
  padding: 16px;
  box-shadow: 0 18px 48px rgba(15, 23, 42, 0.14);
}

.system-copilot-empty-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.system-copilot-eyebrow {
  display: block;
  font-size: 11px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--color-text-secondary, #64748b);
}

.system-copilot-empty h3 {
  margin: 2px 0 0;
  font-size: 16px;
}

.system-copilot-empty p {
  margin: 0;
  font-size: 13px;
  line-height: 1.55;
  color: var(--color-text-secondary, #64748b);
}

.system-copilot-hints {
  margin: 12px 0 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--color-text, #0f172a);
}
</style>
