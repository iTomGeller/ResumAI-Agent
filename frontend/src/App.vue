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
  summary: string;
  durationMs: number;
  tokenCost: number;
  strengths: string[];
  risks: string[];
  interviewQuestions: string[];
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

interface GraphNode {
  id: string;
  label: string;
  type: string;
  score: number;
}

interface GraphEdge {
  from: string;
  to: string;
  label: string;
  confidence: number;
}

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

interface MarkdownBlock {
  type: 'heading' | 'paragraph' | 'list' | 'rule';
  level?: number;
  text?: string;
  items?: string[];
}

interface InlinePart {
  text: string;
  strong: boolean;
}

const JOBS_STORAGE_KEY = 'resumai.jobs.v2';
const sampleResume = `候选人 6 年 Java 后端经验，长期负责 Spring Boot 微服务、MySQL、Redis、Docker 云部署与线上可观测体系建设。
最近项目中参与 AI Agent 简历评估平台，负责 TraceId 链路、SSE 实时事件、RAG 证据召回、Docker Compose 上线和故障排查。
候选人熟悉团队协作、代码评审、生产问题复盘，也具备一定的系统设计和跨团队沟通经验。`;

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

const navItems = [
  { key: 'dashboard', label: '总览', description: '招聘工作台' },
  { key: 'jobs', label: '岗位 JD', description: '长期维护' },
  { key: 'candidates', label: '候选人', description: '批量评估' },
  { key: 'report', label: '报告', description: '对比阅读' },
  { key: 'evolution', label: '每日进化', description: '复盘沉淀' }
] as const;

const appView = ref<(typeof navItems)[number]['key']>('dashboard');
const detailTab = ref<'summary' | 'trace' | 'graph' | 'feedback'>('summary');
const loading = ref(false);
const refreshing = ref(false);
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
const healthStatus = ref('检查中');
const queuedFiles = ref<File[]>([]);
const pastedResume = ref('');
const candidateSearch = ref('');
const statusFilter = ref('ALL');
const selectedJobId = ref('');
const jobs = ref<JobProfile[]>(loadJobs());
const jobDraft = reactive<JobProfile>({ ...jobs.value[0] });
const publicUrl = window.location.origin;

let eventSource: EventSource | null = null;
const pollTimers = new Map<string, number>();

const activeTask = computed(() => tasks.value.find((task) => task.traceId === activeTraceId.value) ?? tasks.value[0]);
const selectedJob = computed(() => jobs.value.find((job) => job.id === selectedJobId.value) ?? jobs.value[0]);
const runningTasks = computed(() => tasks.value.filter((task) => task.status === 'RUNNING'));
const completedTasks = computed(() => tasks.value.filter((task) => task.status === 'SUCCESS'));
const failedTasks = computed(() => tasks.value.filter((task) => task.status === 'FAILED'));
const reportBlocks = computed(() => parseMarkdown(activeTask.value?.summary || ''));
const activeCandidateIndex = computed(() => tasks.value.findIndex((task) => task.traceId === activeTraceId.value));
const reportCandidateRows = computed(() => filteredCandidates.value.length ? filteredCandidates.value : tasks.value);
const recommendationLabel = computed(() => {
  const value = activeTask.value?.recommendation;
  if (value === 'STRONG_RECOMMEND') {
    return '强烈推荐进入面试';
  }
  if (value === 'RECOMMEND') {
    return '建议进入面试';
  }
  if (value === 'NEED_MANUAL_REVIEW') {
    return '建议人工复核';
  }
  return '等待评估';
});
const queuedFileLabel = computed(() => {
  if (!queuedFiles.value.length) {
    return '选择一批简历文件，或在右侧粘贴一份文本简历';
  }
  return queuedFiles.value.length === 1
    ? `${queuedFiles.value[0].name} · ${(queuedFiles.value[0].size / 1024).toFixed(1)} KB`
    : `${queuedFiles.value.length} 份简历待评估`;
});
const canStartEvaluation = computed(() => Boolean(selectedJob.value?.description.trim() && (queuedFiles.value.length || pastedResume.value.trim())));
const filteredCandidates = computed(() => {
  const query = candidateSearch.value.trim().toLowerCase();
  return tasks.value.filter((task) => {
    const matchStatus = statusFilter.value === 'ALL' || task.status === statusFilter.value;
    const matchQuery = !query || [task.fileName, task.jobCategory, task.recommendation].join(' ').toLowerCase().includes(query);
    return matchStatus && matchQuery;
  });
});
const traceSteps = computed(() => traces.value.map((event, index) => ({
  ...event,
  stageNo: index + 1,
  stageLabel: traceStageLabel(event),
  statusLabel: eventStatusText(event.status),
  evidence: traceEvidence(event)
})));
const graphSkillNodes = computed(() => graphNodes.value.filter((node) => node.type === 'skill'));
const graphRiskNodes = computed(() => graphNodes.value.filter((node) => node.type === 'risk'));
const graphOtherNodes = computed(() => graphNodes.value.filter((node) => node.type !== 'skill' && node.type !== 'risk'));
const dailyReport = computed(() => {
  const finished = completedTasks.value;
  const averageScore = finished.length
    ? finished.reduce((sum, task) => sum + (task.overallScore || 0), 0) / finished.length
    : 0;
  const allRisks = finished.flatMap((task) => task.risks || []).filter(Boolean);
  const lowScoreTasks = finished.filter((task) => task.overallScore && task.overallScore < 75);
  const negativeFeedbacks = feedbacks.value.filter((item) => item.ratingScore < 4);
  const frequentRisks = topTerms(allRisks, 5);
  return {
    date: new Date().toLocaleDateString('zh-CN'),
    total: tasks.value.length,
    finished: finished.length,
    running: runningTasks.value.length,
    failed: failedTasks.value.length,
    averageScore,
    feedbackCount: feedbacks.value.length,
    negativeFeedbackCount: negativeFeedbacks.length,
    frequentRisks,
    lowScoreTasks,
    actions: buildEvolutionActions(frequentRisks, lowScoreTasks.length, negativeFeedbacks.length)
  };
});

