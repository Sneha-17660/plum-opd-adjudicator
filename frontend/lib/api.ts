export const API_BASE = (process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000').replace(/\/+$/, '');

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

function detailOf(body: unknown, status: number): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) {
      return d.map((e) => {
        const loc = Array.isArray(e?.loc) ? e.loc[e.loc.length - 1] : '';
        return loc ? `${String(loc).replace(/_/g, ' ')}: ${e.msg}` : String(e?.msg ?? e);
      }).join('; ');
    }
  }
  return `Request failed (HTTP ${status}).`;
}

/** fetch() against the claims API with a timeout and readable errors. */
export async function api<T = unknown>(path: string, init: RequestInit & { timeoutMs?: number } = {}): Promise<T> {
  const { timeoutMs = 150_000, ...rest } = init;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...rest, signal: controller.signal, cache: 'no-store' });
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw new ApiError('The claims API took too long to answer. Please try again.', 0);
    throw new ApiError(`Cannot reach the claims API at ${API_BASE}. It may be starting up; try again in a moment.`, 0);
  } finally {
    clearTimeout(timer);
  }
  const type = res.headers.get('content-type') || '';
  if (!type.includes('application/json')) {
    if (!res.ok) throw new ApiError(`Request failed (HTTP ${res.status}).`, res.status);
    return (await res.blob()) as T;
  }
  const body = await res.json();
  if (!res.ok) throw new ApiError(detailOf(body, res.status), res.status);
  return body as T;
}

export const inr = (n: number | null | undefined) =>
  n == null ? '—' : `₹${Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;

export const label = (code: string) => code.toLowerCase().replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
