/**
 * 统一的 API 请求封装。
 *
 * 后端在设置了 API_TOKEN 时要求请求头 X-API-Token；前端通过构建期变量
 * VITE_API_TOKEN 注入同一个口令（见 frontend/.env.example）。
 * 未设置口令时不会附带该请求头，后端也不做校验，本地开发无需任何配置。
 */

const TOKEN = import.meta.env.VITE_API_TOKEN || ''

export function authHeaders(extra = {}) {
  return TOKEN ? { ...extra, 'X-API-Token': TOKEN } : extra
}

export function apiFetch(url, options = {}) {
  const headers = authHeaders(options.headers || {})
  return fetch(url, { ...options, headers })
}

/** 给图片 / SVG 等无法自定义请求头的资源地址追加 token 参数。 */
export function withToken(path) {
  if (!TOKEN || !path) return path
  const separator = path.includes('?') ? '&' : '?'
  return `${path}${separator}token=${encodeURIComponent(TOKEN)}`
}
