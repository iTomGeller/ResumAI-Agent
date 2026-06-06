<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { splitTextPages, usePagination } from './composables/usePagination';
import { buildQuery, useServerPagination, type PageResult } from './composables/useServerPagination';
import {
  applyBusinessControls,
  applyPreset,
  defaultRagOptions,
  loadStoredRagOptions,
  saveStoredRagOptions,
  STRICTNESS_CHOICES,
  STRATEGY_CHOICES,
  STYLE_CHOICES,
  TOPK_CHOICES,
  type RagOptions,
  type RagPreset,
} from './composables/useRagPresets';

interface TaskQueueFields {
  queueStatus?: string;
  uploadedBy?: string;
  tenantId?: string;
  priority?: number;
  queuedAt?: string;
  startedAt?: string;
  finishedAt?: string;
  attemptCount?: number;
  nextRetryAt?: string;
  workerId?: string;
}

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
  queue?: TaskQueueFields;
  topJdMatches?: Array<{
    jdId: string;
    title: string;
    category: string;
    score: number;
    matchReasons?: string[];
    gaps?: string[];
    interviewChecks?: string[];
    skillMatchScore?: number;
    experienceMatchScore?: number;
    projectMatchScore?: number;
    riskPenalty?: number;
  }>;
  aiRecommendation?: string;
  decisionRationale?: string;
  riskSummary?: string;
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
  stepKind?: string;
  evidenceSummary?: string;
  laneId?: string;
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
  version?: number;
  updatedBy?: string;
}

interface TaskQueueStatus {
  queued: number;
  running: number;
  retrying: number;
  failed: number;
  stuck: number;
  pendingMessages: number;
  oldestWaitSeconds: number;
  activeWorkers: number;
  workerCapacity: number;
  workerUtilization: number;
  recentFailures: string[];
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
type DetailTab = 'resume' | 'report' | 'trace' | 'graph' | 'feedback';

const appView = ref<ViewName>('dashboard');
const detailTab = ref<DetailTab>('report');
const loading = ref(false);
const refreshing = ref(false);
const showUploadModal = ref(false);
const autoMatchJd = ref(true);
type UploadPhase = 'idle' | 'validating' | 'accepted' | 'evaluating';
const uploadPhase = ref<UploadPhase>('idle');
const uploadAbortController = ref<AbortController | null>(null);
const backgroundUploadNotice = ref('');
const taskStageHints = ref<Record<string, string>>({});
const tasks = ref<TaskResponse[]>([]);
const traces = ref<TraceEvent[]>([]);
const metrics = ref<Metrics | null>(null);
const graphNodes = ref<GraphNode[]>([]);
const graphEdges = ref<GraphEdge[]>([]);
const graphSource = ref<'NEO4J' | 'SIMULATED' | string>('SIMULATED');
const feedbacks = ref<FeedbackResponse[]>([]);
const activeTraceId = ref('');
const agentExecutionTree = ref<any>(null);
const expandedAgentNodes = reactive(new Set<number>([0]));
const expandedSubAgents = reactive(new Set<string>());
const feedbackText = ref('');
const errorMessage = ref('');
const successMessage = ref('');
const healthStatus = ref('...');
const embeddingHealth = ref<{ operational?: boolean; provider?: string; message?: string }>({});
const showRagDrawer = ref(false);
const ragDrawerTab = ref<'business' | 'compare' | 'expert'>('business');
const ragPresets = ref<RagPreset[]>([]);
const currentRagOptions = ref<RagOptions>(loadStoredRagOptions());
const ragBusinessTopK = ref(currentRagOptions.value.topK);
const ragStrictness = ref('balanced');
const ragStrategyChoice = ref(currentRagOptions.value.strategy);
const ragStyleChoice = ref('balanced');
const ragRerankerEnabled = ref(currentRagOptions.value.rerankerEnabled);
const ragPreviewText = ref('');
const ragPreviewResult = ref<any[]>([]);
const ragCompareResult = ref<Record<string, any>>({});
const ragCompareLoading = ref(false);
const ragAdvisor = ref<{ show?: boolean; message?: string; suggestedPreset?: string }>({});
const ragAdvisorDismissed = ref(false);
const ragCompareVariants = ref<Array<{ name: string; presetId: string }>>([
  { name: '⚡ 快速筛选', presetId: 'fast' },
  { name: '⭐ 平衡推荐', presetId: 'balanced' },
  { name: '🎯 严格匹配', presetId: 'strict' },
]);
const queuedFiles = ref<File[]>([]);
const pastedResume = ref('');
const candidateSearch = ref('');
const statusFilter = ref('ALL');
const queueStatusFilter = ref('ALL');
const scoreFilter = ref<'ALL' | '90_PLUS' | '80_89' | '70_79' | 'LOW'>('ALL');
const recommendationFilter = ref<'ALL' | 'RECOMMEND' | 'REVIEW'>('ALL');
const candidateSortBy = ref<'created' | 'score_desc' | 'score_asc' | 'duration_desc' | 'duration_asc' | 'priority' | 'queued_at'>('created');
const taskQueueStatus = ref<TaskQueueStatus | null>(null);
const jobSearch = ref('');
const jobCategoryFilter = ref('ALL');
const jobLevelFilter = ref('ALL');
const jobSortBy = ref<'createdAt' | 'title' | 'category'>('createdAt');
const jobDescriptionPage = ref(1);
const expandedListKeys = ref<Record<string, boolean>>({});
const selectedJobId = ref('');
const jobs = ref<JobProfile[]>(loadJobs());
const jobDraft = reactive<JobProfile>({ ...jobs.value[0] });

const candidateListItems = ref<TaskResponse[]>([]);
const jobListItems = ref<JobProfile[]>([]);
const candidateServerPag = useServerPagination(10);
const jobServerPag = useServerPagination(8);
const tasksLoaded = ref(false);
const tasksError = ref(false);

let eventSource: EventSource | null = null;
const resumeViewMode = ref<'pdf' | 'text'>('pdf');
const pdfLoading = ref(false);
const pdfError = ref(false);
let pdfLoadTimer: number | null = null;
const pollTimers = new Map<string, number>();

const pdfPreviewUrl = computed(() => {
  if (!activeTask.value?.resumeFileUrl) return '';
  const base = activeTask.value.resumeFileUrl;
  return base.includes('#') ? base : `${base}#toolbar=1&navpanes=0`;
});

const activeTask = computed(() => tasks.value.find((t) => t.traceId === activeTraceId.value) ?? null);
const langfuseTraceUrl = computed(() => `http://8.166.136.122:3001/project/resumai-project/sessions/${activeTask.value?.traceId || ''}`);
const selectedJob = computed(() => jobs.value.find((j) => j.id === selectedJobId.value) ?? jobs.value[0]);
const runningTasks = computed(() => tasks.value.filter((t) => resolveQueueStatus(t) === 'RUNNING'));
const completedTasks = computed(() => tasks.value.filter((t) => t.status === 'SUCCESS'));
const queuedTasksCount = computed(() => taskQueueStatus.value?.queued ?? tasks.value.filter((t) => resolveQueueStatus(t) === 'QUEUED').length);

const jobDescriptionPages = computed(() => splitTextPages(jobDraft.description || '', 1200));


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
  const map = new Map<string, { total: number; recommended: number; review: number; jdSum: number; jdCount: number }>();
  for (const t of completedTasks.value) {
    const cat = t.matchedJdTitle || t.jobCategory || 'UNKNOWN';
    const entry = map.get(cat) || { total: 0, recommended: 0, review: 0, jdSum: 0, jdCount: 0 };
    entry.total++;
    if ((t.recommendation || '').includes('RECOMMEND')) entry.recommended++;
    else entry.review++;
    if (t.jdMatchScore) {
      entry.jdSum += t.jdMatchScore;
      entry.jdCount++;
    }
    map.set(cat, entry);
  }
  return [...map.entries()].map(([category, stats]) => ({
    category,
    total: stats.total,
    recommended: stats.recommended,
    review: stats.review,
    avgJdMatch: stats.jdCount ? Math.round(stats.jdSum / stats.jdCount * 100) : 0,
    rate: stats.total ? Math.round(stats.recommended / stats.total * 100) : 0,
  }));
});

function inferStepKind(step: { stepKind?: string; agentRole: string; eventType: string; laneId?: string }): string {
  if (step.stepKind) return step.stepKind;
  const r = step.agentRole.toLowerCase();
  if (step.eventType === 'TASK_CREATED') return 'task_create';
  if (step.eventType === 'UPLOAD_PARSE') return 'upload_parse';
  if (r.includes('parser')) return 'resume_parse';
  if (r.includes('jdmatch') || step.eventType === 'JD_MATCH') return 'jd_match';
  if (r.includes('tech') || r.includes('project') || r.includes('risk')) return 'skill_eval';
  if (r.includes('graph')) return 'graph_extraction';
  if (r.includes('external') || r.includes('github')) return 'external_enrichment';
  if (r.includes('hybrid')) return 'rag_retrieve';
  if (r.includes('deepseek') || step.eventType === 'LLM_COMPLETE') return 'llm_complete';
  if (r.includes('ragas') || step.eventType === 'QUALITY_CHECK') return 'quality_check';
  if (r.includes('report') || step.eventType === 'REPORT_READY') return 'report_generate';
  return '';
}

function agentLabelCn(agentRole: string, fallback?: string): string {
  const r = agentRole.toLowerCase();
  if (r.includes('orchestrator')) return '任务编排器';
  if (r.includes('parser')) return '简历解析';
  if (r.includes('jdmatch')) return 'JD 智能匹配';
  if (r.includes('tech')) return '技术能力评估';
  if (r.includes('project')) return '项目经历评估';
  if (r.includes('risk')) return '风险信号识别';
  if (r.includes('hybrid')) return '混合检索 RAG';
  if (r.includes('deepseek')) return 'DeepSeek 大模型';
  if (r.includes('ragas') || r.includes('judge')) return 'RAGAS 质量评估';
  if (r.includes('report') || r.includes('final')) return '最终报告生成';
  if (r.includes('external') || r.includes('github')) return '外部作品检索';
  return fallback || agentRole;
}

function ragFallbackHint(reason?: string): string {
  if (!reason) return '';
  if (reason.includes('RAG_DISABLED_BY_CONFIG')) return '当前已自动回退到关键词匹配，岗位匹配仍可用';
  if (reason.includes('EMBEDDING_API_KEY_MISSING')) return '缺少 EMBEDDING_API_KEY，已回退关键词匹配';
  if (reason.includes('ModelNotFoundException')) return '嵌入模型不可用，已回退关键词匹配';
  return reason;
}

