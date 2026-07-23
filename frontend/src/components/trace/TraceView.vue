<script setup lang="ts">
import { computed, ref, watch } from 'vue';

/**
 * Langfuse 风格的两栏 Trace 视图：
 * 左栏是紧凑 span 树（规划 → 并行组 → Agent → 轮次/工具），
 * 右栏是选中 span 的详情面板（目标 / 判断 / 入参出参 / tokens / 耗时）。
 */

type ToolCategory = 'mcp' | 'skill' | 'builtin' | 'retrieval' | 'llm' | 'tool' | 'external';

interface ToolCallView {
  toolCallId?: string;
  name?: string;
  status?: string;
  durationMs?: number;
  input?: string;
  result?: string;
  output?: string;
  category?: string;
  origin?: string;
  executionBackend?: string;
  mcpServer?: string;
  skillId?: string;
  skillVersion?: string;
}

interface RoundView {
  roundNum?: number;
  type?: string;
  category?: string;
  title?: string;
  tokens?: number;
  durationMs?: number;
  model?: string;
  error?: string;
  hasToolCalls?: boolean;
  toolCalls?: ToolCallView[];
  input?: string;
  output?: string;
  decisionText?: string;
  finalOutput?: string;
  inputMessages?: unknown[];
  outputMessage?: Record<string, unknown>;
}

interface AgentView {
  name?: string;
  role?: string;
  phase?: number;
  status?: string;
  durationMs?: number;
  llmCalls?: number;
  toolCalls?: number;
  confidence?: number;
  output?: string;
  rounds?: RoundView[];
}

interface HistoricalAttempt {
  runId?: string;
  status?: string;
  errorCode?: string;
  errorMessage?: string;
  attemptNo?: number;
  category?: string;
  retryable?: boolean;
  controlPlaneStage?: string;
  finishedAt?: string;
  createdAt?: string;
}

interface ExecTree {
  framework?: string;
  executionTree?: AgentView[];
  harnessPlan?: {
    route?: {
      routeMode?: string;
      selectedAgents?: string[];
      parallelGroups?: string[][];
      whySelected?: string[];
      estimatedLlmCalls?: number;
      memoryHitCount?: number;
    };
    memoryInfluence?: { hitCount?: number; influences?: Array<Record<string, unknown>> };
    reportMode?: string;
  };
  planReason?: string;
  parallelGroups?: string[][];
  memoryHits?: number;
  memoryTop?: Array<{ type?: string; confidence?: number; content?: string }>;
  runId?: string;
  runStatus?: string;
  policyId?: string;
  attemptNo?: number;
  historicalAttempts?: HistoricalAttempt[];
  langfuseTraceUrl?: string;
}

const props = defineProps<{
  tree: ExecTree | null;
  agentNames: Record<string, string>;
  agentPurposes: Record<string, string>;
}>();

interface SpanRow {
  id: string;
  depth: number;
  kind: 'group' | 'agent' | 'round' | 'tool';
  label: string;
  sub?: string;
  status?: string;
  durationMs?: number;
  tokens?: number;
  parallel?: boolean;
  badge?: ToolCategory;
  agent?: AgentView;
  round?: RoundView;
  tool?: ToolCallView;
}

const FILTERS: Array<{ id: ToolCategory | 'all'; label: string }> = [
  { id: 'all', label: '全部' },
  { id: 'llm', label: 'LLM' },
  { id: 'builtin', label: 'BUILTIN' },
  { id: 'mcp', label: 'MCP' },
  { id: 'skill', label: 'SKILL' },
  { id: 'retrieval', label: 'RETRIEVAL' },
];

const selectedId = ref<string>('');
const collapsed = ref<Set<string>>(new Set());
const kindFilter = ref<ToolCategory | 'all'>('all');
const historyOpen = ref(false);

const agents = computed<AgentView[]>(() => props.tree?.executionTree || []);
// Historical attempts stay out of the default candidate Trace tree.
const historicalAttempts = computed(() => props.tree?.historicalAttempts || []);

const groups = computed<AgentView[][]>(() => {
  const byPhase = new Map<number, AgentView[]>();
  for (const agent of agents.value) {
    const phase = agent.phase || 1;
    if (!byPhase.has(phase)) byPhase.set(phase, []);
    byPhase.get(phase)!.push(agent);
  }
  return [...byPhase.entries()].sort((a, b) => a[0] - b[0]).map(([, list]) => list);
});

