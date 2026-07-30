<script setup lang="ts">
import { computed, ref, watch } from 'vue';

/**
 * 两栏 Trace 视图：
 * 左栏是紧凑 span 树（规划 → 并行组 → Agent → 轮次/工具），
 * 右栏是选中 span 的详情面板（目标 / 判断 / 入参出参 / tokens / 耗时）。
 */

type ToolCategory = 'mcp' | 'skill' | 'memory' | 'builtin' | 'retrieval' | 'llm' | 'tool' | 'external';

interface ToolCallView {
  id?: string;
  toolCallId?: string;
  toolName?: string;
  name?: string;
  status?: string;
  durationMs?: number;
  occurredAt?: string;
  startedAt?: string;
  endedAt?: string;
  timeSource?: string;
  input?: string;
  result?: string;
  output?: string;
  category?: string;
  type?: string;
  source?: string;
  origin?: string;
  executionBackend?: string;
  mcpServer?: string;
  skillId?: string;
  skillVersion?: string;
  lifecycleStage?: string;
  disclosureState?: string;
  proposalSource?: string;
  proposalOccurredAt?: string;
  modelGeneratedArguments?: string;
  modelToolName?: string;
  modelName?: string;
  contextRole?: string;
  description?: string;
  inputSchema?: unknown;
  schema?: unknown;
  lifecycle?: string[];
  eventId?: string;
  eventType?: string;
  parentRoundId?: string;
  callIndex?: number;
  title?: string;
  reason?: string;
  content?: string;
  taxonomy?: string;
  memoryType?: string;
  memoryId?: string;
  namespace?: string;
  score?: number;
}

interface ContextEventView extends ToolCallView {
  category?: 'memory' | 'skill' | 'retrieval' | string;
}

interface RoundView {
  id?: string;
  roundId?: string;
  callIndex?: number;
  parentRoundId?: string;
  roundRole?: string;
  purpose?: string;
  reportSection?: string;
  contextRole?: string;
  contextAttachedAt?: string;
  memoryCount?: number;
  skillCount?: number;
  toolCatalogCount?: number;
  contextTokenEstimate?: number;
  promptHash?: string;
  roundNum?: number;
  type?: string;
  category?: string;
  title?: string;
  tokens?: number;
  durationMs?: number;
  occurredAt?: string;
  startedAt?: string;
  endedAt?: string;
  timestamp?: string;
  timeSource?: string;
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
  contextEvents?: ContextEventView[];
  toolCatalogRefs?: ContextEventView[];
  memoryId?: string;
  memoryType?: string;
  taxonomy?: string;
  namespace?: string;
  reason?: string;
  score?: number;
}

interface AgentView {
  name?: string;
  role?: string;
  phase?: number;
  status?: string;
  durationMs?: number;
  occurredAt?: string;
  startedAt?: string;
  endedAt?: string;
  llmCalls?: number;
  toolCalls?: number;
  confidence?: number;
  output?: string;
  rounds?: RoundView[];
  deterministicSteps?: ToolCallView[];
  executionMode?: string;
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
  attemptNo?: number;
  historicalAttempts?: HistoricalAttempt[];
}

const props = defineProps<{
  tree: ExecTree | null;
  agentNames: Record<string, string>;
  agentPurposes: Record<string, string>;
}>();

interface SpanRow {
  id: string;
  depth: number;
  kind: 'group' | 'agent' | 'round' | 'deterministic' | 'context' | 'tool';
  label: string;
  sub?: string;
  status?: string;
  durationMs?: number;
  occurredAt?: string;
  endedAt?: string;
  tokens?: number;
  parallel?: boolean;
  badge?: ToolCategory;
  agent?: AgentView;
  round?: RoundView;
  tool?: ToolCallView;
  context?: ContextEventView;
  parentId?: string;
  causalRole?: 'input' | 'decision' | 'result' | 'deterministic';
}

const FILTERS: Array<{ id: ToolCategory | 'all'; label: string }> = [
  { id: 'all', label: '全部' },
  { id: 'llm', label: 'LLM' },
  { id: 'builtin', label: 'BUILTIN' },
  { id: 'mcp', label: 'MCP' },
  { id: 'skill', label: 'SKILL' },
  { id: 'memory', label: 'MEMORY' },
  { id: 'retrieval', label: 'RETRIEVAL' },
];

const availableFilters = computed(() => {
  const badgesInData = new Set<string>();
  for (const row of spans.value) {
    if (row.badge) badgesInData.add(row.badge);
  }
  return FILTERS.filter(f => f.id === 'all' || badgesInData.has(f.id));
});

const selectedId = ref<string>('');
const collapsed = ref<Set<string>>(new Set());
const kindFilter = ref<ToolCategory | 'all'>('all');
const historyOpen = ref(false);

const agents = computed<AgentView[]>(() =>
  (props.tree?.executionTree || []).map(normalizeAgentCausality));
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
  if (value === 'gateway') return 'external';
  if (value === 'mcp' || value === 'skill' || value === 'memory' || value === 'builtin'
      || value === 'retrieval' || value === 'llm' || value === 'external') {
    return value as ToolCategory;
  }
  const name = toolName || '';
  if (name.startsWith('mcp_') || name.includes('.')) return 'mcp';
  if (name === 'execute_skill' || name === 'list_skills'
      || name === 'load_skill' || name === 'read_skill_resource'
      || name.startsWith('skill:')) return 'skill';
  if (name === 'memory_search' || name === 'memory_write') return 'memory';
  if (['parse_resume', 'check_timeline', 'calculate_jd_coverage', 'locate_evidence',
    'verify_report_evidence', 'resume_lint', 'validate_report_schema'].includes(name)) {
    return 'builtin';
  }
  if (['knowledge_search', 'resume_semantic_search', 'jd_match_search'].includes(name)) {
    return 'retrieval';
  }
  if (name === 'external_profile_lookup') return 'external';
  if (value === 'internal') return 'builtin';
  return 'tool';
}