const embeddingBanner = computed(() => {
  if (embeddingHealth.value.operational) {
    return embeddingHealth.value.message || '';
  }
  if (embeddingHealth.value.message) return embeddingHealth.value.message;
  return '当前已自动回退到关键词匹配，岗位匹配仍可用';
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
  const top = activeTask.value?.topJdMatches?.[0];
  if (top?.skillMatchScore != null) return Math.round(top.skillMatchScore * 100);
  const skills = graphNodes.value.filter(n => n.type === 'skill');
  if (!skills.length) return activeTask.value?.overallScore || 0;
  return Math.round(skills.reduce((s, n) => s + Math.min(100, n.score), 0) / skills.length);
});
const expMatchPercent = computed(() => {
  const top = activeTask.value?.topJdMatches?.[0];
  if (top?.experienceMatchScore != null) return Math.round(top.experienceMatchScore * 100);
  const jobs = graphNodes.value.filter(n => n.type === 'job' || n.type === 'project');
  if (!jobs.length) return Math.max(0, (activeTask.value?.overallScore || 70) - 5);
  return Math.round(jobs.reduce((s, n) => s + Math.min(100, n.score), 0) / jobs.length);
});
const eduMatchPercent = computed(() => {
  const edu = graphNodes.value.filter(n => n.type === 'education');
  if (edu.length) {
    return Math.round(edu.reduce((s, n) => s + Math.min(100, n.score), 0) / edu.length);
  }
  return Math.max(0, (activeTask.value?.overallScore || 70) + 5);
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

const candidatePagination = candidateServerPag;
const jobPagination = jobServerPag;
const dashboardRecentPagination = usePagination(tasks, 8);
const jobCategoryStatsPagination = usePagination(jobCategoryStats, 6);
const pendingReviewPagination = usePagination(pendingReviewTasks, 6);
const analyticsFeedbackPagination = usePagination(validFeedbacks, 8);
const jdMatchPagination = usePagination(jdMatchCards, 3);
const resumeTextPages = computed(() => splitTextPages(activeTask.value?.resumeText || '', 2400));
const resumeTextPagination = usePagination(resumeTextPages, 1);
const activeTaskFeedbacks = computed(() => feedbacks.value.filter((f) => f.traceId === activeTraceId.value));
const taskFeedbackPagination = usePagination(activeTaskFeedbacks, 5);

const showJdMatchPagination = computed(() => jdMatchCards.value.length > 5);
const displayJdMatches = computed(() =>
  showJdMatchPagination.value ? jdMatchPagination.pageItems.value : jdMatchCards.value
);

function listPreview<T>(key: string, rows: T[] = [], limit = 5): T[] {
  return expandedListKeys.value[key] ? rows : rows.slice(0, limit);
}

function toggleList(key: string) {
  expandedListKeys.value[key] = !expandedListKeys.value[key];
}

function listHasMore(rows: unknown[] = [], limit = 5): boolean {
  return rows.length > limit;
}

function jdMatchDisplayRank(idx: number): number {
  if (!showJdMatchPagination.value) return idx + 1;
  return (jdMatchPagination.page.value - 1) * jdMatchPagination.pageSize.value + idx + 1;
}

watch([candidateSearch, statusFilter, queueStatusFilter, recommendationFilter, scoreFilter, candidateSortBy], () => {
  candidatePagination.resetPage();
  void loadCandidateList();
});
watch([jobSearch, jobCategoryFilter, jobSortBy], () => {
  jobPagination.resetPage();
  void loadJobList();
});
watch([() => candidatePagination.page.value, () => candidatePagination.pageSize.value], () => void loadCandidateList());
watch([() => jobPagination.page.value, () => jobPagination.pageSize.value], () => void loadJobList());
watch(() => jobDraft.description, () => { jobDescriptionPage.value = 1; });
watch(activeTraceId, () => {
  resumeTextPagination.resetPage();
  taskFeedbackPagination.resetPage();
  expandedListKeys.value = {};
});

const pagedDashboardTasks = dashboardRecentPagination.pageItems;
const pagedCandidates = computed(() => candidateListItems.value);
const pagedJobItems = computed(() => jobListItems.value);
const pagedTaskFeedbacks = taskFeedbackPagination.pageItems;
const pagedJobCategoryStats = jobCategoryStatsPagination.pageItems;
const pagedPendingReview = pendingReviewPagination.pageItems;
const pagedAnalyticsFeedbacks = analyticsFeedbackPagination.pageItems;
const currentResumeTextPage = computed(() => resumeTextPagination.pageItems.value[0] || activeTask.value?.resumeText || '');

function loadJobs(): JobProfile[] {
  try {
    const saved = localStorage.getItem(JOBS_STORAGE_KEY);
    return saved ? JSON.parse(saved) : [...defaultJobs];
  } catch { return [...defaultJobs]; }
}

watch(jobs, (v) => localStorage.setItem(JOBS_STORAGE_KEY, JSON.stringify(v)), { deep: true });
watch(selectedJobId, (id) => { if (id) void loadJobDetail(id); });
watch(selectedJobId, () => { if (selectedJob.value) Object.assign(jobDraft, selectedJob.value); });

onMounted(async () => {
  if (!selectedJobId.value && jobs.value.length) selectedJobId.value = jobs.value[0].id;
  if (selectedJob.value) Object.assign(jobDraft, selectedJob.value);
  await refreshAll();
  await refreshRunningStages();
  for (const task of tasks.value.filter((t) => isTaskActive(t))) startPolling(task.traceId);
  await loadHealth();
  await loadRagConfig();
  await loadRagAdvisor();
  await loadJobsFromBackend();
  if (activeTraceId.value) subscribeTrace(activeTraceId.value);
  for (const job of jobs.value) { indexJdToBackend(job); }
});

watch([activeTraceId, resumeViewMode], () => {
  if (resumeViewMode.value === 'pdf' && activeTask.value?.resumeFileUrl) {
    resetPdfPreviewState();
  }
}, { immediate: true });

onBeforeUnmount(() => { eventSource?.close(); pollTimers.forEach((t) => clearTimeout(t)); });

async function refreshAll() {
  refreshing.value = true;
  try {
    await Promise.allSettled([loadTasks(), loadMetrics(), loadFeedbacks(), loadCandidateList(), loadJobList(), loadTaskQueueStatus()]);
    await refreshRunningStages();
    if (activeTraceId.value) {
      await Promise.allSettled([loadTraces(activeTraceId.value), loadGraph(activeTraceId.value)]);
    }
  } finally { refreshing.value = false; }
}

async function loadHealth() {
  try {
    const r = await fetch('/api/health');
    const h = (await r.json()) as { status?: string; embedding?: { operational?: boolean; provider?: string; message?: string } };
    healthStatus.value = h.status ?? 'UNKNOWN';
    embeddingHealth.value = h.embedding || {};
  } catch {
    healthStatus.value = 'DOWN';
    embeddingHealth.value = {};
  }
}

async function loadRagConfig() {
  try {
    const r = await fetch('/api/rag/config');
    if (!r.ok) return;
    const data = await r.json() as { options?: RagOptions; presets?: RagPreset[] };
    if (data.options) {
      currentRagOptions.value = { ...defaultRagOptions(), ...data.options };
      saveStoredRagOptions(currentRagOptions.value);
    }
    if (Array.isArray(data.presets)) ragPresets.value = data.presets;
    syncBusinessControlsFromOptions();
  } catch {
    // keep local
  }
}

async function loadRagAdvisor() {
  try {
    const r = await fetch('/api/rag/advisor');
    if (!r.ok) return;
    ragAdvisor.value = await r.json();
  } catch {
    ragAdvisor.value = { show: false };
  }
}

function syncBusinessControlsFromOptions() {
  ragBusinessTopK.value = currentRagOptions.value.topK;
  ragStrategyChoice.value = currentRagOptions.value.strategy;
  ragRerankerEnabled.value = currentRagOptions.value.rerankerEnabled;
  const strict = STRICTNESS_CHOICES.find((c) => Math.abs(c.threshold - currentRagOptions.value.scoreThreshold) < 0.05);
  ragStrictness.value = strict?.id || 'balanced';
  const style = STYLE_CHOICES.find((c) => Math.abs(c.temperature - currentRagOptions.value.generation.temperature) < 0.05);
  ragStyleChoice.value = style?.id || 'balanced';
}

async function saveRagConfig(options: RagOptions) {
  currentRagOptions.value = options;
  saveStoredRagOptions(options);
  try {
    await fetch('/api/rag/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(options),
    });
    successMessage.value = 'RAG 配置已保存';
  } catch {
    errorMessage.value = 'RAG 配置保存失败';
  }
}

function applyRagPreset(preset: RagPreset) {
  const options = applyPreset(preset);
  saveRagConfig(options);
  syncBusinessControlsFromOptions();
}

function applyBusinessRagControls() {
  const options = applyBusinessControls(
    currentRagOptions.value,
    ragBusinessTopK.value,
    ragStrictness.value,
    ragStrategyChoice.value,
    ragStyleChoice.value,
    ragRerankerEnabled.value,
  );
  saveRagConfig(options);
}

async function previewRagConfig() {
  if (!ragPreviewText.value.trim()) {
    ragPreviewText.value = activeTask.value?.resumeText || pastedResume.value || '';
  }
  if (!ragPreviewText.value.trim()) {
    errorMessage.value = '请先粘贴简历文本或选择有简历的任务';
    return;
  }
  const r = await fetch('/api/rag/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ resumeText: ragPreviewText.value, options: currentRagOptions.value }),
  });
  if (!r.ok) {
    errorMessage.value = '预览失败';
    return;
  }
  const data = await r.json() as { candidates?: any[] };
  ragPreviewResult.value = data.candidates || [];
}

async function runRagCompare() {
  const text = ragPreviewText.value.trim() || activeTask.value?.resumeText || '';
  if (!text) {
    errorMessage.value = '需要简历文本才能对比';
    return;
  }
  ragCompareLoading.value = true;
  try {
    const variants = ragCompareVariants.value.map((v) => {
      const preset = ragPresets.value.find((p) => p.id === v.presetId);
      return { name: v.name, options: preset ? preset.options : currentRagOptions.value };
    });
    const r = await fetch('/api/rag/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ resumeText: text, variants }),
    });
    if (!r.ok) throw new Error('compare failed');
    const data = await r.json() as { variants?: Record<string, any> };
    ragCompareResult.value = data.variants || {};
  } catch {
    errorMessage.value = '对比试跑失败';
  } finally {
    ragCompareLoading.value = false;
  }
}

async function applyAdvisorPreset() {
  const preset = ragPresets.value.find((p) => p.id === ragAdvisor.value.suggestedPreset);
  if (preset) applyRagPreset(preset);
  ragAdvisorDismissed.value = true;
}


function scoreFilterParams(): { scoreMin?: number; scoreMax?: number } {
  if (scoreFilter.value === '90_PLUS') return { scoreMin: 90 };
  if (scoreFilter.value === '80_89') return { scoreMin: 80, scoreMax: 89 };
  if (scoreFilter.value === '70_79') return { scoreMin: 70, scoreMax: 79 };
  if (scoreFilter.value === 'LOW') return { scoreMax: 69 };
  return {};
}

function candidateSortParam(): string {
  if (candidateSortBy.value === 'created') return 'create_time';
  return candidateSortBy.value;
}

function resolveQueueStatus(task: TaskResponse): string {
  if (task.status === 'SUCCESS' || task.status === 'FAILED' || task.status === 'CANCELLED') {
    return task.status;
  }
  return task.queue?.queueStatus || task.status || '';
}

function isTaskActive(task?: TaskResponse | null): boolean {
  if (!task) return false;
  const qs = resolveQueueStatus(task);
  return qs === 'RUNNING' || qs === 'QUEUED' || qs === 'RETRYING' || task.status === 'RUNNING';
}

async function loadTaskQueueStatus() {
  try {
    const r = await fetch('/api/task-queue/status');
    if (r.ok) taskQueueStatus.value = (await r.json()) as TaskQueueStatus;
  } catch {
    // ignore transient errors
  }
}