watch(jobs, (value) => {
  localStorage.setItem(JOBS_STORAGE_KEY, JSON.stringify(value));
}, { deep: true });

watch(selectedJobId, () => {
  syncJobDraft();
});

onMounted(async () => {
  if (!selectedJobId.value && jobs.value.length) {
    selectedJobId.value = jobs.value[0].id;
  }
  syncJobDraft();
  await refreshAll();
  await loadHealth();
  if (activeTraceId.value) {
    subscribeTrace(activeTraceId.value);
  }
});

onBeforeUnmount(() => {
  eventSource?.close();
  pollTimers.forEach((timer) => window.clearTimeout(timer));
});

function loadJobs(): JobProfile[] {
  try {
    const raw = localStorage.getItem(JOBS_STORAGE_KEY);
    if (!raw) {
      return defaultJobs;
    }
    const parsed = JSON.parse(raw) as JobProfile[];
    return parsed.length ? parsed : defaultJobs;
  } catch {
    return defaultJobs;
  }
}

function syncJobDraft() {
  const source = selectedJob.value ?? defaultJobs[0];
  Object.assign(jobDraft, { ...source });
}

function createJob() {
  const job: JobProfile = {
    id: `job-${Date.now()}`,
    title: '新岗位 JD',
    department: '未分组',
    level: 'Mid',
    category: 'TECH',
    description: '请填写岗位职责、硬性要求、加分项、风险红线和面试关注点。',
    createdAt: new Date().toISOString()
  };
  jobs.value = [job, ...jobs.value];
  selectedJobId.value = job.id;
  appView.value = 'jobs';
}

function saveJob() {
  jobs.value = jobs.value.map((job) => job.id === jobDraft.id ? { ...jobDraft } : job);
  successMessage.value = '岗位 JD 已保存，后续批量评估会复用这份标准。';
}

function deleteJob() {
  if (jobs.value.length <= 1) {
    errorMessage.value = '至少保留一个岗位 JD。';
    return;
  }
  jobs.value = jobs.value.filter((job) => job.id !== jobDraft.id);
  selectedJobId.value = jobs.value[0].id;
}

async function createEvaluations() {
  errorMessage.value = '';
  successMessage.value = '';
  if (!canStartEvaluation.value || !selectedJob.value) {
    errorMessage.value = '请先选择岗位 JD，并上传简历或粘贴简历文本。';
    return;
  }
  loading.value = true;
  appView.value = 'candidates';
  try {
    const createdTasks: TaskResponse[] = [];
    if (queuedFiles.value.length) {
      for (const file of queuedFiles.value) {
        const response = await createUploadTask(file);
        if (!response.ok) {
          throw new Error(await response.text());
        }
        createdTasks.push((await response.json()) as TaskResponse);
      }
    } else {
      const response = await createTextTask();
      if (!response.ok) {
        throw new Error(await response.text());
      }
      createdTasks.push((await response.json()) as TaskResponse);
    }
    activeTraceId.value = createdTasks[0].traceId;
    subscribeTrace(createdTasks[0].traceId);
    await refreshAll();
    createdTasks.forEach((task) => startPolling(task.traceId));
    successMessage.value = createdTasks.length > 1
      ? `已创建 ${createdTasks.length} 个候选人评估任务。`
      : '已创建 1 个候选人评估任务。';
    queuedFiles.value = [];
    pastedResume.value = '';
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '创建评估失败';
  } finally {
    loading.value = false;
  }
}

