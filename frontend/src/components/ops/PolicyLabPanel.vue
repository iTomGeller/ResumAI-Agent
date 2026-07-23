<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue';

const props = defineProps<{
  bundles?: any[];
  recentRewards?: any[];
  sandboxExecutions?: any[];
}>();

const experiments = ref<any[]>([]);
const selectedId = ref('');
const detail = ref<any>(null);
const loading = ref(false);
const error = ref('');
const actionBusy = ref('');

const form = reactive({
  basePolicyId: 'balanced',
  runType: 'full_evaluation',
  cohortKey: 'default',
  evalDataset: 'gold',
  gateDataset: 'regression',
  safetyDataset: 'safety',
  seedsText: '42',
  repeatsPerCase: 1,
  caseLimit: 1,
  budgetCny: 0.5,
  note: '',
});

async function api(method: string, path: string, body?: any) {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Developer-Actor': 'ops-ui' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${method} ${path} → HTTP ${res.status}: ${text.slice(0, 200)}`);
  }
  return res.status === 204 ? null : res.json();
}

async function loadExperiments() {
  loading.value = true;
  error.value = '';
  try {
    experiments.value = await api('GET', '/api/dev/policy-lab/experiments?limit=50');
  } catch (e: any) {
    error.value = e?.message || '实验列表加载失败';
  } finally {
    loading.value = false;
  }
}

async function openExperiment(id: string) {
  selectedId.value = id;
  loading.value = true;
  error.value = '';
  try {
    detail.value = await api('GET', `/api/dev/policy-lab/experiments/${encodeURIComponent(id)}`);
  } catch (e: any) {
    error.value = e?.message || '实验详情加载失败';
  } finally {
    loading.value = false;
  }
}

async function createExperiment() {
  actionBusy.value = 'create';
  error.value = '';
  try {
    const seeds = form.seedsText
      .split(/[,\s]+/)
      .map((s) => s.trim())
      .filter(Boolean)
      .map((s) => Number(s));
    const created = await api('POST', '/api/dev/policy-lab/experiments', {
      kind: 'OFFLINE_SEARCH',
      basePolicyId: form.basePolicyId,
      runType: form.runType,
      cohortKey: form.cohortKey,
      evalDataset: form.evalDataset,
      gateDataset: form.gateDataset,
      safetyDataset: form.safetyDataset,
      seeds: seeds.length ? seeds : [42],
      repeatsPerCase: form.repeatsPerCase,
      caseLimit: form.caseLimit,
      budgetCny: form.budgetCny,
      note: form.note,
      autoPromote: false,
    });
    await loadExperiments();
    await openExperiment(created.experimentId);
  } catch (e: any) {
    error.value = e?.message || '创建失败';
  } finally {
    actionBusy.value = '';
  }
}

async function act(action: 'pause' | 'resume' | 'cancel' | 'rerun') {
  if (!selectedId.value) return;
  actionBusy.value = action;
  error.value = '';
  try {
    await api('POST', `/api/dev/policy-lab/experiments/${encodeURIComponent(selectedId.value)}/${action}`);
    await loadExperiments();
    await openExperiment(selectedId.value);
  } catch (e: any) {
    error.value = e?.message || `${action} 失败`;
  } finally {
    actionBusy.value = '';
  }
}

async function promoteCandidate(candidateId: string) {
  actionBusy.value = 'promote:' + candidateId;
  error.value = '';
  try {
    await api('POST', `/api/dev/policy-lab/candidates/${encodeURIComponent(candidateId)}/promote`, {
      reason: 'manual promote from Ops UI',
    });
    await openExperiment(selectedId.value);
  } catch (e: any) {
    error.value = e?.message || '晋升失败（仅 PASSED_GATE 可晋升）';
  } finally {
    actionBusy.value = '';
  }
}

async function rollback() {
  if (!selectedId.value || !detail.value?.experiment) return;
  const toPolicyId = window.prompt(
    '回滚到策略 ID',
    detail.value.experiment.basePolicyId || 'balanced',
  );
  if (!toPolicyId) return;
  actionBusy.value = 'rollback';
  error.value = '';
  try {
    await api('POST', `/api/dev/policy-lab/experiments/${encodeURIComponent(selectedId.value)}/rollback`, {
      toPolicyId,
      reason: 'manual rollback from Ops UI',
    });
    await openExperiment(selectedId.value);
  } catch (e: any) {
    error.value = e?.message || '回滚失败';
  } finally {
    actionBusy.value = '';
  }
}

function statusClass(status?: string) {
  const s = (status || '').toUpperCase();
  if (['PASSED', 'PASSED_GATE', 'PROMOTED', 'SUCCEEDED', 'COMPLETED', 'ACTIVE'].includes(s)) return 'ok';
  if (['PENDING', 'RUNNING', 'EVALUATING', 'PAUSED', 'DRAFT'].includes(s)) return 'warn';
  if (['FAILED', 'REJECTED', 'CANCELLED', 'TIMED_OUT'].includes(s)) return 'bad';
  return '';
}