async function loadCandidateList() {
  candidatePagination.loading.value = true;
  try {
    const q = buildQuery({
      keyword: candidateSearch.value.trim() || undefined,
      status: statusFilter.value,
      queueStatus: queueStatusFilter.value,
      recommendation: recommendationFilter.value,
      sortBy: candidateSortParam(),
      page: candidatePagination.page.value,
      pageSize: candidatePagination.pageSize.value,
      ...scoreFilterParams(),
    });
    const r = await fetch(`/api/tasks${q}`);
    if (!r.ok) return;
    const page = (await r.json()) as PageResult<TaskResponse>;
    candidateListItems.value = page.items.map((item) => ({
      ...item,
      strengths: item.strengths ?? [],
      risks: item.risks ?? [],
      interviewQuestions: item.interviewQuestions ?? [],
    }));
    candidatePagination.total.value = page.total;
  } finally {
    candidatePagination.loading.value = false;
  }
}

async function loadTasks() {
  try {
    const q = buildQuery({ page: 1, pageSize: 50, sortBy: 'create_time', sortOrder: 'desc' });
    const r = await fetch(`/api/tasks${q}`);
    if (r.ok) {
      const page = (await r.json()) as PageResult<TaskResponse>;
      tasks.value = page.items.map((item) => ({
        ...item,
        strengths: item.strengths ?? [],
        risks: item.risks ?? [],
        interviewQuestions: item.interviewQuestions ?? [],
      }));
      tasksError.value = false;
    } else {
      tasksError.value = true;
    }
  } catch {
    tasksError.value = true;
  } finally {
    tasksLoaded.value = true;
  }
}

async function loadMetrics() {
  const r = await fetch('/api/metrics');
  if (r.ok) metrics.value = (await r.json()) as Metrics;
}

async function loadFeedbacks() {
  const q = buildQuery({ page: 1, pageSize: 200 });
  const r = await fetch(`/api/feedback${q}`);
  if (r.ok) {
    const page = (await r.json()) as PageResult<FeedbackResponse>;
    feedbacks.value = page.items;
  }
}

async function loadJobList() {
  jobPagination.loading.value = true;
  try {
    const q = buildQuery({
      keyword: jobSearch.value.trim() || undefined,
      category: jobCategoryFilter.value,
      sortBy: jobSortBy.value === 'createdAt' ? 'create_time' : jobSortBy.value,
      page: jobPagination.page.value,
      pageSize: jobPagination.pageSize.value,
    });
    const r = await fetch(`/api/jds${q}`);
    if (!r.ok) return;
    const page = (await r.json()) as PageResult<{ jdId: string; title: string; category: string; createTime?: string }>;
    jobListItems.value = page.items.map((row) => ({
      id: row.jdId,
      title: row.title,
      department: row.category || 'General',
      level: 'Mid-Senior',
      category: row.category || 'TECH',
      description: '',
      createdAt: row.createTime || new Date().toISOString(),
    }));
    jobPagination.total.value = page.total;
    if (!selectedJobId.value && jobListItems.value.length) {
      selectedJobId.value = jobListItems.value[0].id;
    }
  } finally {
    jobPagination.loading.value = false;
  }
}

async function loadJobDetail(jdId: string) {
  const r = await fetch(`/api/jds/${encodeURIComponent(jdId)}`);
  if (!r.ok) return;
  const detail = await r.json() as { jdId: string; title: string; category: string; description: string; createTime?: string; version?: number; updatedBy?: string };
  Object.assign(jobDraft, {
    id: detail.jdId,
    title: detail.title,
    department: detail.category || 'General',
    level: jobDraft.level || 'Mid-Senior',
    category: detail.category || 'TECH',
    description: detail.description || '',
    createdAt: detail.createTime || jobDraft.createdAt,
    version: detail.version ?? 1,
    updatedBy: detail.updatedBy,
  });
  const idx = jobs.value.findIndex((j) => j.id === jdId);
  if (idx >= 0) {
    jobs.value[idx] = { ...jobs.value[idx], ...jobDraft };
  }
}

async function loadTraces(traceId: string) {
  const r = await fetch(`/api/traces/${traceId}`);
  if (r.ok) traces.value = (await r.json()) as TraceEvent[];
}

async function loadGraph(traceId: string) {
  const r = await fetch(`/api/graphs/${traceId}`);
  if (r.ok) {
    const g = (await r.json()) as { nodes: GraphNode[]; edges: GraphEdge[]; source?: string };
    graphNodes.value = g.nodes;
    graphEdges.value = g.edges;
    graphSource.value = g.source || 'SIMULATED';
  }
}

function selectTask(task: TaskResponse) {
  void openTaskDetail(task.traceId, 'report');
}

async function openTaskDetail(traceId: string, tab: DetailTab = 'report') {
  const r = await fetch(`/api/tasks/${encodeURIComponent(traceId)}`);
  if (r.ok) {
    const full = (await r.json()) as TaskResponse;
    const idx = tasks.value.findIndex((t) => t.traceId === traceId);
    if (idx >= 0) tasks.value[idx] = full;
    else tasks.value.unshift(full);
    activeTraceId.value = traceId;
    detailTab.value = tab;
    appView.value = 'detail';
    resumeViewMode.value = full.resumeFileType === 'pdf' ? 'pdf' : 'text';
    loadTraces(traceId);
    loadGraph(traceId);
    loadAgentExecution(traceId);
    if (full.status === 'RUNNING' || isTaskActive(full)) startPolling(traceId);
    return;
  }
  const task = tasks.value.find((t) => t.traceId === traceId);
  if (task) {
    activeTraceId.value = traceId;
    detailTab.value = tab;
    appView.value = 'detail';
    resumeViewMode.value = task.resumeFileType === 'pdf' ? 'pdf' : 'text';
    loadTraces(traceId);
    loadGraph(traceId);
    loadAgentExecution(traceId);
    if (task.status === 'RUNNING' || isTaskActive(task)) startPolling(traceId);
  }
}

function openCandidate(traceId: string, tab: DetailTab = 'report') {
  void openTaskDetail(traceId, tab);
}

function resetPdfPreviewState() {
  pdfLoading.value = true;
  pdfError.value = false;
  if (pdfLoadTimer) window.clearTimeout(pdfLoadTimer);
  pdfLoadTimer = window.setTimeout(() => {
    if (pdfLoading.value) {
      pdfLoading.value = false;
      pdfError.value = true;
    }
  }, 5000);
}

function onPdfLoad() {
  pdfLoading.value = false;
  pdfError.value = false;
  if (pdfLoadTimer) {
    window.clearTimeout(pdfLoadTimer);
    pdfLoadTimer = null;
  }
}

function onPdfError() {
  pdfLoading.value = false;
  pdfError.value = true;
}

