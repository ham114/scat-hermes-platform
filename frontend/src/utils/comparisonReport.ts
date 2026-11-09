import type { AnalysisArtifactView } from '@/api/client'
import type { SnapshotMethodMetric, Track3D } from '@/visualization/types'

export interface ComparisonSnapshotReportInput {
  artifact: AnalysisArtifactView
  snapshotId: string
  tracks: Track3D[]
  metrics: SnapshotMethodMetric[]
}

function escapeHtml(value: unknown): string {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

function formatDistance(value: number | null): string {
  if (value === null) return '—'
  return value >= 1_000 ? `${(value / 1_000).toFixed(2)} km` : `${value.toFixed(1)} m`
}

function formatTime(value: number | null): string {
  if (value === null) return '—'
  const total = Math.max(0, Math.round(value))
  return `+${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
}

function methodId(trackId: string): string {
  return trackId.includes('::') ? trackId.slice(trackId.lastIndexOf('::') + 2) : trackId
}

function buildTrajectorySvg(tracks: Track3D[]): string {
  const points = tracks.flatMap((track) => track.points)
  if (points.length === 0) return '<p class="empty">无可展示轨迹。</p>'

  const width = 960
  const height = 560
  const padding = 52
  const minLongitude = Math.min(...points.map((point) => point.longitude))
  const maxLongitude = Math.max(...points.map((point) => point.longitude))
  const minLatitude = Math.min(...points.map((point) => point.latitude))
  const maxLatitude = Math.max(...points.map((point) => point.latitude))
  const longitudeSpan = Math.max(maxLongitude - minLongitude, 1e-9)
  const latitudeSpan = Math.max(maxLatitude - minLatitude, 1e-9)
  const x = (longitude: number) =>
    padding + ((longitude - minLongitude) / longitudeSpan) * (width - padding * 2)
  const y = (latitude: number) =>
    height - padding - ((latitude - minLatitude) / latitudeSpan) * (height - padding * 2)

  const grid = Array.from({ length: 5 }, (_, index) => {
    const ratio = index / 4
    const gridX = padding + ratio * (width - padding * 2)
    const gridY = padding + ratio * (height - padding * 2)
    return `<line x1="${gridX}" y1="${padding}" x2="${gridX}" y2="${height - padding}"/><line x1="${padding}" y1="${gridY}" x2="${width - padding}" y2="${gridY}"/>`
  }).join('')

  const paths = tracks
    .map((track) => {
      const coordinates = track.points
        .map((point) => `${x(point.longitude).toFixed(2)},${y(point.latitude).toFixed(2)}`)
        .join(' ')
      const worst = track.points.reduce<(typeof track.points)[number] | null>(
        (current, point) =>
          typeof point.horizontalErrorMetres === 'number' &&
          (current === null || point.horizontalErrorMetres > (current.horizontalErrorMetres ?? -1))
            ? point
            : current,
        null,
      )
      const marker =
        worst && typeof worst.horizontalErrorMetres === 'number'
          ? `<g><circle cx="${x(worst.longitude).toFixed(2)}" cy="${y(worst.latitude).toFixed(2)}" r="7" fill="#ef4444" stroke="#fff" stroke-width="2"><title>${escapeHtml(track.label)} 最大误差 ${escapeHtml(formatDistance(worst.horizontalErrorMetres))} @ ${escapeHtml(formatTime(worst.time))}</title></circle></g>`
          : ''
      return `<g><polyline points="${coordinates}" fill="none" stroke="${escapeHtml(track.color)}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"><title>${escapeHtml(track.label)}</title></polyline>${marker}</g>`
    })
    .join('')

  return `<svg class="trajectory" viewBox="0 0 ${width} ${height}" role="img" aria-label="当前快照实际轨迹和预测轨迹"><rect width="${width}" height="${height}" fill="#f8fafc"/><g class="grid">${grid}</g>${paths}<text x="${padding}" y="${height - 14}" class="axis">经度 ${minLongitude.toFixed(4)}° — ${maxLongitude.toFixed(4)}°</text><text x="${width - padding}" y="${height - 14}" text-anchor="end" class="axis">纬度 ${minLatitude.toFixed(4)}° — ${maxLatitude.toFixed(4)}°</text></svg>`
}

export function buildComparisonSnapshotReportHtml(input: ComparisonSnapshotReportInput): string {
  const { artifact, snapshotId, tracks, metrics } = input
  const metricByMethod = new Map(metrics.map((metric) => [metric.methodId, metric]))
  const rows = tracks
    .filter((track) => track.kind === 'predicted')
    .map((track) => ({ track, metric: metricByMethod.get(methodId(track.id)) }))
    .filter((row) => row.metric)
    .sort(
      (left, right) => (right.metric?.maxErrorMetres ?? -1) - (left.metric?.maxErrorMetres ?? -1),
    )
  const protocol = artifact.analysis_result.context?.protocol_version ?? '—'
  const sampleHash = artifact.analysis_result.context?.sample_set_sha256 ?? '—'
  const generatedAt = new Date().toISOString()

  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escapeHtml(snapshotId)} · 统一预测对比静态报告</title>
<style>
body{margin:0;color:#172033;background:#eef2f7;font:14px/1.55 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}main{max-width:1080px;margin:24px auto;padding:28px;background:#fff;box-shadow:0 8px 30px #17203318}h1,h2{margin:0 0 14px}h1{font-size:26px}h2{margin-top:28px;font-size:18px}.meta{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 24px}.meta div{overflow-wrap:anywhere}.label{color:#64748b}.notice{margin:18px 0;padding:12px;border-left:4px solid #0891b2;background:#ecfeff}.legend{display:flex;flex-wrap:wrap;gap:8px 18px;margin:10px 0}.legend span{display:inline-flex;align-items:center;gap:6px}.dot{width:10px;height:10px;border-radius:50%}.trajectory{width:100%;height:auto;border:1px solid #cbd5e1}.grid line{stroke:#cbd5e1;stroke-width:1}.axis{fill:#475569;font-size:13px}table{width:100%;border-collapse:collapse}th,td{padding:9px 10px;border-bottom:1px solid #dbe3ec;text-align:right}th:first-child,td:first-child{text-align:left}.worst{font-weight:700;color:#be123c}.foot{margin-top:28px;color:#64748b;font-size:12px}@media(max-width:700px){main{margin:0;padding:18px}.meta{grid-template-columns:1fr}table{font-size:12px}}@media print{body{background:#fff}main{margin:0;max-width:none;box-shadow:none}.notice{break-inside:avoid}}
</style></head><body><main>
<h1>统一预测对比 · 静态快照报告</h1>
<div class="meta"><div><span class="label">快照：</span>${escapeHtml(snapshotId)}</div><div><span class="label">模型：</span>${escapeHtml(`${artifact.model_key}:${artifact.model_version}`)}</div><div><span class="label">Artifact：</span>${escapeHtml(artifact.artifact_id)}</div><div><span class="label">运行：</span>${escapeHtml(artifact.run_id)}</div><div><span class="label">结果生成：</span>${escapeHtml(artifact.created_at)}</div><div><span class="label">报告导出：</span>${escapeHtml(generatedAt)}</div><div><span class="label">评估协议：</span>${escapeHtml(protocol)}</div><div><span class="label">样本 SHA-256：</span>${escapeHtml(sampleHash)}</div></div>
<p class="notice">所有 ADE、FDE、最大水平误差和最差时刻均直接来自受校验 artifact；本报告只做静态呈现，不在浏览器重新计算科研指标。红点表示各预测方法的最大误差点。</p>
<h2>轨迹快照</h2><div class="legend">${tracks.map((track) => `<span><i class="dot" style="background:${escapeHtml(track.color)}"></i>${escapeHtml(track.label)}</span>`).join('')}</div>${buildTrajectorySvg(tracks)}
<h2>逐方法指标</h2><table><thead><tr><th>方法</th><th>最大水平误差</th><th>最差时刻</th><th>ADE</th><th>FDE</th></tr></thead><tbody>${rows
    .map(
      ({ track, metric }, index) =>
        `<tr${index === 0 ? ' class="worst"' : ''}><td>${escapeHtml(track.label)}${index === 0 ? '（全局最差）' : ''}</td><td>${escapeHtml(formatDistance(metric?.maxErrorMetres ?? null))}</td><td>${escapeHtml(formatTime(metric?.worstTimeSeconds ?? null))}</td><td>${escapeHtml(formatDistance(metric?.adeMetres ?? null))}</td><td>${escapeHtml(formatDistance(metric?.fdeMetres ?? null))}</td></tr>`,
    )
    .join('')}</tbody></table>
<p class="foot">离线单文件报告 · 来源 ${escapeHtml(artifact.analysis_result.title)} · AnalysisResult ${escapeHtml(artifact.analysis_result.schema_version)}。文件不包含 owner 身份、对话 ID、访问令牌或可执行脚本。</p>
</main></body></html>`
}

function safeFileSegment(value: string): string {
  return value.replaceAll(/[^A-Za-z0-9._-]+/g, '-').replaceAll(/^-+|-+$/g, '') || 'snapshot'
}

export function downloadComparisonSnapshotReport(input: ComparisonSnapshotReportInput): void {
  const html = buildComparisonSnapshotReportHtml(input)
  const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${safeFileSegment(input.artifact.artifact_id)}-${safeFileSegment(input.snapshotId)}.html`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  globalThis.setTimeout(() => URL.revokeObjectURL(url), 0)
}
