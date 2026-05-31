export type CallKind = 'skill' | 'tool' | 'mcp' | 'rag' | 'sandbox' | 'llm';

export interface TraceEventLike {
  traceId: string;
  spanId: string;
  parentSpanId?: string;
  agentRole: string;
  eventType: string;
  title: string;
  detail: string;
  status: string;
  durationMs: number;
  tokenCost?: number;
  timestamp?: string;
  dagGroupId?: string;
  laneId?: string;
  stepKind?: string;
  viewType?: string;
  businessLabel?: string;
  evidenceSummary?: string;
  interviewHints?: string[];
  developerLabel?: string;
  skillName?: string;
  promptPreview?: string;
  inputSummary?: string;
  outputSummary?: string;
  toolCalls?: string[];
  mcpCalls?: string[];
  sandboxSummary?: string;
  llmInvocationId?: string;
  nodeId?: string;
  dependsOn?: string[];
  phase?: string;
  expected?: boolean;
  sortOrder?: number;
  fullPrompt?: string;
  fullInput?: string;
  fullOutput?: string;
  sequence?: number;
  roundIndex?: number;
  roundRole?: string;
  callKind?: string;
  callName?: string;
  parentAgentSpanId?: string;
  parentRoundId?: string;
  ioJson?: string;
}

export interface ParsedCallRow {
  name?: string;
  server?: string;
  status?: string;
  durationMs?: number;
  inputSummary?: string;
  outputSummary?: string;
  transport?: string;
  operation?: string;
  raw?: string;
}

export interface AgentCallBase {
  id: string;
  kind: CallKind;
  name: string;
  status: string;
  durationMs?: number;
  inputSummary?: string;
  outputSummary?: string;
  spanId?: string;
}

export interface SkillCall extends AgentCallBase {
  kind: 'skill';
  promptTemplate?: string;
  resolvedPrompt?: string;
  simulated?: boolean;
}

export interface ToolCall extends AgentCallBase {
  kind: 'tool';
  toolName: string;
  operation?: string;
}

export interface McpCall extends AgentCallBase {
  kind: 'mcp';
  server: string;
  tool: string;
  transport?: string;
  mcpLikeHttp?: boolean;
}

export interface RagCall extends AgentCallBase {
  kind: 'rag';
  strategy?: string;
}

export interface SandboxCall extends AgentCallBase {
  kind: 'sandbox';
  summary?: string;
}

export interface LlmCall extends AgentCallBase {
  kind: 'llm';
  llmInvocationId?: string;
  model?: string;
}

export type AgentCall = SkillCall | ToolCall | McpCall | RagCall | SandboxCall | LlmCall;

export interface AgentTurn {
  id: string;
  agentRole: string;
  roundIndex: number;
  status: string;
  startedAt?: string;
  durationMs?: number;
  title: string;
  stepKind?: string;
  inputContext?: string;
  promptPreview?: string;
  llmInvocationId?: string;
  thoughtSummary?: string;
  calls: AgentCall[];
  observation?: string;
  output?: string;
  simulated?: boolean;
}

export interface ExecutionNode {
  id: string;
  nodeId: string;
  agentId: string;
  label: string;
  responsibility?: string;
  subLabel?: string;
  status: string;
  durationMs: number;
  duration: string;
  phaseCol: number;
  phaseLabel: string;
  spanId?: string;
  laneId?: string;
  isParallelGroup?: boolean;
  parallelLanes?: ExecutionNode[];
  nodeType: string;
  skillCount: number;
  toolCount: number;
  mcpCount: number;
  skills: SkillCall[];
  toolCalls: ToolCall[];
  mcpCalls: McpCall[];
  ragCalls: RagCall[];
  llmCalls: LlmCall[];
  tokenCost?: number;
  llmInvocationId?: string;
  fullPrompt?: string;
  fullInput?: string;
  fullOutput?: string;
  evidenceSummary?: string;
  interviewHints?: string[];
  dependsOn?: string[];
  expected?: boolean;
  callKind?: string;
  sequence?: number;
}

const PHASE_COL: Record<string, number> = {
  bootstrap: 0,
  parse: 1,
  match: 2,
  parallel: 3,
  evidence: 4,
  evaluate: 5,
  quality: 6,
  report: 7,
};