function stripMarkdown(text?: string): string {
  if (!text) return '';
  return text
    .replace(/`+/g, '')
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/\*(.+?)\*/g, '$1')
    .replace(/^[-*•]\s+/gm, '')
    .trim();
}


function aiRecommendationText(rec?: string): string {
  if (!rec) return '未生成';
  if (rec.includes('STRONG')) return '强烈推荐面试';
  if (rec.includes('RECOMMEND')) return '推荐面试';
  return '需要人工复核';
}


async function loadJobsFromBackend() {
  try {
    await loadJobList();
    if (!jobListItems.value.length) return;
    jobs.value = [...jobListItems.value];
    if (!selectedJobId.value && jobs.value.length) selectedJobId.value = jobs.value[0].id;
    if (selectedJobId.value) await loadJobDetail(selectedJobId.value);
  } catch {
    // keep local jobs
  }
}

function candidateReviewHint(task: TaskResponse): string {
  const qs = resolveQueueStatus(task);
  if (task.status !== 'SUCCESS' && qs !== 'SUCCESS') return taskStatusLabel(task);
  if ((task.recommendation || '').includes('RECOMMEND')) return '推荐面试';
  return stripMarkdown(task.decisionRationale || task.riskSummary || task.risks?.[0] || '需人工复核');
}

function recommendationShort(rec: string): string {
  if (rec.includes('STRONG')) return '强烈推荐';
  if (rec.includes('RECOMMEND')) return '推荐';
  return '需复核';
}

function goBack() {
  appView.value = 'candidates';
}

function toggleAgentNode(idx: number) {
  if (expandedAgentNodes.has(idx)) expandedAgentNodes.delete(idx);
  else expandedAgentNodes.add(idx);
}

function toggleSubAgent(key: string) {
  if (expandedSubAgents.has(key)) expandedSubAgents.delete(key);
  else expandedSubAgents.add(key);
}

function getAgentIcon(name: string): string {
  if (name?.includes('Intent')) return '🧭';
  if (name?.includes('Parse') || name?.includes('ResumeParse')) return '📄';
  if (name?.includes('JdMatch')) return '🎯';
  if (name?.includes('TechEval')) return '⚙️';
  if (name?.includes('ProjectEval')) return '🏗️';
  if (name?.includes('Risk')) return '⚠️';
  if (name?.includes('EvidenceFusion') || name?.includes('Fusion')) return '🔗';
  if (name?.includes('Report')) return '📋';
  return '🤖';
}

const expandedPhases = reactive(new Set<number>([0, 1, 2, 3, 4, 5]));
const expandedRounds = reactive(new Set<string>());

function togglePhase(idx: number) {
  if (expandedPhases.has(idx)) expandedPhases.delete(idx);
  else expandedPhases.add(idx);
}

function toggleRound(key: string) {
  if (expandedRounds.has(key)) expandedRounds.delete(key);
  else expandedRounds.add(key);
}

interface PhaseGroup {
  phase: number;
  title: string;
  status: string;
  totalDuration: number;
  agents: any[];
}

function groupByPhase(tree: any[]): PhaseGroup[] {
  const phaseMap = new Map<number, PhaseGroup>();
  const phaseTitles: Record<number, string> = {
    1: '意图路由',
    2: '简历解析',
    3: '岗位匹配',
    4: '并行评估（技术 + 项目 + 风险）',
    5: '证据融合',
    6: '报告生成'
  };

  for (const agent of tree) {
    const p = agent.phase || 1;
    if (!phaseMap.has(p)) {
      phaseMap.set(p, {
        phase: p,
        title: phaseTitles[p] || `Phase ${p}`,
        status: 'SUCCESS',
        totalDuration: 0,
        agents: []
      });
    }
    const group = phaseMap.get(p)!;
    group.agents.push(agent);
    group.totalDuration += agent.durationMs || 0;
    if (agent.status === 'FAILED') group.status = 'FAILED';
    else if (agent.status === 'RUNNING' && group.status !== 'FAILED') group.status = 'RUNNING';
  }

  return Array.from(phaseMap.values()).sort((a, b) => a.phase - b.phase);
}

function truncateText(text: string, maxLen: number): string {
  if (!text) return '';
  return text.length > maxLen ? text.substring(0, maxLen) + '...' : text;
}

async function loadAgentExecution(traceId: string) {
  try {
    const res = await fetch(`/api/tasks/${traceId}/agent-execution`);
    if (res.ok) agentExecutionTree.value = await res.json();
  } catch (e) { /* silent */ }
}

function subscribeTrace(traceId: string) {
  eventSource?.close();
  eventSource = new EventSource(`/sse/traces/${traceId}`);
  eventSource.addEventListener('trace', (event) => {
    const step = JSON.parse((event as MessageEvent).data) as TraceEvent;
    traces.value.push(step);
    updateTaskStageFromTrace(step);
    loadTasks();
    loadMetrics();
    loadGraph(traceId);
  });
}

function runningStageLabel(step: TraceEvent): string {
  const kind = inferStepKind(step);
  if (kind === 'upload_parse') return '文件解析中';
  if (kind === 'jd_match') return 'JD 匹配中';
  if (kind === 'resume_parse') return '简历解析中';
  if (kind === 'skill_eval') return `${agentLabelCn(step.agentRole)} 评估中`;
  if (kind === 'rag_retrieve' || kind === 'rag_index_verify') {
    return step.status === 'WARNING' ? 'RAG 降级运行中' : 'RAG 检索中';
  }
  if (kind === 'llm_complete') return 'DeepSeek 生成中';
  if (kind === 'quality_check') return '质量校验中';
  if (kind === 'report_generate') return '报告生成中';
  return `${agentLabelCn(step.agentRole, step.title)}...`;
}

function updateTaskStageFromTrace(step: TraceEvent) {
  if (!step.traceId) return;
  taskStageHints.value[step.traceId] = runningStageLabel(step);
}

async function refreshRunningStages() {
  const running = tasks.value.filter((t) => t.status === 'RUNNING');
  await Promise.all(running.map(async (task) => {
    try {
      const r = await fetch(`/api/traces/${task.traceId}`);
      if (!r.ok) return;
      const events = (await r.json()) as TraceEvent[];
      const latest = [...events].reverse().find((e) => e.stepKind || e.eventType);
      if (latest) updateTaskStageFromTrace(latest);
    } catch {
      // ignore transient network errors during polling
    }
  }));
}

function taskStatusLabel(task: TaskResponse): string {
  const qs = resolveQueueStatus(task);
  if (qs === 'QUEUED') return '排队中';
  if (qs === 'RETRYING') return '待重试';
  if (task.status === 'RUNNING' || qs === 'RUNNING') {
    return taskStageHints.value[task.traceId] || '评估中';
  }
  return statusText(task.status || qs);
}

const activeRagFallback = computed(() => {
  const warn = traces.value.find((e) => e.status === 'WARNING' && (e.eventType?.includes('RAG') || e.agentRole?.toLowerCase().includes('hybrid')));
  return warn ? ragFallbackHint(warn.detail || warn.evidenceSummary || '') : '';
});

function startPolling(traceId: string) {
  const existing = pollTimers.get(traceId);
  if (existing) clearTimeout(existing);
  const poll = async () => {
    await refreshAll();
    await refreshRunningStages();
    const current = tasks.value.find((t) => t.traceId === traceId);
    if (isTaskActive(current)) {
      pollTimers.set(traceId, window.setTimeout(poll, 2000));
    } else {
      pollTimers.delete(traceId);
    }
  };
  pollTimers.set(traceId, window.setTimeout(poll, 2000));
}

function closeUploadModal() {
  showUploadModal.value = false;
}

async function createEvaluations() {
  if (!canStartEvaluation.value) return;
  loading.value = true;
  uploadPhase.value = 'validating';
  errorMessage.value = '';
  backgroundUploadNotice.value = '';
  const controller = new AbortController();
  uploadAbortController.value = controller;
  const timeoutId = window.setTimeout(() => controller.abort(), 45000);
  let failed = 0;
  let created = 0;
  let lastCreated: TaskResponse | null = null;
  try {
    if (queuedFiles.value.length) {
      for (const file of queuedFiles.value) {
        const body = new FormData();
        body.append('file', file);
        body.append('executionMode', 'DAG_CONCURRENT');
        uploadPhase.value = 'evaluating';
        if (autoMatchJd.value) {
          const r = await fetch('/api/tasks/upload-auto', { method: 'POST', body, signal: controller.signal });
          if (!r.ok) {
            failed++;
            continue;
          }
          const task = (await r.json()) as TaskResponse;
          lastCreated = task;
          created++;
          uploadPhase.value = 'accepted';
          await loadTasks();
          await loadCandidateList();
          await loadTaskQueueStatus();
          startPolling(task.traceId);
        } else {
          body.append('jobCategory', selectedJob.value.category);
          body.append('jobDescription', selectedJob.value.description);
          const r = await fetch('/api/tasks/upload', { method: 'POST', body, signal: controller.signal });
          if (!r.ok) {
            failed++;
            continue;
          }
          const task = (await r.json()) as TaskResponse;
          lastCreated = task;
          created++;
          uploadPhase.value = 'accepted';
          await loadTasks();
          await loadCandidateList();
          await loadTaskQueueStatus();
          startPolling(task.traceId);
        }
      }
    } else if (pastedResume.value.trim()) {
      uploadPhase.value = 'evaluating';
      const r = await fetch('/api/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        body: JSON.stringify({
          fileName: 'pasted-resume.txt',
          jobCategory: autoMatchJd.value ? 'AUTO' : selectedJob.value.category,
          executionMode: 'DAG_CONCURRENT',
          jobDescription: autoMatchJd.value ? '' : selectedJob.value.description,
          resumeText: pastedResume.value,
          ragOptions: currentRagOptions.value,
        })
      });
      if (!r.ok) {
        failed++;
      } else {
        const task = (await r.json()) as TaskResponse;
        lastCreated = task;
        created++;
        uploadPhase.value = 'accepted';
        await loadTasks();
        await loadCandidateList();
        await loadTaskQueueStatus();
        startPolling(task.traceId);
      }
    }
    queuedFiles.value = [];
    pastedResume.value = '';
    showUploadModal.value = false;
    if (failed) {
      errorMessage.value = `${failed} 个评估任务创建失败，请检查后端日志与 API 状态。`;
    } else if (created > 1) {
      backgroundUploadNotice.value = `${created} 份简历已进入评估队列，可在候选人列表查看排队与进度。`;
      successMessage.value = `${created} 份简历已进入队列，后台评估进行中。`;
    } else if (lastCreated) {
      backgroundUploadNotice.value = '任务已进入后台队列，可在列表中查看实时进度。';
      successMessage.value = '评估任务已进入队列，后台评估进行中。';
      if (created === 1) {
        selectTask(lastCreated);
        subscribeTrace(lastCreated.traceId);
      }
    } else {
      successMessage.value = '评估任务已创建。';
    }
  } catch (err) {
    if (controller.signal.aborted) {
      errorMessage.value = '上传请求超时（45s），请检查网络或稍后重试。';
    } else {
      errorMessage.value = '创建任务失败，请检查网络与服务状态。';
    }
  } finally {
    window.clearTimeout(timeoutId);
    loading.value = false;
    uploadPhase.value = 'idle';
    uploadAbortController.value = null;
  }
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

async function saveJob() {
  const idx = jobs.value.findIndex((j) => j.id === selectedJobId.value);
  if (idx >= 0) jobs.value[idx] = { ...jobDraft };
  const ok = await indexJdToBackend(jobDraft);
  if (ok) {
    successMessage.value = '岗位已保存并同步到后端索引。';
    if (idx >= 0) jobs.value[idx] = { ...jobDraft };
  } else if (!errorMessage.value) {
    errorMessage.value = '岗位已在本地保存，但后端索引失败（可能 jd_library 表缺失、数据库错误或 RAG 配置未启用）。';
  }
}

async function indexJdToBackend(job: JobProfile): Promise<boolean> {
  try {
    const payload = { jdId: job.id, title: job.title, category: job.category, description: job.description };
    const existsOnBackend = job.version != null && job.version > 0;
    if (!existsOnBackend) {
      const r = await fetch('/api/jds', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) return false;
      const detail = (await r.json()) as { version?: number; updatedBy?: string };
      job.version = detail.version ?? 1;
      job.updatedBy = detail.updatedBy;
      return true;
    }
    const r = await fetch(`/api/jds/${encodeURIComponent(job.id)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...payload, version: job.version }),
    });
    if (r.status === 409) {
      const conflict = (await r.json()) as { message?: string; current?: { title: string; category: string; description: string; version?: number; updatedBy?: string } };
      errorMessage.value = conflict.message || '该岗位已被其他 HR 修改，请刷新后重新编辑。';
      if (conflict.current) {
        Object.assign(jobDraft, {
          title: conflict.current.title,
          category: conflict.current.category,
          description: conflict.current.description,
          version: conflict.current.version ?? job.version,
          updatedBy: conflict.current.updatedBy,
        });
      }
      return false;
    }
    if (!r.ok) return false;
    const detail = (await r.json()) as { version?: number; updatedBy?: string };
    job.version = detail.version ?? (job.version ?? 1) + 1;
    job.updatedBy = detail.updatedBy;
    jobDraft.version = job.version;
    jobDraft.updatedBy = job.updatedBy;
    return true;
  } catch {
    return false;
  }
}

function deleteJob() {
  jobs.value = jobs.value.filter((j) => j.id !== selectedJobId.value);
  if (jobs.value.length) selectedJobId.value = jobs.value[0].id;
}

function statusText(status?: string) {
  if (status === 'SUCCESS') return '已完成';
  if (status === 'RUNNING') return '评估中';
  if (status === 'QUEUED') return '排队中';
  if (status === 'RETRYING') return '待重试';
  if (status === 'FAILED') return '失败';
  if (status === 'CANCELLED') return '已取消';
  return '等待中';
}

function statusClass(status?: string) {
  if (status === 'SUCCESS') return 'badge-success';
  if (status === 'RUNNING') return 'badge-warning';
  if (status === 'QUEUED' || status === 'RETRYING') return 'badge-neutral';
  if (status === 'FAILED') return 'badge-danger';
  return 'badge-neutral';
}

function taskStatusClass(task: TaskResponse): string {
  return statusClass(resolveQueueStatus(task) || task.status);
}

function formatDuration(ms?: number) {
  if (!ms) return '-';
  return `${(ms / 1000).toFixed(1)}s`;
}

function displayJobCategory(category?: string): string {
  if (!category) return '未指定';
  if (category === 'AUTO') return '智能匹配';
  if (category === 'TECH') return '技术岗';
  if (category === 'PRODUCT') return '产品岗';
  return category;
}

function displayDepartment(dept?: string): string {
  if (!dept) return '';
  if (dept === 'AI Platform') return 'AI 平台';
  return dept;
}

function displayLevel(level?: string): string {
  if (!level) return '';
  if (level === 'Senior') return '高级';
  if (level === 'Mid') return '中级';
  if (level === 'Mid-Senior') return '中高级';
  return level;
}

