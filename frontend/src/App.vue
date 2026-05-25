<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';

interface TaskResponse {
  id: number;
  traceId: string;
  fileName: string;
  jobCategory: string;
  executionMode: string;
  status: string;
  overallScore: number;
  recommendation: string;
  durationMs: number;
  tokenCost: number;
  strengths: string[];
  risks: string[];
  interviewQuestions: string[];
  summary?: string;
  resumeText?: string;
  resumeFileUrl?: string;
  resumeFileType?: string;
  matchedJdTitle?: string;
  jdMatchScore?: number;
  topJdMatches?: Array<{
    jdId: string;
    title: string;
    category: string;
    score: number;
    matchReasons?: string[];
    gaps?: string[];
    interviewChecks?: string[];
  }>;
}

interface TraceEvent {
  traceId: string;
  spanId: string;
  agentRole: string;
  eventType: string;
  title: string;
  detail: string;
  status: string;
  durationMs: number;
  tokenCost: number;
  timestamp: string;
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
}

interface Metrics {
  totalTasks: number;
  runningTasks: number;
  successTasks: number;
  failedTasks: number;
  averageDurationMs: number;
  averageScore: number;
  totalTokenCost: number;
  modeDurationMs: Record<string, number>;
  agentDurationMs: Record<string, number>;
}

interface GraphNode { id: string; label: string; type: string; score: number; }
interface GraphEdge { from: string; to: string; label: string; confidence: number; }

interface FeedbackResponse {
  id: number;
  traceId: string;
  ratingScore: number;
  feedbackType: string;
  humanComment: string;
  fixAction: string;
  reviewer: string;
  createTime: string;
}

interface JobProfile {
  id: string;
  title: string;
  department: string;
  level: string;
  category: string;
  description: string;
  createdAt: string;
}

const JOBS_STORAGE_KEY = 'resumai.jobs.v2';

const defaultJobs: JobProfile[] = [
  {
    id: 'job-java-agent',
    title: '高级 Java / AI Agent 平台工程师',
    department: 'AI Platform',
    level: 'Senior',
    category: 'TECH',
    description: '招聘 Java 21 / Spring Boot 3 / AI Agent 平台方向高级后端工程师，要求熟悉 RAG、Trace 可观测、Docker 部署、线上问题排查和端到端交付。',
    createdAt: new Date().toISOString()
  },
  {
    id: 'job-product-ai',
    title: 'AI 产品经理',
    department: 'Product',
    level: 'Mid-Senior',
    category: 'PRODUCT',
    description: '负责 AI 招聘产品从需求洞察、PRD、数据指标到上线迭代，要求理解 LLM/RAG 基础能力、B 端工作流和招聘业务。',
    createdAt: new Date().toISOString()
  }
];

type ViewName = 'dashboard' | 'positions' | 'candidates' | 'detail' | 'analytics';
type DetailTab = 'resume' | 'report' | 'process' | 'graph' | 'feedback';

const appView = ref<ViewName>('dashboard');
const detailTab = ref<DetailTab>('report');
const loading = ref(false);
const refreshing = ref(false);
const showUploadModal = ref(false);
const autoMatchJd = ref(true);
const tasks = ref<TaskResponse[]>([]);
const traces = ref<TraceEvent[]>([]);
const metrics = ref<Metrics | null>(null);
const graphNodes = ref<GraphNode[]>([]);
const graphEdges = ref<GraphEdge[]>([]);
const feedbacks = ref<FeedbackResponse[]>([]);
const activeTraceId = ref('');
const feedbackText = ref('');
const errorMessage = ref('');
const successMessage = ref('');
const healthStatus = ref('...');
const queuedFiles = ref<File[]>([]);
const pastedResume = ref('');
const candidateSearch = ref('');
const statusFilter = ref('ALL');
const selectedJobId = ref('');
const jobs = ref<JobProfile[]>(loadJobs());
const jobDraft = reactive<JobProfile>({ ...jobs.value[0] });

let eventSource: EventSource | null = null;
const dagViewMode = ref<'hr' | 'dev'>('hr');
const expandedDagNode = ref<string>('');
const resumeViewMode = ref<'pdf' | 'text'>('pdf');
const pollTimers = new Map<string, number>();

const activeTask = computed(() => tasks.value.find((t) => t.traceId === activeTraceId.value) ?? null);
const selectedJob = computed(() => jobs.value.find((j) => j.id === selectedJobId.value) ?? jobs.value[0]);
const runningTasks = computed(() => tasks.value.filter((t) => t.status === 'RUNNING'));
const completedTasks = computed(() => tasks.value.filter((t) => t.status === 'SUCCESS'));

const filteredCandidates = computed(() => {
  let list = tasks.value;
  if (statusFilter.value !== 'ALL') {
    list = list.filter((t) => t.status === statusFilter.value);
  }
  if (candidateSearch.value.trim()) {
    const q = candidateSearch.value.toLowerCase();
    list = list.filter((t) => t.fileName.toLowerCase().includes(q) || t.jobCategory.toLowerCase().includes(q) || (t.recommendation || '').toLowerCase().includes(q));
  }
  return list;
});


const traceSteps = computed(() => {
  const dagOnly = traces.value.filter(e =>
    e.stepKind || e.dagGroupId || e.eventType === 'DAG_START' || e.eventType === 'REPORT_READY'
  );
  const source = dagOnly.length ? dagOnly : traces.value;
  return source.map((e, i) => ({
    ...e,
    stageNo: i + 1,
    stageLabel: traceStageLabel(e),
    statusLabel: eventStatusText(e.status),
    evidence: traceEvidence(e),
  }));
});

const isLegacyTrace = computed(() =>
  traces.value.length > 0 && !traces.value.some(e => e.stepKind || e.dagGroupId)
);

const jdMatchCards = computed(() => activeTask.value?.topJdMatches ?? []);

const jdMatchSuccessRate = computed(() => {
  const withMatch = tasks.value.filter(t => t.matchedJdTitle && (t.jdMatchScore || 0) > 0).length;
  if (!tasks.value.length) return 0;
  return Math.round(withMatch / tasks.value.length * 100);
});

const pendingReviewTasks = computed(() =>
  tasks.value.filter(t => t.status === 'SUCCESS' && !(t.recommendation || '').includes('RECOMMEND'))
);

const jobCategoryStats = computed(() => {
  const map = new Map<string, { total: number; recommended: number }>();
  for (const t of completedTasks.value) {
    const cat = t.jobCategory || 'UNKNOWN';
    const entry = map.get(cat) || { total: 0, recommended: 0 };
    entry.total++;
    if ((t.recommendation || '').includes('RECOMMEND')) entry.recommended++;
    map.set(cat, entry);
  }
  return [...map.entries()].map(([category, stats]) => ({
    category,
    total: stats.total,
    recommended: stats.recommended,
    rate: stats.total ? Math.round(stats.recommended / stats.total * 100) : 0,
  }));
});

function parseCallDetail(raw: string): Record<string, string> {
  try {
    return JSON.parse(raw);
  } catch {
    return { name: raw };
  }
}

function formatCallDetail(raw: string): string {
  const obj = parseCallDetail(raw);
  const parts = [obj.name || obj.server, obj.status, obj.durationMs ? `${obj.durationMs}ms` : '', obj.inputSummary, obj.outputSummary].filter(Boolean);
  return parts.join(' | ');
}