async function createTextTask() {
  return fetch('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      fileName: 'pasted-resume.txt',
      jobCategory: selectedJob.value.category,
      executionMode: 'DAG_CONCURRENT',
      jobDescription: selectedJob.value.description,
      resumeText: pastedResume.value
    })
  });
}

async function createUploadTask(file: File) {
  const body = new FormData();
  body.append('file', file);
  body.append('jobCategory', selectedJob.value.category);
  body.append('executionMode', 'DAG_CONCURRENT');
  body.append('jobDescription', selectedJob.value.description);
  return fetch('/api/tasks/upload', { method: 'POST', body });
}

async function refreshAll() {
  refreshing.value = true;
  try {
    const results = await Promise.allSettled([loadTasks(), loadMetrics(), loadFeedbacks()]);
    const failures = results.filter((r) => r.status === 'rejected');
    if (failures.length === results.length) {
      errorMessage.value = '后端服务连接失败，请稍后重试';
    } else if (failures.length > 0) {
      errorMessage.value = '';
    }
    if (activeTraceId.value) {
      await Promise.allSettled([loadTraces(activeTraceId.value), loadGraph(activeTraceId.value)]);
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '刷新数据失败';
  } finally {
    refreshing.value = false;
  }
}

async function loadHealth() {
  try {
    const response = await fetch('/api/health');
    const health = (await response.json()) as { status?: string };
    healthStatus.value = health.status ?? 'UNKNOWN';
  } catch {
    healthStatus.value = 'DOWN';
  }
}

async function loadTasks() {
  const response = await fetch('/api/tasks');
  if (!response.ok) {
    throw new Error('任务列表加载失败');
  }
  tasks.value = (await response.json()) as TaskResponse[];
  if (!activeTraceId.value && tasks.value.length) {
    activeTraceId.value = tasks.value[0].traceId;
  }
}

async function loadMetrics() {
  const response = await fetch('/api/metrics');
  if (!response.ok) {
    throw new Error('性能指标加载失败');
  }
  metrics.value = (await response.json()) as Metrics;
}

async function loadTraces(traceId: string) {
  const response = await fetch(`/api/traces/${traceId}`);
  if (!response.ok) {
    throw new Error('Trace 加载失败');
  }
  traces.value = (await response.json()) as TraceEvent[];
}

async function loadGraph(traceId: string) {
  const response = await fetch(`/api/graphs/${traceId}`);
  if (!response.ok) {
    throw new Error('GraphRAG 图谱加载失败');
  }
  const graph = (await response.json()) as { nodes: GraphNode[]; edges: GraphEdge[] };
  graphNodes.value = graph.nodes;
  graphEdges.value = graph.edges;
}

async function loadFeedbacks() {
  const response = await fetch('/api/feedback');
  if (!response.ok) {
    throw new Error('反馈列表加载失败');
  }
  feedbacks.value = (await response.json()) as FeedbackResponse[];
}

function selectTask(task: TaskResponse) {
  activeTraceId.value = task.traceId;
  detailTab.value = 'summary';
  subscribeTrace(task.traceId);
  refreshAll();
  appView.value = task.status === 'RUNNING' ? 'candidates' : 'report';
  if (task.status === 'RUNNING') {
    startPolling(task.traceId);
  }
}

function selectAdjacentCandidate(direction: -1 | 1) {
  if (!tasks.value.length) {
    return;
  }
  const current = activeCandidateIndex.value >= 0 ? activeCandidateIndex.value : 0;
  const next = (current + direction + tasks.value.length) % tasks.value.length;
  selectTask(tasks.value[next]);
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
  if (existing) {
    window.clearTimeout(existing);
  }
  const poll = async () => {
    await refreshAll();
    const current = tasks.value.find((task) => task.traceId === traceId);
    if (current?.status === 'RUNNING') {
      pollTimers.set(traceId, window.setTimeout(poll, 1800));
      return;
    }
    pollTimers.delete(traceId);
  };
  pollTimers.set(traceId, window.setTimeout(poll, 1800));
}

function importResume(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  if (!files.length) {
    return;
  }
  const invalidFile = files.find((file) => !['pdf', 'txt', 'md', 'csv'].includes(file.name.split('.').pop()?.toLowerCase() ?? ''));
  if (invalidFile) {
    queuedFiles.value = [];
    errorMessage.value = `暂不支持 ${invalidFile.name}，请上传 PDF/TXT/MD/CSV，Word 简历请另存为 PDF。`;
    input.value = '';
    return;
  }
  queuedFiles.value = files;
  successMessage.value = `已加入 ${files.length} 份简历，选择岗位 JD 后即可批量评估。`;
  input.value = '';
}

function useSample() {
  queuedFiles.value = [];
  pastedResume.value = sampleResume;
  appView.value = 'candidates';
  successMessage.value = '已填入示例简历，可直接创建文本评估任务。';
}

function clearUpload() {
  queuedFiles.value = [];
  pastedResume.value = '';
  successMessage.value = '';
  errorMessage.value = '';
}

async function sendFeedback(score: number) {
  if (!activeTask.value) {
    errorMessage.value = '请先选择一个已评估候选人。';
    return;
  }
  const response = await fetch('/api/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      traceId: activeTask.value.traceId,
      ratingScore: score,
      feedbackType: score >= 4 ? 'LIKE' : 'DISLIKE',
      humanComment: feedbackText.value || 'HR 已确认本次评估结果。',
      reviewer: 'HR'
    })
  });
  if (!response.ok) {
    errorMessage.value = '反馈提交失败';
    return;
  }
  feedbackText.value = '';
  successMessage.value = '反馈已写入 Meta-Agent 反思池。';
  await loadTraces(activeTask.value.traceId);
}