function allowed(action: string) {
  return (detail.value?.actionsAllowed || []).includes(action);
}

onMounted(loadExperiments);
</script>

<template>
  <div class="ops-panel policy-lab-panel">
    <div class="card ops-note">
      <strong>Policy Optimization Lab（无 GPU）· Active Loop</strong>
      <p>
        真相源是 policy_experiment / candidate / trial。Gate 通过只到 PASSED_GATE，
        <strong>永不自动晋升</strong>。Sandbox 仅服务实验隔离，不是候选人评估。
      </p>
    </div>

    <div v-if="error" class="trace-health-warning">{{ error }}</div>

    <!-- CreateExperimentForm -->
    <div class="card create-form">
      <h3>Create Experiment</h3>
      <div class="form-grid">
        <label>basePolicyId<input v-model="form.basePolicyId" /></label>
        <label>runType<input v-model="form.runType" /></label>
        <label>cohortKey<input v-model="form.cohortKey" /></label>
        <label>evalDataset<input v-model="form.evalDataset" /></label>
        <label>gateDataset<input v-model="form.gateDataset" /></label>
        <label>safetyDataset<input v-model="form.safetyDataset" /></label>
        <label>seeds<input v-model="form.seedsText" placeholder="42" /></label>
        <label>repeats<input v-model.number="form.repeatsPerCase" type="number" min="1" /></label>
        <label>caseLimit<input v-model.number="form.caseLimit" type="number" min="1" /></label>
        <label>budgetCny<input v-model.number="form.budgetCny" type="number" min="0.01" step="0.1" /></label>
        <label class="span2">note<input v-model="form.note" /></label>
      </div>
      <p class="text-muted text-xs">defaults: seeds=[42], repeats=1, caseLimit=1, budgetCny=0.5 · autoPromote 强制 false</p>
      <button class="btn" :disabled="!!actionBusy" @click="createExperiment">
        {{ actionBusy === 'create' ? '创建中...' : '创建实验' }}
      </button>
    </div>

    <div class="ops-columns two">
      <!-- ExperimentList -->
      <div class="card">
        <div class="ops-row-head" style="justify-content:space-between;margin-bottom:8px">
          <h3 style="margin:0">Experiments</h3>
          <button class="btn btn-ghost" :disabled="loading" @click="loadExperiments">刷新</button>
        </div>
        <ul class="ops-list">
          <li
            v-for="exp in experiments"
            :key="exp.experimentId"
            class="ops-click-row"
            :class="{ active: selectedId === exp.experimentId }"
            @click="openExperiment(exp.experimentId)"
          >
            <div class="ops-row-head">
              <strong class="mono">{{ exp.experimentId }}</strong>
              <span class="ops-chip" :class="statusClass(exp.status)">{{ exp.status }}</span>
              <span class="ops-chip">{{ exp.progressPhase || '-' }}</span>
            </div>
            <p class="text-muted text-xs">
              base={{ exp.basePolicyId }} · spent={{ exp.spentCny ?? 0 }}/{{ exp.budgetCny }}
              · autoPromote={{ exp.autoPromote ? 'true' : 'false' }}
            </p>
          </li>
          <li v-if="!experiments.length" class="text-muted text-sm">暂无实验 — 上方创建后由 policy-lab-worker 执行</li>
        </ul>
      </div>

      <!-- ExperimentDetail -->
      <div class="card">
        <h3>Experiment Detail</h3>
        <template v-if="detail?.experiment">
          <div class="ops-row-head" style="margin-bottom:8px">
            <span class="ops-chip" :class="statusClass(detail.experiment.status)">{{ detail.experiment.status }}</span>
            <span class="mono text-xs">{{ detail.experiment.experimentId }}</span>
          </div>
          <div class="action-row">
            <button class="btn btn-ghost" :disabled="!allowed('pause') || !!actionBusy" @click="act('pause')">Pause</button>
            <button class="btn btn-ghost" :disabled="!allowed('resume') || !!actionBusy" @click="act('resume')">Resume</button>
            <button class="btn btn-ghost" :disabled="!allowed('cancel') || !!actionBusy" @click="act('cancel')">Cancel</button>
            <button class="btn btn-ghost" :disabled="!allowed('rerun') || !!actionBusy" @click="act('rerun')">Rerun</button>
            <button class="btn btn-ghost" :disabled="!allowed('rollback') || !!actionBusy" @click="rollback">Rollback</button>
          </div>

          <h4 class="ops-subhead">Candidates</h4>
          <ul class="ops-list compact">
            <li v-for="c in (detail.candidates || [])" :key="c.candidateId">
              <div class="ops-row-head">
                <strong class="mono">{{ c.candidateId }}</strong>
                <span class="ops-chip" :class="statusClass(c.status)">{{ c.status }}</span>
                <span class="text-xs">{{ c.bundlePolicyId || '-' }}</span>
                <button
                  v-if="c.status === 'PASSED_GATE'"
                  class="btn btn-ghost"
                  :disabled="!!actionBusy"
                  @click="promoteCandidate(c.candidateId)"
                >Promote</button>
              </div>
              <p class="text-muted text-xs">{{ c.mutationReason || c.configHash || '-' }}</p>
            </li>
            <li v-if="!(detail.candidates || []).length" class="text-muted text-sm">尚无 candidate</li>
          </ul>

          <h4 class="ops-subhead">Trial matrix</h4>
          <div class="table-wrap">
            <table class="ops-table">
              <thead>
                <tr>
                  <th>Split</th><th>Case</th><th>Seed</th><th>Status</th><th>Reward</th><th>Cost</th><th>Error</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="t in (detail.trials || [])" :key="t.trialId">
                  <td>{{ t.datasetSplit }}</td>
                  <td class="mono text-xs">{{ t.caseId || '-' }}</td>
                  <td>{{ t.seed ?? '-' }}</td>
                  <td><span class="ops-chip" :class="statusClass(t.status)">{{ t.status }}</span></td>
                  <td>{{ t.totalReward ?? '-' }}</td>
                  <td>{{ t.costCny ?? '-' }}</td>
                  <td class="text-muted text-xs">{{ t.error || '-' }}</td>
                </tr>
                <tr v-if="!(detail.trials || []).length">
                  <td colspan="7" class="text-muted">尚无 trial（worker 写入后出现）</td>
                </tr>
              </tbody>
            </table>
          </div>

          <h4 class="ops-subhead">Hard gates</h4>
          <ul class="ops-list compact">
            <li v-for="(g, idx) in (detail.hardGates || [])" :key="idx">
              <span class="ops-chip" :class="statusClass(g.status)">{{ g.status }}</span>
              <strong>{{ g.name }}</strong>
              <span class="text-muted text-xs">{{ g.detail }}</span>
            </li>
            <li v-if="!(detail.hardGates || []).length" class="text-muted text-sm">尚无硬门禁结果</li>
          </ul>

          <h4 class="ops-subhead">Sandbox diagnostics</h4>
          <ul class="ops-list compact">
            <li v-for="s in (detail.sandboxes || [])" :key="s.sandboxId + String(s.createTime)">
              <div class="ops-row-head">
                <span class="ops-chip">{{ s.purpose || 'UNKNOWN' }}</span>
                <span class="ops-chip" :class="statusClass(s.status)">{{ s.status }}</span>
                <span>{{ s.toolName }}</span>
                <span class="text-xs">{{ s.isolationMode }}</span>
                <span class="mono text-xs">trial={{ s.trialId || '-' }}</span>
              </div>
              <p class="text-muted text-xs">{{ s.error || (s.durationMs != null ? s.durationMs + 'ms' : '-') }}</p>
            </li>
            <li v-if="!(detail.sandboxes || []).length" class="text-muted text-sm">
              无 experiment 关联的 sandbox（不要仅凭 Docker SUCCEEDED 判断实验结果）
            </li>
          </ul>
        </template>
        <p v-else class="text-muted text-sm">选择左侧实验查看 trial / gates / sandbox。</p>
      </div>
    </div>

    <!-- Champion / reward context from legacy policy tab -->
    <div class="ops-columns two" style="margin-top:12px">
      <div class="card">
        <h3>活跃策略包</h3>
        <ul class="ops-list">
          <li v-for="b in (props.bundles || [])" :key="b.policyId">
            <div class="ops-row-head">
              <strong>{{ b.name || b.policyId }}</strong>
              <span v-if="b.isChampion" class="ops-chip ok">champion</span>
              <span class="ops-chip">g{{ b.generation ?? 0 }}</span>
            </div>
            <p class="text-muted text-sm">{{ b.description || b.policyId }}</p>
          </li>
          <li v-if="!(props.bundles || []).length" class="text-muted text-sm">暂无 ACTIVE policy bundle</li>
        </ul>
      </div>
      <div class="card">
        <h3>最近 Reward（参考）</h3>
        <ul class="ops-list">
          <li v-for="r in (props.recentRewards || []).slice(0, 12)" :key="r.id">
            <div class="ops-row-head">
              <span class="ops-chip">{{ r.source }}</span>
              <strong>{{ Number(r.totalReward ?? 0).toFixed(3) }}</strong>
              <span class="text-muted text-xs">{{ r.policyId }}</span>
            </div>
          </li>
          <li v-if="!(props.recentRewards || []).length" class="text-muted text-sm">暂无 reward</li>
        </ul>
      </div>
    </div>
  </div>
</template>

<style scoped>
.create-form { margin-bottom: 12px; }
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 8px;
  margin-bottom: 8px;
}
.form-grid label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.form-grid input {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 6px 8px;
  font-size: 13px;
}
.form-grid .span2 { grid-column: span 2; }
.action-row { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.ops-click-row { cursor: pointer; }
.ops-click-row.active { outline: 1px solid var(--color-primary); }
</style>
