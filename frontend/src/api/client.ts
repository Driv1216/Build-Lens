export class ApiError extends Error {
  constructor(public readonly status: number, public readonly code: string, message: string) { super(message); this.name = "ApiError" }
}

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000/api/v1'

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    ...init,
  })
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null)
    const detail = typeof body === 'object' && body !== null && 'detail' in body ? body.detail : null
    const message =
      typeof detail === 'string'
        ? detail
        : typeof detail === 'object' && detail !== null && 'message' in detail && typeof detail.message === 'string'
          ? detail.message
          : `Request failed with status ${response.status}.`
    const code = typeof detail === "object" && detail !== null && "code" in detail && typeof detail.code === "string" ? detail.code : "HTTP_ERROR"
    throw new ApiError(response.status, code, message)
  }
  return response.json() as Promise<T>
}
