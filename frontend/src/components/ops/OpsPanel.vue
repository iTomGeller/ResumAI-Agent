<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue';
import ArtifactInspector from './ArtifactInspector.vue';
import ErrorDiagnostic from './ErrorDiagnostic.vue';
import McpInspector from './McpInspector.vue';
import MemoryInspector from './MemoryInspector.vue';
import PlanBudgetPanel from './PlanBudgetPanel.vue';
import RagInspector from './RagInspector.vue';
import RunExplorer from './RunExplorer.vue';
import RunTimeline from './RunTimeline.vue';
import SkillInspector from './SkillInspector.vue';
import PolicyLabPanel from './PolicyLabPanel.vue';

type OpsTab = 'runs' | 'memory' | 'policyLab' | 'policy' | 'mcp' | 'skills' | 'rag' | 'observability';

const props = defineProps<{ initialTab?: OpsTab }>();
const activeTab = ref<OpsTab>(props.initialTab || 'runs');
const loading = ref(false);
const tabLoading = ref(false);
const error = ref('');
const overview = ref<any>(null);
const panel = ref<Record<string, any>>({});
const selectedRunId = ref('');
const highlightSeq = ref<number | null>(null);
const runDetail = ref<any>(null);
const runFilter = ref({ traceId: '', runId: '', conversationId: '', status: '' });
const memoryFilter = ref({ runId: '', decision: '' });
const ragRefreshKey = ref(0);
let initialized = false;

const tabs: Array<{ id: OpsTab; label: string }> = [
  { id: 'runs', label: 'Runs' },
  { id: 'memory', label: 'Memory' },
  { id: 'mcp', label: 'MCP' },
  { id: 'skills', label: 'Skills' },
  { id: 'rag', label: 'RAG' },
  { id: 'policyLab', label: 'Policy Lab' },
  { id: 'policy', label: 'Policy Optimization Lab' },
  { id: 'observability', label: 'Observability' },
];

const endpointByTab: Record<OpsTab, string> = {
  runs: '/api/ops/runs',
  memory: '/api/ops/memory',
  policyLab: '/api/ops/policy-lab',
  policy: '/api/ops/policy',
  mcp: '/api/ops/mcp',
  skills: '/api/ops/skills',
  rag: '/api/ops/rag',
  observability: '/api/ops/observability',
};

function parseOpsHash() {
  const hash = window.location.hash || '';
  const qIndex = hash.indexOf('?');
  if (qIndex < 0) return;
  const params = new URLSearchParams(hash.slice(qIndex + 1));
  const tab = params.get('tab') as OpsTab | null;
  if (tab && tabs.some((t) => t.id === tab)) activeTab.value = tab;
  const runId = params.get('runId');
  if (runId) {
    selectedRunId.value = runId;
    runFilter.value.runId = runId;
    memoryFilter.value.runId = runId;
  }
  const seq = params.get('eventSeq');
  if (seq && !Number.isNaN(Number(seq))) highlightSeq.value = Number(seq);
}

function syncHash() {
  const params = new URLSearchParams();
  params.set('tab', activeTab.value);
  if (selectedRunId.value) params.set('runId', selectedRunId.value);
  if (highlightSeq.value != null) params.set('eventSeq', String(highlightSeq.value));
  const next = `#/dev/ops?${params.toString()}`;
  if (window.location.hash !== next) {
    history.replaceState(null, '', next);
  }
}

async function loadOverview() {
  loading.value = true;
  error.value = '';
  try {
    const res = await fetch('/api/ops');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    overview.value = await res.json();
  } catch (e: any) {
    error.value = e?.message || 'Ops 数据读取失败';
  } finally {
    loading.value = false;
  }
}

async function loadTab(tab: OpsTab, force = false) {
  // RagInspector owns its request so its refresh button and loading state stay local.
  if (tab === 'rag') return;
  if (!force && panel.value[tab]) return;
  tabLoading.value = true;
  error.value = '';
  try {
    let url = endpointByTab[tab];
    if (tab === 'runs') {
      const q = new URLSearchParams();
      Object.entries(runFilter.value).forEach(([k, v]) => {
        if (v) q.set(k, v);
      });
      q.set('limit', '40');
      url = `${url}?${q.toString()}`;
    } else if (tab === 'memory') {
      const q = new URLSearchParams();
      if (memoryFilter.value.runId) q.set('runId', memoryFilter.value.runId);
      if (memoryFilter.value.decision) q.set('decision', memoryFilter.value.decision);
      q.set('limit', '50');
      url = `${url}?${q.toString()}`;
    }
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    panel.value = { ...panel.value, [tab]: await res.json() };
  } catch (e: any) {
    error.value = e?.message || `${tab} 读取失败`;
  } finally {
    tabLoading.value = false;
  }
}