function statusText(status?: string) {
  if (status === 'SUCCESS') {
    return '已完成';
  }
  if (status === 'RUNNING') {
    return '评估中';
  }
  if (status === 'FAILED') {
    return '失败';
  }
  return '待启动';
}

function formatDuration(duration?: number) {
  if (!duration) {
    return '0s';
  }
  return `${(duration / 1000).toFixed(1)}s`;
}

function traceStageLabel(event: TraceEvent) {
  const role = event.agentRole.toLowerCase();
  if (role.includes('orchestrator')) {
    return '任务编排';
  }
  if (role.includes('parser')) {
    return '简历解析';
  }
  if (role.includes('dag')) {
    return '并发评估';
  }
  if (role.includes('tech')) {
    return '技能匹配';
  }
  if (role.includes('project')) {
    return '项目深度';
  }
  if (role.includes('risk')) {
    return '风险识别';
  }
  if (role.includes('deepseek') || role.includes('llm')) {
    return '报告生成';
  }
  if (role.includes('human') || role.includes('feedback')) {
    return '人工反馈';
  }
  return '评估步骤';
}

function eventStatusText(status: string) {
  if (status === 'SUCCESS') {
    return '完成';
  }
  if (status === 'FAILED') {
    return '失败';
  }
  return '进行中';
}

function traceEvidence(event: TraceEvent) {
  if (event.tokenCost > 0) {
    return `耗时 ${event.durationMs}ms，消耗 ${event.tokenCost} tokens。`;
  }
  return `耗时 ${event.durationMs}ms，未调用大模型。`;
}

function topTerms(items: string[], limit: number) {
  const counts = new Map<string, number>();
  for (const item of items) {
    const key = item.replace(/[：:，。,.\s].*$/, '').trim().slice(0, 24);
    if (!key) {
      continue;
    }
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit)
    .map(([label, count]) => ({ label, count }));
}

function buildEvolutionActions(
  frequentRisks: Array<{ label: string; count: number }>,
  lowScoreCount: number,
  negativeFeedbackCount: number
) {
  const actions = [
    '明天评估时优先要求候选人提供项目量化结果、职责边界和生产事故复盘证据。',
    '对强推荐候选人追加“经验真实性”和“深度验证”面试追问，避免只看关键词匹配。'
  ];
  if (frequentRisks.length) {
    actions.unshift(`高频风险集中在「${frequentRisks[0].label}」，JD 和报告提示词需要强化该项证据要求。`);
  }
  if (lowScoreCount > 0) {
    actions.push(`今日有 ${lowScoreCount} 位候选人低于 75 分，建议复查岗位要求是否过宽或简历证据是否不足。`);
  }
  if (negativeFeedbackCount > 0) {
    actions.push(`收到 ${negativeFeedbackCount} 条低分反馈，Meta-Agent 应优先回看这些 Trace 并调整评分口径。`);
  }
  return actions;
}