function badgeLabel(badge?: ToolCategory): string {
  if (!badge) return 'TOOL';
  return badge.toUpperCase();
}

function isGenerationRound(round: RoundView): boolean {
  const type = (round.type || '').toLowerCase();
  if (['generation', 'llm', 'llm_generation', 'llm_complete'].includes(type)) return true;
  return (round.category || '').toLowerCase() === 'llm';
}

function reportSectionLabel(round: RoundView): string {
  const purpose = (round.purpose || '').toLowerCase();
  const model = (round.model || '').toLowerCase().includes('pro') ? 'Pro' : 'Flash';
  if (purpose === 'report_score') return `并行报告 · 评分与结论（${model}）`;
  if (purpose === 'report_risk') return `并行报告 · 风险核验（${model}）`;
  if (purpose === 'report_question') return `并行报告 · 面试追问（${model}）`;
  return '';
}

function roundLabel(round: RoundView, index: number): string {
  return reportSectionLabel(round)
    || round.title
    || `LLM 第 ${round.callIndex ?? round.roundNum ?? index + 1} 轮`;
}

function roundKey(round: RoundView, fallbackIndex: number): string {
  return round.roundId || round.id || round.parentRoundId
    || (round.callIndex != null ? `call-${round.callIndex}` : `round-${round.roundNum ?? fallbackIndex + 1}`);
}

function eventTime(event: { proposalOccurredAt?: string; startedAt?: string; occurredAt?: string; endedAt?: string }): string | undefined {
  return event.proposalOccurredAt || event.startedAt || event.occurredAt || event.endedAt;
}

function isContextLifecycle(round: RoundView): boolean {
  const category = (round.category || round.type || '').toLowerCase();
  if (category === 'memory') return true;
  if (category === 'tool_catalog' || category === 'context'
    || round.contextEvents?.length || round.toolCatalogRefs?.length) return true;
  if (category !== 'skill') return false;
  const tool = round.toolCalls?.[0];
  const stage = String(tool?.lifecycleStage || '').toUpperCase();
  const eventType = String(tool?.eventType || '').toLowerCase();
  // Catalog/selection/skips are audit facts, not model input. Only content
  // that was actually loaded/applied may be shown as an LLM attachment.
  return ['RESOURCE_LOADED', 'LOADED', 'APPLIED'].includes(stage)
    || ['skill.loaded', 'skill.applied'].includes(eventType)
    || /已加载|已应用|渐进加载/.test(round.title || '');
}

function contextFromLegacyRound(round: RoundView, index: number): ContextEventView {
  const tool = round.toolCalls?.[0] || {};
  const category = normalizeCategory(
    round.category || round.type || tool.category,
    tool.name,
  );
  return {
    ...tool,
    eventId: tool.eventId || round.id || `legacy-context-${index}`,
    eventType: tool.eventType || (category === 'memory' ? 'memory.used' : 'skill.applied'),
    category,
    title: round.title || tool.title,
    occurredAt: round.occurredAt || round.startedAt || round.timestamp || tool.occurredAt,
    startedAt: round.startedAt || tool.startedAt,
    endedAt: round.endedAt || tool.endedAt,
    memoryId: round.memoryId || tool.memoryId,
    memoryType: round.memoryType || round.taxonomy || tool.memoryType || tool.taxonomy,
    taxonomy: round.taxonomy || round.memoryType || tool.taxonomy || tool.memoryType,
    namespace: round.namespace || tool.namespace,
    reason: round.reason || tool.reason,
    score: round.score ?? tool.score,
    content: tool.content || tool.result || tool.output,
  };
}

function contextIdentity(event: ContextEventView): string {
  const category = (event.category || event.type || event.origin || event.source || '').toLowerCase();
  const contextId = event.memoryId || event.id;
  const catalogName = event.modelToolName || event.modelName || event.toolName || event.name;
  if ((category === 'memory' || event.memoryId) && contextId) {
    return `memory|${contextId}`;
  }
  if ((category === 'skill' || event.skillId) && (event.skillId || event.name)) {
    return `skill|${event.skillId || event.name}`;
  }
  if (category === 'mcp' || category === 'tool_catalog'
    || event.eventType === 'tool.catalog.attached' || event.modelName || event.toolName) {
    return `catalog|${event.mcpServer || ''}|${catalogName || event.eventId || ''}`;
  }
  return event.eventId || [event.eventType, event.category, event.name,
    event.lifecycleStage, eventTime(event)].filter(Boolean).join('|');
}

function toolIdentity(tool: ToolCallView): string {
  return tool.toolCallId || [tool.name, tool.category, tool.proposalOccurredAt,
    tool.startedAt, tool.endedAt].filter(Boolean).join('|');
}

function dedupeContexts(events: ContextEventView[]): ContextEventView[] {
  const merged = new Map<string, ContextEventView>();
  events.forEach((event, index) => {
    // Keep anonymous legacy placeholders in the raw audit feed, but never
    // render them as if they were prompt material or an executable sibling.
    const meaningful = Boolean(
      event.memoryId || event.memoryType || event.taxonomy
      || event.skillId || event.name || event.title
      || event.toolName || event.modelName || event.modelToolName);
    if (!meaningful) return;
    const key = contextIdentity(event) || `context-${index}`;
    const previous = merged.get(key);
    if (!previous) {
      merged.set(key, event);
      return;
    }
    merged.set(key, {
      ...previous,
      ...event,
      lifecycle: [...new Set([...(previous.lifecycle || []), ...(event.lifecycle || [])])],
    });
  });
  return [...merged.values()];
}