function displayName(name?: string): string {
  return props.agentNames[name || ''] || name || 'Agent';
}

function purpose(agent: AgentView): string {
  return props.agentPurposes[agent.name || ''] || agent.role || '';
}

function normalizeCategory(raw?: string, toolName?: string, isLlm = false): ToolCategory {
  if (isLlm) return 'llm';
  const value = (raw || '').toLowerCase();
  // Legacy labels remapped for candidate Trace.
  if (value === 'sandbox' || value === 'internal') return 'builtin';
  if (value === 'gateway') return 'external';
  if (value === 'mcp' || value === 'skill' || value === 'builtin'
      || value === 'retrieval' || value === 'llm' || value === 'external') {
    return value as ToolCategory;
  }
  const name = toolName || '';
  if (name.startsWith('mcp_') || name.includes('.')) return 'mcp';
  if (name === 'execute_skill' || name === 'list_skills' || name === 'load_skill') return 'skill';
  if (['parse_resume', 'check_timeline', 'calculate_jd_coverage', 'locate_evidence',
    'verify_report_evidence', 'resume_lint', 'validate_report_schema'].includes(name)) {
    return 'builtin';
  }
  if (['knowledge_search', 'resume_semantic_search', 'jd_match_search'].includes(name)) {
    return 'retrieval';
  }
  if (name === 'external_profile_lookup') return 'external';
  return 'builtin';
}

function badgeLabel(badge?: ToolCategory): string {
  if (!badge) return 'TOOL';
  return badge.toUpperCase();
}

function roundBadge(round: RoundView): ToolCategory {
  const tool = round.toolCalls?.[0];
  const isTool = round.type === 'tool' || round.hasToolCalls;
  return normalizeCategory(
    round.category || tool?.category || tool?.origin,
    tool?.name,
    !isTool,
  );
}

const spans = computed<SpanRow[]>(() => {
  const rows: SpanRow[] = [];
  groups.value.forEach((group, gi) => {
    const parallel = group.length > 1;
    const groupId = `g-${gi}`;
    const groupDuration = parallel
      ? Math.max(...group.map((a) => a.durationMs || 0))
      : group.reduce((s, a) => s + (a.durationMs || 0), 0);
    rows.push({
      id: groupId,
      depth: 0,
      kind: 'group',
      label: parallel
        ? `${group.map((a) => displayName(a.name)).join(' ∥ ')}`
        : displayName(group[0]?.name),
      sub: parallel ? `并行组 · ${group.length} 个 Agent` : undefined,
      status: group.some((a) => a.status === 'FAILED') ? 'FAILED'
        : group.some((a) => a.status === 'RUNNING') ? 'RUNNING' : 'SUCCESS',
      durationMs: groupDuration,
      parallel,
    });
    if (collapsed.value.has(groupId)) return;
    group.forEach((agent, ai) => {
      const agentId = `${groupId}-a-${ai}`;
      rows.push({
        id: agentId,
        depth: 1,
        kind: 'agent',
        label: displayName(agent.name),
        sub: purpose(agent),
        status: agent.status,
        durationMs: agent.durationMs,
        agent,
      });
      if (collapsed.value.has(agentId)) return;
      (agent.rounds || []).forEach((round, ri) => {
        const roundId = `${agentId}-r-${ri}`;
        const isTool = round.type === 'tool' || !!round.hasToolCalls;
        const badge = roundBadge(round);
        rows.push({
          id: roundId,
          depth: 2,
          kind: isTool ? 'tool' : 'round',
          label: round.title || (isTool ? '工具调用' : `第 ${round.roundNum ?? ri + 1} 轮`),
          status: round.error ? 'FAILED'
            : round.toolCalls?.some((t) => t.status === 'FAILED') ? 'FAILED' : 'SUCCESS',
          durationMs: round.durationMs ?? round.toolCalls?.[0]?.durationMs,
          tokens: round.tokens,
          badge,
          agent,
          round,
          tool: round.toolCalls?.[0],
        });
      });
    });
  });
  return rows;
});

const filteredSpans = computed(() => {
  if (kindFilter.value === 'all') return spans.value;
  return spans.value.filter((row) => {
    if (row.kind === 'group' || row.kind === 'agent') return true;
    return row.badge === kindFilter.value;
  });
});

