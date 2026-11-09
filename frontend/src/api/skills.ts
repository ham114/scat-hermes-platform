export interface SkillEntry {
  name: string
  version: string
  requires_skills: Record<string, string>
  allowed_operations: string[]
  contract: { version: number; inputs: string[]; outputs: string[] }
  files: Record<string, string>
}
export interface Draft {
  draft: string
  revision: number
  sha: string
  base: string
  content: { changes: SkillEntry[] }
}
export interface Trial {
  id: string
  draft: string
  revision: number
  state: string
  report: unknown
}
export interface ReleasePreview {
  id: string
  sha256: string
  expires: number
  details: unknown
}
export async function authorRequest<T>(
  path: string,
  key: string,
  method = 'GET',
  body?: unknown,
  releaseKey?: string,
  base = '/author-api/v1/skills',
): Promise<T> {
  if (!key.trim()) throw Error('请输入作者凭据')
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 30000)
  try {
    const response = await fetch(base + path, {
      method,
      signal: controller.signal,
      credentials: 'same-origin',
      redirect: 'error',
      headers: {
        'Content-Type': 'application/json',
        'X-SCAT-Author-Key': key,
        ...(releaseKey ? { 'X-SCAT-Release-Key': releaseKey } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    if (!response.ok)
      throw Error(
        response.status === 409
          ? '版本或试跑状态冲突，请刷新核对'
          : response.status === 401 || response.status === 403
            ? '凭据无效或缺少独立发布权限'
            : '作者服务不可用，请读取核对后重试',
      )
    const raw = await response.text()
    if (raw.length > 4_000_000) throw Error('服务响应过大')
    return JSON.parse(raw) as T
  } catch (e) {
    if (e instanceof Error && e.name === 'AbortError')
      throw Error('请求超时，结果待核实；请刷新，勿重复提交', { cause: e })
    throw e
  } finally {
    clearTimeout(timer)
  }
}