const PHASE_LABEL: Record<string, string> = {
  bootstrap: '任务创建',
  parse: '简历解析',
  match: '岗位匹配',
  parallel: '并行评估',
  evidence: '证据准备',
  evaluate: 'AI 评估',
  quality: '质量校验',
  report: '报告生成',
};

const SKIP_STEP_KINDS = new Set(['dag_start', 'EXPECTED_NODE', 'task_failed']);

export function parseToolCallRow(raw: string): ParsedCallRow {
  if (!raw) return { raw: '' };
  try {
    const parsed = JSON.parse(raw);
    return {
      name: parsed.name || parsed.tool || parsed.skill,
      server: parsed.server || parsed.mcpServer,
      status: parsed.status,
      durationMs: parsed.durationMs ?? parsed.duration,
      inputSummary: parsed.input || parsed.inputSummary || parsed.args,
      outputSummary: parsed.output || parsed.outputSummary || parsed.result,
      transport: parsed.transport,
      operation: parsed.operation || parsed.method,
      raw,
    };
  } catch {
    return { name: raw.slice(0, 48), raw };
  }
}

export function sortTraceEvents(events: TraceEventLike[]): TraceEventLike[] {
  return [...events].sort((a, b) => {
    const so = (a.sortOrder ?? 999) - (b.sortOrder ?? 999);
    if (so !== 0) return so;
    const seq = (a.sequence ?? Number.MAX_SAFE_INTEGER) - (b.sequence ?? Number.MAX_SAFE_INTEGER);
    if (seq !== 0) return seq;
    return (a.timestamp || '').localeCompare(b.timestamp || '');
  });
}

function inferNodeType(step: TraceEventLike): string {
  const kind = step.callKind || step.stepKind || '';
  if (kind === 'skill' || step.skillName || step.stepKind === 'skill_eval') return 'agent';
  if (kind === 'mcp' || step.stepKind === 'external_enrichment') return 'mcp';
  if (kind === 'tool' || step.stepKind === 'quality_check') return 'tool';
  if (kind === 'rag' || (step.stepKind || '').includes('rag') || step.stepKind === 'jd_match' || step.stepKind === 'historical_match') return 'retrieval';
  if (kind === 'llm' || step.stepKind === 'llm_complete' || step.stepKind === 'graph_extraction' || step.stepKind === 'jd_requirements') return 'agent';
  if (step.stepKind === 'task_create' || step.stepKind === 'upload_parse') return 'trigger';
  if (step.stepKind === 'resume_parse') return 'parser';
  if (step.stepKind === 'report_generate') return 'output';
  return 'agent';
}