async function refreshAll() {
  panel.value = {};
  runDetail.value = null;
  if (activeTab.value === 'rag') ragRefreshKey.value += 1;
  await Promise.all([loadOverview(), loadTab(activeTab.value, true)]);
  if (selectedRunId.value) await openRun(selectedRunId.value);
}

async function openRun(runId: string) {
  if (!runId) return;
  selectedRunId.value = runId;
  activeTab.value = 'runs';
  syncHash();
  tabLoading.value = true;
  try {
    const res = await fetch(`/api/ops/runs/${encodeURIComponent(runId)}?eventLimit=120`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    runDetail.value = await res.json();
  } catch (e: any) {
    error.value = e?.message || 'Run 详情读取失败';
  } finally {
    tabLoading.value = false;
  }
}

function statusClass(status?: string) {
  const s = (status || '').toUpperCase();
  if (['SUCCESS', 'SUCCEEDED', 'READY', 'OK'].includes(s)) return 'ok';
  if (['RATE_LIMITED', 'DEGRADED', 'PENDING', 'AUTH_REQUIRED', 'DISABLED', 'PARTIAL_SUCCESS', 'QUEUED', 'RUNNING', 'INFO'].includes(s)) return 'warn';
  if (['DOWN', 'FAILED', 'AUTH_FAILED', 'UNREACHABLE', 'CANCELLED', 'TIMED_OUT'].includes(s)) return 'bad';
  return '';
}

/** Inventory ACTIVE/AVAILABLE stay neutral — not green success. */
function inventoryClass(status?: string) {
  const s = (status || '').toUpperCase();
  if (['AVAILABLE', 'ACTIVE', 'READY'].includes(s)) return 'neutral';
  if (['RATE_LIMITED', 'DEGRADED', 'AUTH_REQUIRED', 'DISABLED', 'PENDING'].includes(s)) return 'warn';
  if (['DOWN', 'FAILED', 'AUTH_FAILED', 'UNREACHABLE'].includes(s)) return 'bad';
  return '';
}

watch(activeTab, (tab) => {
  if (!initialized) return;
  void loadTab(tab);
  syncHash();
  if (tab !== 'runs') {
    // keep selectedRunId for deep link / memory filters
  }
});

watch(selectedRunId, () => syncHash());

onMounted(async () => {
  parseOpsHash();
  const initialTab = activeTab.value;
  await Promise.all([loadOverview(), loadTab(initialTab, true)]);
  initialized = true;
  if (activeTab.value !== initialTab) await loadTab(activeTab.value, true);
  syncHash();
  if (selectedRunId.value) await openRun(selectedRunId.value);
  window.addEventListener('hashchange', onHashChange);
});

onUnmounted(() => {
  window.removeEventListener('hashchange', onHashChange);
});

function onHashChange() {
  parseOpsHash();
  if (selectedRunId.value && (!runDetail.value || runDetail.value?.run?.runId !== selectedRunId.value)) {
    openRun(selectedRunId.value);
  }
}

async function onMemoryFilter(payload: { runId: string; decision: string }) {
  memoryFilter.value = payload;
  delete panel.value.memory;
  await loadTab('memory', true);
}
</script>

<template>
  <section class="ops-page">
    <div class="ops-hero">
      <div>
        <h1>Agent Ops · Debug Console</h1>
        <p>
          以 Run 为中心的开发者调试台。MCP/Skills 来自 Python runtime 真实状态；
          Sandbox 仅属 Policy Optimization Lab（无 GPU），不是候选人评估。
        </p>
      </div>
      <div class="ops-hero-actions">
        <span class="ops-chip" :class="overview?.runtimeReady ? 'ok' : 'bad'">
          runtime {{ overview?.runtimeReady ? 'ready' : 'down' }}
        </span>
        <button class="btn btn-ghost" :disabled="loading || tabLoading" @click="refreshAll">
          {{ loading || tabLoading ? '刷新中...' : '刷新' }}
        </button>
      </div>
    </div>

    <div v-if="error" class="trace-health-warning">{{ error }}</div>

    <div class="ops-tabs">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        :class="{ active: activeTab === tab.id }"
        @click="activeTab = tab.id"
      >{{ tab.label }}</button>
    </div>

    <div v-if="loading && !overview" class="empty-state"><p>加载中...</p></div>

    <template v-else>
      <div v-if="activeTab === 'runs'" class="ops-panel">
        <div class="ops-filters card">
          <input v-model="runFilter.traceId" placeholder="traceId" @keyup.enter="loadTab('runs', true)" />
          <input v-model="runFilter.runId" placeholder="runId" @keyup.enter="loadTab('runs', true)" />
          <input v-model="runFilter.conversationId" placeholder="conversationId" @keyup.enter="loadTab('runs', true)" />
          <input v-model="runFilter.status" placeholder="status" @keyup.enter="loadTab('runs', true)" />
          <button class="btn btn-ghost" @click="loadTab('runs', true)">筛选</button>
        </div>
        <div class="ops-columns two">
          <RunExplorer
            :items="panel.runs?.items || overview?.recentRuns || []"
            :selected-run-id="selectedRunId"
            :status-class="statusClass"
            @select="openRun"
          />
          <div class="ops-detail-stack">
            <div class="card">
              <h3>Run Detail</h3>
              <template v-if="runDetail">
                <div class="ops-row-head" style="margin-bottom:8px">
                  <span class="ops-chip" :class="statusClass(runDetail.run?.status)">{{ runDetail.run?.status }}</span>
                  <span class="mono text-xs">{{ runDetail.run?.runId }}</span>
                  <span class="text-muted text-xs">{{ runDetail.run?.policyId || '-' }}</span>
                </div>
                <p class="text-muted text-xs">
                  corr: conv={{ runDetail.correlation?.conversationId || '-' }}
                  · trace={{ runDetail.correlation?.traceId || '-' }}
                  · rev={{ runDetail.correlation?.revisionNo ?? '-' }}
                </p>
              </template>
              <p v-else class="text-muted text-sm">点击左侧 run 查看 plan / timeline / MCP / skills / memory。</p>
            </div>
            <ErrorDiagnostic
              v-if="runDetail"
              :errors="runDetail.errors || []"
              :run-status="runDetail.run?.status"
              :error-code="runDetail.run?.errorCode"
              :error-message="runDetail.run?.errorMessage"
            />
            <PlanBudgetPanel v-if="runDetail" :plan="runDetail.plan" :budget="runDetail.budget" />
            <ArtifactInspector v-if="runDetail" :artifacts="runDetail.artifacts" />
            <RunTimeline
              v-if="runDetail"
              :timeline="runDetail.timeline || runDetail.events || []"
              :highlight-seq="highlightSeq"
              :status-class="statusClass"
            />
            <div v-if="runDetail" class="card">
              <h4 class="ops-subhead">Skills</h4>
              <ul class="ops-list compact">
                <li v-for="(ev, idx) in (runDetail.skills || runDetail.skillsSelected || []).slice(0, 20)" :key="idx">
                  <span class="ops-chip">{{ ev.eventType }}</span>
                  <span class="text-sm">{{ ev.skillId || ev.toolName }}</span>
                  <span class="text-muted text-xs">{{ ev.triggerReason || ev.agentId }}</span>
                </li>
              </ul>
              <h4 class="ops-subhead">MCP</h4>
              <ul class="ops-list compact">
                <li v-for="(ev, idx) in (runDetail.mcpCalls || []).slice(0, 20)" :key="idx">
                  <span class="ops-chip" :class="statusClass(ev.outcome)">{{ ev.tool || ev.toolName }}</span>
                  <span class="text-xs">{{ ev.server || ev.payload?.mcpServer }}</span>
                  <span class="text-muted text-xs">{{ ev.outcome || ev.eventType }}</span>
                </li>
              </ul>
            </div>
          </div>
        </div>
      </div>

      <div v-else-if="activeTab === 'memory'" class="ops-panel">
        <MemoryInspector
          :panel="panel.memory"
          :initial-run-id="memoryFilter.runId"
          :initial-decision="memoryFilter.decision"
          @filter="onMemoryFilter"
        />
      </div>

      <div v-else-if="activeTab === 'policyLab'" class="ops-panel">
        <PolicyLabPanel
          :bundles="panel.policy?.bundles || panel.policyLab?.bundles || []"
          :recent-rewards="panel.policy?.recentRewards || []"
          :sandbox-executions="panel.policyLab?.executions || []"
        />
      </div>

      <div v-else-if="activeTab === 'policy'" class="ops-panel">
        <PolicyLabPanel
          :bundles="panel.policy?.bundles || []"
          :recent-rewards="panel.policy?.recentRewards || []"
          :sandbox-executions="panel.policyLab?.executions || []"
        />
      </div>

      <div v-else-if="activeTab === 'mcp'" class="ops-panel">
        <McpInspector
          :panel="panel.mcp"
          :inventory-class="inventoryClass"
          :status-class="statusClass"
          @open-run="openRun"
        />
      </div>

      <div v-else-if="activeTab === 'skills'" class="ops-panel">
        <SkillInspector :panel="panel.skills" :inventory-class="inventoryClass" />
      </div>

      <div v-else-if="activeTab === 'rag'" class="ops-panel">
        <RagInspector :key="ragRefreshKey" />
      </div>

      <div v-else-if="activeTab === 'observability'" class="ops-panel">
        <div class="card ops-note">
          <strong>Langfuse Exporter</strong>
          <p>仅当 endpoint + public key + secret key 三者齐全才启用。</p>
        </div>
        <div class="ops-metrics">
          <div>
            <span>状态</span>
            <strong>
              <span class="ops-chip" :class="statusClass(panel.observability?.langfuse?.status || overview?.observability?.langfuse?.status)">
                {{ panel.observability?.langfuse?.status || overview?.observability?.langfuse?.status || 'UNKNOWN' }}
              </span>
            </strong>
          </div>
          <div><span>Exporter</span><strong>{{ (panel.observability || overview?.observability)?.langfuse?.enabled ? 'ON' : 'OFF' }}</strong></div>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.ops-page { display: flex; flex-direction: column; gap: var(--space-lg); }
.ops-hero {
  display: flex; justify-content: space-between; gap: var(--space-md); align-items: flex-start;
  padding: var(--space-xl); border-radius: var(--radius-lg);
  background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%); color: #fff;
}
.ops-hero h1 { margin: 0 0 6px; font-size: 1.5rem; }
.ops-hero p { margin: 0; color: rgba(255,255,255,0.78); font-size: 14px; max-width: 720px; }
.ops-hero-actions { display: flex; align-items: center; gap: 8px; }
.ops-hero .btn-ghost { color: #fff; border-color: rgba(255,255,255,0.35); }
.ops-tabs { display: flex; flex-wrap: wrap; gap: 8px; }
.ops-tabs button {
  border: 1px solid var(--color-border); background: var(--color-surface);
  border-radius: 999px; padding: 6px 12px; font-size: 13px; cursor: pointer;
}
.ops-tabs button.active { border-color: var(--color-primary); color: var(--color-primary); background: var(--color-primary-light, #eef2ff); }
.ops-filters { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; padding: 10px; }
.ops-filters input {
  border: 1px solid var(--color-border); border-radius: 8px; padding: 6px 10px; min-width: 140px; font-size: 13px;
}
.ops-metrics {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; margin-bottom: 12px;
}
.ops-metrics > div {
  border: 1px solid var(--color-border); border-radius: 10px; padding: 10px 12px; background: var(--color-surface);
}
.ops-metrics span { display: block; font-size: 12px; color: var(--color-text-secondary); }
.ops-columns { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.ops-columns.two { grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr); }
.ops-detail-stack { display: flex; flex-direction: column; gap: 12px; }
.ops-list { list-style: none; display: grid; gap: 10px; margin: 0; padding: 0; }
.ops-list.compact { gap: 6px; }
.ops-list li { border: 1px solid var(--color-border-light); border-radius: 8px; padding: 10px; background: var(--color-bg); }
.ops-row-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.ops-subhead { margin: 12px 0 6px; font-size: 13px; color: var(--color-text-secondary); }
.ops-chip {
  display: inline-flex; align-items: center; padding: 2px 7px; border-radius: 999px;
  background: var(--color-bg); border: 1px solid var(--color-border); font-size: 11px;
}
.ops-chip.ok { background: var(--color-success-light); color: var(--color-success); border-color: transparent; }
.ops-chip.warn { background: var(--color-warning-light); color: var(--color-warning); border-color: transparent; }
.ops-chip.bad { background: var(--color-danger-light); color: var(--color-danger); border-color: transparent; }
.ops-chip.neutral { background: #eef2f7; color: #475569; border-color: transparent; }
.ops-note { padding: 12px 14px; margin-bottom: 12px; }
.ops-note p { margin: 4px 0 0; color: var(--color-text-secondary); font-size: 13px; }
.table-wrap { overflow-x: auto; }
.ops-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ops-table th, .ops-table td { text-align: left; padding: 8px; border-bottom: 1px solid var(--color-border-light); vertical-align: top; }
.ops-click-row { cursor: pointer; }
.ops-click-row:hover, .ops-click-row.active { background: var(--color-primary-light, #eef2ff); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
@media (max-width: 900px) {
  .ops-columns, .ops-columns.two { grid-template-columns: 1fr; }
}
</style>
