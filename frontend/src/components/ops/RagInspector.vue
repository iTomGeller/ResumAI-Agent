<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';

interface RagStageTiming {
  queryRewriteMs?: number | null;
  embeddingMs?: number | null;
  retrievalMs?: number | null;
  embeddingRetrievalMs?: number | null;
  fusionMs?: number | null;
  rerankMs?: number | null;
  totalMs?: number | null;
}

interface RagStageAggregate {
  stage: string;
  samples: number;
  averageMs?: number | null;
  p90Ms?: number | null;
  averageShare?: number | null;
}

interface RagChunk {
  chunkId?: string | null;
  documentId?: string | null;
  title?: string | null;
  source?: string | null;
  uri?: string | null;
  score?: number | null;
  scoreType?: string | null;
  rank?: number | null;
  preview?: string | null;
  provenance?: unknown;
}

interface RagQuality {
  groundTruthAvailable: boolean;
  judgeSource?: string | null;
  precisionAtK?: number | null;
  recallAtK?: number | null;
  groundedness?: number | null;
  relevanceScoreSemantics?: string;
  note?: string;
}

interface RagEvent {
  runId?: string | null;
  traceId?: string | null;
  seq?: number | null;
  toolCallId?: string | null;
  toolName?: string | null;
  agentId?: string | null;
  query?: string | null;
  querySummary?: string | null;
  queriesUsed: string[];
  outcome: string;
  occurredAt?: string | null;
  startedAt?: string | null;
  endedAt?: string | null;
  retrievedAt?: string | null;
  durationMs?: number | null;
  strategy?: string | null;
  fusionStrategy?: string | null;
  indexName?: string | null;
  source?: string | null;
  requestedK?: number | null;
  returnedK?: number | null;
  uniqueDocuments?: number | null;
  candidateCount?: number | null;
  lexicalHits?: number | null;
  vectorHits?: number | null;
  filteredCount?: number | null;
  droppedCount?: number | null;
  deduplicatedCount?: number | null;
  zeroHit?: boolean | null;
  topScore?: number | null;
  meanScore?: number | null;
  minScore?: number | null;
  scoreSpread?: number | null;
  scoreSampleSize?: number | null;
  rerankApplied?: boolean | null;
  rerankBeforeTopScore?: number | null;
  rerankAfterTopScore?: number | null;
  rerankLift?: number | null;
  cacheHit?: boolean | null;
  fallback?: boolean | null;
  fallbackStage?: string | null;
  fallbackChain: string[];
  degraded?: boolean | null;
  degradationReason?: string | null;
  error?: string | null;
  stages: RagStageTiming;
  chunks: RagChunk[];
  quality: RagQuality;
  telemetryComplete: boolean;
}

interface RagSummary {
  volume: number;
  terminalCount: number;
  successCount: number;
  zeroHitCount: number;
  zeroHitEligibleCount: number;
  errorCount: number;
  degradedCount: number;
  cacheHitCount: number;
  successRate?: number | null;
  zeroHitRate?: number | null;
  p50LatencyMs?: number | null;
  p90LatencyMs?: number | null;
  averageTopScoreProxy?: number | null;
  averageReturnedK?: number | null;
  topKFillRateProxy?: number | null;
  averageRerankLift?: number | null;
  rerankLiftSamples: number;
  bottleneckStage?: string | null;
  bottleneckAverageMs?: number | null;
  stageBreakdown: RagStageAggregate[];
  completeTelemetryCount: number;
}

const events = ref<RagEvent[]>([]);
const summary = ref<RagSummary | null>(null);
const loading = ref(false);
const error = ref('');
const search = ref('');
const outcomeFilter = ref('ALL');
const PAGE_SIZE = 12;
const page = ref(0);
const expandedKey = ref('');

const filteredEvents = computed(() => {
  const needle = search.value.trim().toLowerCase();
  return events.value.filter((event) => {
    if (outcomeFilter.value !== 'ALL' && event.outcome !== outcomeFilter.value) return false;
    if (!needle) return true;
    return [
      event.runId,
      event.agentId,
      event.toolName,
      event.query,
      event.strategy,
      event.indexName,
    ].some((value) => String(value || '').toLowerCase().includes(needle));
  });
});

const totalPages = computed(() =>
  Math.max(1, Math.ceil(filteredEvents.value.length / PAGE_SIZE)));
