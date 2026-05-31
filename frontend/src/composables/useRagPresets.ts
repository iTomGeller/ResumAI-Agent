export interface RagGeneration {
  temperature: number;
  topP: number;
  maxTokens: number;
}

export interface RagOptions {
  strategy: string;
  topK: number;
  scoreThreshold: number;
  semanticWeight: number;
  keywordWeight: number;
  rrfK: number;
  rerankerEnabled: boolean;
  rerankerModel: string;
  chunkSize: number;
  chunkOverlap: number;
  embeddingProvider: string;
  generation: RagGeneration;
  presetName?: string;
}

export interface RagPreset {
  id: string;
  name: string;
  icon: string;
  description: string;
  tagline: string;
  default?: boolean;
  options: RagOptions;
}

const RAG_STORAGE_KEY = 'resumai.rag.options.v1';

export const TOPK_CHOICES = [
  { label: '1 个', value: 1 },
  { label: '3 个', value: 3 },
  { label: '5 个', value: 5 },
  { label: '10 个', value: 10 },
];

export const STRICTNESS_CHOICES = [
  { id: 'loose', label: '宽松（多召回）', threshold: 0.2 },
  { id: 'balanced', label: '平衡（推荐）', threshold: 0.35 },
  { id: 'strict', label: '严格（高精度）', threshold: 0.55 },
];

export const STRATEGY_CHOICES = [
  { id: 'lexical', label: '按关键词' },
  { id: 'vector', label: '按语义理解' },
  { id: 'hybrid', label: '两者都用（推荐）' },
];

export const STYLE_CHOICES = [
  { id: 'conservative', label: '保守稳定', temperature: 0.1 },
  { id: 'balanced', label: '平衡（推荐）', temperature: 0.4 },
  { id: 'creative', label: '创新发散', temperature: 0.8 },
];

export function defaultRagOptions(): RagOptions {
  return {
    strategy: 'hybrid',
    topK: 5,
    scoreThreshold: 0.35,
    semanticWeight: 0.7,
    keywordWeight: 0.3,
    rrfK: 60,
    rerankerEnabled: false,
    rerankerModel: 'none',
    chunkSize: 400,
    chunkOverlap: 80,
    embeddingProvider: 'local',
    generation: { temperature: 0.4, topP: 0.9, maxTokens: 1200 },
    presetName: 'balanced',
  };
}

export function loadStoredRagOptions(): RagOptions {
  try {
    const raw = localStorage.getItem(RAG_STORAGE_KEY);
    if (!raw) return defaultRagOptions();
    return { ...defaultRagOptions(), ...JSON.parse(raw) };
  } catch {
    return defaultRagOptions();
  }
}

export function saveStoredRagOptions(options: RagOptions) {
  localStorage.setItem(RAG_STORAGE_KEY, JSON.stringify(options));
}

export function applyPreset(preset: RagPreset): RagOptions {
  return { ...preset.options };
}

export function applyBusinessControls(
  base: RagOptions,
  topK: number,
  strictnessId: string,
  strategyId: string,
  styleId: string,
  rerankerEnabled: boolean,
): RagOptions {
  const strictness = STRICTNESS_CHOICES.find((c) => c.id === strictnessId) || STRICTNESS_CHOICES[1];
  const style = STYLE_CHOICES.find((c) => c.id === styleId) || STYLE_CHOICES[1];
  return {
    ...base,
    topK,
    scoreThreshold: strictness.threshold,
    strategy: strategyId,
    rerankerEnabled,
    rerankerModel: rerankerEnabled ? 'bge-reranker-v2-m3' : 'none',
    generation: { ...base.generation, temperature: style.temperature },
    presetName: undefined,
  };
}

export function presetLabel(presetName?: string): string {
  const map: Record<string, string> = {
    fast: '⚡ 快速筛选',
    balanced: '⭐ 平衡推荐',
    strict: '🎯 严格匹配',
    wide: '🌐 广撒网',
  };
  return presetName ? (map[presetName] || presetName) : '自定义';
}