function formatDuration(ms?: number): string {
  if (!ms || ms <= 0) return '-';
  if (ms < 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function buildCallsFromStep(step: TraceEventLike): AgentCall[] {
  const calls: AgentCall[] = [];
  if (step.skillName) {
    calls.push({
      id: `${step.spanId}-skill`,
      kind: 'skill',
      name: step.skillName,
      status: step.status,
      durationMs: step.durationMs,
      inputSummary: step.inputSummary,
      outputSummary: step.outputSummary,
      spanId: step.spanId,
      promptTemplate: step.promptPreview,
      resolvedPrompt: step.fullPrompt,
      simulated: step.stepKind === 'skill_eval' && !step.llmInvocationId,
    });
  }
  for (const tc of step.toolCalls || []) {
    const row = parseToolCallRow(tc);
    calls.push({
      id: `${step.spanId}-tool-${row.name || calls.length}`,
      kind: 'tool',
      name: row.name || 'tool',
      toolName: row.name || 'tool',
      operation: row.operation,
      status: row.status || step.status,
      durationMs: row.durationMs ?? step.durationMs,
      inputSummary: typeof row.inputSummary === 'string' ? row.inputSummary : JSON.stringify(row.inputSummary ?? ''),
      outputSummary: typeof row.outputSummary === 'string' ? row.outputSummary : JSON.stringify(row.outputSummary ?? ''),
      spanId: step.spanId,
    });
  }
  for (const mc of step.mcpCalls || []) {
    const row = parseToolCallRow(mc);
    const server = row.server || row.name || 'mcp';
    calls.push({
      id: `${step.spanId}-mcp-${server}`,
      kind: 'mcp',
      name: server,
      server,
      tool: row.name || server,
      status: row.status || step.status,
      durationMs: row.durationMs ?? step.durationMs,
      inputSummary: typeof row.inputSummary === 'string' ? row.inputSummary : JSON.stringify(row.inputSummary ?? ''),
      outputSummary: typeof row.outputSummary === 'string' ? row.outputSummary : JSON.stringify(row.outputSummary ?? ''),
      transport: row.transport || 'MCP-like HTTP',
      mcpLikeHttp: true,
      spanId: step.spanId,
    });
  }
  if (step.callKind === 'rag' || (step.stepKind || '').includes('rag') || step.stepKind === 'jd_match' || step.stepKind === 'historical_match') {
    if (!calls.some((c) => c.kind === 'rag')) {
      calls.push({
        id: `${step.spanId}-rag`,
        kind: 'rag',
        name: step.callName || step.stepKind || 'rag',
        status: step.status,
        durationMs: step.durationMs,
        inputSummary: step.inputSummary,
        outputSummary: step.outputSummary || step.evidenceSummary,
        spanId: step.spanId,
        strategy: step.detail,
      });
    }
  }
  if (step.sandboxSummary) {
    calls.push({
      id: `${step.spanId}-sandbox`,
      kind: 'sandbox',
      name: 'sandbox',
      status: step.status,
      durationMs: step.durationMs,
      summary: step.sandboxSummary,
      spanId: step.spanId,
    });
  }
  if (step.llmInvocationId) {
    calls.push({
      id: step.llmInvocationId,
      kind: 'llm',
      name: step.callName || 'LLM',
      status: step.status,
      durationMs: step.durationMs,
      inputSummary: step.promptPreview || step.inputSummary,
      outputSummary: step.outputSummary,
      llmInvocationId: step.llmInvocationId,
      spanId: step.spanId,
    });
  }
  return calls;
}

export function buildExecutionGraph(
  events: TraceEventLike[],
  options?: { hideDev?: boolean; resolveAgentId?: (step: TraceEventLike) => string },
): ExecutionNode[] {
  const sorted = sortTraceEvents(events).filter((e) => !e.expected && !SKIP_STEP_KINDS.has(e.stepKind || '') && e.eventType !== 'EXPECTED_NODE');
  const nodes: ExecutionNode[] = [];
  const parallelLanes: ExecutionNode[] = [];

  for (const step of sorted) {
    if (options?.hideDev && step.viewType === 'DEV') continue;
    const calls = buildCallsFromStep(step);
    const skills = calls.filter((c): c is SkillCall => c.kind === 'skill');
    const toolCalls = calls.filter((c): c is ToolCall => c.kind === 'tool');
    const mcpCalls = calls.filter((c): c is McpCall => c.kind === 'mcp');
    const ragCalls = calls.filter((c): c is RagCall => c.kind === 'rag');
    const llmCalls = calls.filter((c): c is LlmCall => c.kind === 'llm');
    const phase = step.phase || 'bootstrap';
    const phaseCol = PHASE_COL[phase] ?? 0;
    const agentId = options?.resolveAgentId?.(step) || step.agentRole;
    const node: ExecutionNode = {
      id: step.spanId || step.nodeId || `${step.stepKind}-${step.sequence}`,
      nodeId: step.nodeId || step.stepKind || step.spanId,
      agentId,
      label: step.businessLabel || step.title || step.agentRole,
      responsibility: step.developerLabel || step.detail,
      subLabel: step.evidenceSummary || step.outputSummary || step.detail,
      status: step.status,
      durationMs: step.durationMs || 0,
      duration: formatDuration(step.durationMs),
      phaseCol,
      phaseLabel: PHASE_LABEL[phase] || phase,
      spanId: step.spanId,
      laneId: step.laneId,
      nodeType: inferNodeType(step),
      skillCount: skills.length,
      toolCount: toolCalls.length,
      mcpCount: mcpCalls.length,
      skills,
      toolCalls,
      mcpCalls,
      ragCalls,
      llmCalls,
      tokenCost: step.tokenCost,
      llmInvocationId: step.llmInvocationId,
      fullPrompt: step.fullPrompt,
      fullInput: step.fullInput,
      fullOutput: step.fullOutput,
      evidenceSummary: step.evidenceSummary,
      interviewHints: step.interviewHints,
      dependsOn: step.dependsOn,
      callKind: step.callKind,
      sequence: step.sequence,
    };
    if (step.stepKind === 'skill_eval' && step.laneId) {
      parallelLanes.push(node);
    } else {
      nodes.push(node);
    }
  }

  if (parallelLanes.length) {
    const groupStatus = parallelLanes.some((n) => n.status === 'FAILED') ? 'FAILED'
      : parallelLanes.some((n) => n.status === 'RUNNING') ? 'RUNNING'
      : parallelLanes.every((n) => n.status === 'SUCCESS') ? 'SUCCESS'
      : parallelLanes.some((n) => n.status === 'WARNING') ? 'WARNING' : 'PENDING';
    nodes.push({
      id: 'parallel-eval-group',
      nodeId: 'parallel-eval-group',
      agentId: 'parallel-eval-group',
      label: '并行评估 Agent 组',
      responsibility: '技术 / 项目 / 风险三泳道并行评估',
      subLabel: `${parallelLanes.filter((n) => n.status === 'SUCCESS').length}/${parallelLanes.length} 泳道完成`,
      status: groupStatus,
      durationMs: parallelLanes.reduce((s, n) => s + n.durationMs, 0),
      duration: formatDuration(parallelLanes.reduce((s, n) => s + n.durationMs, 0)),
      phaseCol: PHASE_COL.parallel,
      phaseLabel: PHASE_LABEL.parallel,
      isParallelGroup: true,
      parallelLanes,
      nodeType: 'agent',
      skillCount: parallelLanes.reduce((s, n) => s + n.skillCount, 0),
      toolCount: parallelLanes.reduce((s, n) => s + n.toolCount, 0),
      mcpCount: 0,
      skills: parallelLanes.flatMap((n) => n.skills),
      toolCalls: parallelLanes.flatMap((n) => n.toolCalls),
      mcpCalls: [],
      ragCalls: [],
      llmCalls: parallelLanes.flatMap((n) => n.llmCalls),
    });
  }

  return nodes.sort((a, b) => a.phaseCol - b.phaseCol || (a.sequence ?? 0) - (b.sequence ?? 0));
}

export function buildAgentTurns(events: TraceEventLike[], agentId: string, resolveAgentId: (step: TraceEventLike) => string): AgentTurn[] {
  const sorted = sortTraceEvents(events).filter((e) => !e.expected && resolveAgentId(e) === agentId);
  return sorted.map((step, index) => {
    const calls = buildCallsFromStep(step);
    return {
      id: step.spanId || `turn-${index + 1}`,
      agentRole: step.agentRole,
      roundIndex: step.roundIndex ?? step.sequence ?? index + 1,
      status: step.status,
      startedAt: step.timestamp,
      durationMs: step.durationMs,
      title: step.title || step.businessLabel || step.agentRole,
      stepKind: step.stepKind,
      inputContext: step.fullInput || step.inputSummary,
      promptPreview: step.promptPreview || step.fullPrompt,
      llmInvocationId: step.llmInvocationId,
      thoughtSummary: step.detail,
      calls,
      observation: step.evidenceSummary || step.outputSummary,
      output: step.fullOutput || step.outputSummary,
      simulated: step.stepKind === 'skill_eval' && !step.llmInvocationId,
    };
  });
}

export function groupExecutionStages(nodes: ExecutionNode[]): Array<{ key: string; label: string; nodes: ExecutionNode[]; isParallel?: boolean }> {
  const cols = [...new Set(nodes.map((n) => n.phaseCol))].sort((a, b) => a - b);
  return cols.map((col) => {
    const stageNodes = nodes.filter((n) => n.phaseCol === col);
    const label = stageNodes[0]?.phaseLabel || `阶段 ${col + 1}`;
    return {
      key: `phase-${col}`,
      label,
      nodes: stageNodes,
      isParallel: label.includes('并行'),
    };
  });
}

export function previewText(value?: string, max = 160): string {
  if (!value) return '—';
  const normalized = value.replace(/\s+/g, ' ').trim();
  return normalized.length <= max ? normalized : `${normalized.slice(0, max)}…`;
}