const pagedEvents = computed(() => {
  const start = page.value * PAGE_SIZE;
  return filteredEvents.value.slice(start, start + PAGE_SIZE);
});
const maxStageMs = computed(() => Math.max(
  1,
  ...(summary.value?.stageBreakdown || []).map((stage) => stage.averageMs || 0),
));

function numberOrNull(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizeEvent(raw: any): RagEvent {
  const legacyLatency = raw.latency || {};
  const returnedK = numberOrNull(raw.returnedK ?? raw.hitCount);
  const snippets = Array.isArray(raw.snippets) ? raw.snippets : [];
  return {
    ...raw,
    queriesUsed: Array.isArray(raw.queriesUsed) ? raw.queriesUsed : [],
    outcome: raw.outcome || (raw.error ? 'FAILED' : 'UNKNOWN'),
    occurredAt: raw.occurredAt || raw.retrievedAt || raw.completedAt || raw.createdAt || null,
    startedAt: raw.startedAt || null,
    endedAt: raw.endedAt || raw.completedAt || raw.createdAt || null,
    requestedK: numberOrNull(raw.requestedK),
    returnedK,
    uniqueDocuments: numberOrNull(raw.uniqueDocuments),
    zeroHit: typeof raw.zeroHit === 'boolean'
      ? raw.zeroHit
      : (returnedK === null ? null : returnedK === 0),
    topScore: numberOrNull(raw.topScore),
    meanScore: numberOrNull(raw.meanScore),
    minScore: numberOrNull(raw.minScore),
    scoreSpread: numberOrNull(raw.scoreSpread),
    fallbackChain: Array.isArray(raw.fallbackChain) ? raw.fallbackChain : [],
    stages: raw.stages || {
      queryRewriteMs: numberOrNull(legacyLatency.rewrite_ms),
      embeddingRetrievalMs: numberOrNull(legacyLatency.embedding_search_ms),
      fusionMs: numberOrNull(legacyLatency.fusion_ms),
      rerankMs: numberOrNull(legacyLatency.rerank_ms),
      totalMs: numberOrNull(legacyLatency.total_ms),
    },
    chunks: Array.isArray(raw.chunks) ? raw.chunks : snippets,
    quality: raw.quality || {
      groundTruthAvailable: false,
      relevanceScoreSemantics: 'retriever_or_reranker_score_proxy',
      note: '旧数据未附带 ground truth 或 judge。',
    },
    telemetryComplete: Boolean(raw.telemetryComplete),
  };
}

async function loadRagEvents() {
  loading.value = true;
  error.value = '';
  try {
    const response = await fetch('/api/ops/rag?limit=200');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    events.value = (data.items || data.events || []).map(normalizeEvent);
    summary.value = data.summary || null;
    page.value = 0;
  } catch (reason: any) {
    error.value = reason?.message || 'RAG 数据加载失败';
  } finally {
    loading.value = false;
  }
}

function eventKey(event: RagEvent): string {
  return event.toolCallId || `${event.runId || 'run'}:${event.seq ?? 'seq'}:${event.toolName || 'rag'}`;
}

function toggleRow(event: RagEvent) {
  const key = eventKey(event);
  expandedKey.value = expandedKey.value === key ? '' : key;
}

function formatNumber(value?: number | null, digits = 2): string {
  return value === null || value === undefined ? '未采集' : value.toFixed(digits);
}

function formatPercent(value?: number | null): string {
  return value === null || value === undefined ? '未采集' : `${(value * 100).toFixed(1)}%`;
}

function formatMs(value?: number | null): string {
  return value === null || value === undefined ? '未采集' : `${value.toFixed(value < 10 ? 1 : 0)}ms`;
}

function formatTimestamp(value?: string | null): string {
  if (!value) return '未采集';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (number: number, width = 2) => String(number).padStart(width, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.`
    + pad(date.getMilliseconds(), 3);
}

function isoTimestamp(value?: string | null): string {
  if (!value) return 'timestamp not collected';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toISOString();
}

function shortId(value?: string | null, length = 12): string {
  if (!value) return '—';
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

function scoreClass(value?: number | null): string {
  if (value === null || value === undefined) return 'muted';
  if (value >= 0.7) return 'ok';
  if (value >= 0.4) return 'warn';
  return 'bad';
}

function displayRankingScore(event: RagEvent, value?: number | null): number | null {
  if (value === null || value === undefined) return null;
  const fusion = String(event.fusionStrategy || '').toLowerCase();
  if (event.rerankApplied !== true && fusion.includes('rrf') && value <= 0.05) {
    return Math.min(1, value / (2 / 61));
  }
  return value;
}

function stageEntries(stages: RagStageTiming) {
  return [
    ['Query rewrite', stages.queryRewriteMs],
    ['Embedding', stages.embeddingMs],
    ['Retrieve', stages.retrievalMs],
    ['Embedding + retrieve', stages.embeddingRetrievalMs],
    ['Fusion', stages.fusionMs],
    ['Rerank', stages.rerankMs],
    ['Total', stages.totalMs],
  ].filter((entry) => entry[1] !== null && entry[1] !== undefined) as Array<[string, number]>;
}

function provenanceText(value: unknown): string {
  if (value === null || value === undefined) return '未采集';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

onMounted(loadRagEvents);
</script>

<template>
  <div class="ops-panel-inner">
    <div class="card ops-note rag-intro">
      <div>
        <strong>多阶段 RAG 监控</strong>
        <p>查看每次真实检索的结果数量、耗时、排序分数和来源。</p>
      </div>
      <button class="refresh-button" type="button" :disabled="loading" @click="loadRagEvents">
        {{ loading ? '刷新中…' : '刷新' }}
      </button>
    </div>

    <div class="proxy-notice">
      <strong>怎么看：</strong>
      归一排序分用于观察同一检索策略的排序强度；原始 RRF 分另行保留供审计。Top-K 表示结果是否返回完整，两者都不是人工正确率。
    </div>

    <div v-if="summary" class="rag-kpi-grid">
      <div class="metric-card">
        <span class="metric-label">检索调用</span>
        <strong class="metric-value">{{ summary.volume }}</strong>
        <span class="metric-note">完整 telemetry {{ summary.completeTelemetryCount }}/{{ summary.volume }}</span>
      </div>
      <div class="metric-card">
        <span class="metric-label">成功 / 零结果 / 失败</span>
        <strong class="metric-value compact-value">
          {{ summary.successCount }} / {{ summary.zeroHitCount }} / {{ summary.errorCount }}
        </strong>
        <span class="metric-note">
          成功 {{ formatPercent(summary.successRate) }} · 零召回
          {{ formatPercent(summary.zeroHitRate) }}
          (n={{ summary.zeroHitEligibleCount }})
        </span>
      </div>
      <div class="metric-card">
        <span class="metric-label">耗时 P50 / P90</span>
        <strong class="metric-value compact-value">
          {{ formatMs(summary.p50LatencyMs) }} / {{ formatMs(summary.p90LatencyMs) }}
        </strong>
        <span class="metric-note">端到端 retrieval pipeline</span>
      </div>
      <div class="metric-card">
        <span class="metric-label">归一排序分 / 结果完整率</span>
        <strong class="metric-value compact-value">
          {{ formatNumber(summary.averageTopScoreProxy, 3) }} /
          {{ formatPercent(summary.topKFillRateProxy) }}
        </strong>
        <span class="metric-note">平均 top score / K 填充率</span>
      </div>
      <div class="metric-card">
        <span class="metric-label">二次排序效果</span>
        <strong class="metric-value compact-value">
          {{ summary.rerankLiftSamples
            ? formatNumber(summary.averageRerankLift, 4)
            : '未启用二次排序' }}
        </strong>
        <span class="metric-note">
          {{ summary.rerankLiftSamples
            ? `已对比 ${summary.rerankLiftSamples} 次排序前后结果`
            : '本批调用没有执行重排步骤' }}
        </span>
      </div>
      <div class="metric-card">
        <span class="metric-label">最慢阶段</span>
        <strong class="metric-value compact-value">
          {{ summary.bottleneckStage || '未采集' }}
        </strong>
        <span class="metric-note">{{ formatMs(summary.bottleneckAverageMs) }} avg</span>
      </div>
    </div>

    <div v-if="summary?.stageBreakdown?.length" class="card stage-card">
      <div class="section-heading">
        <strong>阶段瓶颈</strong>
        <span>显示各阶段的平均耗时、P90 和调用次数。</span>
      </div>
      <div class="stage-list">
        <div v-for="stage in summary.stageBreakdown" :key="stage.stage" class="stage-row">
          <span class="stage-name">{{ stage.stage }}</span>
          <div class="stage-track">
            <span
              class="stage-fill"
              :style="{ width: `${Math.max(2, ((stage.averageMs || 0) / maxStageMs) * 100)}%` }"
            />
          </div>
          <span class="stage-stat">
            avg {{ formatMs(stage.averageMs) }} · P90 {{ formatMs(stage.p90Ms) }} · n={{ stage.samples }}
          </span>
        </div>
      </div>
    </div>

    <div class="rag-toolbar">
      <input
        v-model="search"
        class="filter-input"
        placeholder="筛选 run / agent / tool / query / strategy"
        @input="page = 0"
      />
      <select v-model="outcomeFilter" class="filter-select" @change="page = 0">
        <option value="ALL">全部 outcome</option>
        <option value="SUCCESS">SUCCESS</option>
        <option value="FAILED">FAILED</option>
        <option value="RUNNING">RUNNING</option>
        <option value="UNKNOWN">UNKNOWN</option>
      </select>
      <span class="result-count">{{ filteredEvents.length }} calls</span>
    </div>

    <div v-if="loading && !events.length" class="empty-state"><p>加载中…</p></div>
    <div v-else-if="error" class="trace-health-warning">{{ error }}</div>
    <div v-else-if="!events.length" class="empty-state">
      <p>暂无 RAG retrieval 调用记录。</p>
      <p class="hint">新调用会显示时间、agent、阶段耗时、召回统计与 provenance；旧事件缺失字段保持“未采集”。</p>
    </div>

    <div v-else class="card table-wrap rag-table-wrap">
      <table class="ops-table rag-detail-table">
        <thead>
          <tr>
            <th class="expand-column"></th>
            <th>时间 / Agent</th>
            <th>Tool / Query</th>
            <th>Outcome</th>
            <th>Strategy / Index</th>
            <th>K / Docs</th>
            <th>归一排序分</th>
            <th>Latency</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="event in pagedEvents" :key="eventKey(event)">
            <tr class="rag-row" :class="{ expanded: expandedKey === eventKey(event) }" @click="toggleRow(event)">
              <td class="expand-icon">{{ expandedKey === eventKey(event) ? '▾' : '▸' }}</td>
              <td>
                <time :title="isoTimestamp(event.occurredAt)" class="event-time">
                  {{ formatTimestamp(event.occurredAt) }}
                </time>
                <span class="cell-subtitle">{{ event.agentId || 'agent 未采集' }}</span>
              </td>
              <td>
                <span class="tool-name">{{ event.toolName || 'retrieval' }}</span>
                <span class="query-preview" :title="event.query || ''">
                  {{ event.querySummary || event.query || 'query 未采集' }}
                </span>
              </td>
              <td>
                <span class="status-chip" :class="event.outcome.toLowerCase()">{{ event.outcome }}</span>
                <span v-if="event.cacheHit" class="mini-chip">cache</span>
                <span v-if="event.degraded" class="mini-chip warning">degraded</span>
              </td>
              <td>
                <span>{{ event.strategy || event.fusionStrategy || '未采集' }}</span>
                <span class="cell-subtitle">{{ event.indexName || event.source || 'index/source 未采集' }}</span>
              </td>
              <td>
                <span>{{ event.returnedK ?? '—' }} / {{ event.requestedK ?? '—' }}</span>
                <span class="cell-subtitle">{{ event.uniqueDocuments ?? '—' }} unique docs</span>
              </td>
              <td>
                <span class="score-chip" :class="scoreClass(displayRankingScore(event, event.topScore))">
                  top {{ formatNumber(displayRankingScore(event, event.topScore), 3) }}
                </span>
                <span class="cell-subtitle">mean {{ formatNumber(displayRankingScore(event, event.meanScore), 3) }}</span>
              </td>
              <td>
                <strong>{{ formatMs(event.stages?.totalMs ?? event.durationMs) }}</strong>
                <span class="cell-subtitle">seq {{ event.seq ?? '—' }}</span>
              </td>
            </tr>
            <tr v-if="expandedKey === eventKey(event)" class="detail-row">
              <td colspan="8">
                <div class="detail-panel">
                  <div class="detail-section">
                    <h4>调用时间与关联</h4>
                    <div class="detail-grid">
                      <div><span>occurredAt</span><time :title="isoTimestamp(event.occurredAt)">{{ formatTimestamp(event.occurredAt) }}</time></div>
                      <div><span>startedAt</span><time :title="isoTimestamp(event.startedAt)">{{ formatTimestamp(event.startedAt) }}</time></div>
                      <div><span>endedAt</span><time :title="isoTimestamp(event.endedAt)">{{ formatTimestamp(event.endedAt) }}</time></div>
                      <div><span>retrievedAt</span><time :title="isoTimestamp(event.retrievedAt)">{{ formatTimestamp(event.retrievedAt) }}</time></div>
                      <div><span>runId</span><code :title="event.runId || ''">{{ shortId(event.runId, 22) }}</code></div>
                      <div><span>traceId</span><code :title="event.traceId || ''">{{ shortId(event.traceId, 22) }}</code></div>
                      <div><span>toolCallId</span><code :title="event.toolCallId || ''">{{ shortId(event.toolCallId, 22) }}</code></div>
                    </div>
                  </div>

                  <div class="detail-section">
                    <h4>多阶段 pipeline</h4>
                    <div v-if="stageEntries(event.stages).length" class="chip-row">
                      <span v-for="[name, value] in stageEntries(event.stages)" :key="name" class="stage-chip">
                        {{ name }}: {{ formatMs(value) }}
                      </span>
                    </div>
                    <p v-else class="missing-copy">该旧事件未采集 stage timing，不补 0。</p>
                  </div>

                  <div class="detail-section">
                    <h4>召回流量与排序</h4>
                    <div class="detail-grid metrics-grid">
                      <div><span>requested / returned K</span><strong>{{ event.requestedK ?? '未采集' }} / {{ event.returnedK ?? '未采集' }}</strong></div>
                      <div><span>candidate / unique docs</span><strong>{{ event.candidateCount ?? '未采集' }} / {{ event.uniqueDocuments ?? '未采集' }}</strong></div>
                      <div><span>lexical / vector hits</span><strong>{{ event.lexicalHits ?? '未采集' }} / {{ event.vectorHits ?? '未采集' }}</strong></div>
                      <div><span>filtered / dropped / dedup</span><strong>{{ event.filteredCount ?? '未采集' }} / {{ event.droppedCount ?? '未采集' }} / {{ event.deduplicatedCount ?? '未采集' }}</strong></div>
                      <div><span>top / mean / min（归一展示）</span><strong>{{ formatNumber(displayRankingScore(event, event.topScore), 4) }} / {{ formatNumber(displayRankingScore(event, event.meanScore), 4) }} / {{ formatNumber(displayRankingScore(event, event.minScore), 4) }}</strong></div>
                      <div><span>spread / score samples</span><strong>{{ formatNumber(event.scoreSpread, 4) }} / {{ event.scoreSampleSize ?? '未采集' }}</strong></div>
                      <div><span>rerank before / after / lift</span><strong>{{ formatNumber(event.rerankBeforeTopScore, 4) }} / {{ formatNumber(event.rerankAfterTopScore, 4) }} / {{ formatNumber(event.rerankLift, 4) }}</strong></div>
                      <div><span>cache / zero hit</span><strong>{{ event.cacheHit ?? '未采集' }} / {{ event.zeroHit ?? '未采集' }}</strong></div>
                    </div>
                    <p class="proxy-copy">表格优先展示 final/rerank 归一排序分；展开 chunk 可看 scoreType。原始 RRF 是倒数秩融合值，不是百分比；不同 strategy 仍不可直接横比。</p>
                  </div>

                  <div v-if="event.fallback || event.degraded || event.error" class="detail-section issue-section">
                    <h4>Fallback / degraded / error</h4>
                    <p>
                      stage={{ event.fallbackStage || '未采集' }}；
                      chain={{ event.fallbackChain.join(' → ') || '未采集' }}；
                      reason={{ event.degradationReason || event.error || '未采集' }}
                    </p>
                  </div>

                  <div v-if="event.queriesUsed.length" class="detail-section">
                    <h4>Query rewrite / variants</h4>
                    <ol class="query-list">
                      <li v-for="query in event.queriesUsed" :key="query">{{ query }}</li>
                    </ol>
                  </div>

                  <div class="detail-section">
                    <h4>质量真值边界</h4>
                    <div v-if="event.quality.groundTruthAvailable || event.quality.judgeSource" class="quality-grid">
                      <div><span>Precision@K（GT）</span><strong>{{ formatNumber(event.quality.precisionAtK, 4) }}</strong></div>
                      <div><span>Recall@K（GT）</span><strong>{{ formatNumber(event.quality.recallAtK, 4) }}</strong></div>
                      <div><span>Groundedness（judge）</span><strong>{{ formatNumber(event.quality.groundedness, 4) }}</strong></div>
                      <div><span>Judge</span><strong>{{ event.quality.judgeSource || '未采集' }}</strong></div>
                    </div>
                    <p v-else class="missing-copy">{{ event.quality.note }}</p>
                  </div>

                  <div class="detail-section">
                    <h4>Chunks / source / provenance</h4>
                    <div v-if="event.chunks.length" class="chunk-list">
                      <article v-for="(chunk, index) in event.chunks" :key="`${chunk.chunkId || chunk.documentId || index}`" class="chunk-card">
                        <header>
                          <strong>#{{ chunk.rank ?? index + 1 }} {{ chunk.title || chunk.documentId || chunk.chunkId || 'chunk' }}</strong>
                          <span>{{ chunk.scoreType || 'score' }}={{ formatNumber(chunk.score, 4) }}</span>
                        </header>
                        <p>{{ chunk.preview || '片段正文未采集' }}</p>
                        <footer>
                          <span>doc={{ chunk.documentId || '—' }}</span>
                          <span>chunk={{ chunk.chunkId || '—' }}</span>
                          <span>source={{ chunk.source || '—' }}</span>
                          <a v-if="chunk.uri" :href="chunk.uri" target="_blank" rel="noreferrer">source URI</a>
                        </footer>
                        <details>
                          <summary>provenance</summary>
                          <pre>{{ provenanceText(chunk.provenance) }}</pre>
                        </details>
                      </article>
                    </div>
                    <p v-else class="missing-copy">该调用未采集 chunk 详情；不能从命中数反推 provenance。</p>
                  </div>
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>

      <div v-if="totalPages > 1" class="pagination">
        <button type="button" :disabled="page === 0" @click="page--">‹</button>
        <span>{{ page + 1 }} / {{ totalPages }}</span>
        <button type="button" :disabled="page >= totalPages - 1" @click="page++">›</button>
      </div>
    </div>

  </div>
</template>

<style scoped>
.rag-intro,
.section-heading,
.rag-toolbar,
.chunk-card header,
.chunk-card footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.rag-intro p,
.section-heading span {
  margin: 4px 0 0;
  color: var(--color-text-secondary);
  font-size: 12px;
}

.refresh-button,
.pagination button {
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  border-radius: 7px;
  padding: 6px 12px;
  cursor: pointer;
}

.refresh-button:disabled,
.pagination button:disabled {
  opacity: .45;
  cursor: not-allowed;
}

.proxy-notice {
  border: 1px solid rgba(245, 158, 11, .35);
  background: rgba(245, 158, 11, .08);
  border-radius: 9px;
  padding: 10px 12px;
  margin-bottom: 12px;
  font-size: 12px;
  line-height: 1.55;
}

.rag-kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}

.metric-card {
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: var(--color-surface);
  padding: 12px 14px;
}

.metric-label,
.metric-note,
.cell-subtitle,
.query-preview {
  display: block;
  color: var(--color-text-secondary);
  font-size: 11px;
}

.metric-value {
  display: block;
  font-size: 1.35rem;
  margin: 5px 0 3px;
}

.compact-value {
  font-size: 1.08rem;
}

.stage-card {
  margin-bottom: 12px;
  padding: 13px 15px;
}

.stage-list {
  display: grid;
  gap: 8px;
  margin-top: 12px;
}

.stage-row {
  display: grid;
  grid-template-columns: 145px minmax(100px, 1fr) 250px;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}

.stage-name {
  font-weight: 600;
}

.stage-track {
  height: 8px;
  background: rgba(99, 102, 241, .1);
  border-radius: 999px;
  overflow: hidden;
}

.stage-fill {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, #6366f1, #06b6d4);
  border-radius: inherit;
}

.stage-stat {
  color: var(--color-text-secondary);
}

.warning-list {
  margin: 0 0 12px;
}

.warning-list p {
  margin: 5px 0;
  padding: 7px 10px;
  border-left: 3px solid #f59e0b;
  background: rgba(245, 158, 11, .06);
  font-size: 12px;
}

.rag-toolbar {
  justify-content: flex-start;
  margin-bottom: 10px;
}

.filter-input,
.filter-select {
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  border-radius: 7px;
  padding: 7px 10px;
}

.filter-input {
  width: min(460px, 60vw);
}

.result-count {
  color: var(--color-text-secondary);
  font-size: 12px;
}

.rag-table-wrap {
  overflow-x: auto;
}

.rag-detail-table {
  min-width: 1120px;
  table-layout: fixed;
}

.rag-detail-table th,
.rag-detail-table td {
  padding: 9px 10px;
  vertical-align: top;
}

.expand-column,
.expand-icon {
  width: 28px;
  text-align: center;
}

.rag-row {
  cursor: pointer;
  transition: background .15s ease;
}

.rag-row:hover,
.rag-row.expanded {
  background: rgba(99, 102, 241, .06);
}

.event-time,
.tool-name {
  display: block;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}

.query-preview {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-top: 3px;
}

.status-chip,
.mini-chip,
.score-chip,
.stage-chip {
  display: inline-block;
  border-radius: 5px;
  padding: 2px 6px;
  font-size: 10px;
}

.status-chip.success,
.score-chip.ok {
  color: #15803d;
  background: rgba(34, 197, 94, .13);
}

.status-chip.failed,
.score-chip.bad {
  color: #dc2626;
  background: rgba(239, 68, 68, .12);
}

.status-chip.running,
.score-chip.warn {
  color: #b45309;
  background: rgba(245, 158, 11, .14);
}

.status-chip.unknown,
.score-chip.muted {
  color: var(--color-text-secondary);
  background: rgba(148, 163, 184, .14);
}

.mini-chip {
  margin: 4px 4px 0 0;
  background: rgba(99, 102, 241, .11);
}

.mini-chip.warning {
  background: rgba(245, 158, 11, .14);
  color: #b45309;
}

.detail-row td {
  padding: 0 !important;
}

.detail-panel {
  padding: 6px 18px 18px 42px;
  background: rgba(99, 102, 241, .025);
}

.detail-section {
  border-top: 1px solid var(--color-border);
  padding: 13px 0 2px;
}

.detail-section h4 {
  margin: 0 0 9px;
  font-size: 13px;
}

.detail-grid,
.quality-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 8px 14px;
}

.detail-grid > div,
.quality-grid > div {
  min-width: 0;
}

.detail-grid span,
.quality-grid span {
  display: block;
  color: var(--color-text-secondary);
  font-size: 10px;
  margin-bottom: 2px;
}

.detail-grid strong,
.detail-grid time,
.detail-grid code,
.quality-grid strong {
  font-size: 12px;
  overflow-wrap: anywhere;
}

.chip-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.stage-chip {
  background: rgba(99, 102, 241, .09);
  font-size: 11px;
}

.proxy-copy,
.missing-copy {
  color: var(--color-text-secondary);
  font-size: 11px;
  margin: 8px 0;
}

.issue-section {
  color: #b45309;
}

.query-list {
  margin: 0;
  padding-left: 20px;
  font-size: 12px;
}

.chunk-list {
  display: grid;
  gap: 9px;
}

.chunk-card {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  padding: 9px 11px;
}

.chunk-card header,
.chunk-card footer {
  font-size: 11px;
}

.chunk-card header span,
.chunk-card footer {
  color: var(--color-text-secondary);
}

.chunk-card p {
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}

.chunk-card footer {
  justify-content: flex-start;
  flex-wrap: wrap;
}

.chunk-card details {
  margin-top: 7px;
  font-size: 11px;
}

.chunk-card pre {
  max-height: 180px;
  overflow: auto;
  padding: 8px;
  background: rgba(15, 23, 42, .06);
  white-space: pre-wrap;
}

.pagination {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 10px;
  padding: 12px;
}

.semantics-details {
  margin-top: 12px;
  font-size: 12px;
}

.semantics-details dl {
  display: grid;
  grid-template-columns: 160px 1fr;
  gap: 6px 12px;
}

.semantics-details dt {
  font-weight: 600;
}

.semantics-details dd {
  margin: 0;
  color: var(--color-text-secondary);
}

@media (max-width: 900px) {
  .stage-row {
    grid-template-columns: 110px 1fr;
  }

  .stage-stat {
    grid-column: 1 / -1;
  }

  .detail-panel {
    padding-left: 14px;
  }
}
</style>