const dagGroups = computed(() => {
  const groups: Array<{
    type: 'step' | 'parallel';
    label?: string;
    status?: string;
    duration?: string;
    spanId?: string;
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
    tokenCost?: number;
    stepKind?: string;
    viewType?: string;
    lanes?: Array<{
      label: string;
      status: string;
      duration: string;
      spanId: string;
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
      tokenCost?: number;
      stepKind?: string;
    }>;
  }> = [];
  const steps = traceSteps.value;

  const parallelSteps = steps.filter(s => s.dagGroupId === 'parallel-evaluation');
  const nonParallelSteps = steps.filter(s => s.dagGroupId !== 'parallel-evaluation');

  let parallelInserted = false;
  for (const step of nonParallelSteps) {
    if (!parallelInserted && step.eventType === 'DAG_START') {
      const lanes = parallelSteps
        .filter(s => s.laneId)
        .map(s => ({
          label: s.businessLabel || hrStepLabel(s),
          status: s.status,
          duration: formatDuration(s.durationMs),
          spanId: s.spanId,
          businessLabel: s.businessLabel,
          evidenceSummary: s.evidenceSummary,
          interviewHints: s.interviewHints,
          developerLabel: s.developerLabel,
          skillName: s.skillName,
          promptPreview: s.promptPreview,
          inputSummary: s.inputSummary,
          outputSummary: s.outputSummary,
          toolCalls: s.toolCalls,
          mcpCalls: s.mcpCalls,
          tokenCost: s.tokenCost,
          stepKind: s.stepKind,
        }));
      if (lanes.length) {
        groups.push({ type: 'parallel', lanes });
        parallelInserted = true;
      }
      continue;
    }
    if (step.eventType === 'QUALITY_CHECK' && dagViewMode.value === 'hr') continue;
    if (step.viewType === 'DEV' && dagViewMode.value === 'hr') continue;
    groups.push({
      type: 'step',
      label: dagViewMode.value === 'hr' ? (step.businessLabel || hrStepLabel(step)) : (step.developerLabel || step.title),
      status: step.status,
      duration: formatDuration(step.durationMs),
      spanId: step.spanId,
      businessLabel: step.businessLabel,
      evidenceSummary: step.evidenceSummary,
      interviewHints: step.interviewHints,
      developerLabel: step.developerLabel,
      skillName: step.skillName,
      promptPreview: step.promptPreview,
      inputSummary: step.inputSummary,
      outputSummary: step.outputSummary,
      toolCalls: step.toolCalls,
      mcpCalls: step.mcpCalls,
      sandboxSummary: step.sandboxSummary,
      tokenCost: step.tokenCost,
      stepKind: step.stepKind,
      viewType: step.viewType,
    });
  }

  if (!parallelInserted && parallelSteps.length) {
    const lanes = parallelSteps.filter(s => s.laneId).map(s => ({
      label: s.businessLabel || hrStepLabel(s),
      status: s.status,
      duration: formatDuration(s.durationMs),
      spanId: s.spanId,
      businessLabel: s.businessLabel,
      evidenceSummary: s.evidenceSummary,
      interviewHints: s.interviewHints,
      developerLabel: s.developerLabel,
      skillName: s.skillName,
      promptPreview: s.promptPreview,
      inputSummary: s.inputSummary,
      outputSummary: s.outputSummary,
      toolCalls: s.toolCalls,
      mcpCalls: s.mcpCalls,
      tokenCost: s.tokenCost,
      stepKind: s.stepKind,
    }));
    if (lanes.length) groups.splice(1, 0, { type: 'parallel', lanes });
  }
  return groups;
});

const canStartEvaluation = computed(() => (queuedFiles.value.length > 0 || pastedResume.value.trim().length > 0) && (autoMatchJd.value || selectedJob.value));

const matchedSkills = computed(() => graphNodes.value.filter(n => n.type === 'skill' && n.score >= 60));
const missingSkills = computed(() => graphNodes.value.filter(n => n.type === 'risk' || (n.type === 'skill' && n.score < 60)));
const matchRate = computed(() => {
  if (activeTask.value?.jdMatchScore) {
    return Math.round((activeTask.value.jdMatchScore || 0) * 100);
  }
  const total = matchedSkills.value.length + missingSkills.value.length;
  if (!total) return activeTask.value?.overallScore || 0;
  return Math.round((matchedSkills.value.length / total) * 100);
});
const skillMatchPercent = computed(() => {
  const skills = graphNodes.value.filter(n => n.type === 'skill');
  if (!skills.length) return activeTask.value?.overallScore || 0;
  return Math.round(skills.reduce((s, n) => s + Math.min(100, n.score), 0) / skills.length);
});
const expMatchPercent = computed(() => {
  const jobs = graphNodes.value.filter(n => n.type === 'job' || n.type === 'project');
  if (!jobs.length) return Math.max(0, (activeTask.value?.overallScore || 70) - 5);
  return Math.round(jobs.reduce((s, n) => s + Math.min(100, n.score), 0) / jobs.length);
});
const eduMatchPercent = computed(() => {
  const edu = graphNodes.value.filter(n => n.type === 'education');
  if (!edu.length) return Math.max(0, (activeTask.value?.overallScore || 70) + 5);
  return Math.round(edu.reduce((s, n) => s + Math.min(100, n.score), 0) / edu.length);
});

const recommendationLabel = computed(() => {
  const r = activeTask.value?.recommendation || '';
  if (r.includes('STRONG')) return '强烈推荐面试';
  if (r.includes('RECOMMEND')) return '推荐面试';
  return '需要人工复核';
});

const recommendedCount = computed(() => tasks.value.filter(t => t.status === 'SUCCESS' && (t.recommendation || '').includes('RECOMMEND')).length);
const reviewCount = computed(() => tasks.value.filter(t => t.status === 'SUCCESS' && !(t.recommendation || '').includes('RECOMMEND')).length);
const passRate = computed(() => {
  if (!completedTasks.value.length) return 0;
  return Math.round(recommendedCount.value / completedTasks.value.length * 100);
});
const avgEvalTime = computed(() => {
  const finished = completedTasks.value.filter(t => t.durationMs);
  if (!finished.length) return '-';
  const avg = finished.reduce((s, t) => s + (t.durationMs || 0), 0) / finished.length;
  return (avg / 1000).toFixed(1) + 's';
});
const scoreBand90 = computed(() => completedTasks.value.filter(t => (t.overallScore || 0) >= 90).length);
const scoreBand80 = computed(() => completedTasks.value.filter(t => (t.overallScore || 0) >= 80 && (t.overallScore || 0) < 90).length);
const scoreBand70 = computed(() => completedTasks.value.filter(t => (t.overallScore || 0) >= 70 && (t.overallScore || 0) < 80).length);
const scoreBandLow = computed(() => completedTasks.value.filter(t => (t.overallScore || 0) < 70).length);
const validFeedbacks = computed(() => {
  const taskTraceIds = new Set(tasks.value.map(t => t.traceId));
  return feedbacks.value.filter(f => taskTraceIds.has(f.traceId) && f.humanComment && !f.humanComment.includes('验证反馈'));
});
const feedbackAgreeCount = computed(() => validFeedbacks.value.filter(f => f.feedbackType === 'LIKE').length);
const feedbackDisagreeCount = computed(() => validFeedbacks.value.filter(f => f.feedbackType !== 'LIKE').length);

function loadJobs(): JobProfile[] {
  try {
    const saved = localStorage.getItem(JOBS_STORAGE_KEY);
    return saved ? JSON.parse(saved) : [...defaultJobs];
  } catch { return [...defaultJobs]; }
}

watch(jobs, (v) => localStorage.setItem(JOBS_STORAGE_KEY, JSON.stringify(v)), { deep: true });
watch(selectedJobId, () => { if (selectedJob.value) Object.assign(jobDraft, selectedJob.value); });

onMounted(async () => {
  if (!selectedJobId.value && jobs.value.length) selectedJobId.value = jobs.value[0].id;
  if (selectedJob.value) Object.assign(jobDraft, selectedJob.value);
  await refreshAll();
  await loadHealth();
  if (activeTraceId.value) subscribeTrace(activeTraceId.value);
  for (const job of jobs.value) { indexJdToBackend(job); }
});

onBeforeUnmount(() => { eventSource?.close(); pollTimers.forEach((t) => clearTimeout(t)); });

async function refreshAll() {
  refreshing.value = true;
  try {
    await Promise.allSettled([loadTasks(), loadMetrics(), loadFeedbacks()]);
    if (activeTraceId.value) {
      await Promise.allSettled([loadTraces(activeTraceId.value), loadGraph(activeTraceId.value)]);
    }
  } finally { refreshing.value = false; }
}

async function loadHealth() {
  try {
    const r = await fetch('/api/health');
    const h = (await r.json()) as { status?: string };
    healthStatus.value = h.status ?? 'UNKNOWN';
  } catch { healthStatus.value = 'DOWN'; }
}