function parseMarkdown(source: string): MarkdownBlock[] {
  const lines = source.split(/\r?\n/);
  const blocks: MarkdownBlock[] = [];
  let listItems: string[] = [];
  const flushList = () => {
    if (listItems.length) {
      blocks.push({ type: 'list', items: listItems });
      listItems = [];
    }
  };
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      flushList();
      continue;
    }
    if (/^[-*_]{3,}$/.test(line)) {
      flushList();
      blocks.push({ type: 'rule' });
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushList();
      blocks.push({ type: 'heading', level: heading[1].length, text: heading[2] });
      continue;
    }
    const list = line.match(/^[-*]\s+(.+)$|^\d+[.)]\s+(.+)$/);
    if (list) {
      listItems.push(list[1] || list[2]);
      continue;
    }
    flushList();
    blocks.push({ type: 'paragraph', text: line });
  }
  flushList();
  return blocks.length ? blocks : [{ type: 'paragraph', text: '报告生成中。' }];
}

function inlineParts(text = ''): InlinePart[] {
  const parts: InlinePart[] = [];
  const pattern = /\*\*(.+?)\*\*/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push({ text: text.slice(lastIndex, match.index), strong: false });
    }
    parts.push({ text: match[1], strong: true });
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    parts.push({ text: text.slice(lastIndex), strong: false });
  }
  return parts.length ? parts : [{ text, strong: false }];
}
</script>

