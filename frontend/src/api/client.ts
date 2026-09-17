const API_ORIGIN = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '')

export function apiUrl(path: string): string {
  return `${API_ORIGIN}/api/v1${path.startsWith('/') ? path : `/${path}`}`
}

export class ApiError extends Error {
  constructor(
    public status: number | null,
    public code: string,
    message: string,
    public fields: string[] = [],
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export async function parseError(response: Response): Promise<ApiError> {
  const fallback = `Request failed (${response.status})`
  let data: unknown
  try { data = await response.json() } catch { return new ApiError(response.status, 'http_error', fallback) }
  if (data && typeof data === 'object' && 'detail' in data) {
    const detail = data.detail
    if (detail && typeof detail === 'object' && !Array.isArray(detail) && 'code' in detail) {
      const value = detail as {code?: unknown; message?: unknown}
      if (typeof value.code === 'string' && typeof value.message === 'string') {
        return new ApiError(response.status, value.code, value.message)
      }
    }
    if (Array.isArray(detail)) {
      const fields = detail.map(item => {
        const location = Array.isArray(item?.loc) ? item.loc.join('.') : 'input'
        return `${location}: ${typeof item?.msg === 'string' ? item.msg : 'Invalid value'}`
      })
      return new ApiError(response.status, 'validation_error', 'Please check the highlighted input.', fields)
    }
  }
  return new ApiError(response.status, 'http_error', fallback)
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(apiUrl(path), {
      ...options,
      headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
    })
  } catch {
    throw new ApiError(null, 'network_error', 'Could not reach the server.')
  }
  if (!response.ok) throw await parseError(response)
  return response.json() as Promise<T>
}

export function messageOf(error: unknown): string {
  if (error instanceof ApiError) return [error.message, ...error.fields].join(' ')
  return 'Something went wrong.'
}