function dedupeTools(tools: ToolCallView[]): ToolCallView[] {
  const byId = new Map<string, ToolCallView>();
  tools.forEach((tool, index) => {
    const key = toolIdentity(tool) || `tool-${index}`;
    const previous = byId.get(key);
    if (!previous) {
      byId.set(key, tool);
      return;
    }
    const merged: ToolCallView = { ...previous, ...tool };
    (Object.keys(previous) as Array<keyof ToolCallView>).forEach((field) => {
      const next = merged[field];
      if (next == null || next === '') merged[field] = previous[field] as never;
    });
    merged.lifecycle = [...new Set([...(previous.lifecycle || []), ...(tool.lifecycle || [])])];
    byId.set(key, merged);
  });
  return [...byId.values()];
}

/**
 * Normalise both the causal API and traces created before roundId existed.
 * The rendered unit is always an LLM turn: prompt attachments belong to its
 * input, and native tool calls belong to the turn that proposed them.
 */
function normalizeAgentCausality(agent: AgentView): AgentView {
  const rawRounds = agent.rounds || [];
  const generationEntries = rawRounds
    .map((round, index) => ({ round, index }))
    .filter(({ round }) => isGenerationRound(round));

  const normalizedRounds = generationEntries.map(({ round, index }) => ({
    ...round,
    roundId: roundKey(round, index),
    contextEvents: dedupeContexts([
      ...(round.contextEvents || []),
      ...(round.toolCatalogRefs || []),
    ]),
    toolCalls: dedupeTools(round.toolCalls || []),
  }));

  const byRoundId = new Map<string, RoundView>();
  normalizedRounds.forEach((round, index) => {
    [round.roundId, round.id, round.parentRoundId, `round-${round.roundNum ?? index + 1}`]
      .filter((key): key is string => !!key)
      .forEach((key) => byRoundId.set(key, round));
  });

  const deterministicSteps = [...(agent.deterministicSteps || [])];
  rawRounds.forEach((legacy, legacyIndex) => {
    if (isGenerationRound(legacy)) return;
    const explicitParent = legacy.parentRoundId
      || legacy.toolCalls?.find((tool) => tool.parentRoundId)?.parentRoundId;
    const explicitTarget = explicitParent ? byRoundId.get(explicitParent) : undefined;

    if (isContextLifecycle(legacy)) {
      const contexts = legacy.contextEvents?.length
        ? legacy.contextEvents
        : legacy.toolCatalogRefs?.length
          ? legacy.toolCatalogRefs
          : [contextFromLegacyRound(legacy, legacyIndex)];
      const nextEntry = generationEntries.find(({ index }) => index > legacyIndex);
      const target = explicitTarget || (nextEntry
        ? normalizedRounds[generationEntries.indexOf(nextEntry)]
        : undefined);
      if (target) target.contextEvents = dedupeContexts([...(target.contextEvents || []), ...contexts]);
      return;
    }

    const tools = legacy.toolCalls || [];
    const legacyCategory = (legacy.category || legacy.type || '').toLowerCase();
    if (legacyCategory === 'skill'
      && !tools.some((tool) => ['load_skill', 'read_skill_resource'].includes(tool.name || ''))) {
      // Legacy bridge exposed audit lifecycle events as pseudo tool rounds.
      // They are not executions and must not reappear as orphan Skill rows.
      return;
    }
    if (!tools.length) return;
    const previousEntry = [...generationEntries].reverse().find(({ index }) => index < legacyIndex);
    const target = explicitTarget || (previousEntry
      ? normalizedRounds[generationEntries.indexOf(previousEntry)]
      : undefined);
    if (target) {
      target.toolCalls = dedupeTools([...(target.toolCalls || []), ...tools]);
      return;
    }

    // Older traces had no parent ids. Preserve only real deterministic
    // pre-processing; never invent a model decision for orphan MCP/Skill rows.
    tools.forEach((tool) => {
      const category = normalizeCategory(tool.category || legacy.category, tool.name);
      if (category === 'builtin' || category === 'retrieval') {
        deterministicSteps.push({ ...tool, category });
      }
    });
  });

  return {
    ...agent,
    rounds: normalizedRounds,
    deterministicSteps: dedupeTools(deterministicSteps),
  };
}

function contextBadge(context: ContextEventView): ToolCategory {
  const category = (context.category || context.type || '').toLowerCase();
  if (category === 'tool_catalog') {
    return (context.mcpServer || context.origin === 'mcp') ? 'mcp' : 'builtin';
  }
  return normalizeCategory(context.category || context.type || context.origin || context.source, context.name);
}

function contextLabel(context: ContextEventView): string {
  const badge = contextBadge(context);
  if (badge === 'memory') {
    const memoryId = context.memoryId ? ` · ${String(context.memoryId).slice(0, 16)}` : '';
    return `MODEL_INPUT · 记忆 ${context.memoryType || context.taxonomy || ''}${memoryId}`.trim();
  }
  if (badge === 'skill') {
    const stage = String(context.lifecycleStage || '').toUpperCase();
    const state = stage.includes('APPLIED') ? '已应用' : '已加载';
    return `Skill ${state} · ${context.skillId || context.name || ''}`.trim();
  }
  if (context.eventType === 'tool.catalog.attached' || context.category === 'tool_catalog'
    || context.toolName || context.modelName) {
    return `MODEL_INPUT · 工具描述 ${context.modelToolName || context.modelName || context.toolName || context.name || ''}`.trim();
  }
  return `MODEL_INPUT · ${context.title || context.name || '上下文'}`;
}