<template>
  <main class="app-shell enterprise-app">
    <aside class="sidebar">
      <div class="brand">
        <span>RA</span>
        <div>
          <strong>ResumAI</strong>
          <small>{{ publicUrl }}</small>
        </div>
      </div>
      <nav class="side-nav" aria-label="主导航">
        <button
          v-for="item in navItems"
          :key="item.key"
          :class="{ active: appView === item.key }"
          @click="appView = item.key"
        >
          <strong>{{ item.label }}</strong>
          <small>{{ item.description }}</small>
        </button>
      </nav>
      <div class="sidebar-status">
        <span class="status-dot" :class="{ online: healthStatus === 'UP' }"></span>
        <div>
          <strong>{{ healthStatus }}</strong>
          <small>ECS Docker Stack</small>
        </div>
      </div>
    </aside>

    <section class="main-panel">
      <header class="page-header">
        <div>
          <p class="eyebrow">AI Resume Evaluation Workspace</p>
          <h1>{{ appView === 'dashboard' ? '招聘评估总览' : appView === 'jobs' ? '岗位 JD 库' : appView === 'candidates' ? '候选人管理' : appView === 'report' ? '候选人报告' : '每日总结进化' }}</h1>
          <p>{{ appView === 'dashboard' ? '长期维护岗位、批量评估候选人，并持续沉淀历史报告。' : appView === 'jobs' ? '岗位 JD 是可复用资产，不需要每次重新输入。' : appView === 'candidates' ? '围绕岗位批量上传简历，并从列表进入单个候选人报告。' : appView === 'report' ? '左侧保留候选人队列，右侧阅读报告、证据链和专家过程。' : '每天汇总评估结果、HR 反馈和高频风险，沉淀下一轮评分策略。' }}</p>
        </div>
        <div class="header-actions">
          <button class="ghost" :disabled="refreshing" @click="refreshAll">{{ refreshing ? '刷新中...' : '刷新数据' }}</button>
          <button class="primary" @click="appView = 'candidates'">批量上传</button>
        </div>
      </header>

      <p v-if="errorMessage" class="notice error">{{ errorMessage }}</p>
      <p v-if="successMessage" class="notice success">{{ successMessage }}</p>

      <section v-if="appView === 'dashboard'" class="dashboard-view">
        <div class="kpi-grid">
          <article><span>候选人</span><strong>{{ metrics?.totalTasks ?? tasks.length }}</strong></article>
          <article><span>评估中</span><strong>{{ runningTasks.length }}</strong></article>
          <article><span>已完成</span><strong>{{ completedTasks.length }}</strong></article>
          <article><span>平均分</span><strong>{{ metrics?.averageScore?.toFixed(1) ?? '0.0' }}</strong></article>
        </div>
        <div class="content-grid">
          <article class="card">
            <div class="card-head">
              <h2>最近候选人</h2>
              <button class="ghost compact" @click="appView = 'candidates'">查看全部</button>
            </div>
            <div class="candidate-list compact-list">
              <button v-for="task in tasks.slice(0, 6)" :key="task.traceId" class="candidate-row" @click="selectTask(task)">
                <span class="candidate-name">{{ task.fileName }}</span>
                <span>{{ statusText(task.status) }}</span>
                <span>{{ task.jobCategory }}</span>
                <strong>{{ task.overallScore || '-' }}</strong>
                <small>{{ formatDuration(task.durationMs) }}</small>
              </button>
              <p v-if="!tasks.length" class="muted empty-copy">暂无候选人，先去批量上传简历。</p>
            </div>
          </article>
          <article class="card">
            <div class="card-head">
              <h2>岗位 JD</h2>
              <button class="ghost compact" @click="createJob">新增岗位</button>
            </div>
            <div class="job-list">
              <button v-for="job in jobs" :key="job.id" :class="{ active: job.id === selectedJobId }" @click="selectedJobId = job.id; appView = 'jobs'">
                <strong>{{ job.title }}</strong>
                <span>{{ job.department }} · {{ job.level }} · {{ job.category }}</span>
              </button>
            </div>
          </article>
        </div>
      </section>

      <section v-if="appView === 'jobs'" class="jobs-view">
        <aside class="card job-library">
          <div class="card-head">
            <h2>岗位列表</h2>
            <button class="primary compact" @click="createJob">新增</button>
          </div>
          <div class="job-list">
            <button v-for="job in jobs" :key="job.id" :class="{ active: job.id === selectedJobId }" @click="selectedJobId = job.id">
              <strong>{{ job.title }}</strong>
              <span>{{ job.department }} · {{ job.level }}</span>
            </button>
          </div>
        </aside>
        <article class="card job-editor">
          <h2>编辑岗位 JD</h2>
          <div class="two-col">
            <label>岗位名称<input v-model="jobDraft.title" /></label>
            <label>部门<input v-model="jobDraft.department" /></label>
          </div>
          <div class="two-col">
            <label>级别<input v-model="jobDraft.level" /></label>
            <label>
              岗位类别
              <select v-model="jobDraft.category">
                <option value="TECH">技术岗</option>
                <option value="PRODUCT">产品岗</option>
                <option value="DESIGN">设计岗</option>
              </select>
            </label>
          </div>
          <label>JD 与评估标准<textarea v-model="jobDraft.description" rows="13" /></label>
          <div class="form-actions">
            <button class="primary" @click="saveJob">保存岗位 JD</button>
            <button class="danger" @click="deleteJob">删除</button>
          </div>
        </article>
      </section>

      <section v-if="appView === 'candidates'" class="candidates-view">
        <article class="card upload-card">
          <div class="upload-header">
            <div>
              <h2>批量导入候选人</h2>
              <p>选择一个岗位 JD，再上传多份简历。任务会进入下方候选人列表。</p>
            </div>
            <button class="ghost compact" @click="useSample">填入示例</button>
          </div>
          <div class="two-col">
            <label>
              评估岗位
              <select v-model="selectedJobId">
                <option v-for="job in jobs" :key="job.id" :value="job.id">{{ job.title }}</option>
              </select>
            </label>
            <label>
              当前岗位类别
              <input :value="selectedJob?.category ?? '-'" disabled />
            </label>
          </div>
          <label class="upload-zone">
            <input type="file" accept=".pdf,.txt,.md,.csv" multiple @change="importResume" />
            <strong>{{ queuedFiles.length ? `${queuedFiles.length} 份简历待评估` : '选择或拖入一批简历' }}</strong>
            <span>{{ queuedFileLabel }}</span>
          </label>
          <div v-if="queuedFiles.length" class="file-queue">
            <div v-for="file in queuedFiles" :key="`${file.name}-${file.size}`">
              <span>{{ file.name }}</span>
              <small>{{ (file.size / 1024).toFixed(1) }} KB</small>
            </div>
          </div>
          <label>也可以粘贴单份简历<textarea v-model="pastedResume" rows="6" placeholder="粘贴简历正文，用于快速单人评估。" /></label>
          <div class="form-actions">
            <button class="primary" :disabled="loading || !canStartEvaluation" @click="createEvaluations">{{ loading ? '创建中...' : '创建评估任务' }}</button>
            <button class="ghost" @click="clearUpload">清空导入区</button>
          </div>
        </article>

        <article class="card candidate-board">
          <div class="candidate-board-head">
            <div>
              <h2>候选人列表</h2>
              <p class="muted">这是用户真正会反复使用的主列表。</p>
            </div>
            <div class="board-stats">
              <span>全部 {{ filteredCandidates.length }}</span>
              <span>完成 {{ completedTasks.length }}</span>
              <span>运行 {{ runningTasks.length }}</span>
              <span>失败 {{ failedTasks.length }}</span>
            </div>
          </div>
          <div class="table-tools">
            <input v-model="candidateSearch" placeholder="搜索文件名、岗位或推荐结论" />
            <select v-model="statusFilter">
              <option value="ALL">全部状态</option>
              <option value="RUNNING">评估中</option>
              <option value="SUCCESS">已完成</option>
              <option value="FAILED">失败</option>
            </select>
          </div>
          <div class="candidate-list">
            <button v-for="task in filteredCandidates" :key="task.traceId" class="candidate-row" :class="{ active: task.traceId === activeTraceId }" @click="selectTask(task)">
              <span class="candidate-name">{{ task.fileName }}</span>
              <span>{{ statusText(task.status) }}</span>
              <span>{{ task.jobCategory }}</span>
              <strong>{{ task.overallScore || '-' }}</strong>
              <small>{{ formatDuration(task.durationMs) }}</small>
            </button>
            <p v-if="!filteredCandidates.length" class="muted empty-copy">没有匹配的候选人。</p>
          </div>
        </article>
      </section>

      <section v-if="appView === 'report'" class="report-view">
        <aside class="card report-candidate-rail">
          <div class="card-head">
            <div>
              <h2>候选人队列</h2>
              <p>看报告时必须能随时切换和对比候选人。</p>
            </div>
            <span class="rail-count">{{ reportCandidateRows.length }}</span>
          </div>
          <input v-model="candidateSearch" placeholder="搜索候选人" />
          <div class="rail-list">
            <button
              v-for="task in reportCandidateRows"
              :key="task.traceId"
              :class="{ active: task.traceId === activeTraceId }"
              @click="selectTask(task)"
            >
              <strong>{{ task.fileName }}</strong>
              <span>{{ statusText(task.status) }} · {{ task.jobCategory }} · {{ task.overallScore || '-' }} 分</span>
            </button>
          </div>
        </aside>

        <article v-if="activeTask" class="card report-reader">
          <div class="report-hero">
            <div class="score-ring"><strong>{{ activeTask.overallScore }}</strong><span>综合分</span></div>
            <div>
              <p class="eyebrow">{{ statusText(activeTask.status) }} · {{ formatDuration(activeTask.durationMs) }}</p>
              <h2>{{ recommendationLabel }}</h2>
              <p>{{ activeTask.fileName }}</p>
            </div>
            <div class="report-switcher">
              <button class="ghost compact" @click="selectAdjacentCandidate(-1)">上一个</button>
              <button class="ghost compact" @click="selectAdjacentCandidate(1)">下一个</button>
            </div>
          </div>
          <div class="tab-bar">
            <button :class="{ active: detailTab === 'summary' }" @click="detailTab = 'summary'">报告正文</button>
            <button :class="{ active: detailTab === 'trace' }" @click="detailTab = 'trace'">评估过程</button>
            <button :class="{ active: detailTab === 'graph' }" @click="detailTab = 'graph'">证据图谱</button>
            <button :class="{ active: detailTab === 'feedback' }" @click="detailTab = 'feedback'">反馈</button>
          </div>
          <section v-if="detailTab === 'summary'" class="tab-panel">
            <article class="markdown-report">
              <template v-for="(block, index) in reportBlocks" :key="index">
                <h2 v-if="block.type === 'heading' && block.level === 1"><template v-for="part in inlineParts(block.text)" :key="`${index}-${part.text}-${part.strong}`"><strong v-if="part.strong">{{ part.text }}</strong><span v-else>{{ part.text }}</span></template></h2>
                <h3 v-else-if="block.type === 'heading' && (block.level ?? 2) <= 3"><template v-for="part in inlineParts(block.text)" :key="`${index}-${part.text}-${part.strong}`"><strong v-if="part.strong">{{ part.text }}</strong><span v-else>{{ part.text }}</span></template></h3>
                <h4 v-else-if="block.type === 'heading'"><template v-for="part in inlineParts(block.text)" :key="`${index}-${part.text}-${part.strong}`"><strong v-if="part.strong">{{ part.text }}</strong><span v-else>{{ part.text }}</span></template></h4>
                <hr v-else-if="block.type === 'rule'" />
                <ul v-else-if="block.type === 'list'"><li v-for="item in block.items ?? []" :key="item"><template v-for="part in inlineParts(item)" :key="`${item}-${part.text}-${part.strong}`"><strong v-if="part.strong">{{ part.text }}</strong><span v-else>{{ part.text }}</span></template></li></ul>
                <p v-else><template v-for="part in inlineParts(block.text)" :key="`${index}-${part.text}-${part.strong}`"><strong v-if="part.strong">{{ part.text }}</strong><span v-else>{{ part.text }}</span></template></p>
              </template>
            </article>
            <div class="insight-grid">
              <section><h3>优势证据</h3><ul><li v-for="item in activeTask.strengths" :key="item">{{ item }}</li><li v-if="!activeTask.strengths?.length">暂无结构化优势。</li></ul></section>
              <section><h3>风险证据</h3><ul><li v-for="item in activeTask.risks" :key="item">{{ item }}</li><li v-if="!activeTask.risks?.length">暂无结构化风险。</li></ul></section>
            </div>
            <section class="questions"><h3>面试追问</h3><ol><li v-for="item in activeTask.interviewQuestions" :key="item">{{ item }}</li><li v-if="!activeTask.interviewQuestions?.length">暂无追问建议。</li></ol></section>
          </section>
          <section v-if="detailTab === 'trace'" class="tab-panel">
            <div class="human-trace">
              <div v-for="event in traceSteps" :key="event.spanId" class="trace-step" :class="{ failed: event.status === 'FAILED' }">
                <span class="step-index">{{ event.stageNo }}</span>
                <div>
                  <div class="step-head">
                    <strong>{{ event.stageLabel }}</strong>
                    <small>{{ event.statusLabel }} · {{ event.agentRole }}</small>
                  </div>
                  <h3>{{ event.title }}</h3>
                  <p>{{ event.detail }}</p>
                  <em>{{ event.evidence }}</em>
                </div>
              </div>
              <p v-if="!traceSteps.length" class="muted empty-copy">暂无 Trace，评估开始后会展示每一步的人类可读说明。</p>
            </div>
          </section>
          <section v-if="detailTab === 'graph'" class="tab-panel">
            <div class="evidence-map">
              <div class="map-center">
                <strong>{{ activeTask.fileName }}</strong>
                <span>候选人 · {{ activeTask.overallScore || '-' }} 分</span>
              </div>
              <div class="map-column">
                <h3>匹配技能</h3>
                <article v-for="node in graphSkillNodes" :key="node.id">
                  <strong>{{ node.label }}</strong>
                  <span>{{ node.score }} 分</span>
                  <i :style="{ width: `${Math.min(100, Math.max(0, node.score))}%` }"></i>
                </article>
              </div>
              <div class="map-column risk-column">
                <h3>风险信号</h3>
                <article v-for="node in graphRiskNodes" :key="node.id">
                  <strong>{{ node.label }}</strong>
                  <span>{{ node.score }} 分</span>
                  <i :style="{ width: `${Math.min(100, Math.max(0, node.score))}%` }"></i>
                </article>
              </div>
              <div class="map-column">
                <h3>岗位与项目</h3>
                <article v-for="node in graphOtherNodes" :key="node.id">
                  <strong>{{ node.label }}</strong>
                  <span>{{ node.type }} · {{ node.score }}</span>
                  <i :style="{ width: `${Math.min(100, Math.max(0, node.score))}%` }"></i>
                </article>
              </div>
            </div>
            <div class="edge-list">
              <p v-for="edge in graphEdges" :key="`${edge.from}-${edge.to}`">{{ edge.from }} → {{ edge.to }}：{{ edge.label }} · {{ Math.round(edge.confidence * 100) }}%</p>
              <p v-if="!graphEdges.length" class="muted">暂无图谱关系。</p>
            </div>
          </section>
          <section v-if="detailTab === 'feedback'" class="tab-panel">
            <h3>这份报告是否可用？</h3>
            <textarea v-model="feedbackText" rows="5" placeholder="例如：风险点准确，但项目深度判断还可以更严格。" />
            <div class="feedback-actions"><button @click="sendFeedback(5)">认可结论</button><button class="danger" @click="sendFeedback(2)">要求复核</button></div>
          </section>
        </article>
        <article v-else class="card empty-state"><strong>请选择候选人</strong><p>从候选人列表进入报告详情。</p></article>
      </section>

      <section v-if="appView === 'evolution'" class="evolution-view">
        <article class="card evolution-hero">
          <p class="eyebrow">{{ dailyReport.date }}</p>
          <h2>每日总结进化</h2>
          <p>系统把今天的评估、HR 反馈、高频风险和低分候选人汇总成下一轮策略，不让 Agent 每天从零开始。</p>
        </article>
        <div class="kpi-grid">
          <article><span>今日候选人</span><strong>{{ dailyReport.total }}</strong></article>
          <article><span>完成评估</span><strong>{{ dailyReport.finished }}</strong></article>
          <article><span>平均分</span><strong>{{ dailyReport.averageScore.toFixed(1) }}</strong></article>
          <article><span>HR 反馈</span><strong>{{ dailyReport.feedbackCount }}</strong></article>
        </div>
        <div class="content-grid">
          <article class="card">
            <h2>高频风险</h2>
            <div class="risk-list">
              <p v-for="risk in dailyReport.frequentRisks" :key="risk.label"><strong>{{ risk.label }}</strong><span>{{ risk.count }} 次</span></p>
              <p v-if="!dailyReport.frequentRisks.length" class="muted">暂无足够风险样本。</p>
            </div>
          </article>
          <article class="card">
            <h2>明日进化动作</h2>
            <ol class="action-list">
              <li v-for="action in dailyReport.actions" :key="action">{{ action }}</li>
            </ol>
          </article>
        </div>
      </section>
    </section>
  </main>
</template>
