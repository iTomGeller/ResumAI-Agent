import { computed, ref, watch, type ComputedRef, type Ref } from 'vue';

export interface PaginationState<T> {
  page: Ref<number>;
  pageSize: Ref<number>;
  total: ComputedRef<number>;
  totalPages: ComputedRef<number>;
  pageItems: ComputedRef<T[]>;
  resetPage: () => void;
  goPrev: () => void;
  goNext: () => void;
  canPrev: ComputedRef<boolean>;
  canNext: ComputedRef<boolean>;
}

export function usePagination<T>(
  items: Ref<T[]> | ComputedRef<T[]>,
  defaultPageSize = 10,
): PaginationState<T> {
  const page = ref(1);
  const pageSize = ref(defaultPageSize);
  const total = computed(() => items.value.length);
  const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)));
  const pageItems = computed(() => {
    const start = (page.value - 1) * pageSize.value;
    return items.value.slice(start, start + pageSize.value);
  });
  const canPrev = computed(() => page.value > 1);
  const canNext = computed(() => page.value < totalPages.value);

  watch([items, pageSize], () => {
    if (page.value > totalPages.value) page.value = totalPages.value;
    if (page.value < 1) page.value = 1;
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

  return { page, pageSize, total, totalPages, pageItems, resetPage, goPrev, goNext, canPrev, canNext };
}

export function paginateSlice<T>(items: T[], page: number, pageSize: number): T[] {
  const start = (page - 1) * pageSize;
  return items.slice(start, start + pageSize);
}

export function splitTextPages(text: string, charsPerPage = 2400): string[] {
  if (!text?.trim()) return [];
  const paragraphs = text.split(/\n{2,}/).map((p) => p.trim()).filter(Boolean);
  const pages: string[] = [];
  let current = '';
  for (const para of paragraphs) {
    const candidate = current ? `${current}\n\n${para}` : para;
    if (candidate.length <= charsPerPage) {
      current = candidate;
      continue;
    }
    if (current) pages.push(current);
    if (para.length <= charsPerPage) {
      current = para;
      continue;
    }
    for (let i = 0; i < para.length; i += charsPerPage) {
      pages.push(para.slice(i, i + charsPerPage));
    }
    current = '';
  }
  if (current) pages.push(current);
  return pages.length ? pages : [text];
}