async function copyTraceText(text?: string) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    successMessage.value = '已复制到剪贴板';
  } catch {
    errorMessage.value = '复制失败，请手动选择文本复制';
  }
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function renderMarkdown(source: string): string {
  if (!source) return '';
  const escaped = escapeHtml(source);
  return escaped
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
      <p v-if="backgroundUploadNotice" class="notice info" @click="backgroundUploadNotice = ''">{{ backgroundUploadNotice }}</p>
      <p v-if="embeddingBanner && !embeddingHealth.operational" class="notice info rag-embedding-banner">{{ embeddingBanner }}</p>
      <p v-else-if="embeddingHealth.operational && embeddingHealth.provider === 'local'" class="notice success rag-embedding-banner">{{ embeddingBanner }}</p>

      <!-- ========== DASHBOARD ========== -->
      <section v-if="appView === 'dashboard'">
        <div class="page-header">
          <div>
            <h1>总览</h1>
            <p>评估数据概况与快捷操作</p>
          </div>
          <div class="header-actions">
            <button class="btn btn-ghost" :disabled="refreshing" @click="refreshAll">{{ refreshing ? '刷新中...' : '刷新' }}</button>
            <button class="btn btn-ghost" @click="showRagDrawer = true">RAG 配置</button>
            <button class="btn btn-primary" @click="showUploadModal = true">上传简历</button>
          </div>
        </div>

        <div class="kpi-grid">
          <div class="kpi-card"><span class="kpi-label">候选人</span><div class="kpi-value">{{ tasks.length }}</div></div>
          <div class="kpi-card"><span class="kpi-label">排队中</span><div class="kpi-value">{{ taskQueueStatus?.queued ?? queuedTasksCount }}</div></div>
          <div class="kpi-card"><span class="kpi-label">评估中</span><div class="kpi-value">{{ taskQueueStatus?.running ?? runningTasks.length }}</div></div>
          <div class="kpi-card"><span class="kpi-label">已完成</span><div class="kpi-value">{{ completedTasks.length }}</div></div>
          <div class="kpi-card"><span class="kpi-label">平均分</span><div class="kpi-value">{{ metrics?.averageScore?.toFixed(1) ?? '-' }}</div></div>
        </div>

        <div v-if="taskQueueStatus" class="card queue-status-panel">
          <div class="card-header">
            <h2>异步队列状态</h2>
            <span class="text-muted text-sm">Worker {{ taskQueueStatus.activeWorkers }}/{{ taskQueueStatus.workerCapacity }} · 利用率 {{ Math.round((taskQueueStatus.workerUtilization || 0) * 100) }}%</span>
          </div>
          <div class="kpi-grid queue-kpi-grid">
            <div class="kpi-card"><span class="kpi-label">排队</span><div class="kpi-value">{{ taskQueueStatus.queued }}</div></div>
            <div class="kpi-card"><span class="kpi-label">运行中</span><div class="kpi-value">{{ taskQueueStatus.running }}</div></div>
            <div class="kpi-card"><span class="kpi-label">卡住</span><div class="kpi-value">{{ taskQueueStatus.stuck ?? 0 }}</div></div>
            <div class="kpi-card"><span class="kpi-label">待重试</span><div class="kpi-value">{{ taskQueueStatus.retrying }}</div></div>
            <div class="kpi-card"><span class="kpi-label">最长等待</span><div class="kpi-value">{{ taskQueueStatus.oldestWaitSeconds }}s</div></div>
            <div class="kpi-card"><span class="kpi-label">Stream Pending</span><div class="kpi-value">{{ taskQueueStatus.pendingMessages }}</div></div>
            <div class="kpi-card"><span class="kpi-label">近期失败</span><div class="kpi-value">{{ taskQueueStatus.failed }}</div></div>
          </div>
          <p v-if="taskQueueStatus.recentFailures?.length" class="text-muted text-sm queue-fail-hint">
            最近失败：{{ taskQueueStatus.recentFailures.slice(0, 2).join(' · ') }}
          </p>
        </div>

        <div class="card">
          <div class="card-header">
            <h2>最近评估</h2>
            <button class="btn btn-ghost btn-sm" @click="appView = 'candidates'">查看全部</button>
          </div>
          <table class="data-table" v-if="tasks.length">
            <thead><tr><th>候选人</th><th>状态</th><th>岗位</th><th>评分</th><th>耗时</th></tr></thead>
            <tbody>
              <tr v-for="task in pagedDashboardTasks" :key="task.traceId" @click="selectTask(task)">
                <td class="truncate" style="max-width:200px">{{ task.fileName }}</td>
                <td><span class="badge" :class="taskStatusClass(task)">{{ taskStatusLabel(task) }}</span></td>
                <td>{{ displayJobCategory(task.jobCategory) }}</td>
                <td><strong>{{ task.overallScore || '-' }}</strong></td>
                <td class="text-muted">{{ formatDuration(task.durationMs) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-if="tasks.length > 8" class="pagination-bar">
            <span class="pagination-meta">最近 {{ dashboardRecentPagination.total }} 条 · 第 {{ dashboardRecentPagination.page }}/{{ dashboardRecentPagination.totalPages }} 页</span>
            <div class="pagination-actions">
              <button class="btn btn-ghost btn-sm" :disabled="!dashboardRecentPagination.canPrev" @click="dashboardRecentPagination.goPrev()">上一页</button>
              <button class="btn btn-ghost btn-sm" :disabled="!dashboardRecentPagination.canNext" @click="dashboardRecentPagination.goNext()">下一页</button>
              <button class="btn btn-ghost btn-sm" @click="appView = 'candidates'">查看全部候选人</button>
            </div>
          </div>
          <div v-else-if="!tasksLoaded" class="empty-state"><p>加载评估记录中...</p></div>
          <div v-else-if="tasksError && !tasks.length" class="empty-state"><p>加载记录失败，请刷新重试</p></div>
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
            <div class="job-list-toolbar">
              <input class="form-input" v-model="jobSearch" placeholder="搜索 JD、部门、关键词..." />
              <select class="form-input" v-model="jobCategoryFilter">
                <option value="ALL">全部类别</option>
                <option value="TECH">技术岗</option>
                <option value="PRODUCT">产品岗</option>
                <option value="DESIGN">设计岗</option>
              </select>
              <select class="form-input" v-model="jobLevelFilter">
                <option value="ALL">全部级别</option>
                <option value="Senior">高级</option>
                <option value="Mid-Senior">中高级</option>
                <option value="Mid">中级</option>
              </select>
              <select class="form-input" v-model="jobSortBy">
                <option value="createdAt">按最近创建</option>
                <option value="title">按岗位名称</option>
                <option value="category">按岗位类别</option>
              </select>
            </div>
            <div class="job-list-panel">
              <button v-for="job in pagedJobItems" :key="job.id" class="job-item" :class="{ active: job.id === selectedJobId }" @click="selectedJobId = job.id">
                <span class="job-title">{{ job.title }}</span>
                <span class="job-meta">{{ displayDepartment(job.department) }} · {{ displayLevel(job.level) }}</span>
              </button>
            </div>
            <div v-if="jobPagination.total" class="pagination-bar compact stacked">
              <span>第 {{ jobPagination.page }}/{{ jobPagination.totalPages }} 页 · 共 {{ jobPagination.total }} 个</span>
              <select class="form-input" v-model.number="jobPagination.pageSize" @change="jobPagination.resetPage()">
                <option :value="8">8 / 页</option>
                <option :value="16">16 / 页</option>
                <option :value="32">32 / 页</option>
              </select>
              <div class="pg-actions">
                <button class="btn btn-ghost btn-sm" :disabled="!jobPagination.canPrev" @click="jobPagination.goPrev()">上一页</button>
                <button class="btn btn-ghost btn-sm" :disabled="!jobPagination.canNext" @click="jobPagination.goNext()">下一页</button>
              </div>
            </div>
            <div v-else class="empty-state">
              <p>没有找到匹配的 JD，请调整搜索关键词或筛选条件。</p>
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
            <div class="jd-editor-grid">
              <div class="form-field mb-lg"><label>JD 描述</label><textarea class="form-input" v-model="jobDraft.description" rows="8" /></div>
              <div class="jd-description-reader">
                <div class="reader-title">JD 分段预览</div>
                <pre>{{ jobDescriptionPages[jobDescriptionPage - 1] || '暂无 JD 内容' }}</pre>
                <div v-if="jobDescriptionPages.length > 1" class="pagination-bar compact">
                  <button class="btn btn-ghost btn-sm" :disabled="jobDescriptionPage <= 1" @click="jobDescriptionPage--">上一页</button>
                  <span class="pagination-meta">{{ jobDescriptionPage }}/{{ jobDescriptionPages.length }}</span>
                  <button class="btn btn-ghost btn-sm" :disabled="jobDescriptionPage >= jobDescriptionPages.length" @click="jobDescriptionPage++">下一页</button>
                </div>
              </div>
            </div>
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

        <div v-if="ragAdvisor.show && !ragAdvisorDismissed" class="card rag-advisor-card">
          <div class="rag-advisor-title">💡 RAG 调参建议</div>
          <p>{{ ragAdvisor.message }}</p>
          <div class="rag-advisor-actions">
            <button class="btn btn-primary btn-sm" @click="applyAdvisorPreset">切换并保存</button>
            <button class="btn btn-ghost btn-sm" @click="showRagDrawer = true; ragDrawerTab = 'compare'">先试一下不保存</button>
            <button class="btn btn-ghost btn-sm" @click="ragAdvisorDismissed = true">不再提醒</button>
          </div>
        </div>

        <div v-if="taskQueueStatus" class="card queue-status-panel compact">
          <div class="card-header">
            <h2>队列运行态</h2>
            <span class="text-muted text-sm">排队 {{ taskQueueStatus.queued }} · 运行 {{ taskQueueStatus.running }} · 卡住 {{ taskQueueStatus.stuck ?? 0 }} · 等待 {{ taskQueueStatus.oldestWaitSeconds }}s</span>
          </div>
        </div>

        <div class="card">
          <div class="candidate-list-toolbar">
            <input class="form-input search-input" v-model="candidateSearch" placeholder="搜索候选人..." />
            <select class="form-input" v-model="statusFilter" style="width:120px">
              <option value="ALL">全部状态</option>
              <option value="RUNNING">评估中</option>
              <option value="SUCCESS">已完成</option>
              <option value="FAILED">失败</option>
            </select>
            <select class="form-input" v-model="queueStatusFilter" style="width:120px">
              <option value="ALL">全部队列</option>
              <option value="QUEUED">排队中</option>
              <option value="RUNNING">消费中</option>
              <option value="RETRYING">待重试</option>
              <option value="SUCCESS">队列完成</option>
              <option value="FAILED">队列失败</option>
            </select>
            <select class="form-input" v-model="scoreFilter">
              <option value="ALL">全部分数</option>
              <option value="90_PLUS">90 分以上</option>
              <option value="80_89">80-89 分</option>
              <option value="70_79">70-79 分</option>
              <option value="LOW">70 分以下</option>
            </select>
            <select class="form-input" v-model="recommendationFilter">
              <option value="ALL">全部结论</option>
              <option value="RECOMMEND">推荐面试</option>
              <option value="REVIEW">需复核</option>
            </select>
            <select class="form-input" v-model="candidateSortBy">
              <option value="created">最新创建</option>
              <option value="queued_at">排队时间</option>
              <option value="priority">优先级</option>
              <option value="score_desc">分数从高到低</option>
              <option value="score_asc">分数从低到高</option>
              <option value="duration_desc">耗时从高到低</option>
              <option value="duration_asc">耗时从低到高</option>
            </select>
            <span class="text-muted text-sm" style="margin-left:auto">共 {{ candidatePagination.total }} 条</span>
          </div>

          <table class="data-table" v-if="candidateListItems.length">
            <thead><tr><th>文件名</th><th>状态</th><th>岗位</th><th>评分</th><th>推荐</th><th>摘要</th><th>耗时</th></tr></thead>
            <tbody>
              <tr v-for="task in pagedCandidates" :key="task.traceId" :class="{ active: task.traceId === activeTraceId, 'row-review': task.status === 'SUCCESS' && !(task.recommendation || '').includes('RECOMMEND') }" @click="selectTask(task)">
                <td><strong>{{ task.fileName }}</strong><div class="text-muted text-xs" v-if="task.matchedJdTitle">{{ task.matchedJdTitle }}</div><div class="text-muted text-xs" v-if="task.queue?.uploadedBy">HR: {{ task.queue.uploadedBy }}</div></td>
                <td><span class="badge" :class="taskStatusClass(task)">{{ taskStatusLabel(task) }}</span></td>
                <td>{{ displayJobCategory(task.jobCategory) }}</td>
                <td><strong>{{ task.overallScore || '-' }}</strong></td>
                <td><span class="badge" :class="(task.recommendation || '').includes('RECOMMEND') ? 'badge-success' : 'badge-warning'">{{ recommendationShort(task.recommendation || '') }}</span></td>
                <td class="text-muted text-sm truncate" style="max-width:180px">{{ candidateReviewHint(task) }}</td>
                <td class="text-muted">{{ formatDuration(task.durationMs) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-if="candidateListItems.length" class="pagination-bar">
            <span class="pagination-meta">共 {{ candidatePagination.total }} 条 · 第 {{ candidatePagination.page }}/{{ candidatePagination.totalPages }} 页</span>
            <div class="pagination-actions">
              <label class="pagination-size">
                每页
                <select class="form-input" v-model.number="candidatePagination.pageSize" @change="candidatePagination.resetPage()">
                  <option :value="10">10</option>
                  <option :value="20">20</option>
                  <option :value="50">50</option>
                </select>
              </label>
              <button class="btn btn-ghost btn-sm" :disabled="!candidatePagination.canPrev" @click="candidatePagination.goPrev()">上一页</button>
              <button class="btn btn-ghost btn-sm" :disabled="!candidatePagination.canNext" @click="candidatePagination.goNext()">下一页</button>
            </div>
          </div>
          <div v-else-if="!tasksLoaded" class="empty-state"><p>加载评估记录中...</p></div>
          <div v-else-if="tasksError && !tasks.length" class="empty-state"><p>加载记录失败，请刷新重试</p></div>
          <div v-else class="empty-state"><p>暂无候选人数据</p></div>
        </div>
      </section>

      <!-- ========== CANDIDATE DETAIL ========== -->
      <section v-if="appView === 'detail' && activeTask">
        <button class="back-link" @click="goBack">← 返回列表</button>

        <div class="detail-header">
          <div class="score-circle" :class="{ low: (activeTask.overallScore || 0) < 60, mid: (activeTask.overallScore || 0) >= 60 && (activeTask.overallScore || 0) < 75 }">
            <span class="score-value">{{ activeTask.overallScore || '-' }}</span>
            <span class="score-label">综合评估</span>
          </div>
          <div class="score-circle" v-if="activeTask.jdMatchScore" :class="{ low: activeTask.jdMatchScore < 0.5, mid: activeTask.jdMatchScore >= 0.5 && activeTask.jdMatchScore < 0.7 }">
            <span class="score-value">{{ Math.round(activeTask.jdMatchScore * 100) }}%</span>
            <span class="score-label">JD 匹配</span>
          </div>
          <div class="detail-meta">
            <h2>{{ recommendationLabel }}</h2>
            <p>{{ activeTask.fileName }} · {{ activeTask.jobCategory }} · {{ formatDuration(activeTask.durationMs) }}</p>
          </div>
          <div class="detail-actions">
            <span class="badge" :class="taskStatusClass(activeTask)">{{ taskStatusLabel(activeTask) }}</span>
          </div>
        </div>

        <div class="tab-bar">
          <button :class="{ active: detailTab === 'resume' }" @click="detailTab = 'resume'">简历原文</button>
          <button :class="{ active: detailTab === 'report' }" @click="detailTab = 'report'">评估报告</button>
          <button :class="{ active: detailTab === 'trace' }" @click="detailTab = 'trace'">Trace 详情</button>
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
            <div v-if="pdfLoading" class="pdf-loading">PDF 加载中...</div>
            <div v-if="pdfError" class="pdf-error">
              预览加载失败，请点击「打开原件」在新窗口查看。
            </div>
            <iframe
              :src="pdfPreviewUrl"
              title="简历 PDF 预览"
              @load="onPdfLoad"
              @error="onPdfError"
            />
            <a class="btn btn-primary btn-sm" :href="activeTask.resumeFileUrl" target="_blank" rel="noopener">打开原件</a>
          </div>
          <div class="resume-preview resume-text-reader" v-else-if="activeTask.resumeText">
            <pre class="resume-text-page">{{ currentResumeTextPage }}</pre>
            <div v-if="resumeTextPages.length > 1" class="pagination-bar segment-pager">
              <span class="pagination-meta">文本分段 · 第 {{ resumeTextPagination.page }}/{{ resumeTextPagination.totalPages }} 页 · 约 2400 字/页</span>
              <div class="pagination-actions">
                <button class="btn btn-ghost btn-sm" :disabled="!resumeTextPagination.canPrev" @click="resumeTextPagination.goPrev()">上一页</button>
                <button class="btn btn-ghost btn-sm" :disabled="!resumeTextPagination.canNext" @click="resumeTextPagination.goNext()">下一页</button>
              </div>
            </div>
          </div>
          <div class="empty-state" v-else><p>简历原文将在解析后展示。PDF 上传后会保留原件预览，文本抽取用于核对解析质量。</p></div>
        </div>

        <!-- Report Tab -->
        <div v-if="detailTab === 'report'" class="report-content">
          <div v-if="activeRagFallback" class="notice warning rag-report-hint">{{ activeRagFallback }}</div>
          <div class="hr-decision-card" :class="recommendationLabel.includes('强烈推荐') ? 'strong' : recommendationLabel.includes('推荐') ? 'recommend' : 'review'">
            <div class="hr-decision-header">
              <span class="hr-decision-badge">{{ recommendationLabel }}</span>
              <span class="hr-decision-score" v-if="activeTask.overallScore">综合评分 {{ activeTask.overallScore }}</span>
              <span class="hr-decision-score" v-if="activeTask.jdMatchScore" style="margin-left: 8px; color: var(--color-text-light);">JD 匹配 {{ Math.round(activeTask.jdMatchScore * 100) }}%</span>
            </div>
            <div class="hr-decision-meta" v-if="activeTask.aiRecommendation && activeTask.aiRecommendation !== activeTask.recommendation">
              <span>AI 建议：{{ aiRecommendationText(activeTask.aiRecommendation) }}</span>
              <span>系统决策：{{ recommendationLabel }}</span>
              <span v-if="activeTask.decisionRationale">降级原因：{{ stripMarkdown(activeTask.decisionRationale) }}</span>
            </div>
            <p class="hr-decision-summary">{{ stripMarkdown(activeTask.riskSummary || activeTask.summary?.split('\n').find(l => l.trim().length > 10)?.trim().slice(0, 180) || '评估报告生成中...') }}</p>
            <p class="text-muted text-sm mt-sm">注：综合评分代表候选人整体技术素质，JD 匹配度代表与当前具体岗位的贴合程度。</p>
          </div>

          <div v-if="activeTask.matchedJdTitle" class="jd-match-card">
            <div class="jd-match-header">
              <span class="jd-match-badge">RAG 智能匹配</span>
              <span class="jd-match-score" v-if="activeTask.jdMatchScore">匹配度 {{ Math.round((activeTask.jdMatchScore || 0) * 100) }}%</span>
            </div>
            <div class="jd-match-title">{{ activeTask.matchedJdTitle }}</div>
            <div v-if="activeTask.topJdMatches && activeTask.topJdMatches.length > 1" class="jd-match-alts">
              <span class="text-muted" style="font-size:11px">其他候选岗位：</span>
              <span v-for="(m, mi) in listPreview('jd-alts', activeTask.topJdMatches.slice(1), 4)" :key="mi" class="jd-alt-chip">
                {{ m.title }} ({{ Math.round(m.score * 100) }}%)
              </span>
              <button
                v-if="listHasMore(activeTask.topJdMatches.slice(1), 4)"
                class="btn btn-ghost btn-sm show-more-btn"
                @click="toggleList('jd-alts')"
              >
                {{ expandedListKeys['jd-alts'] ? '收起' : `查看更多 (${activeTask.topJdMatches.length - 1})` }}
              </button>
            </div>
          </div>
          <div class="hr-evidence-grid">
            <div class="hr-evidence-card" v-if="activeTask.strengths?.length">
              <h3>关键优势</h3>
              <ul><li v-for="s in listPreview('report-strengths', activeTask.strengths)" :key="s">{{ stripMarkdown(s) }}</li></ul>
              <button v-if="listHasMore(activeTask.strengths)" class="btn btn-ghost btn-sm show-more-btn" @click="toggleList('report-strengths')">
                {{ expandedListKeys['report-strengths'] ? '收起' : `展开全部 (${activeTask.strengths.length})` }}
              </button>
            </div>
            <div class="hr-evidence-card risk" v-if="activeTask.risks?.length">
              <h3>关键风险</h3>
              <ul><li v-for="r in listPreview('report-risks', activeTask.risks)" :key="r">{{ stripMarkdown(r) }}</li></ul>
              <button v-if="listHasMore(activeTask.risks)" class="btn btn-ghost btn-sm show-more-btn" @click="toggleList('report-risks')">
                {{ expandedListKeys['report-risks'] ? '收起' : `展开全部 (${activeTask.risks.length})` }}
              </button>
            </div>
            <div class="hr-evidence-card" v-if="activeTask.interviewQuestions?.length">
              <h3>面试追问</h3>
              <ol><li v-for="q in listPreview('report-interview', activeTask.interviewQuestions)" :key="q">{{ stripMarkdown(q) }}</li></ol>
              <button v-if="listHasMore(activeTask.interviewQuestions)" class="btn btn-ghost btn-sm show-more-btn" @click="toggleList('report-interview')">
                {{ expandedListKeys['report-interview'] ? '收起' : `展开全部 (${activeTask.interviewQuestions.length})` }}
              </button>
            </div>
          </div>
          <details class="report-raw-md" v-if="activeTask.summary">
            <summary>查看完整 AI 评估原文</summary>
            <div v-html="renderMarkdown(activeTask.summary)"></div>
          </details>
          <div class="empty-state" v-if="!activeTask.summary && !activeTask.strengths?.length"><p>报告生成中...</p></div>
        </div>

        <!-- Trace Tab → Agent Execution Tree + Langfuse -->
        <div v-if="detailTab === 'trace'" class="trace-link-panel">
          <!-- Agent Execution Tree -->
          <div class="card" style="padding:var(--space-xl);margin-bottom:var(--space-lg)">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:var(--space-lg)">
              <h3 style="margin:0">Multi-Agent 执行链路</h3>
              <span class="badge badge-info">8-Agent DAG · AiServices + MCP + Skills</span>
            </div>
            <div v-if="agentExecutionTree && agentExecutionTree.executionTree && agentExecutionTree.executionTree.length" class="agent-tree">
              <!-- Phase Groups -->
              <div v-for="(phase, pidx) in groupByPhase(agentExecutionTree.executionTree)" :key="pidx" class="phase-group">
                <div class="phase-header" @click="togglePhase(pidx)">
                  <span class="phase-arrow">{{ expandedPhases.has(pidx) ? '▼' : '▶' }}</span>
                  <span class="phase-label">Phase {{ phase.phase }}</span>
                  <span class="phase-title">{{ phase.title }}</span>
                  <span :class="['badge', phase.status === 'SUCCESS' ? 'badge-success' : phase.status === 'RUNNING' ? 'badge-warning' : 'badge-danger']">{{ phase.status }}</span>
                  <span class="phase-duration" v-if="phase.totalDuration">{{ phase.totalDuration }}ms</span>
                </div>
                <div v-if="expandedPhases.has(pidx)" class="phase-children">
                  <!-- Agent Nodes -->
                  <div v-for="(agent, aidx) in phase.agents" :key="aidx" class="agent-tree-node">
                    <div class="agent-tree-header" @click="toggleAgentNode(pidx * 100 + aidx)">
                      <span class="agent-arrow">{{ expandedAgentNodes.has(pidx * 100 + aidx) ? '▼' : '▶' }}</span>
                      <span class="agent-icon">{{ getAgentIcon(agent.name) }}</span>
                      <span class="agent-name">{{ agent.name }}</span>
                      <span class="agent-role">{{ agent.role }}</span>
                      <span :class="['badge', agent.status === 'SUCCESS' ? 'badge-success' : agent.status === 'RUNNING' ? 'badge-warning' : 'badge-danger']">{{ agent.status }}</span>
                      <span v-if="agent.durationMs" class="agent-duration">{{ agent.durationMs }}ms</span>
                    </div>
                    <div v-if="expandedAgentNodes.has(pidx * 100 + aidx)" class="agent-tree-children">
                      <!-- Rounds -->
                      <div v-for="(round, ridx) in (agent.rounds || [])" :key="ridx" class="round-node">
                        <div class="round-header" @click="toggleRound(`${pidx}-${aidx}-${ridx}`)">
                          <span class="round-arrow">{{ expandedRounds.has(`${pidx}-${aidx}-${ridx}`) ? '▼' : '▶' }}</span>
                          <span class="round-label">Round {{ ridx + 1 }}</span>
                          <span class="round-type" v-if="round.type">{{ round.type }}</span>
                          <span v-if="round.tokens" class="round-tokens">{{ round.tokens }} tokens</span>
                        </div>
                        <div v-if="expandedRounds.has(`${pidx}-${aidx}-${ridx}`)" class="round-detail">
                          <div v-if="round.input" class="trace-io-block">
                            <div class="trace-io-label">LLM 输入</div>
                            <pre class="trace-io-content">{{ truncateText(round.input, 600) }}</pre>
                          </div>
                          <div v-if="round.output" class="trace-io-block">
                            <div class="trace-io-label">LLM 输出</div>
                            <pre class="trace-io-content">{{ truncateText(round.output, 600) }}</pre>
                          </div>
                          <!-- Tool/MCP/Skill Calls -->
                          <div v-if="round.toolCalls && round.toolCalls.length" class="tool-calls-section">
                            <div v-for="(tool, tidx) in round.toolCalls" :key="tidx" class="tool-call-item">
                              <div class="tool-call-header">
                                <span :class="['tool-type-badge', `tool-type-${tool.category || 'tool'}`]">{{ tool.category || 'Tool' }}</span>
                                <span class="tool-name">{{ tool.name }}</span>
                                <span v-if="tool.durationMs" class="tool-duration">{{ tool.durationMs }}ms</span>
                              </div>
                              <div v-if="tool.input" class="tool-io">
                                <span class="tool-io-label">入参：</span>
                                <code>{{ truncateText(tool.input, 300) }}</code>
                              </div>
                              <div v-if="tool.output" class="tool-io">
                                <span class="tool-io-label">返回：</span>
                                <code>{{ truncateText(tool.output, 300) }}</code>
                              </div>
                            </div>
                          </div>
                        </div>
                      </div>
                      <!-- Fallback: show output if no rounds -->
                      <div v-if="!agent.rounds || !agent.rounds.length" class="agent-detail-panel">
                        <div v-if="agent.output" class="trace-io-block">
                          <div class="trace-io-label">Agent 输出</div>
                          <pre class="trace-io-content">{{ truncateText(agent.output, 500) }}</pre>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <div v-else class="empty-state" style="padding:var(--space-xl);text-align:center;color:var(--text-secondary)">
              <p>暂无执行记录</p>
              <p style="font-size:0.85rem">评估完成后将展示完整的 8-Agent 执行链路（6 Phase DAG）</p>
            </div>
          </div>
          <!-- Langfuse External Link -->
          <div class="card" style="padding:var(--space-lg);text-align:center">
            <p style="margin-bottom:var(--space-md);color:var(--text-secondary);font-size:0.9rem">深度调试：在 Langfuse 中查看完整的 OTel Trace 数据（含每轮 LLM 调用 + Tool/MCP/Skill）</p>
            <a :href="langfuseTraceUrl" target="_blank" class="langfuse-link">在 Langfuse 中查看 Trace →</a>
          </div>
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

            <div v-for="(m, idx) in displayJdMatches" :key="m.jdId" class="jd-top-card" :class="{ active: jdMatchCards.indexOf(m) === 0 }">
              <div class="jd-top-header">
                <span class="jd-top-rank">#{{ jdMatchDisplayRank(idx) }}</span>
                <strong>{{ m.title }}</strong>
                <span class="jd-match-score">{{ Math.round(m.score * 100) }}%</span>
              </div>
              <p class="text-muted text-sm">{{ m.category }}</p>
              <div v-if="m.skillMatchScore != null" class="jd-dimension-grid">
                <span>技能 {{ Math.round((m.skillMatchScore || 0) * 100) }}%</span>
                <span>经验 {{ Math.round((m.experienceMatchScore || 0) * 100) }}%</span>
                <span>项目 {{ Math.round((m.projectMatchScore || 0) * 100) }}%</span>
                <span v-if="m.riskPenalty">风险惩罚 {{ Math.round((m.riskPenalty || 0) * 100) }}%</span>
              </div>
              <div v-if="m.matchReasons?.length" class="jd-reasons">
                <h4>匹配依据</h4>
                <ul><li v-for="(r, ri) in listPreview(`jd-reasons-${m.jdId}`, m.matchReasons)" :key="ri">{{ r }}</li></ul>
                <button v-if="listHasMore(m.matchReasons)" class="btn btn-ghost btn-sm show-more-btn" @click="toggleList(`jd-reasons-${m.jdId}`)">
                  {{ expandedListKeys[`jd-reasons-${m.jdId}`] ? '收起' : `展开全部 (${m.matchReasons.length})` }}
                </button>
              </div>
              <div v-if="m.gaps?.length" class="jd-gaps">
                <h4>能力缺口</h4>
                <ul><li v-for="(g, gi) in listPreview(`jd-gaps-${m.jdId}`, m.gaps)" :key="gi">{{ g }}</li></ul>
                <button v-if="listHasMore(m.gaps)" class="btn btn-ghost btn-sm show-more-btn" @click="toggleList(`jd-gaps-${m.jdId}`)">
                  {{ expandedListKeys[`jd-gaps-${m.jdId}`] ? '收起' : `展开全部 (${m.gaps.length})` }}
                </button>
              </div>
              <div v-if="m.interviewChecks?.length" class="jd-checks">
                <h4>面试验证点</h4>
                <ul><li v-for="(c, ci) in listPreview(`jd-checks-${m.jdId}`, m.interviewChecks)" :key="ci">{{ c }}</li></ul>
                <button v-if="listHasMore(m.interviewChecks)" class="btn btn-ghost btn-sm show-more-btn" @click="toggleList(`jd-checks-${m.jdId}`)">
                  {{ expandedListKeys[`jd-checks-${m.jdId}`] ? '收起' : `展开全部 (${m.interviewChecks.length})` }}
                </button>
              </div>
            </div>
            <div v-if="showJdMatchPagination" class="pagination-bar">
              <span class="pagination-meta">JD Top 匹配 · 第 {{ jdMatchPagination.page }}/{{ jdMatchPagination.totalPages }} 页</span>
              <div class="pagination-actions">
                <button class="btn btn-ghost btn-sm" :disabled="!jdMatchPagination.canPrev" @click="jdMatchPagination.goPrev()">上一页</button>
                <button class="btn btn-ghost btn-sm" :disabled="!jdMatchPagination.canNext" @click="jdMatchPagination.goNext()">下一页</button>
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
                  <div v-for="skill in listPreview('graph-matched-skills', matchedSkills, 8)" :key="skill.id" style="display:flex;align-items:center;gap:8px;font-size:13px">
                    <span style="color:var(--color-success)">&#10003;</span>
                    <span>{{ skill.label }}</span>
                    <span class="text-muted text-xs" style="margin-left:auto">{{ skill.score }}分</span>
                  </div>
                  <p v-if="!matchedSkills.length" class="text-muted text-sm">暂无数据</p>
                  <button v-if="listHasMore(matchedSkills, 8)" class="btn btn-ghost btn-sm show-more-btn" @click="toggleList('graph-matched-skills')">
                    {{ expandedListKeys['graph-matched-skills'] ? '收起' : `展开全部 (${matchedSkills.length})` }}
                  </button>
                </div>
              </div>
              <div>
                <h4 style="font-size:13px;font-weight:600;color:var(--color-danger);margin-bottom:var(--space-md)">待补充 / 风险项</h4>
                <div style="display:flex;flex-direction:column;gap:6px">
                  <div v-for="skill in listPreview('graph-missing-skills', missingSkills, 8)" :key="skill.id" style="display:flex;align-items:center;gap:8px;font-size:13px">
                    <span style="color:var(--color-danger)">&#10007;</span>
                    <span>{{ skill.label }}</span>
                    <span class="text-muted text-xs" style="margin-left:auto">{{ skill.score }}分</span>
                  </div>
                  <p v-if="!missingSkills.length" class="text-muted text-sm">无明显短板</p>
                  <button v-if="listHasMore(missingSkills, 8)" class="btn btn-ghost btn-sm show-more-btn" @click="toggleList('graph-missing-skills')">
                    {{ expandedListKeys['graph-missing-skills'] ? '收起' : `展开全部 (${missingSkills.length})` }}
                  </button>
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
            <div v-for="fb in pagedTaskFeedbacks" :key="fb.id" style="padding:8px 0;border-bottom:1px solid var(--color-border-light);font-size:13px">
              <span class="badge" :class="fb.feedbackType === 'LIKE' ? 'badge-success' : 'badge-danger'">{{ fb.feedbackType === 'LIKE' ? '认可' : '复核' }}</span>
              <span class="text-muted" style="margin-left:8px">{{ fb.humanComment }}</span>
            </div>
            <div v-if="activeTaskFeedbacks.length > 5" class="pagination-bar compact">
              <button class="btn btn-ghost btn-sm" :disabled="!taskFeedbackPagination.canPrev" @click="taskFeedbackPagination.goPrev()">上一页</button>
              <span class="pagination-meta">{{ taskFeedbackPagination.page }}/{{ taskFeedbackPagination.totalPages }}</span>
              <button class="btn btn-ghost btn-sm" :disabled="!taskFeedbackPagination.canNext" @click="taskFeedbackPagination.goNext()">下一页</button>
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
              <div v-for="row in pagedJobCategoryStats" :key="row.category" style="font-size:12px;display:flex;flex-direction:column;gap:4px;padding:8px 0;border-bottom:1px solid var(--color-border-light)">
                <div style="display:flex;gap:8px;align-items:center">
                  <span style="width:120px">{{ row.category }}</span>
                  <div style="flex:1;height:18px;background:var(--color-border-light);border-radius:3px;overflow:hidden">
                    <div :style="{ width: row.rate + '%', height: '100%', background: 'var(--color-primary)' }"></div>
                  </div>
                  <span>{{ row.recommended }}/{{ row.total }}</span>
                </div>
                <div class="text-muted text-xs">待复核 {{ row.review }} · 平均 JD 匹配 {{ row.avgJdMatch }}%</div>
              </div>
            </div>
            <div v-if="jobCategoryStats.length > 6" class="pagination-bar compact">
              <button class="btn btn-ghost btn-sm" :disabled="!jobCategoryStatsPagination.canPrev" @click="jobCategoryStatsPagination.goPrev()">上一页</button>
              <span class="pagination-meta">{{ jobCategoryStatsPagination.page }}/{{ jobCategoryStatsPagination.totalPages }}</span>
              <button class="btn btn-ghost btn-sm" :disabled="!jobCategoryStatsPagination.canNext" @click="jobCategoryStatsPagination.goNext()">下一页</button>
            </div>
            <p v-if="!jobCategoryStats.length" class="text-muted text-sm">暂无岗位维度数据</p>
          </div>

          <div class="analytics-card">
            <h3>待复核原因入口</h3>
            <div v-if="pendingReviewTasks.length" style="display:flex;flex-direction:column;gap:6px;margin-top:var(--space-md)">
              <div v-for="t in pagedPendingReview" :key="t.traceId" class="review-queue-item" @click="openCandidate(t.traceId, 'feedback')">
                <strong>{{ t.fileName }}</strong>
                <span class="text-muted"> — {{ stripMarkdown(t.decisionRationale || t.riskSummary || '需人工复核') }}</span>
              </div>
            </div>
            <div v-if="pendingReviewTasks.length > 6" class="pagination-bar compact">
              <button class="btn btn-ghost btn-sm" :disabled="!pendingReviewPagination.canPrev" @click="pendingReviewPagination.goPrev()">上一页</button>
              <span class="pagination-meta">{{ pendingReviewPagination.page }}/{{ pendingReviewPagination.totalPages }}</span>
              <button class="btn btn-ghost btn-sm" :disabled="!pendingReviewPagination.canNext" @click="pendingReviewPagination.goNext()">下一页</button>
            </div>
            <p v-if="!pendingReviewTasks.length" class="text-muted text-sm">当前无待复核候选人</p>
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
              <div v-for="fb in pagedAnalyticsFeedbacks" :key="fb.id" style="font-size:12px;display:flex;gap:8px;align-items:center;padding:4px 0;border-bottom:1px solid var(--color-border-light)">
                <span class="badge" :class="fb.feedbackType === 'LIKE' ? 'badge-success' : 'badge-danger'" style="font-size:10px">{{ fb.feedbackType === 'LIKE' ? '认可' : '复核' }}</span>
                <span class="text-muted text-xs">{{ tasks.find(t => t.traceId === fb.traceId)?.fileName || fb.traceId.substring(0, 8) }}</span>
                <span class="truncate" style="flex:1">{{ fb.humanComment }}</span>
                <span class="text-muted text-xs">{{ fb.reviewer }}</span>
              </div>
            </div>
            <div v-if="validFeedbacks.length > 8" class="pagination-bar compact">
              <button class="btn btn-ghost btn-sm" :disabled="!analyticsFeedbackPagination.canPrev" @click="analyticsFeedbackPagination.goPrev()">上一页</button>
              <span class="pagination-meta">{{ analyticsFeedbackPagination.page }}/{{ analyticsFeedbackPagination.totalPages }}</span>
              <button class="btn btn-ghost btn-sm" :disabled="!analyticsFeedbackPagination.canNext" @click="analyticsFeedbackPagination.goNext()">下一页</button>
            </div>
            <p v-if="!validFeedbacks.length" class="text-muted text-sm">暂无有效反馈</p>
          </div>
        </div>
      </section>

      <!-- ========== UPLOAD MODAL ========== -->
      <div v-if="showUploadModal" class="upload-overlay" @click.self="closeUploadModal">
        <div class="upload-modal">
          <h2>上传简历评估</h2>
          <p v-if="uploadPhase === 'validating'" class="upload-phase-hint">正在校验文件...</p>
          <p v-else-if="uploadPhase === 'evaluating'" class="upload-phase-hint">正在提交任务，请稍候...</p>
          <p v-else-if="uploadPhase === 'accepted'" class="upload-phase-hint">任务已接收，即将进入后台评估</p>
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
            <button class="btn btn-primary" :disabled="loading || !canStartEvaluation" @click="createEvaluations">
              {{ loading ? (uploadPhase === 'accepted' ? '已接收...' : '提交中...') : '开始评估' }}
            </button>
            <button class="btn btn-ghost" @click="closeUploadModal">{{ loading ? '后台继续，关闭弹窗' : '取消' }}</button>
          </div>
        </div>
      </div>

      <!-- RAG 调参抽屉 -->
      <div v-if="showRagDrawer" class="rag-drawer-overlay" @click.self="showRagDrawer = false">
        <aside class="rag-drawer">
          <header class="rag-drawer-header">
            <h2>RAG 调参</h2>
            <button class="btn btn-ghost btn-sm" @click="showRagDrawer = false">关闭</button>
          </header>
          <div class="rag-drawer-tabs">
            <button :class="{ active: ragDrawerTab === 'business' }" @click="ragDrawerTab = 'business'">业务模式</button>
            <button :class="{ active: ragDrawerTab === 'compare' }" @click="ragDrawerTab = 'compare'">对比试试不同方案</button>
            <button :class="{ active: ragDrawerTab === 'expert' }" @click="ragDrawerTab = 'expert'">⚙ 专家模式</button>
          </div>
          <div class="rag-drawer-body">
            <template v-if="ragDrawerTab === 'business'">
              <div class="rag-preset-grid">
                <button
                  v-for="preset in ragPresets"
                  :key="preset.id"
                  type="button"
                  class="rag-preset-card"
                  :class="{ active: currentRagOptions.presetName === preset.id }"
                  @click="applyRagPreset(preset)"
                >
                  <div class="rag-preset-icon">{{ preset.icon }}</div>
                  <strong>{{ preset.name }}</strong>
                  <p>{{ preset.description }}</p>
                  <span class="text-muted text-xs">{{ preset.tagline }}</span>
                  <span v-if="currentRagOptions.presetName === preset.id" class="rag-preset-active">当前使用 ✓</span>
                  <span v-else class="rag-preset-use">使用</span>
                </button>
              </div>
              <div class="rag-business-controls">
                <label>想看几个匹配岗位？</label>
                <div class="rag-choice-row">
                  <button v-for="c in TOPK_CHOICES" :key="c.value" type="button" class="rag-chip" :class="{ active: ragBusinessTopK === c.value }" @click="ragBusinessTopK = c.value; applyBusinessRagControls()">{{ c.label }}</button>
                </div>
                <label>匹配严格度</label>
                <div class="rag-choice-row">
                  <button v-for="c in STRICTNESS_CHOICES" :key="c.id" type="button" class="rag-chip" :class="{ active: ragStrictness === c.id }" @click="ragStrictness = c.id; applyBusinessRagControls()">{{ c.label }}</button>
                </div>
                <label>检索方式</label>
                <div class="rag-choice-row">
                  <button v-for="c in STRATEGY_CHOICES" :key="c.id" type="button" class="rag-chip" :class="{ active: ragStrategyChoice === c.id }" @click="ragStrategyChoice = c.id; applyBusinessRagControls()">{{ c.label }}</button>
                </div>
                <label>AI 评估风格</label>
                <div class="rag-choice-row">
                  <button v-for="c in STYLE_CHOICES" :key="c.id" type="button" class="rag-chip" :class="{ active: ragStyleChoice === c.id }" @click="ragStyleChoice = c.id; applyBusinessRagControls()">{{ c.label }}</button>
                </div>
                <label class="rag-toggle-row">
                  <input type="checkbox" v-model="ragRerankerEnabled" @change="applyBusinessRagControls()" />
                  启用智能重排序 — 用 AI 二次精排候选岗位，提升 Top1 准确度，会增加 ~3s 耗时
                </label>
              </div>
            </template>
            <template v-if="ragDrawerTab === 'compare'">
              <div class="rag-compare-actions">
                <button class="btn btn-primary btn-sm" :disabled="ragCompareLoading" @click="runRagCompare">{{ ragCompareLoading ? '试跑中...' : '全部试跑' }}</button>
              </div>
              <div class="rag-variant-grid">
                <div v-for="variant in ragCompareVariants" :key="variant.presetId" class="rag-variant-card">
                  <h4>{{ variant.name }}</h4>
                  <template v-if="ragCompareResult[variant.name]">
                    <p class="text-muted text-xs">耗时 {{ ragCompareResult[variant.name].metricsMs }}ms</p>
                    <ul>
                      <li v-for="(c, ci) in (ragCompareResult[variant.name].candidates || []).slice(0, 3)" :key="ci">
                        Top{{ Number(ci) + 1 }}: {{ c.title }} · {{ Math.round((c.score || 0) * 100) }}%
                      </li>
                    </ul>
                    <button class="btn btn-ghost btn-sm" @click="applyRagPreset(ragPresets.find(p => p.id === variant.presetId)!)">用这个</button>
                  </template>
                  <p v-else class="text-muted text-sm">点击「全部试跑」查看结果</p>
                </div>
              </div>
            </template>
            <template v-if="ragDrawerTab === 'expert'">
              <div class="rag-expert-grid">
                <label>策略 <select v-model="currentRagOptions.strategy" class="form-input"><option value="lexical">lexical</option><option value="vector">vector</option><option value="hybrid">hybrid</option><option value="graph">graph</option></select></label>
                <label>TopK <input type="number" v-model.number="currentRagOptions.topK" class="form-input" min="1" max="20" /></label>
                <label>Score Threshold <input type="number" v-model.number="currentRagOptions.scoreThreshold" class="form-input" min="0" max="1" step="0.05" /></label>
                <label>语义权重 <input type="number" v-model.number="currentRagOptions.semanticWeight" class="form-input" min="0" max="1" step="0.1" /></label>
                <label>关键词权重 <input type="number" v-model.number="currentRagOptions.keywordWeight" class="form-input" min="0" max="1" step="0.1" /></label>
                <label>RRF k <input type="number" v-model.number="currentRagOptions.rrfK" class="form-input" min="1" max="200" /></label>
                <label>Chunk Size <input type="number" v-model.number="currentRagOptions.chunkSize" class="form-input" /></label>
                <label>Chunk Overlap <input type="number" v-model.number="currentRagOptions.chunkOverlap" class="form-input" /></label>
                <label>Embedding Provider <select v-model="currentRagOptions.embeddingProvider" class="form-input"><option value="local">local (MiniLM 384)</option><option value="openai">openai</option><option value="bailian">bailian</option><option value="zhipu">zhipu</option></select></label>
                <label>Temperature <input type="number" v-model.number="currentRagOptions.generation.temperature" class="form-input" min="0" max="2" step="0.1" /></label>
              </div>
              <div class="rag-expert-actions">
                <button class="btn btn-ghost btn-sm" @click="copyTraceText(JSON.stringify(currentRagOptions, null, 2))">导出 JSON</button>
              </div>
            </template>
            <div class="rag-drawer-footer">
              <textarea v-model="ragPreviewText" class="form-input" rows="3" placeholder="粘贴简历文本预览匹配效果..." />
              <div class="flex gap-sm">
                <button class="btn btn-ghost btn-sm" @click="previewRagConfig">预览看效果</button>
                <button class="btn btn-primary btn-sm" @click="saveRagConfig(currentRagOptions)">保存为默认</button>
                <button class="btn btn-ghost btn-sm" @click="applyRagPreset(ragPresets.find(p => p.id === 'balanced') || { id: 'balanced', name: '平衡推荐', icon: '⭐', description: '', tagline: '', options: defaultRagOptions() })">恢复推荐配置</button>
              </div>
              <ul v-if="ragPreviewResult.length" class="rag-preview-list">
                <li v-for="(c, i) in ragPreviewResult.slice(0, 5)" :key="i">{{ c.title }} · {{ Math.round((c.score || 0) * 100) }}%</li>
              </ul>
            </div>
          </div>
        </aside>
      </div>
    </main>
  </div>
</template>