async function loadTasks() {
  const r = await fetch('/api/tasks');
  if (r.ok) tasks.value = (await r.json()) as TaskResponse[];
}

async function loadMetrics() {
  const r = await fetch('/api/metrics');
  if (r.ok) metrics.value = (await r.json()) as Metrics;
}

async function loadFeedbacks() {
  const r = await fetch('/api/feedback');
  if (r.ok) feedbacks.value = (await r.json()) as FeedbackResponse[];
}

async function loadTraces(traceId: string) {
  const r = await fetch(`/api/traces/${traceId}`);
  if (r.ok) traces.value = (await r.json()) as TraceEvent[];
}

async function loadGraph(traceId: string) {
  const r = await fetch(`/api/graphs/${traceId}`);
  if (r.ok) {
    const g = (await r.json()) as { nodes: GraphNode[]; edges: GraphEdge[] };
    graphNodes.value = g.nodes;
    graphEdges.value = g.edges;
  }
}

function selectTask(task: TaskResponse) {
  activeTraceId.value = task.traceId;
  detailTab.value = 'report';
  appView.value = 'detail';
  loadTraces(task.traceId);
  loadGraph(task.traceId);
  if (task.status === 'RUNNING') startPolling(task.traceId);
}

function openCandidate(traceId: string) {
  const task = tasks.value.find(t => t.traceId === traceId);
  if (task) selectTask(task);
}

function goBack() {
  appView.value = 'candidates';
}

function subscribeTrace(traceId: string) {
  eventSource?.close();
  eventSource = new EventSource(`/sse/traces/${traceId}`);
  eventSource.addEventListener('trace', (event) => {
    traces.value.push(JSON.parse((event as MessageEvent).data) as TraceEvent);
    loadTasks();
    loadMetrics();
    loadGraph(traceId);
  });
}

function startPolling(traceId: string) {
  const existing = pollTimers.get(traceId);
  if (existing) clearTimeout(existing);
  const poll = async () => {
    await refreshAll();
    const current = tasks.value.find((t) => t.traceId === traceId);
    if (current?.status === 'RUNNING') {
      pollTimers.set(traceId, window.setTimeout(poll, 2000));
    } else { pollTimers.delete(traceId); }
  };
  pollTimers.set(traceId, window.setTimeout(poll, 2000));
}

function importResume(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  if (!files.length) return;
  const invalid = files.find((f) => !['pdf', 'txt', 'md', 'csv'].includes(f.name.split('.').pop()?.toLowerCase() ?? ''));
  if (invalid) {
    errorMessage.value = `不支持 ${invalid.name}，请上传 PDF/TXT/MD/CSV。`;
    input.value = '';
    return;
  }
  queuedFiles.value = files;
  successMessage.value = `已选择 ${files.length} 份简历。`;
  input.value = '';
}

async function createEvaluations() {
  if (!canStartEvaluation.value) return;
  loading.value = true;
  errorMessage.value = '';
  try {
    if (queuedFiles.value.length) {
      for (const file of queuedFiles.value) {
        const body = new FormData();
        body.append('file', file);
        if (autoMatchJd.value) {
          body.append('executionMode', 'DAG_CONCURRENT');
          await fetch('/api/tasks/upload-auto', { method: 'POST', body });
        } else {
          body.append('jobCategory', selectedJob.value.category);
          body.append('executionMode', 'DAG_CONCURRENT');
          body.append('jobDescription', selectedJob.value.description);
          await fetch('/api/tasks/upload', { method: 'POST', body });
        }
      }
    } else if (pastedResume.value.trim()) {
      await fetch('/api/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          jobCategory: selectedJob.value.category,
          executionMode: 'DAG_CONCURRENT',
          jobDescription: autoMatchJd.value ? '' : selectedJob.value.description,
          resumeText: pastedResume.value
        })
      });
    }
    queuedFiles.value = [];
    pastedResume.value = '';
    showUploadModal.value = false;
    successMessage.value = '评估任务已创建。';
    await loadTasks();
    const latest = tasks.value[0];
    if (latest) { selectTask(latest); subscribeTrace(latest.traceId); }
  } catch (e) { errorMessage.value = '创建任务失败。'; }
  finally { loading.value = false; }
}

async function sendFeedback(score: number) {
  if (!activeTask.value) return;
  const r = await fetch('/api/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      traceId: activeTask.value.traceId,
      ratingScore: score,
      feedbackType: score >= 4 ? 'LIKE' : 'DISLIKE',
      humanComment: feedbackText.value || 'HR 已确认。',
      reviewer: 'HR'
    })
  });
  if (r.ok) {
    feedbackText.value = '';
    successMessage.value = '反馈已提交。';
    await loadFeedbacks();
  }
}

function createJob() {
  const newJob: JobProfile = { id: `job-${Date.now()}`, title: '新岗位', department: '', level: '', category: 'TECH', description: '', createdAt: new Date().toISOString() };
  jobs.value.unshift(newJob);
  selectedJobId.value = newJob.id;
}

function saveJob() {
  const idx = jobs.value.findIndex((j) => j.id === selectedJobId.value);
  if (idx >= 0) jobs.value[idx] = { ...jobDraft };
  indexJdToBackend(jobDraft);
  successMessage.value = '岗位已保存。';
}

async function indexJdToBackend(job: JobProfile) {
  try {
    await fetch('/api/jd', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jdId: job.id, title: job.title, category: job.category, description: job.description })
    });
  } catch { /* best-effort indexing */ }
}

function deleteJob() {
  jobs.value = jobs.value.filter((j) => j.id !== selectedJobId.value);
  if (jobs.value.length) selectedJobId.value = jobs.value[0].id;
}

function statusText(status?: string) {
  if (status === 'SUCCESS') return '已完成';
  if (status === 'RUNNING') return '评估中';
  if (status === 'FAILED') return '失败';
  return '等待中';
}

function statusClass(status?: string) {
  if (status === 'SUCCESS') return 'badge-success';
  if (status === 'RUNNING') return 'badge-warning';
  if (status === 'FAILED') return 'badge-danger';
  return 'badge-neutral';
}

function formatDuration(ms?: number) {
  if (!ms) return '-';
  return `${(ms / 1000).toFixed(1)}s`;
}

function traceStageLabel(e: TraceEvent) {
  const r = e.agentRole.toLowerCase();
  if (r.includes('orchestrator')) return '任务编排';
  if (r.includes('parser')) return '简历解析';
  if (r.includes('dag')) return '并发评估';
  if (r.includes('tech')) return '技能匹配';
  if (r.includes('project')) return '项目深度';
  if (r.includes('risk')) return '风险识别';
  if (r.includes('deepseek') || r.includes('llm')) return '报告生成';
  if (r.includes('human') || r.includes('feedback')) return '人工反馈';
  return '评估步骤';
}

function hrStepLabel(step: { agentRole: string; eventType: string; title: string }): string {
  const r = step.agentRole.toLowerCase();
  if (r.includes('orchestrator') && step.eventType === 'TASK_CREATED') return '创建评估任务';
  if (r.includes('orchestrator')) return '任务调度';
  if (r.includes('parser')) return '解析简历内容';
  if (r.includes('rag') || r.includes('hybrid')) return '知识库检索匹配';
  if (r.includes('deepseek') || r.includes('llm') || step.eventType === 'LLM_COMPLETE') return 'AI 综合评估';
  if (r.includes('ragas') || r.includes('judge')) return '评估质量校验';
  if (r.includes('report') || r.includes('final')) return '生成评估报告';
  if (r.includes('feedback') || r.includes('human')) return 'HR 反馈记录';
  if (r.includes('external') || r.includes('github')) return '外部作品检索';
  return step.title || '评估步骤';
}

function eventStatusText(status: string) {
  if (status === 'SUCCESS') return '完成';
  if (status === 'FAILED') return '失败';
  return '进行中';
}

function traceEvidence(e: TraceEvent) {
  if (e.tokenCost > 0) return `${e.durationMs}ms · ${e.tokenCost} tokens`;
  return `${e.durationMs}ms`;
}