const selected = computed<SpanRow | null>(() =>
  filteredSpans.value.find((s) => s.id === selectedId.value)
  || spans.value.find((s) => s.id === selectedId.value)
  || null);

watch(filteredSpans, (rows) => {
  if (!rows.length) return;
  if (!rows.some((r) => r.id === selectedId.value)) {
    selectedId.value = rows.find((r) => r.kind === 'agent')?.id || rows[0].id;
  }
}, { immediate: true });

function toggle(id: string) {
  const next = new Set(collapsed.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  collapsed.value = next;
}

const totals = computed(() => {
  let duration = 0;
  let tokens = 0;
  let llmCalls = 0;
  let toolCalls = 0;
  groups.value.forEach((group) => {
    duration += group.length > 1
      ? Math.max(...group.map((a) => a.durationMs || 0))
      : group.reduce((s, a) => s + (a.durationMs || 0), 0);
  });
  agents.value.forEach((agent) => {
    llmCalls += agent.llmCalls || 0;
    toolCalls += agent.toolCalls || 0;
    (agent.rounds || []).forEach((round) => { tokens += round.tokens || 0; });
  });
  return { duration, tokens, llmCalls, toolCalls };
});

const route = computed(() => props.tree?.harnessPlan?.route || null);
const memoryTop = computed(() => props.tree?.memoryTop
  || (props.tree?.harnessPlan?.memoryInfluence?.influences as ExecTree['memoryTop']) || []);
const memoryHits = computed(() => props.tree?.memoryHits
  ?? props.tree?.harnessPlan?.route?.memoryHitCount ?? 0);

function fmtMs(ms?: number): string {
  if (ms == null || ms <= 0) return '';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function statusClass(status?: string): string {
  if (status === 'FAILED') return 'is-failed';
  if (status === 'RUNNING') return 'is-running';
  return 'is-success';
}

function pretty(text?: string): string {
  if (!text) return '';
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

async function copyText(text?: string) {
  if (!text) return;
  try { await navigator.clipboard.writeText(text); } catch { /* ignore */ }
}
</script>

<template>
  <div class="trace-view">
    <!-- 概览条 -->
    <div class="trace-summary" v-if="agents.length">
      <span class="trace-summary-item"><em>总耗时</em>{{ fmtMs(totals.duration) || '-' }}</span>
      <span class="trace-summary-item"><em>LLM 调用</em>{{ totals.llmCalls }}</span>
      <span class="trace-summary-item"><em>工具调用</em>{{ totals.toolCalls }}</span>
      <span class="trace-summary-item"><em>Tokens</em>{{ totals.tokens || '-' }}</span>
      <span class="trace-summary-item" v-if="tree?.policyId"><em>策略</em>{{ tree.policyId }}</span>
      <span class="trace-summary-item" v-if="tree?.runStatus"><em>状态</em>{{ tree.runStatus }}</span>
      <span class="trace-summary-item" v-if="tree?.attemptNo"><em>当前尝试</em>#{{ tree.attemptNo }}</span>
    </div>

    <!-- 历史尝试（控制面失败折叠） -->
    <div class="trace-history" v-if="historicalAttempts.length">
      <button class="history-toggle" type="button" @click="historyOpen = !historyOpen">
        {{ historyOpen ? '▾' : '▸' }}
        历史尝试（{{ historicalAttempts.length }}）
        <span class="history-tag">CONTROL_PLANE</span>
      </button>
      <div v-if="historyOpen" class="history-list">
        <div v-for="(attempt, ai) in historicalAttempts" :key="attempt.runId || ai" class="history-card">
          <div class="history-card-head">
            <span class="history-badge">CONTROL_PLANE</span>
            <strong>#{{ attempt.attemptNo ?? ai + 1 }}</strong>
            <span class="history-code">{{ attempt.errorCode || attempt.status }}</span>
            <span class="history-retry" v-if="attempt.retryable">可重试</span>
          </div>
          <p class="history-msg">{{ attempt.errorMessage || '控制面失败（非 Agent 推理错误）' }}</p>
          <div class="history-meta">
            <span v-if="attempt.controlPlaneStage">阶段 {{ attempt.controlPlaneStage }}</span>
            <span v-if="attempt.runId">run {{ attempt.runId }}</span>
            <span v-if="attempt.finishedAt">{{ attempt.finishedAt }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 路由决策卡（Coordinator 规划） -->
    <div class="trace-route" v-if="route">
      <div class="trace-route-head">
        <h4>Coordinator 规划</h4>
        <span class="route-mode">{{ route.routeMode }}</span>
        <span class="route-meta" v-if="memoryHits">记忆命中 {{ memoryHits }}</span>
        <span class="route-meta" v-if="route.estimatedLlmCalls != null">LLM {{ route.estimatedLlmCalls }} 次</span>
      </div>
      <div class="route-plan" v-if="(route.parallelGroups || []).length">
        <template v-for="(group, gi) in route.parallelGroups" :key="gi">
          <span v-if="gi > 0" class="route-arrow">→</span>
          <span class="route-group" :class="{ parallel: group.length > 1 }">
            <template v-for="(agentName, ai) in group" :key="agentName">
              <span v-if="ai > 0" class="route-parallel-sep">∥</span>
              <span class="route-chip">{{ displayName(agentName) }}</span>
            </template>
          </span>
        </template>
      </div>
      <blockquote class="route-reason" v-if="route.whySelected?.length">{{ route.whySelected[0] }}</blockquote>
      <div class="route-memory" v-if="memoryTop.length">
        <div v-for="(hit, hi) in memoryTop" :key="hi" class="route-memory-item">
          <span class="memory-type">{{ hit.type }}</span>
          <span class="memory-content">{{ hit.content }}</span>
          <span class="memory-conf" v-if="hit.confidence != null">{{ Math.round(Number(hit.confidence) * 100) }}%</span>
        </div>
      </div>
    </div>

    <!-- 类型筛选 -->
    <div class="trace-filters" v-if="spans.length">
      <button
        v-for="f in FILTERS"
        :key="f.id"
        type="button"
        class="filter-chip"
        :class="[{ active: kindFilter === f.id }, f.id !== 'all' ? `badge-${f.id}` : '']"
        @click="kindFilter = f.id"
      >{{ f.label }}</button>
    </div>

    <!-- 两栏：span 树 + 详情 -->
    <div class="trace-panes" v-if="filteredSpans.length">
      <div class="span-tree" role="tree">
        <div
          v-for="row in filteredSpans"
          :key="row.id"
          class="span-row"
          :class="[statusClass(row.status), { selected: row.id === selectedId, [`depth-${row.depth}`]: true }]"
          role="treeitem"
          tabindex="0"
          @click="selectedId = row.id"
          @keydown.enter="selectedId = row.id"
        >
          <button
            v-if="row.kind === 'group' || (row.kind === 'agent' && (row.agent?.rounds?.length || 0) > 0)"
            class="span-caret"
            @click.stop="toggle(row.id)"
          >{{ collapsed.has(row.id) ? '▸' : '▾' }}</button>
          <span v-else class="span-caret-placeholder"></span>
          <span class="span-dot" :class="statusClass(row.status)"></span>
          <span
            v-if="row.kind === 'group'"
            class="span-kind badge-group"
          >GROUP</span>
          <span
            v-else-if="row.kind === 'agent'"
            class="span-kind badge-agent"
          >AGENT</span>
          <span
            v-else-if="row.kind === 'tool' || row.kind === 'round'"
            class="span-kind"
            :class="`badge-${row.badge || (row.kind === 'round' ? 'llm' : 'builtin')}`"
          >{{ badgeLabel(row.badge || (row.kind === 'round' ? 'llm' : 'builtin')) }}</span>
          <span class="span-label">{{ row.label }}</span>
          <span class="span-parallel" v-if="row.parallel">∥ 并行</span>
          <span class="span-tokens" v-if="row.tokens">{{ row.tokens }} tok</span>
          <span class="span-duration">{{ fmtMs(row.durationMs) }}</span>
        </div>
      </div>

      <div class="span-detail" v-if="selected">
        <div class="detail-head">
          <span class="span-dot" :class="statusClass(selected.status)"></span>
          <h4>{{ selected.label }}</h4>
          <span
            v-if="selected.badge"
            class="span-kind"
            :class="`badge-${selected.badge}`"
          >{{ badgeLabel(selected.badge) }}</span>
          <span class="detail-duration">{{ fmtMs(selected.durationMs) }}</span>
        </div>

        <template v-if="selected.kind === 'group'">
          <p class="detail-sub">{{ selected.sub || '顺序执行' }}</p>
          <p class="text-muted-sm">组内 Agent 读取共享黑板的只读视图并行推理，输出串行合并，互不覆盖。</p>
        </template>

        <template v-else-if="selected.kind === 'agent' && selected.agent">
          <p class="detail-sub">{{ purpose(selected.agent) }}</p>
          <div class="detail-grid">
            <div><em>LLM 轮次</em><strong>{{ selected.agent.llmCalls ?? (selected.agent.rounds || []).filter(r => r.type !== 'tool').length }}</strong></div>
            <div><em>工具调用</em><strong>{{ selected.agent.toolCalls ?? (selected.agent.rounds || []).filter(r => r.type === 'tool').length }}</strong></div>
            <div v-if="selected.agent.confidence != null"><em>置信度</em><strong>{{ Math.round((selected.agent.confidence || 0) * 100) }}%</strong></div>
            <div><em>状态</em><strong>{{ selected.agent.status }}</strong></div>
          </div>
          <div class="detail-block" v-if="selected.agent.output">
            <div class="detail-block-head"><span>Agent 结论</span><button @click="copyText(selected.agent.output)">复制</button></div>
            <pre>{{ selected.agent.output }}</pre>
          </div>
        </template>

        <template v-else-if="selected.round">
          <p class="detail-sub" v-if="selected.agent">{{ displayName(selected.agent.name) }} · {{ selected.round.model || (selected.kind === 'tool' ? '确定性工具' : 'DeepSeek') }}</p>
          <div class="detail-grid">
            <div v-if="selected.round.tokens"><em>Tokens</em><strong>{{ selected.round.tokens }}</strong></div>
            <div v-if="selected.durationMs"><em>耗时</em><strong>{{ fmtMs(selected.durationMs) }}</strong></div>
            <div v-if="selected.tool?.origin || selected.tool?.category"><em>来源</em><strong>{{ selected.tool?.origin || selected.tool?.category }}</strong></div>
            <div v-if="selected.tool?.mcpServer"><em>MCP</em><strong>{{ selected.tool.mcpServer }}</strong></div>
            <div v-if="selected.tool?.skillId"><em>Skill</em><strong>{{ selected.tool.skillId }}{{ selected.tool.skillVersion ? '@' + selected.tool.skillVersion : '' }}</strong></div>
          </div>
          <div class="detail-block warning" v-if="selected.round.error">
            <div class="detail-block-head"><span>错误</span></div>
            <pre>{{ selected.round.error }}</pre>
          </div>
          <template v-for="(tool, ti) in (selected.round.toolCalls || [])" :key="ti">
            <div class="detail-block" v-if="tool.input">
              <div class="detail-block-head"><span>{{ tool.name }} · 入参</span><button @click="copyText(tool.input)">复制</button></div>
              <pre>{{ pretty(tool.input) }}</pre>
            </div>
            <div class="detail-block" v-if="tool.output || tool.result">
              <div class="detail-block-head"><span>{{ tool.name }} · 返回（{{ tool.status }}，{{ fmtMs(tool.durationMs) || '-' }}）</span><button @click="copyText(tool.output || tool.result)">复制</button></div>
              <pre>{{ pretty(tool.output || tool.result) }}</pre>
            </div>
          </template>
          <div class="detail-block" v-if="selected.round.input">
            <div class="detail-block-head"><span>输入上下文</span><button @click="copyText(selected.round.input)">复制</button></div>
            <pre>{{ selected.round.input }}</pre>
          </div>
          <div class="detail-block" v-if="selected.round.decisionText || selected.round.output || selected.round.finalOutput">
            <div class="detail-block-head"><span>模型输出</span><button @click="copyText(selected.round.finalOutput || selected.round.output || selected.round.decisionText)">复制</button></div>
            <pre>{{ selected.round.finalOutput || selected.round.output || selected.round.decisionText }}</pre>
          </div>
          <p class="text-muted-sm" v-if="!selected.round.error && !(selected.round.toolCalls || []).length && !selected.round.input && !selected.round.output && !selected.round.decisionText && !selected.round.finalOutput">
            本轮为 LLM 生成（{{ selected.round.tokens || '-' }} tokens）；完整入出参可在 Langfuse 深度调试中查看。
          </p>
        </template>
      </div>
    </div>

    <div v-else class="trace-empty">
      <p>暂无执行记录</p>
      <p class="text-muted-sm">运行开始后，Coordinator 规划的 Agent 流水线（含并行分组）会在这里实时展开。</p>
    </div>
  </div>
</template>

<style scoped>
.trace-view { display: flex; flex-direction: column; gap: var(--space-lg); }

.trace-summary {
  display: flex; flex-wrap: wrap; gap: var(--space-lg);
  padding: 10px 14px; border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md); background: var(--color-surface);
}
.trace-summary-item { display: flex; align-items: baseline; gap: 6px; font-size: 13px; font-weight: 600; }
.trace-summary-item em { font-style: normal; font-size: 11px; color: var(--color-text-secondary); font-weight: 500; }

.trace-history {
  border: 1px solid #fde68a; border-radius: var(--radius-md);
  background: #fffbeb; padding: 8px 12px;
}
.history-toggle {
  border: none; background: none; cursor: pointer; font-size: 13px; font-weight: 600;
  display: inline-flex; align-items: center; gap: 8px; color: #92400e; padding: 0;
}
.history-tag {
  padding: 1px 6px; border-radius: 4px; background: #fef3c7; color: #b45309;
  font-size: 10px; font-weight: 700;
}
.history-list { margin-top: 8px; display: flex; flex-direction: column; gap: 8px; }
.history-card {
  padding: 8px 10px; border-radius: 8px; background: #fff; border: 1px solid #fde68a;
}
.history-card-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; }
.history-badge {
  padding: 1px 6px; border-radius: 4px; background: #fee2e2; color: #b91c1c;
  font-size: 10px; font-weight: 700;
}
.history-code { font-family: ui-monospace, monospace; color: #9a3412; }
.history-retry { font-size: 10px; color: #047857; font-weight: 600; }
.history-msg { margin: 6px 0 4px; font-size: 12px; color: #78350f; }
.history-meta { display: flex; flex-wrap: wrap; gap: 10px; font-size: 11px; color: #a16207; }

.trace-filters { display: flex; flex-wrap: wrap; gap: 6px; }
.filter-chip {
  border: 1px solid var(--color-border-light); background: var(--color-surface);
  border-radius: 999px; padding: 3px 10px; font-size: 11px; font-weight: 600;
  cursor: pointer; color: var(--color-text-secondary);
}
.filter-chip.active { border-color: var(--color-primary); color: var(--color-primary); background: var(--color-primary-light, #eef2ff); }

.trace-route {
  padding: 14px; border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md); background: var(--color-surface);
}
.trace-route-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
.trace-route-head h4 { margin: 0; font-size: 13px; }
.route-mode { padding: 2px 8px; border-radius: 999px; background: var(--color-primary-light, #eef2ff); color: var(--color-primary); font-size: 11px; font-weight: 600; }
.route-meta { font-size: 11px; color: var(--color-text-secondary); }
.route-plan { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.route-arrow { color: var(--color-text-secondary); font-size: 12px; }
.route-group { display: inline-flex; align-items: center; gap: 4px; }
.route-group.parallel { padding: 3px 6px; border: 1px dashed var(--color-border); border-radius: 8px; background: var(--color-bg); }
.route-parallel-sep { color: var(--color-primary); font-size: 11px; font-weight: 700; }
.route-chip { padding: 2px 8px; border-radius: 999px; background: var(--color-bg); border: 1px solid var(--color-border-light); font-size: 12px; }
.route-reason { margin: 8px 0 0; padding: 6px 10px; border-left: 3px solid var(--color-primary); background: var(--color-bg); color: var(--color-text-secondary); font-size: 12px; border-radius: 0 6px 6px 0; }
.route-memory { margin-top: 8px; display: flex; flex-direction: column; gap: 4px; }
.route-memory-item { display: flex; align-items: baseline; gap: 8px; font-size: 12px; }
.memory-type { flex-shrink: 0; padding: 1px 6px; border-radius: 4px; background: #f0e7ff; color: #7c3aed; font-size: 10px; font-weight: 600; }
.memory-content { color: var(--color-text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.memory-conf { flex-shrink: 0; color: var(--color-text-secondary); font-size: 11px; }

.trace-panes {
  display: grid; grid-template-columns: minmax(300px, 5fr) minmax(320px, 7fr);
  gap: var(--space-lg); align-items: start;
}
@media (max-width: 1100px) { .trace-panes { grid-template-columns: 1fr; } }

.span-tree {
  border: 1px solid var(--color-border-light); border-radius: var(--radius-md);
  background: var(--color-surface); overflow: hidden; max-height: 640px; overflow-y: auto;
}
.span-row {
  display: flex; align-items: center; gap: 6px; padding: 7px 10px;
  cursor: pointer; border-bottom: 1px solid var(--color-border-light);
  font-size: 13px; transition: background .12s;
}
.span-row:last-child { border-bottom: none; }
.span-row:hover { background: var(--color-bg); }
.span-row.selected { background: var(--color-primary-light, #eef2ff); }
.span-row.depth-1 { padding-left: 26px; }
.span-row.depth-2 { padding-left: 46px; font-size: 12px; }
.span-caret { border: none; background: none; cursor: pointer; padding: 0; width: 14px; color: var(--color-text-secondary); font-size: 11px; }
.span-caret-placeholder { width: 14px; }
.span-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.span-dot.is-success { background: var(--color-success); }
.span-dot.is-failed { background: var(--color-danger); }
.span-dot.is-running { background: var(--color-warning); animation: span-pulse 1.2s infinite; }
@keyframes span-pulse { 50% { opacity: .35; } }
.span-kind { flex-shrink: 0; padding: 1px 5px; border-radius: 4px; font-size: 9px; font-weight: 700; }
.badge-llm { background: #dbeafe; color: #1d4ed8; }
.badge-mcp { background: #dcfce7; color: #15803d; }
.badge-skill { background: #f3e8ff; color: #7e22ce; }
.badge-sandbox { background: #ffedd5; color: #c2410c; }
.badge-builtin { background: #ecfdf5; color: #047857; }
.badge-retrieval { background: #eef2ff; color: #4338ca; }
.badge-external { background: #f1f5f9; color: #475569; }
.badge-internal { background: #ecfdf5; color: #047857; }
.badge-gateway { background: #f1f5f9; color: #475569; }
.badge-internal { background: #f1f5f9; color: #475569; }
.badge-gateway { background: #e0e7ff; color: #4338ca; }
.badge-tool { background: #fef3c7; color: #b45309; }
.badge-group { background: #ecfdf5; color: #047857; }
.badge-agent { background: #e0e7ff; color: #4338ca; }
.span-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }
.depth-0 .span-label { font-weight: 600; }
.span-parallel { flex-shrink: 0; font-size: 10px; color: var(--color-primary); font-weight: 600; }
.span-tokens { flex-shrink: 0; margin-left: auto; font-size: 11px; color: var(--color-text-secondary); }
.span-duration { flex-shrink: 0; font-size: 11px; color: var(--color-text-secondary); font-variant-numeric: tabular-nums; min-width: 44px; text-align: right; }
.span-tokens + .span-duration { margin-left: 6px; }
.span-row:not(:has(.span-tokens)) .span-duration { margin-left: auto; }

.span-detail {
  border: 1px solid var(--color-border-light); border-radius: var(--radius-md);
  background: var(--color-surface); padding: 14px; max-height: 640px; overflow-y: auto;
  position: sticky; top: 12px;
}
.detail-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.detail-head h4 { margin: 0; font-size: 14px; flex: 1; }
.detail-duration { font-size: 12px; color: var(--color-text-secondary); }
.detail-sub { margin: 0 0 10px; font-size: 12px; color: var(--color-text-secondary); }
.detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(90px, 1fr)); gap: 8px; margin-bottom: 12px; }
.detail-grid > div { padding: 8px; border-radius: 8px; background: var(--color-bg); display: flex; flex-direction: column; gap: 2px; }
.detail-grid em { font-style: normal; font-size: 10px; color: var(--color-text-secondary); }
.detail-grid strong { font-size: 14px; }
.detail-block { margin-bottom: 10px; border: 1px solid var(--color-border-light); border-radius: 8px; overflow: hidden; }
.detail-block.warning { border-color: var(--color-danger); }
.detail-block-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 5px 10px; background: var(--color-bg); font-size: 11px; font-weight: 600;
  color: var(--color-text-secondary);
}
.detail-block-head button { border: none; background: none; color: var(--color-primary); cursor: pointer; font-size: 11px; }
.detail-block pre {
  margin: 0; padding: 10px; font-size: 11px; line-height: 1.5;
  white-space: pre-wrap; word-break: break-word; max-height: 260px; overflow-y: auto;
}
.text-muted-sm { font-size: 12px; color: var(--color-text-secondary); }
.trace-empty { padding: var(--space-2xl); text-align: center; color: var(--color-text-secondary); }
</style>