function firstTime(events: Array<{ proposalOccurredAt?: string; startedAt?: string; occurredAt?: string; endedAt?: string }>): string | undefined {
  return events.map(eventTime).filter((value): value is string => !!value).sort()[0];
}

function lastTime(events: Array<{ proposalOccurredAt?: string; startedAt?: string; occurredAt?: string; endedAt?: string }>): string | undefined {
  return events.map((event) => event.endedAt || event.occurredAt || event.startedAt)
    .filter((value): value is string => !!value).sort().at(-1);
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
      occurredAt: group.map((a) => a.startedAt || a.occurredAt).filter(Boolean).sort()[0],
      endedAt: group.map((a) => a.endedAt).filter(Boolean).sort().at(-1),
      parallel,
    });
    if (collapsed.value.has(groupId)) return;
    group.forEach((agent, ai) => {
      const agentId = `${groupId}-a-${ai}`;
      rows.push({
        id: agentId,
        parentId: groupId,
        depth: 1,
        kind: 'agent',
        label: displayName(agent.name),
        sub: purpose(agent),
        status: agent.status,
        durationMs: agent.durationMs,
        occurredAt: agent.startedAt || agent.occurredAt,
        endedAt: agent.endedAt,
        agent,
      });
      if (collapsed.value.has(agentId)) return;
      const deterministic = agent.deterministicSteps || [];
      if (deterministic.length) {
        const deterministicId = `${agentId}-deterministic`;
        rows.push({
          id: deterministicId,
          parentId: agentId,
          depth: 2,
          kind: 'deterministic',
          label: '确定性预处理',
          sub: `${deterministic.length} 个真实工具步骤（非 LLM 决策）`,
          status: deterministic.some((tool) => tool.status === 'FAILED') ? 'FAILED' : 'SUCCESS',
          durationMs: deterministic.reduce((sum, tool) => sum + (tool.durationMs || 0), 0),
          occurredAt: firstTime(deterministic),
          endedAt: lastTime(deterministic),
          badge: 'builtin',
          agent,
          causalRole: 'deterministic',
        });
        if (!collapsed.value.has(deterministicId)) {
          deterministic.forEach((tool, ti) => {
            const badge = normalizeCategory(tool.category || tool.origin, tool.name);
            rows.push({
              id: `${deterministicId}-t-${ti}`,
              parentId: deterministicId,
              depth: 3,
              kind: 'tool',
              label: `预处理 · ${tool.name || '工具'}`,
              status: tool.status,
              durationMs: tool.durationMs,
              occurredAt: tool.startedAt || tool.occurredAt,
              endedAt: tool.endedAt,
              badge,
              agent,
              tool,
              causalRole: 'deterministic',
            });
          });
        }
      }
      (agent.rounds || []).forEach((round, ri) => {
        const roundId = `${agentId}-r-${ri}`;
        const contexts = round.contextEvents || [];
        const tools = round.toolCalls || [];
        rows.push({
          id: roundId,
          parentId: agentId,
          depth: 2,
          kind: 'round',
          label: roundLabel(round, ri),
          sub: reportSectionLabel(round)
            ? `三路同时启动 · 输入附件 ${contexts.length}`
            : `输入附件 ${contexts.length} · 模型工具调用 ${tools.length}`,
          status: round.error ? 'FAILED'
            : tools.some((tool) => tool.status === 'FAILED') ? 'FAILED' : 'SUCCESS',
          durationMs: round.durationMs,
          occurredAt: round.startedAt || round.occurredAt || round.timestamp
            || firstTime(contexts) || firstTime(tools),
          endedAt: round.endedAt,
          tokens: round.tokens,
          parallel: Boolean(reportSectionLabel(round)),
          badge: 'llm',
          agent,
          round,
          causalRole: 'decision',
        });
        if (collapsed.value.has(roundId)) return;
        contexts.forEach((context, ci) => {
          rows.push({
            id: `${roundId}-c-${ci}`,
            parentId: roundId,
            depth: 3,
            kind: 'context',
            label: contextLabel(context),
            sub: context.reason,
            status: context.status || 'SUCCESS',
            occurredAt: context.startedAt || context.occurredAt,
            endedAt: context.endedAt,
            badge: contextBadge(context),
            agent,
            round,
            context,
            causalRole: 'input',
          });
        });
        tools.forEach((tool, ti) => {
          const badge = normalizeCategory(tool.category || tool.origin, tool.name);
          rows.push({
            id: `${roundId}-t-${ti}`,
            parentId: roundId,
            depth: 3,
            kind: 'tool',
            label: `模型调用 · ${tool.name || '工具'}`,
            status: tool.status,
            durationMs: tool.durationMs,
            occurredAt: tool.proposalOccurredAt || tool.startedAt || tool.occurredAt,
            endedAt: tool.endedAt,
            badge,
            agent,
            round,
            tool,
            causalRole: 'result',
          });
        });
      });
    });
  });
  return rows;
});