function clearNotices() { errorMessage.value = ''; successMessage.value = ''; }
</script>

<template>
  <div class="app-shell">
    <!-- SIDEBAR -->
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-icon">R</div>
        <span class="brand-text">ResumAI</span>
      </div>

      <nav class="side-nav">
        <button :class="{ active: appView === 'dashboard' }" @click="appView = 'dashboard'; clearNotices()">
          <span class="nav-icon">◇</span> 总览
        </button>
        <button :class="{ active: appView === 'positions' }" @click="appView = 'positions'; clearNotices()">
          <span class="nav-icon">◆</span> 岗位管理
        </button>
        <button :class="{ active: appView === 'candidates' || appView === 'detail' }" @click="appView = 'candidates'; clearNotices()">
          <span class="nav-icon">○</span> 候选人
        </button>
        <button :class="{ active: appView === 'analytics' }" @click="appView = 'analytics'; clearNotices()">
          <span class="nav-icon">◈</span> 招聘洞察
        </button>
      </nav>

      <div class="sidebar-footer">
        <span class="status-dot" :class="{ online: healthStatus === 'UP' }"></span>
        <span>{{ healthStatus === 'UP' ? '服务正常' : healthStatus }}</span>
      </div>
    </aside>

    <!-- MAIN -->
    <main class="main-content">
      <p v-if="errorMessage" class="notice error" @click="errorMessage = ''">{{ errorMessage }}</p>
      <p v-if="successMessage" class="notice success" @click="successMessage = ''">{{ successMessage }}</p>

      <!-- ========== DASHBOARD ========== -->
      <section v-if="appView === 'dashboard'">
        <div class="page-header">
          <div>
            <h1>总览</h1>
            <p>评估数据概况与快捷操作</p>
          </div>
          <div class="header-actions">
            <button class="btn btn-ghost" :disabled="refreshing" @click="refreshAll">{{ refreshing ? '刷新中...' : '刷新' }}</button>
            <button class="btn btn-primary" @click="showUploadModal = true">上传简历</button>
          </div>
        </div>

        <div class="kpi-grid">
          <div class="kpi-card"><span class="kpi-label">候选人</span><div class="kpi-value">{{ tasks.length }}</div></div>
          <div class="kpi-card"><span class="kpi-label">评估中</span><div class="kpi-value">{{ runningTasks.length }}</div></div>
          <div class="kpi-card"><span class="kpi-label">已完成</span><div class="kpi-value">{{ completedTasks.length }}</div></div>
          <div class="kpi-card"><span class="kpi-label">平均分</span><div class="kpi-value">{{ metrics?.averageScore?.toFixed(1) ?? '-' }}</div></div>
        </div>

        <div class="card">
          <div class="card-header">
            <h2>最近评估</h2>
            <button class="btn btn-ghost btn-sm" @click="appView = 'candidates'">查看全部</button>
          </div>
          <table class="data-table" v-if="tasks.length">
            <thead><tr><th>候选人</th><th>状态</th><th>岗位</th><th>评分</th><th>耗时</th></tr></thead>
            <tbody>
              <tr v-for="task in tasks.slice(0, 8)" :key="task.traceId" @click="selectTask(task)">
                <td class="truncate" style="max-width:200px">{{ task.fileName }}</td>
                <td><span class="badge" :class="statusClass(task.status)">{{ statusText(task.status) }}</span></td>
                <td>{{ task.jobCategory }}</td>
                <td><strong>{{ task.overallScore || '-' }}</strong></td>
                <td class="text-muted">{{ formatDuration(task.durationMs) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="empty-state"><p>暂无评估记录，上传简历开始使用</p></div>
        </div>
      </section>

      <!-- ========== POSITIONS ========== -->
      <section v-if="appView === 'positions'">
        <div class="page-header">
          <div><h1>岗位管理</h1><p>维护岗位 JD，评估时自动引用</p></div>
          <div class="header-actions"><button class="btn btn-primary" @click="createJob">新建岗位</button></div>
        </div>

        <div class="job-layout">
          <div class="card">
            <div class="job-list-panel">
              <button v-for="job in jobs" :key="job.id" class="job-item" :class="{ active: job.id === selectedJobId }" @click="selectedJobId = job.id">
                <span class="job-title">{{ job.title }}</span>
                <span class="job-meta">{{ job.department }} · {{ job.level }}</span>
              </button>
            </div>
          </div>
          <div class="card">
            <div class="card-header"><h2>编辑岗位</h2></div>
            <div class="grid-2 mb-lg">
              <div class="form-field"><label>岗位名称</label><input class="form-input" v-model="jobDraft.title" /></div>
              <div class="form-field"><label>部门</label><input class="form-input" v-model="jobDraft.department" /></div>
            </div>
            <div class="grid-2 mb-lg">
              <div class="form-field"><label>级别</label><input class="form-input" v-model="jobDraft.level" /></div>
              <div class="form-field"><label>类别</label>
                <select class="form-input" v-model="jobDraft.category">
                  <option value="TECH">技术岗</option>
                  <option value="PRODUCT">产品岗</option>
                  <option value="DESIGN">设计岗</option>
                </select>
              </div>
            </div>
            <div class="form-field mb-lg"><label>JD 描述</label><textarea class="form-input" v-model="jobDraft.description" rows="8" /></div>
            <div class="flex gap-sm">
              <button class="btn btn-primary" @click="saveJob">保存</button>
              <button class="btn btn-ghost" @click="deleteJob">删除</button>
            </div>
          </div>
        </div>
      </section>

      <!-- ========== CANDIDATES LIST ========== -->
      <section v-if="appView === 'candidates'">
        <div class="page-header">
          <div><h1>候选人</h1><p>查看所有评估任务，点击进入详情</p></div>
          <div class="header-actions">
            <button class="btn btn-ghost" :disabled="refreshing" @click="refreshAll">刷新</button>
            <button class="btn btn-primary" @click="showUploadModal = true">上传简历</button>
          </div>
        </div>

        <div class="card">
          <div class="candidate-list-toolbar">
            <input class="form-input search-input" v-model="candidateSearch" placeholder="搜索候选人..." />
            <select class="form-input" v-model="statusFilter" style="width:120px">
              <option value="ALL">全部</option>
              <option value="RUNNING">评估中</option>
              <option value="SUCCESS">已完成</option>
              <option value="FAILED">失败</option>
            </select>
            <span class="text-muted text-sm" style="margin-left:auto">共 {{ filteredCandidates.length }} 条</span>
          </div>

          <table class="data-table" v-if="filteredCandidates.length">
            <thead><tr><th>文件名</th><th>状态</th><th>岗位</th><th>评分</th><th>推荐</th><th>耗时</th></tr></thead>
            <tbody>
              <tr v-for="task in filteredCandidates" :key="task.traceId" :class="{ active: task.traceId === activeTraceId }" @click="selectTask(task)">
                <td>{{ task.fileName }}</td>
                <td><span class="badge" :class="statusClass(task.status)">{{ statusText(task.status) }}</span></td>
                <td>{{ task.jobCategory }}</td>
                <td><strong>{{ task.overallScore || '-' }}</strong></td>
                <td class="text-muted text-sm">{{ task.recommendation || '-' }}</td>
                <td class="text-muted">{{ formatDuration(task.durationMs) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="empty-state"><p>暂无候选人数据</p></div>
        </div>
      </section>

      <!-- ========== CANDIDATE DETAIL ========== -->
      <section v-if="appView === 'detail' && activeTask">
        <button class="back-link" @click="goBack">← 返回列表</button>

        <div class="detail-header">
          <div class="score-circle" :class="{ low: (activeTask.overallScore || 0) < 60, mid: (activeTask.overallScore || 0) >= 60 && (activeTask.overallScore || 0) < 75 }">
            <span class="score-value">{{ activeTask.overallScore || '-' }}</span>
            <span class="score-label">综合</span>
          </div>
          <div class="detail-meta">
            <h2>{{ recommendationLabel }}</h2>
            <p>{{ activeTask.fileName }} · {{ activeTask.jobCategory }} · {{ formatDuration(activeTask.durationMs) }}</p>
          </div>
          <div class="detail-actions">
            <span class="badge" :class="statusClass(activeTask.status)">{{ statusText(activeTask.status) }}</span>
          </div>
        </div>

        <div class="tab-bar">
          <button :class="{ active: detailTab === 'resume' }" @click="detailTab = 'resume'">简历原文</button>
          <button :class="{ active: detailTab === 'report' }" @click="detailTab = 'report'">评估报告</button>
          <button :class="{ active: detailTab === 'process' }" @click="detailTab = 'process'">评估过程</button>
          <button :class="{ active: detailTab === 'graph' }" @click="detailTab = 'graph'">JD 匹配</button>
          <button :class="{ active: detailTab === 'feedback' }" @click="detailTab = 'feedback'">HR 反馈</button>
        </div>

        <!-- Resume Tab -->
        <div v-if="detailTab === 'resume'">
          <div class="resume-view-toggle" v-if="activeTask.resumeFileUrl">
            <button :class="{ active: resumeViewMode === 'pdf' }" @click="resumeViewMode = 'pdf'">PDF 原件</button>
            <button :class="{ active: resumeViewMode === 'text' }" @click="resumeViewMode = 'text'">文本抽取</button>
          </div>
          <div v-if="activeTask.resumeFileUrl && resumeViewMode === 'pdf'" class="resume-pdf-frame">
            <iframe :src="activeTask.resumeFileUrl" title="简历 PDF 预览" />
            <a class="btn btn-ghost btn-sm" :href="activeTask.resumeFileUrl" target="_blank" rel="noopener">新窗口打开</a>
          </div>
          <div class="resume-preview" v-else-if="activeTask.resumeText">{{ activeTask.resumeText }}</div>
          <div class="empty-state" v-else><p>简历原文将在解析后展示。PDF 上传后会保留原件预览，文本抽取用于核对解析质量。</p></div>
        </div>

        <!-- Report Tab -->
        <div v-if="detailTab === 'report'" class="report-content">
          <div class="hr-decision-card" :class="recommendationLabel.includes('强烈推荐') ? 'strong' : recommendationLabel.includes('推荐') ? 'recommend' : 'review'">
            <div class="hr-decision-header">
              <span class="hr-decision-badge">{{ recommendationLabel }}</span>
              <span class="hr-decision-score" v-if="activeTask.overallScore">综合评分 {{ activeTask.overallScore }}</span>
            </div>
            <p class="hr-decision-summary">{{ activeTask.summary ? activeTask.summary.split('\n').find(l => l.trim().length > 10)?.trim().slice(0, 180) : '评估报告生成中...' }}</p>
          </div>

          <div v-if="activeTask.matchedJdTitle" class="jd-match-card">
            <div class="jd-match-header">
              <span class="jd-match-badge">RAG 智能匹配</span>
              <span class="jd-match-score" v-if="activeTask.jdMatchScore">匹配度 {{ Math.round((activeTask.jdMatchScore || 0) * 100) }}%</span>
            </div>
            <div class="jd-match-title">{{ activeTask.matchedJdTitle }}</div>
            <div v-if="activeTask.topJdMatches && activeTask.topJdMatches.length > 1" class="jd-match-alts">
              <span class="text-muted" style="font-size:11px">其他候选岗位：</span>
              <span v-for="(m, mi) in activeTask.topJdMatches.slice(1)" :key="mi" class="jd-alt-chip">
                {{ m.title }} ({{ Math.round(m.score * 100) }}%)
              </span>
            </div>
          </div>
          <div class="hr-evidence-grid">
            <div class="hr-evidence-card" v-if="activeTask.strengths?.length">
              <h3>关键优势</h3>
              <ul><li v-for="s in activeTask.strengths" :key="s">{{ s }}</li></ul>
            </div>
            <div class="hr-evidence-card risk" v-if="activeTask.risks?.length">
              <h3>关键风险</h3>
              <ul><li v-for="r in activeTask.risks" :key="r">{{ r }}</li></ul>
            </div>
            <div class="hr-evidence-card" v-if="activeTask.interviewQuestions?.length">
              <h3>面试追问</h3>
              <ol><li v-for="q in activeTask.interviewQuestions" :key="q">{{ q }}</li></ol>
            </div>
          </div>
          <details class="report-raw-md" v-if="activeTask.summary">
            <summary>查看完整 AI 评估原文</summary>
            <div v-html="renderMarkdown(activeTask.summary)"></div>
          </details>
          <div class="empty-state" v-if="!activeTask.summary && !activeTask.strengths?.length"><p>报告生成中...</p></div>
        </div>

        <!-- Process Tab (Dual-view DAG) -->
        <div v-if="detailTab === 'process'">
          <div class="dag-view-toggle">
            <button :class="{ active: dagViewMode === 'hr' }" @click="dagViewMode = 'hr'">HR 视图</button>
            <button :class="{ active: dagViewMode === 'dev' }" @click="dagViewMode = 'dev'">开发者视图</button>
          </div>

          <div v-if="isLegacyTrace" class="dag-legacy-warning">
            <span>旧 Trace 数据，不支持 DAG 详情视图</span>
          </div>

          <div class="dag-flow" v-if="traceSteps.length">
            <template v-for="(group, gi) in dagGroups" :key="gi">
              <!-- Sequential step node -->
              <div v-if="group.type === 'step'" class="dag-step" :class="{ failed: group.status === 'FAILED', running: group.status !== 'SUCCESS' && group.status !== 'FAILED' }" @click="expandedDagNode = expandedDagNode === group.spanId ? '' : (group.spanId || '')">
                <span class="dag-dot" :class="'kind-' + (group.stepKind || 'default')"></span>
                <div class="dag-step-main">
                  <span class="dag-label">{{ group.label }}</span>
                  <span class="dag-sublabel" v-if="dagViewMode === 'hr' && group.evidenceSummary">{{ group.evidenceSummary }}</span>
                  <span class="dag-sublabel" v-if="dagViewMode === 'dev' && group.skillName">Skill: {{ group.skillName }}</span>
                </div>
                <span class="dag-status">{{ group.status === 'SUCCESS' ? '✓' : group.status === 'FAILED' ? '✗' : '◌' }}</span>
                <span class="dag-time">{{ group.duration }}</span>
                <span class="dag-token" v-if="dagViewMode === 'dev' && group.tokenCost">{{ group.tokenCost }} tok</span>
              </div>
              <!-- Expanded detail panel -->
              <div v-if="group.type === 'step' && expandedDagNode === group.spanId" class="dag-detail-panel">
                <template v-if="dagViewMode === 'hr'">
                  <div v-if="group.evidenceSummary" class="dag-detail-row"><strong>证据：</strong>{{ group.evidenceSummary }}</div>
                  <div v-if="group.interviewHints && group.interviewHints.length" class="dag-detail-row">
                    <strong>面试追问建议：</strong>
                    <ul><li v-for="(h, hi) in group.interviewHints" :key="hi">{{ h }}</li></ul>
                  </div>
                </template>
                <template v-if="dagViewMode === 'dev'">
                  <div v-if="group.developerLabel" class="dag-detail-row"><strong>Agent/Skill：</strong>{{ group.developerLabel }}</div>
                  <div v-if="group.promptPreview" class="dag-detail-row"><strong>Prompt：</strong><code>{{ group.promptPreview }}</code></div>
                  <div v-if="group.inputSummary" class="dag-detail-row"><strong>Input：</strong>{{ group.inputSummary }}</div>
                  <div v-if="group.outputSummary" class="dag-detail-row"><strong>Output：</strong>{{ group.outputSummary }}</div>
                  <div v-if="group.toolCalls && group.toolCalls.length" class="dag-detail-row"><strong>Tool Calls：</strong><ul><li v-for="(tc, ti) in group.toolCalls" :key="ti"><code>{{ formatCallDetail(tc) }}</code></li></ul></div>
                  <div v-if="group.mcpCalls && group.mcpCalls.length" class="dag-detail-row"><strong>MCP Calls：</strong><ul><li v-for="(mc, mi) in group.mcpCalls" :key="mi"><code>{{ formatCallDetail(mc) }}</code></li></ul></div>
                  <div v-if="group.sandboxSummary" class="dag-detail-row"><strong>Sandbox：</strong>{{ group.sandboxSummary }}</div>
                  <div v-if="group.tokenCost" class="dag-detail-row"><strong>Token Cost：</strong>{{ group.tokenCost }}</div>
                </template>
              </div>

              <!-- Parallel group -->
              <div v-if="group.type === 'parallel'" class="dag-parallel">
                <div class="dag-parallel-header">
                  <span class="dag-parallel-icon">⫘</span>
                  {{ dagViewMode === 'hr' ? '多维度并行评估' : 'ConcurrentExecutor / Parallel Group' }}
                </div>
                <div class="dag-parallel-lanes">
                  <div v-for="lane in group.lanes" :key="lane.spanId || lane.label" class="dag-lane" @click="expandedDagNode = expandedDagNode === lane.spanId ? '' : lane.spanId">
                    <div class="dag-dot" :style="{ background: lane.status === 'SUCCESS' ? 'var(--color-success)' : lane.status === 'FAILED' ? 'var(--color-danger)' : 'var(--color-primary)' }"></div>
                    <div class="dag-lane-label">{{ dagViewMode === 'hr' ? lane.businessLabel || lane.label : lane.developerLabel || lane.label }}</div>
                    <div class="dag-lane-desc">{{ lane.duration }}</div>
                    <span class="dag-token" v-if="dagViewMode === 'dev' && lane.tokenCost">{{ lane.tokenCost }} tok</span>
                  </div>
                </div>
                <!-- Expanded lane detail -->
                <div v-for="lane in group.lanes" :key="'detail-' + lane.spanId" v-show="expandedDagNode === lane.spanId" class="dag-detail-panel">
                  <template v-if="dagViewMode === 'hr'">
                    <div v-if="lane.evidenceSummary" class="dag-detail-row"><strong>证据：</strong>{{ lane.evidenceSummary }}</div>
                    <div v-if="lane.interviewHints && lane.interviewHints.length" class="dag-detail-row">
                      <strong>面试追问建议：</strong>
                      <ul><li v-for="(h, hi) in lane.interviewHints" :key="hi">{{ h }}</li></ul>
                    </div>
                  </template>
                  <template v-if="dagViewMode === 'dev'">
                    <div v-if="lane.developerLabel" class="dag-detail-row"><strong>Agent/Skill：</strong>{{ lane.developerLabel }}</div>
                    <div v-if="lane.skillName" class="dag-detail-row"><strong>Skill：</strong>{{ lane.skillName }}</div>
                    <div v-if="lane.promptPreview" class="dag-detail-row"><strong>Prompt：</strong><code>{{ lane.promptPreview }}</code></div>
                    <div v-if="lane.inputSummary" class="dag-detail-row"><strong>Input：</strong>{{ lane.inputSummary }}</div>
                    <div v-if="lane.outputSummary" class="dag-detail-row"><strong>Output：</strong>{{ lane.outputSummary }}</div>
                    <div v-if="lane.toolCalls && lane.toolCalls.length" class="dag-detail-row"><strong>Tool Calls：</strong><ul><li v-for="(tc, ti) in lane.toolCalls" :key="ti"><code>{{ formatCallDetail(tc) }}</code></li></ul></div>
                    <div v-if="lane.mcpCalls && lane.mcpCalls.length" class="dag-detail-row"><strong>MCP Calls：</strong><ul><li v-for="(mc, mi) in lane.mcpCalls" :key="mi"><code>{{ formatCallDetail(mc) }}</code></li></ul></div>
                  </template>
                </div>
              </div>
            </template>
          </div>
          <div class="empty-state" v-else><p>评估开始后将展示实时进度</p></div>
        </div>

        <!-- Graph Tab → JD Match Analysis -->
        <div v-if="detailTab === 'graph'">
          <div v-if="jdMatchCards.length" class="card" style="padding:var(--space-xl)">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:var(--space-xl)">
              <h3 style="font-size:15px;font-weight:600">JD Top3 智能匹配</h3>
              <span class="badge badge-success" v-if="matchRate >= 70">匹配度高</span>
              <span class="badge badge-warning" v-else-if="matchRate >= 50">部分匹配</span>
              <span class="badge badge-danger" v-else-if="jdMatchCards.length">匹配度低</span>
              <span class="badge badge-warning" v-else>暂无匹配</span>
            </div>

            <div v-for="(m, idx) in jdMatchCards" :key="m.jdId" class="jd-top-card" :class="{ active: idx === 0 }">
              <div class="jd-top-header">
                <span class="jd-top-rank">#{{ idx + 1 }}</span>
                <strong>{{ m.title }}</strong>
                <span class="jd-match-score">{{ Math.round(m.score * 100) }}%</span>
              </div>
              <p class="text-muted text-sm">{{ m.category }}</p>
              <div v-if="m.matchReasons?.length" class="jd-reasons">
                <h4>匹配依据</h4>
                <ul><li v-for="(r, ri) in m.matchReasons" :key="ri">{{ r }}</li></ul>
              </div>
              <div v-if="m.gaps?.length" class="jd-gaps">
                <h4>能力缺口</h4>
                <ul><li v-for="(g, gi) in m.gaps" :key="gi">{{ g }}</li></ul>
              </div>
              <div v-if="m.interviewChecks?.length" class="jd-checks">
                <h4>面试验证点</h4>
                <ul><li v-for="(c, ci) in m.interviewChecks" :key="ci">{{ c }}</li></ul>
              </div>
            </div>
          </div>
          <div v-else-if="graphNodes.length" class="card" style="padding:var(--space-xl)">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:var(--space-xl)">
              <h3 style="font-size:15px;font-weight:600">JD 匹配分析</h3>
              <span class="badge badge-success" v-if="matchRate >= 70">匹配度高</span>
              <span class="badge badge-warning" v-else-if="matchRate >= 50">部分匹配</span>
              <span class="badge badge-danger" v-else>匹配度低</span>
            </div>

            <div style="display:flex;align-items:center;gap:var(--space-xl);margin-bottom:var(--space-2xl)">
              <div class="score-circle" :class="{ low: matchRate < 50, mid: matchRate >= 50 && matchRate < 70 }">
                <span class="score-value">{{ matchRate }}%</span>
                <span class="score-label">匹配</span>
              </div>
              <div style="flex:1">
                <p style="font-size:14px;color:var(--color-text);margin-bottom:4px">满足 <strong>{{ matchedSkills.length }}</strong> / {{ matchedSkills.length + missingSkills.length }} 项岗位要求</p>
                <p style="font-size:13px;color:var(--color-text-secondary)">基于 AI 评估的岗位匹配度分析，包含技能、经验和教育背景三个维度</p>
              </div>
            </div>

            <div style="display:flex;flex-direction:column;gap:var(--space-lg);margin-bottom:var(--space-2xl)">
              <div>
                <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px"><span>技能匹配</span><span class="text-muted">{{ skillMatchPercent }}%</span></div>
                <div style="height:8px;background:var(--color-border-light);border-radius:4px;overflow:hidden"><div :style="{ width: skillMatchPercent + '%', height: '100%', background: skillMatchPercent >= 70 ? 'var(--color-success)' : skillMatchPercent >= 50 ? 'var(--color-warning)' : 'var(--color-danger)', borderRadius: '4px' }"></div></div>
              </div>
              <div>
                <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px"><span>经验匹配</span><span class="text-muted">{{ expMatchPercent }}%</span></div>
                <div style="height:8px;background:var(--color-border-light);border-radius:4px;overflow:hidden"><div :style="{ width: expMatchPercent + '%', height: '100%', background: expMatchPercent >= 70 ? 'var(--color-success)' : expMatchPercent >= 50 ? 'var(--color-warning)' : 'var(--color-danger)', borderRadius: '4px' }"></div></div>
              </div>
              <div>
                <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px"><span>教育背景</span><span class="text-muted">{{ eduMatchPercent }}%</span></div>
                <div style="height:8px;background:var(--color-border-light);border-radius:4px;overflow:hidden"><div :style="{ width: eduMatchPercent + '%', height: '100%', background: eduMatchPercent >= 70 ? 'var(--color-success)' : eduMatchPercent >= 50 ? 'var(--color-warning)' : 'var(--color-danger)', borderRadius: '4px' }"></div></div>
              </div>
            </div>

            <div class="grid-2">
              <div>
                <h4 style="font-size:13px;font-weight:600;color:var(--color-success);margin-bottom:var(--space-md)">已满足的要求</h4>
                <div style="display:flex;flex-direction:column;gap:6px">
                  <div v-for="skill in matchedSkills" :key="skill.id" style="display:flex;align-items:center;gap:8px;font-size:13px">
                    <span style="color:var(--color-success)">&#10003;</span>
                    <span>{{ skill.label }}</span>
                    <span class="text-muted text-xs" style="margin-left:auto">{{ skill.score }}分</span>
                  </div>
                  <p v-if="!matchedSkills.length" class="text-muted text-sm">暂无数据</p>
                </div>
              </div>
              <div>
                <h4 style="font-size:13px;font-weight:600;color:var(--color-danger);margin-bottom:var(--space-md)">待补充 / 风险项</h4>
                <div style="display:flex;flex-direction:column;gap:6px">
                  <div v-for="skill in missingSkills" :key="skill.id" style="display:flex;align-items:center;gap:8px;font-size:13px">
                    <span style="color:var(--color-danger)">&#10007;</span>
                    <span>{{ skill.label }}</span>
                    <span class="text-muted text-xs" style="margin-left:auto">{{ skill.score }}分</span>
                  </div>
                  <p v-if="!missingSkills.length" class="text-muted text-sm">无明显短板</p>
                </div>
              </div>
            </div>
          </div>
          <div class="empty-state" v-else><p>匹配分析将在评估完成后生成</p></div>
        </div>

        <!-- Feedback Tab -->
        <div v-if="detailTab === 'feedback'" class="feedback-section">
          <h3 style="font-size:14px;font-weight:600;margin-bottom:12px">对本次评估的反馈</h3>
          <textarea class="form-input" v-model="feedbackText" rows="4" placeholder="评估结论是否准确？有哪些需要调整？" />
          <div class="feedback-actions">
            <button class="btn btn-primary" @click="sendFeedback(5)">认可结论</button>
            <button class="btn btn-danger" @click="sendFeedback(2)">需要复核</button>
          </div>
          <div v-if="feedbacks.filter(f => f.traceId === activeTraceId).length" class="mt-lg">
            <h3 style="font-size:13px;font-weight:600;margin-bottom:8px;color:var(--color-text-secondary)">历史反馈</h3>
            <div v-for="fb in feedbacks.filter(f => f.traceId === activeTraceId)" :key="fb.id" style="padding:8px 0;border-bottom:1px solid var(--color-border-light);font-size:13px">
              <span class="badge" :class="fb.feedbackType === 'LIKE' ? 'badge-success' : 'badge-danger'">{{ fb.feedbackType === 'LIKE' ? '认可' : '复核' }}</span>
              <span class="text-muted" style="margin-left:8px">{{ fb.humanComment }}</span>
            </div>
          </div>
        </div>
      </section>

      <!-- ========== ANALYTICS (HR-focused) ========== -->
      <section v-if="appView === 'analytics'">
        <div class="page-header">
          <div><h1>招聘洞察</h1><p>候选人漏斗、评分分布与 AI-HR 一致性分析</p></div>
        </div>

        <div class="kpi-grid">
          <div class="kpi-card"><span class="kpi-label">总候选人</span><div class="kpi-value">{{ tasks.length }}</div></div>
          <div class="kpi-card"><span class="kpi-label">推荐面试</span><div class="kpi-value">{{ recommendedCount }}</div></div>
          <div class="kpi-card"><span class="kpi-label">通过率</span><div class="kpi-value">{{ tasks.length >= 3 ? passRate + '%' : (recommendedCount + '/' + completedTasks.length) }}</div></div>
          <div class="kpi-card"><span class="kpi-label">JD 匹配成功率</span><div class="kpi-value">{{ tasks.length >= 3 ? jdMatchSuccessRate + '%' : '-' }}</div><span class="kpi-hint" v-if="tasks.length < 3">样本不足 ({{ tasks.length }})</span></div>
          <div class="kpi-card"><span class="kpi-label">待复核队列</span><div class="kpi-value">{{ pendingReviewTasks.length }}</div></div>
        </div>

        <div class="analytics-grid">
          <div class="analytics-card">
            <h3>招聘漏斗</h3>
            <div style="display:flex;flex-direction:column;gap:10px;margin-top:var(--space-md)">
              <div style="display:flex;align-items:center;gap:12px">
                <span style="width:80px;font-size:12px;color:var(--color-text-secondary)">已提交</span>
                <div style="flex:1;height:24px;background:var(--color-primary-light);border-radius:4px;display:flex;align-items:center;padding:0 10px"><span style="font-size:12px;font-weight:600">{{ tasks.length }}</span></div>
              </div>
              <div style="display:flex;align-items:center;gap:12px">
                <span style="width:80px;font-size:12px;color:var(--color-text-secondary)">已评估</span>
                <div :style="{ flex: 1, height: '24px', background: 'var(--color-success-light)', borderRadius: '4px', display: 'flex', alignItems: 'center', padding: '0 10px', maxWidth: (completedTasks.length / Math.max(1, tasks.length) * 100) + '%' }"><span style="font-size:12px;font-weight:600">{{ completedTasks.length }}</span></div>
              </div>
              <div style="display:flex;align-items:center;gap:12px">
                <span style="width:80px;font-size:12px;color:var(--color-text-secondary)">推荐面试</span>
                <div :style="{ flex: 1, height: '24px', background: 'var(--color-success-light)', borderRadius: '4px', display: 'flex', alignItems: 'center', padding: '0 10px', maxWidth: (recommendedCount / Math.max(1, tasks.length) * 100) + '%' }"><span style="font-size:12px;font-weight:600">{{ recommendedCount }}</span></div>
              </div>
              <div style="display:flex;align-items:center;gap:12px">
                <span style="width:80px;font-size:12px;color:var(--color-text-secondary)">需复核</span>
                <div :style="{ flex: 1, height: '24px', background: 'var(--color-warning-light)', borderRadius: '4px', display: 'flex', alignItems: 'center', padding: '0 10px', maxWidth: (reviewCount / Math.max(1, tasks.length) * 100) + '%' }"><span style="font-size:12px;font-weight:600">{{ reviewCount }}</span></div>
              </div>
            </div>
          </div>

          <div class="analytics-card">
            <h3>评分分布</h3>
            <div v-if="completedTasks.length" style="display:flex;flex-direction:column;gap:8px;margin-top:var(--space-md)">
              <div style="display:flex;align-items:center;gap:8px;font-size:12px">
                <span style="width:60px">90+ 分</span>
                <div style="flex:1;height:20px;background:var(--color-border-light);border-radius:3px;overflow:hidden"><div :style="{ width: (scoreBand90 / Math.max(1, completedTasks.length) * 100) + '%', height: '100%', background: 'var(--color-success)', borderRadius: '3px' }"></div></div>
                <span style="width:24px;text-align:right;font-weight:600">{{ scoreBand90 }}</span>
              </div>
              <div style="display:flex;align-items:center;gap:8px;font-size:12px">
                <span style="width:60px">80-89 分</span>
                <div style="flex:1;height:20px;background:var(--color-border-light);border-radius:3px;overflow:hidden"><div :style="{ width: (scoreBand80 / Math.max(1, completedTasks.length) * 100) + '%', height: '100%', background: '#34d399', borderRadius: '3px' }"></div></div>
                <span style="width:24px;text-align:right;font-weight:600">{{ scoreBand80 }}</span>
              </div>
              <div style="display:flex;align-items:center;gap:8px;font-size:12px">
                <span style="width:60px">70-79 分</span>
                <div style="flex:1;height:20px;background:var(--color-border-light);border-radius:3px;overflow:hidden"><div :style="{ width: (scoreBand70 / Math.max(1, completedTasks.length) * 100) + '%', height: '100%', background: 'var(--color-warning)', borderRadius: '3px' }"></div></div>
                <span style="width:24px;text-align:right;font-weight:600">{{ scoreBand70 }}</span>
              </div>
              <div style="display:flex;align-items:center;gap:8px;font-size:12px">
                <span style="width:60px">&lt;70 分</span>
                <div style="flex:1;height:20px;background:var(--color-border-light);border-radius:3px;overflow:hidden"><div :style="{ width: (scoreBandLow / Math.max(1, completedTasks.length) * 100) + '%', height: '100%', background: 'var(--color-danger)', borderRadius: '3px' }"></div></div>
                <span style="width:24px;text-align:right;font-weight:600">{{ scoreBandLow }}</span>
              </div>
            </div>
            <p v-else class="text-muted text-sm">暂无完成的评估</p>
          </div>

          <div class="analytics-card">
            <h3>岗位维度通过率</h3>
            <div v-if="jobCategoryStats.length" style="display:flex;flex-direction:column;gap:8px;margin-top:var(--space-md)">
              <div v-for="row in jobCategoryStats" :key="row.category" style="font-size:12px;display:flex;gap:8px;align-items:center">
                <span style="width:80px">{{ row.category }}</span>
                <div style="flex:1;height:18px;background:var(--color-border-light);border-radius:3px;overflow:hidden">
                  <div :style="{ width: row.rate + '%', height: '100%', background: 'var(--color-primary)' }"></div>
                </div>
                <span>{{ row.recommended }}/{{ row.total }}</span>
              </div>
            </div>
            <p v-else class="text-muted text-sm">暂无岗位维度数据</p>
          </div>

          <div class="analytics-card">
            <h3>待复核原因入口</h3>
            <div v-if="pendingReviewTasks.length" style="display:flex;flex-direction:column;gap:6px;margin-top:var(--space-md)">
              <div v-for="t in pendingReviewTasks.slice(0, 6)" :key="t.traceId" style="font-size:12px;padding:6px 0;border-bottom:1px solid var(--color-border-light);cursor:pointer" @click="openCandidate(t.traceId)">
                <strong>{{ t.fileName }}</strong>
                <span class="text-muted"> — {{ t.risks?.[0] || '需人工复核' }}</span>
              </div>
            </div>
            <p v-else class="text-muted text-sm">当前无待复核候选人</p>
          </div>

          <div class="analytics-card">
            <h3>平均评估耗时</h3>
            <p style="font-size:24px;font-weight:700;margin-top:var(--space-md)">{{ avgEvalTime }}</p>
          </div>

          <div class="analytics-card">
            <h3>AI-HR 反馈一致性</h3>
            <div v-if="validFeedbacks.length >= 3" style="margin-top:var(--space-md)">
              <div style="display:flex;align-items:baseline;gap:16px;margin-bottom:var(--space-lg)">
                <div><span style="font-size:28px;font-weight:700;color:var(--color-success)">{{ feedbackAgreeCount }}</span><span style="font-size:12px;color:var(--color-text-muted);margin-left:4px">认可</span></div>
                <div><span style="font-size:28px;font-weight:700;color:var(--color-danger)">{{ feedbackDisagreeCount }}</span><span style="font-size:12px;color:var(--color-text-muted);margin-left:4px">需复核</span></div>
              </div>
              <div style="height:8px;background:var(--color-border-light);border-radius:4px;overflow:hidden;display:flex">
                <div :style="{ width: (feedbackAgreeCount / validFeedbacks.length * 100) + '%', height: '100%', background: 'var(--color-success)' }"></div>
                <div :style="{ width: (feedbackDisagreeCount / validFeedbacks.length * 100) + '%', height: '100%', background: 'var(--color-danger)' }"></div>
              </div>
              <p style="font-size:12px;color:var(--color-text-muted);margin-top:8px">AI 评估与 HR 判断的一致率：{{ Math.round(feedbackAgreeCount / validFeedbacks.length * 100) }}%</p>
            </div>
            <div v-else style="margin-top:var(--space-md)">
              <p class="text-muted text-sm">暂无足够数据计算一致性</p>
              <p style="font-size:12px;color:var(--color-text-muted);margin-top:4px">需要至少 3 条有效 HR 反馈（当前：{{ validFeedbacks.length }} 条有效 / {{ feedbacks.length }} 条总计）</p>
            </div>
          </div>

          <div class="analytics-card">
            <h3>最近 HR 反馈</h3>
            <div v-if="validFeedbacks.length" style="display:flex;flex-direction:column;gap:6px;margin-top:var(--space-md)">
              <div v-for="fb in validFeedbacks.slice(0, 8)" :key="fb.id" style="font-size:12px;display:flex;gap:8px;align-items:center;padding:4px 0;border-bottom:1px solid var(--color-border-light)">
                <span class="badge" :class="fb.feedbackType === 'LIKE' ? 'badge-success' : 'badge-danger'" style="font-size:10px">{{ fb.feedbackType === 'LIKE' ? '认可' : '复核' }}</span>
                <span class="text-muted text-xs">{{ tasks.find(t => t.traceId === fb.traceId)?.fileName || fb.traceId.substring(0, 8) }}</span>
                <span class="truncate" style="flex:1">{{ fb.humanComment }}</span>
                <span class="text-muted text-xs">{{ fb.reviewer }}</span>
              </div>
            </div>
            <p v-else class="text-muted text-sm">暂无有效反馈</p>
          </div>
        </div>
      </section>

      <!-- ========== UPLOAD MODAL ========== -->
      <div v-if="showUploadModal" class="upload-overlay" @click.self="showUploadModal = false">
        <div class="upload-modal">
          <h2>上传简历评估</h2>
          <div class="form-field mb-lg" style="display:flex;align-items:center;gap:12px">
            <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px">
              <input type="checkbox" v-model="autoMatchJd" style="accent-color:var(--color-primary)" />
              <span>RAG 智能匹配岗位</span>
            </label>
            <span style="font-size:11px;color:var(--color-text-muted)">{{ autoMatchJd ? '系统将自动从 JD 库中匹配最佳岗位' : '手动选择目标岗位' }}</span>
          </div>
          <div v-if="!autoMatchJd" class="form-field mb-lg">
            <label>目标岗位</label>
            <select class="form-input" v-model="selectedJobId">
              <option v-for="job in jobs" :key="job.id" :value="job.id">{{ job.title }}</option>
            </select>
          </div>
          <label class="upload-zone">
            <input type="file" accept=".pdf,.txt,.md,.csv" multiple @change="importResume" />
            <div class="upload-label">{{ queuedFiles.length ? `${queuedFiles.length} 份简历已选择` : '点击选择或拖入简历文件' }}</div>
            <div class="upload-hint">支持 PDF、TXT、MD、CSV</div>
          </label>
          <div v-if="queuedFiles.length" style="margin-top:12px;font-size:12px;color:var(--color-text-secondary)">
            <div v-for="f in queuedFiles" :key="f.name">{{ f.name }} ({{ (f.size/1024).toFixed(0) }}KB)</div>
          </div>
          <div class="form-field mt-lg">
            <label>或粘贴简历文本</label>
            <textarea class="form-input" v-model="pastedResume" rows="4" placeholder="粘贴简历正文..." />
          </div>
          <div class="flex gap-sm mt-lg">
            <button class="btn btn-primary" :disabled="loading || !canStartEvaluation" @click="createEvaluations">{{ loading ? '处理中...' : '开始评估' }}</button>
            <button class="btn btn-ghost" @click="showUploadModal = false">取消</button>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<script lang="ts">
function renderMarkdown(source: string): string {
  if (!source) return '';
  return source
    .replace(/^#### (.+)$/gm, '<h4>$1</h4>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h2>$1</h2>')
    .replace(/^---$/gm, '<hr />')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/^[-*] (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>')
    .replace(/^(?!<[hulo])(.*\S.*)$/gm, '<p>$1</p>')
    .replace(/\n{2,}/g, '');
}

export default {};
</script>
