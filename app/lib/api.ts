export const API_BASE =
  process.env.NEXT_PUBLIC_KARAOKE_API ?? 'http://127.0.0.1:8000';

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers:
      init?.body instanceof FormData
        ? init.headers
        : { 'Content-Type': 'application/json', ...init?.headers },
  });
  if (!response.ok) {
    let message = `Yêu cầu thất bại (${response.status})`;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      message =
        typeof payload.detail === 'string'
          ? payload.detail
          : JSON.stringify(payload.detail ?? payload);
    } catch {
      // Keep the status-based fallback.
    }
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}

export function assetUrl(path: string): string {
  return `${API_BASE}${path}`;
}
