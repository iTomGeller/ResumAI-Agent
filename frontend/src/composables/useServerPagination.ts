import { computed, ref, watch, type Ref } from 'vue';

export interface PageResult<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
}

export interface ServerPaginationState {
  page: Ref<number>;
  pageSize: Ref<number>;
  total: Ref<number>;
  totalPages: Ref<number>;
  loading: Ref<boolean>;
  canPrev: Ref<boolean>;
  canNext: Ref<boolean>;
  resetPage: () => void;
  goPrev: () => void;
  goNext: () => void;
}

export function useServerPagination(defaultPageSize = 10): ServerPaginationState {
  const page = ref(1);
  const pageSize = ref(defaultPageSize);
  const total = ref(0);
  const loading = ref(false);
  const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)));
  const canPrev = computed(() => page.value > 1);
  const canNext = computed(() => page.value < totalPages.value);

  watch(pageSize, () => {
    page.value = 1;
  });

  function resetPage() {
    page.value = 1;
  }

  function goPrev() {
    page.value = Math.max(1, page.value - 1);
  }

  function goNext() {
    page.value = Math.min(totalPages.value, page.value + 1);
  }

  return { page, pageSize, total, totalPages, loading, canPrev, canNext, resetPage, goPrev, goNext };
}

export function buildQuery(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const q = search.toString();
  return q ? `?${q}` : '';
}