const filteredSpans = computed(() => {
  // Keep the default audit readable: prompt attachments remain available in
  // the selected LLM detail and via MEMORY/SKILL/MCP filters, but should not
  // occupy one tree row per token of model context.
  if (kindFilter.value === 'all') {
    return spans.value.filter((row) => row.kind !== 'context' || row.badge === 'skill');
  }
  const byId = new Map(spans.value.map((row) => [row.id, row]));
  const visible = new Set<string>();
  const includeWithAncestors = (row: SpanRow) => {
    let current: SpanRow | undefined = row;
    while (current) {
      visible.add(current.id);
      current = current.parentId ? byId.get(current.parentId) : undefined;
    }
  };
  for (const row of spans.value) {
    const matchesLlm = kindFilter.value === 'llm' && row.kind === 'round';
    const matchesChild = kindFilter.value !== 'llm'
      && (row.kind === 'context' || row.kind === 'tool')
      && row.badge === kindFilter.value;
    if (matchesLlm || matchesChild) includeWithAncestors(row);
  }
  return spans.value.filter((row) => visible.has(row.id));
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
  const appliedSkills = new Set<string>();
  let mcpCalls = 0;
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
  spans.value.forEach((row) => {
    if (row.kind === 'tool' && row.badge === 'mcp') mcpCalls += 1;
    if (row.kind === 'context' && row.badge === 'skill' && row.context?.skillId) {
      appliedSkills.add(`${row.agent?.name || ''}:${row.context.skillId}`);
    }
  });
  return { duration, tokens, llmCalls, toolCalls,
    mcpCalls, skillsApplied: appliedSkills.size };
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

function fmtTime(value?: string): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const rendered = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date).replaceAll('/', '-');
  return `${rendered}.${String(date.getMilliseconds()).padStart(3, '0')}`;
}

