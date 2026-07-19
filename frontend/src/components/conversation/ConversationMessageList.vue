<script setup lang="ts">
import { nextTick, ref, watch } from 'vue';
import type { ConversationMessage } from '../../composables/useConversation';

const props = defineProps<{
  messages: ConversationMessage[];
  loading?: boolean;
}>();

const scroller = ref<HTMLElement | null>(null);

const intentLabels: Record<string, string> = {
  SIDE_QUESTION: '独立问题',
  CLARIFY_GOAL_CHANGE: '等待确认',
  GOAL_CHANGE: '目标变更',
  EVALUATION_FOCUS_CHANGE: '评估重点',
  CONTEXT_ADD: '补充事实',
  CONTEXT_NOTE: '会话备注',
  CONTROL_COMMAND: '运行控制',
};

function intentLabel(intent?: string): string {
  return intent ? (intentLabels[intent] || intent) : '';
}

function roleLabel(role: string): string {
  return role.toUpperCase() === 'USER' ? '你' : 'ResumAI';
}

function formatTime(value?: string): string {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

watch(() => props.messages.length, async () => {
  await nextTick();
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
}, { immediate: true });
</script>

<template>
  <div ref="scroller" class="conversation-message-list" aria-live="polite">
    <div v-if="loading && !messages.length" class="conversation-empty">正在加载会话历史…</div>
    <div v-else-if="!messages.length" class="conversation-empty">
      <strong>还没有对话</strong>
      <span>可询问评估依据、临时比较岗位，或明确修改 JD / 评估重点。</span>
    </div>
    <article
      v-for="message in messages"
      :key="`${message.id}-${message.clientMessageId}`"
      class="conversation-message"
      :class="message.role.toUpperCase() === 'USER' ? 'is-user' : 'is-assistant'"
    >
      <header>
        <strong>{{ roleLabel(message.role) }}</strong>
        <span v-if="message.intent" class="conversation-intent">{{ intentLabel(message.intent) }}</span>
        <span class="conversation-message-revision">v{{ message.revision }}</span>
        <time>{{ formatTime(message.createdAt) }}</time>
      </header>
      <p>{{ message.content }}</p>
    </article>
  </div>
</template>

<style scoped>
.conversation-message-list {
  min-height: 260px;
  max-height: min(46vh, 520px);
  overflow-y: auto;
  padding: 14px;
  background: #f8fafc;
  border-block: 1px solid var(--color-border-light);
  scroll-behavior: smooth;
}

.conversation-empty {
  min-height: 220px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 24px;
  text-align: center;
  color: var(--color-text-secondary);
  font-size: 12px;
}

.conversation-empty strong { color: var(--color-text); font-size: 13px; }

.conversation-message { margin-bottom: 14px; }
.conversation-message:last-child { margin-bottom: 0; }

.conversation-message header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
  color: var(--color-text-muted);
  font-size: 10px;
}

.conversation-message header strong { color: var(--color-text-secondary); font-size: 11px; }
.conversation-message time { margin-left: auto; }

.conversation-message p {
  width: fit-content;
  max-width: 92%;
  padding: 9px 11px;
  border: 1px solid var(--color-border);
  border-radius: 4px 12px 12px 12px;
  background: var(--color-surface);
  color: var(--color-text);
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.conversation-message.is-user header { justify-content: flex-end; }
.conversation-message.is-user header time { margin-left: 0; }
.conversation-message.is-user p {
  margin-left: auto;
  border-color: rgba(37, 99, 235, 0.2);
  border-radius: 12px 4px 12px 12px;
  background: #eff6ff;
}

.conversation-intent,
.conversation-message-revision {
  padding: 1px 5px;
  border-radius: 999px;
  background: #e2e8f0;
  color: #475569;
  font-size: 9px;
}
</style>