function fmtClock(value?: string): string {
  const rendered = fmtTime(value);
  return rendered.includes(' ') ? rendered.split(' ').at(-1) || rendered : rendered;
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
      <span class="trace-summary-item"><em>Skill 已应用</em>{{ totals.skillsApplied }}</span>
      <span class="trace-summary-item"><em>MCP 真实调用</em>{{ totals.mcpCalls }}</span>
      <span class="trace-summary-item"><em>Tokens</em>{{ totals.tokens || '-' }}</span>
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
        <div class="route-memory-caption">Coordinator 规划阶段记忆摘要（非独立执行 span；实际注入见对应 LLM 轮次）</div>
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
        v-for="f in availableFilters"
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
          :aria-level="row.depth + 1"
          :aria-expanded="(row.kind === 'group' || row.kind === 'agent' || row.kind === 'deterministic' || row.kind === 'round') ? (!collapsed.has(row.id)) : undefined"
          :data-parent-id="row.parentId || undefined"
          :data-causal-role="row.causalRole || undefined"
          tabindex="0"
          @click="selectedId = row.id"
          @keydown.enter="selectedId = row.id"
        >
          <button
            v-if="row.kind === 'group'
              || (row.kind === 'agent' && (((row.agent?.rounds?.length || 0) + (row.agent?.deterministicSteps?.length || 0)) > 0))
              || row.kind === 'deterministic'
              || (row.kind === 'round' && (((row.round?.contextEvents?.length || 0) + (row.round?.toolCalls?.length || 0)) > 0))"
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
            v-else-if="row.kind === 'agent' && row.agent?.executionMode === 'deterministic'"
            class="span-kind badge-builtin"
          >工具处理</span>
          <span
            v-else-if="row.kind === 'agent'"
            class="span-kind badge-agent"
          >AGENT</span>
          <span
            v-else-if="row.kind === 'tool' || row.kind === 'round' || row.kind === 'context' || row.kind === 'deterministic'"
            class="span-kind"
            :class="`badge-${row.badge || (row.kind === 'round' ? 'llm' : 'builtin')}`"
          >{{ badgeLabel(row.badge || (row.kind === 'round' ? 'llm' : 'builtin')) }}</span>
          <span class="span-label">{{ row.kind === 'round' ? `MODEL_INPUT → ${row.label}` : row.kind === 'tool' && row.causalRole === 'result' ? `LLM → TOOL → RESULT · ${row.label.replace(/^模型调用 · /, '')}` : row.label }}</span>
          <span class="span-causal-summary" v-if="row.sub && (row.kind === 'round' || row.kind === 'deterministic')">{{ row.sub }}</span>
          <span class="span-parallel" v-if="row.parallel">∥ 并行</span>
          <span class="span-tokens" v-if="row.tokens">{{ row.tokens }} tok</span>
          <time
            v-if="row.occurredAt"
            class="span-time"
            :datetime="row.occurredAt"
            :title="fmtTime(row.occurredAt)"
          >{{ fmtClock(row.occurredAt) }}</time>
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

        <template v-else-if="selected.kind === 'deterministic' && selected.agent">
          <p class="detail-sub">{{ displayName(selected.agent.name) }} · 不经过 LLM 决策的固定预处理链</p>
          <div class="causal-banner deterministic-banner">
            <strong>确定性输入处理</strong>
            <span>只展示真实执行的 builtin / RAG；不会伪装成模型工具调用。</span>
          </div>
          <div class="attachment-list">
            <div v-for="(tool, ti) in (selected.agent.deterministicSteps || [])" :key="tool.toolCallId || ti" class="attachment-card">
              <div class="attachment-head">
                <span class="span-kind" :class="`badge-${normalizeCategory(tool.category || tool.origin, tool.name)}`">{{ badgeLabel(normalizeCategory(tool.category || tool.origin, tool.name)) }}</span>
                <strong>{{ tool.name || '工具' }}</strong>
                <time v-if="tool.startedAt || tool.occurredAt">{{ fmtTime(tool.startedAt || tool.occurredAt) }}</time>
                <span>{{ fmtMs(tool.durationMs) }}</span>
              </div>
            </div>
          </div>
        </template>

        <template v-else-if="selected.kind === 'context' && selected.context && selected.round">
          <p class="detail-sub">{{ displayName(selected.agent?.name) }} · LLM 第 {{ selected.round.callIndex ?? selected.round.roundNum ?? '-' }} 轮输入附件</p>
          <div class="causal-banner">
            <strong>MODEL_INPUT</strong>
            <span>该内容随本轮 prompt 一起提交给模型，不是一次独立执行。</span>
          </div>
          <div class="detail-grid">
            <div><em>类别</em><strong>{{ badgeLabel(contextBadge(selected.context)) }}</strong></div>
            <div v-if="selected.context.contextRole"><em>Prompt 角色</em><strong>{{ selected.context.contextRole }}</strong></div>
            <div v-if="selected.context.source || selected.context.origin"><em>来源</em><strong>{{ selected.context.source || selected.context.origin }}</strong></div>
            <div v-if="selected.context.memoryType || selected.context.taxonomy"><em>记忆类型</em><strong>{{ selected.context.memoryType || selected.context.taxonomy }}</strong></div>
            <div v-if="selected.context.skillId"><em>Skill</em><strong>{{ selected.context.skillId }}</strong></div>
            <div v-if="selected.context.mcpServer"><em>MCP Server</em><strong>{{ selected.context.mcpServer }}</strong></div>
            <div v-if="selected.context.score != null"><em>召回分数</em><strong>{{ Number(selected.context.score).toFixed(3) }}</strong></div>
            <div v-if="selected.context.occurredAt || selected.context.startedAt"><em>附加时间</em><strong class="detail-time">{{ fmtTime(selected.context.startedAt || selected.context.occurredAt) }}</strong></div>
          </div>
          <div class="detail-block" v-if="selected.context.reason">
            <div class="detail-block-head"><span>选入原因</span></div>
            <pre>{{ selected.context.reason }}</pre>
          </div>
          <div class="detail-block" v-if="selected.context.content || selected.context.description || selected.context.input || selected.context.result || selected.context.output">
            <div class="detail-block-head"><span>注入内容</span><button @click="copyText(selected.context.content || selected.context.description || selected.context.input || selected.context.result || selected.context.output)">复制</button></div>
            <pre>{{ pretty(selected.context.content || selected.context.description || selected.context.input || selected.context.result || selected.context.output) }}</pre>
          </div>
          <div class="detail-block" v-if="selected.context.inputSchema || selected.context.schema">
            <div class="detail-block-head"><span>注入模型的参数 Schema</span></div>
            <pre>{{ JSON.stringify(selected.context.inputSchema || selected.context.schema, null, 2) }}</pre>
          </div>
        </template>

        <template v-else-if="selected.kind === 'tool' && selected.tool">
          <p class="detail-sub" v-if="selected.causalRole === 'result'">
            {{ displayName(selected.agent?.name) }} · 由 LLM 第 {{ selected.round?.callIndex ?? selected.round?.roundNum ?? '-' }} 轮提出
          </p>
          <p class="detail-sub" v-else>{{ displayName(selected.agent?.name) }} · 确定性预处理</p>
          <div class="causal-banner" :class="{ 'deterministic-banner': selected.causalRole === 'deterministic' }">
            <strong>{{ selected.causalRole === 'result' ? 'LLM → TOOL → RESULT' : 'DETERMINISTIC TOOL' }}</strong>
            <span>{{ selected.causalRole === 'result' ? '同一 toolCallId 合并模型提议、执行开始和真实返回。' : '该步骤没有伪造 LLM 决策。' }}</span>
          </div>
          <div class="detail-grid">
            <div><em>来源</em><strong>{{ selected.tool.origin || selected.tool.category || '-' }}</strong></div>
            <div v-if="selected.tool.mcpServer"><em>MCP</em><strong>{{ selected.tool.mcpServer }}</strong></div>
            <div v-if="selected.tool.skillId"><em>Skill</em><strong>{{ selected.tool.skillId }}{{ selected.tool.skillVersion ? '@' + selected.tool.skillVersion : '' }}</strong></div>
            <div v-if="selected.tool.proposalSource"><em>调用决策</em><strong>{{ selected.tool.proposalSource === 'LLM_NATIVE' ? 'LLM 原生 tool_call' : selected.tool.proposalSource }}</strong></div>
            <div v-if="selected.tool.proposalOccurredAt"><em>模型提议时间</em><strong class="detail-time">{{ fmtTime(selected.tool.proposalOccurredAt) }}</strong></div>
            <div v-if="selected.tool.startedAt || selected.tool.occurredAt"><em>执行开始</em><strong class="detail-time">{{ fmtTime(selected.tool.startedAt || selected.tool.occurredAt) }}</strong></div>
            <div v-if="selected.tool.endedAt"><em>执行结束</em><strong class="detail-time">{{ fmtTime(selected.tool.endedAt) }}</strong></div>
          </div>
          <div class="detail-lifecycle" v-if="selected.tool.lifecycle?.length">
            <span v-for="stage in selected.tool.lifecycle" :key="stage">{{ stage }}</span>
          </div>
          <div class="detail-block" v-if="selected.tool.modelGeneratedArguments || selected.tool.input">
            <div class="detail-block-head"><span>{{ selected.tool.name }} · {{ selected.tool.proposalSource === 'LLM_NATIVE' ? '模型生成参数' : '入参' }}</span><button @click="copyText(selected.tool.modelGeneratedArguments || selected.tool.input)">复制</button></div>
            <pre>{{ pretty(selected.tool.modelGeneratedArguments || selected.tool.input) }}</pre>
          </div>
          <div class="detail-block" v-if="selected.tool.output || selected.tool.result">
            <div class="detail-block-head"><span>{{ selected.tool.name }} · 真实返回（{{ selected.tool.status }}，{{ fmtMs(selected.tool.durationMs) || '-' }}）</span><button @click="copyText(selected.tool.output || selected.tool.result)">复制</button></div>
            <pre>{{ pretty(selected.tool.output || selected.tool.result) }}</pre>
          </div>
        </template>

        <template v-else-if="selected.kind === 'round' && selected.round">
          <p class="detail-sub" v-if="selected.agent">
            {{ displayName(selected.agent.name) }} · {{ reportSectionLabel(selected.round) || selected.round.purpose || '模型推理' }} · {{ selected.round.model || 'DeepSeek' }}
          </p>
          <div class="causal-banner" v-if="reportSectionLabel(selected.round)">
            <strong>并行报告生成</strong>
            <span>评分、风险、追问三路同时启动；这里是一条独立小节，不是前一轮的串行重试。</span>
          </div>
          <div class="causal-flow">
            <span class="causal-node input-node">输入附件 {{ selected.round.contextEvents?.length || 0 }}</span>
            <span class="causal-arrow">→</span>
            <span class="causal-node llm-node">{{ reportSectionLabel(selected.round) || `LLM 第 ${selected.round.callIndex ?? selected.round.roundNum ?? '-'} 轮` }}</span>
            <template v-if="selected.round.toolCalls?.length">
              <span class="causal-arrow">→</span>
              <span class="causal-node tool-node">模型工具调用 {{ selected.round.toolCalls.length }}</span>
              <span class="causal-arrow">→</span>
              <span class="causal-node result-node">真实返回</span>
            </template>
          </div>
          <div class="detail-grid">
            <div v-if="selected.round.tokens"><em>Tokens</em><strong>{{ selected.round.tokens }}</strong></div>
            <div v-if="selected.round.contextRole"><em>Prompt 角色</em><strong>{{ selected.round.contextRole }}</strong></div>
            <div v-if="selected.round.contextAttachedAt"><em>上下文附加时间</em><strong class="detail-time">{{ fmtTime(selected.round.contextAttachedAt) }}</strong></div>
            <div v-if="selected.round.contextTokenEstimate"><em>上下文估算</em><strong>{{ selected.round.contextTokenEstimate }} tok</strong></div>
            <div v-if="selected.durationMs"><em>耗时</em><strong>{{ fmtMs(selected.durationMs) }}</strong></div>
            <div v-if="selected.occurredAt"><em>开始时间（北京时间）</em><strong class="detail-time" :title="selected.occurredAt">{{ fmtTime(selected.occurredAt) }}</strong></div>
            <div v-if="selected.endedAt"><em>结束时间（北京时间）</em><strong class="detail-time" :title="selected.endedAt">{{ fmtTime(selected.endedAt) }}</strong></div>
            <div v-if="selected.tool?.origin || selected.tool?.category"><em>来源</em><strong>{{ selected.tool?.origin || selected.tool?.category }}</strong></div>
            <div v-if="selected.tool?.mcpServer"><em>MCP</em><strong>{{ selected.tool.mcpServer }}</strong></div>
            <div v-if="selected.tool?.skillId"><em>Skill</em><strong>{{ selected.tool.skillId }}{{ selected.tool.skillVersion ? '@' + selected.tool.skillVersion : '' }}</strong></div>
            <div v-if="selected.tool?.proposalSource"><em>调用决策</em><strong>{{ selected.tool.proposalSource === 'LLM_NATIVE' ? 'LLM 原生 tool_call' : selected.tool.proposalSource }}</strong></div>
            <div v-if="selected.tool?.proposalOccurredAt"><em>模型提议时间</em><strong class="detail-time" :title="selected.tool.proposalOccurredAt">{{ fmtTime(selected.tool.proposalOccurredAt) }}</strong></div>
            <div v-if="selected.tool?.lifecycleStage"><em>Skill 阶段</em><strong>{{ selected.tool.lifecycleStage }}</strong></div>
            <div v-if="selected.tool?.disclosureState"><em>加载状态</em><strong>{{ selected.tool.disclosureState }}</strong></div>
          </div>
          <div class="detail-block warning" v-if="selected.round.error">
            <div class="detail-block-head"><span>错误</span></div>
            <pre>{{ selected.round.error }}</pre>
          </div>
          <div class="detail-section" v-if="selected.round.contextEvents?.length">
            <h5>本轮 prompt 输入附件</h5>
            <div class="attachment-list">
              <div v-for="(context, ci) in selected.round.contextEvents" :key="contextIdentity(context) || ci" class="attachment-card">
                <div class="attachment-head">
                  <span class="span-kind" :class="`badge-${contextBadge(context)}`">{{ badgeLabel(contextBadge(context)) }}</span>
                  <strong>{{ contextLabel(context).replace(/^输入 · /, '') }}</strong>
                  <time v-if="context.startedAt || context.occurredAt">{{ fmtTime(context.startedAt || context.occurredAt) }}</time>
                </div>
                <p v-if="context.reason">{{ context.reason }}</p>
              </div>
            </div>
          </div>
          <div class="detail-section" v-if="selected.round.toolCalls?.length">
            <h5>本轮模型工具调用</h5>
          <template v-for="(tool, ti) in (selected.round.toolCalls || [])" :key="ti">
            <div class="detail-lifecycle" v-if="tool.lifecycle?.length">
              <span v-for="stage in tool.lifecycle" :key="stage">{{ stage }}</span>
            </div>
            <div class="detail-call-time" v-if="tool.startedAt || tool.occurredAt || tool.endedAt">
              <span>调用时间</span>
              <time
                v-if="tool.startedAt || tool.occurredAt"
                :datetime="tool.startedAt || tool.occurredAt"
                :title="tool.startedAt || tool.occurredAt"
              >{{ fmtTime(tool.startedAt || tool.occurredAt) }}</time>
              <span v-if="tool.endedAt">→</span>
              <time v-if="tool.endedAt" :datetime="tool.endedAt" :title="tool.endedAt">{{ fmtTime(tool.endedAt) }}</time>
            </div>
            <div class="detail-block" v-if="tool.input">
              <div class="detail-block-head"><span>{{ tool.name }} · 入参</span><button @click="copyText(tool.input)">复制</button></div>
              <pre>{{ pretty(tool.input) }}</pre>
            </div>
            <div class="detail-block" v-if="tool.modelGeneratedArguments && tool.proposalSource === 'LLM_NATIVE'">
              <div class="detail-block-head"><span>{{ tool.modelToolName || tool.name }} · 模型生成参数</span><button @click="copyText(tool.modelGeneratedArguments)">复制</button></div>
              <pre>{{ pretty(tool.modelGeneratedArguments) }}</pre>
            </div>
            <div class="detail-block" v-if="tool.output || tool.result">
              <div class="detail-block-head"><span>{{ tool.name }} · 返回（{{ tool.status }}，{{ fmtMs(tool.durationMs) || '-' }}）</span><button @click="copyText(tool.output || tool.result)">复制</button></div>
              <pre>{{ pretty(tool.output || tool.result) }}</pre>
            </div>
          </template>
          </div>
          <div class="detail-block" v-if="selected.round.input">
            <div class="detail-block-head"><span>输入上下文</span><button @click="copyText(selected.round.input)">复制</button></div>
            <pre>{{ selected.round.input }}</pre>
          </div>
          <div class="detail-block" v-if="selected.round.decisionText || selected.round.output || selected.round.finalOutput">
            <div class="detail-block-head"><span>模型输出</span><button @click="copyText(selected.round.finalOutput || selected.round.output || selected.round.decisionText)">复制</button></div>
            <pre>{{ selected.round.finalOutput || selected.round.output || selected.round.decisionText }}</pre>
          </div>
          <p class="text-muted-sm" v-if="!selected.round.error && !(selected.round.toolCalls || []).length && !selected.round.input && !selected.round.output && !selected.round.decisionText && !selected.round.finalOutput">
            本轮为 LLM 生成（{{ selected.round.tokens || '-' }} tokens），暂无可展示的输入/输出摘要。
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
.route-memory-caption { font-size: 10px; color: var(--color-text-secondary); }
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
.span-row.depth-3 {
  padding-left: 68px; font-size: 11px; background: color-mix(in srgb, var(--color-bg) 65%, transparent);
  border-left: 3px solid color-mix(in srgb, var(--color-primary) 28%, transparent);
}
.span-row.depth-3 .span-label { font-weight: 400; }
.span-row[data-causal-role="input"] .span-label { color: var(--color-text-secondary); }
.span-row[data-causal-role="result"] .span-label { color: var(--color-text); }
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
.badge-memory { background: #fef3c7; color: #92400e; }
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
.span-causal-summary {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--color-text-secondary); font-size: 9px; font-weight: 400;
}
.span-parallel { flex-shrink: 0; font-size: 10px; color: var(--color-primary); font-weight: 600; }
.span-tokens { flex-shrink: 0; margin-left: auto; font-size: 11px; color: var(--color-text-secondary); }
.span-time { flex-shrink: 0; margin-left: auto; font-size: 10px; color: var(--color-text-secondary); font-variant-numeric: tabular-nums; }
.span-tokens + .span-time { margin-left: 6px; }
.span-duration { flex-shrink: 0; font-size: 11px; color: var(--color-text-secondary); font-variant-numeric: tabular-nums; min-width: 44px; text-align: right; }
.span-tokens + .span-duration { margin-left: 6px; }
.span-row:not(:has(.span-tokens)):not(:has(.span-time)) .span-duration { margin-left: auto; }

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
.detail-grid .detail-time { font-size: 11px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.causal-banner {
  display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px;
  margin: 0 0 10px; padding: 8px 10px; border-radius: 8px;
  border: 1px solid #bfdbfe; background: #eff6ff; color: #1e40af;
}
.causal-banner strong { font-size: 10px; letter-spacing: .03em; }
.causal-banner span { font-size: 11px; color: #475569; }
.deterministic-banner { border-color: #a7f3d0; background: #ecfdf5; color: #047857; }
.causal-flow {
  display: flex; align-items: center; flex-wrap: wrap; gap: 5px; margin: 0 0 12px;
  padding: 8px; border-radius: 8px; background: var(--color-bg);
}
.causal-node { padding: 3px 7px; border-radius: 999px; font-size: 10px; font-weight: 600; }
.input-node { background: #fef3c7; color: #92400e; }
.llm-node { background: #dbeafe; color: #1d4ed8; }
.tool-node { background: #f3e8ff; color: #7e22ce; }
.result-node { background: #dcfce7; color: #15803d; }
.causal-arrow { color: var(--color-text-secondary); font-size: 10px; }
.detail-section { margin: 0 0 12px; }
.detail-section h5 { margin: 0 0 7px; font-size: 11px; color: var(--color-text-secondary); }
.attachment-list { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
.attachment-card {
  padding: 7px 9px; border: 1px solid var(--color-border-light);
  border-radius: 8px; background: var(--color-bg);
}
.attachment-head { display: flex; align-items: center; gap: 7px; min-width: 0; }
.attachment-head strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }
.attachment-head time, .attachment-head > span:last-child {
  margin-left: auto; flex-shrink: 0; color: var(--color-text-secondary); font-size: 9px;
  font-variant-numeric: tabular-nums;
}
.attachment-card p { margin: 5px 0 0; font-size: 10px; color: var(--color-text-secondary); }
.detail-call-time {
  display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin: 0 0 8px;
  color: var(--color-text-secondary); font-size: 11px; font-variant-numeric: tabular-nums;
}
.detail-lifecycle {
  display: flex; align-items: center; flex-wrap: wrap; gap: 5px; margin: 0 0 8px;
}
.detail-lifecycle span {
  padding: 2px 6px; border-radius: 999px; background: #eef2ff;
  color: #4338ca; font-size: 9px; font-weight: 700;
}
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
