/** 三维轨迹可视化契约类型（引擎隔离，见 docs/3D_VISUALIZATION.md）。 */

export type TrackSegmentKind = 'observed' | 'predicted' | 'actual_future' | 'unknown'

export interface TrackPoint3D {
  /** 相对时间（秒）；所属轨迹 timeBasis=sequence 时仅为点序，不表示秒。 */
  time: number
  longitude: number
  latitude: number
  /** 高度（米）；无高度序列时为 null */
  altitudeMetres: number | null
  /** 水平地速（米/秒）；无速度序列时为 null，仅用于展示，不参与科研指标。 */
  groundSpeedMps: number | null
  /** 东向速度（米/秒）；GBAR 六特征可直接提供，旧 artifact 可省略。 */
  eastVelocityMps?: number | null
  /** 北向速度（米/秒）；GBAR 六特征可直接提供，旧 artifact 可省略。 */
  northVelocityMps?: number | null
  /** 垂直速度（米/秒）；GBAR 的 rocd 换算后可直接提供，旧 artifact 可省略。 */
  verticalRateMps?: number | null
  /** 同一共享时刻的水平 Haversine 误差（米）；实际轨迹或旧 artifact 为 null。 */
  horizontalErrorMetres?: number | null
  /** 分段键原值，用于颜色映射与后续误差定位 */
  segment: string | null
}

export interface AircraftAttitudeEstimate {
  /** 航向角：正北为 0，顺时针为正，弧度。 */
  headingRadians: number
  /** 俯仰角：抬头为正，弧度。 */
  pitchRadians: number
  /** 滚转角：协调转弯近似，右滚为正，弧度。 */
  rollRadians: number
  /** velocity 表示使用速度特征；geometry 表示从相邻轨迹点反推。 */
  source: 'velocity' | 'geometry'
}

export interface Track3D {
  /** Missing observation time: indices are geometry order only, never seconds. */
  timeBasis?: 'sequence'
  /** 轨迹唯一键，如 "100002#predicted" */
  id: string
  /** 所属快照；同一快照内的轨迹共享时间游标。 */
  snapshotId: string
  label: string
  kind: TrackSegmentKind
  color: string
  /** 按 time 升序 */
  points: TrackPoint3D[]
}

export const TRAJECTORY_RENDER_SCHEMA_VERSION = 'trajectory-render-v1' as const
export const TRAJECTORY_DOWNSAMPLING_ALGORITHM =
  'segment-aware-spatiotemporal-max-error-v1' as const
export const DEFAULT_TRAJECTORY_SNAPSHOT_ID = '__default__' as const

export interface TrajectorySnapshot {
  id: string
  label: string
  trackCount: number
}

export interface TrajectoryRenderDownsampling {
  algorithm: typeof TRAJECTORY_DOWNSAMPLING_ALGORITHM
  maxPointsPerTrack: number
  /** 去除无效点和重复点后的输入点数。 */
  sourcePointCount: number
  renderedPointCount: number
  downsampledTrackCount: number
  /** 分段边界属于强制锚点；极端情况下锚点数可超过软上限。 */
  semanticAnchorOverflowTrackCount: number
}

/**
 * artifact 轨迹块到所有二维/三维视图之间的唯一渲染数据契约。
 * 页面和渲染引擎不得各自重新归一化或降采样。
 */
export interface TrajectoryRenderData {
  schemaVersion: typeof TRAJECTORY_RENDER_SCHEMA_VERSION
  tracks: Track3D[]
  snapshots: TrajectorySnapshot[]
  downsampling: TrajectoryRenderDownsampling
}

/** 轨迹分段配色，与 Streamlit 二维渲染一致：蓝=观测、橙=预测、绿=真实未来。 */
export const SEGMENT_COLORS: Record<TrackSegmentKind, string> = {
  observed: '#2563eb',
  predicted: '#f59e0b',
  actual_future: '#16a34a',
  unknown: '#9ca3af',
}

/** 统一对比方法的固定展示名称和颜色；未知方法由归一化器确定性分配颜色。 */
export const COMPARISON_TRACK_STYLES: Record<string, { label: string; color: string }> = {
  actual: { label: '实际轨迹', color: SEGMENT_COLORS.actual_future },
  'scat-gbar-v23': { label: 'GBAR v23', color: '#f59e0b' },
  'kalman-cv': { label: 'Kalman 常速度', color: '#8b5cf6' },
  'constant-velocity-heading': { label: '匀速-恒航向', color: '#06b6d4' },
  cfpl: { label: 'CFPL 计划航路', color: '#f43f5e' },
}

/** 底图源描述（引擎无关；具体提供者由实现内置，见 CesiumTrajectoryVisualizer.BASEMAPS）。 */
export interface BasemapPreset {
  id: string
  label: string
  /** 最大可用层级（超出后引擎用最大层级放大，避免请求空白层级） */
  maxLevel: number
}

export type TrajectoryViewPreset = 'oblique' | 'top' | 'side'

export interface TrajectoryErrorSelection {
  trackId: string
  time: number
  horizontalErrorMetres: number
}

export interface SnapshotMethodMetric {
  methodId: string
  adeMetres: number | null
  fdeMetres: number | null
  maxErrorMetres: number | null
  worstTimeSeconds: number | null
}

/**
 * 引擎无关的三维轨迹渲染接口。页面只依赖本接口；
 * M7 如需更换引擎只替换实现（当前实现：CesiumTrajectoryVisualizer）。
 */
export interface TrajectoryVisualizer {
  mount(container: HTMLElement): void
  setTracks(tracks: Track3D[]): void
  /** 时间游标（秒），唯一时间真源是 TimeCursorStore */
  setTime(seconds: number): void
  setSpeed(multiplier: number): void
  play(): void
  pause(): void
  /** 切换底图源（id 见实现提供的 BASEMAPS 列表） */
  setBasemap(id: string): void
  /** glTF 飞机模型；null 恢复项目内置模型。 */
  setAircraftModel(url: string | null): void
  /** 跟随视角：相机锁定目标轨迹尾部（原型简版，M7 细化平滑与目标选择） */
  setFollow(enabled: boolean): void
  /** 选择跟随轨迹；null 时使用第一条轨迹。 */
  setFollowTarget(trackId: string | null): void
  /** 仅改变三维显示高度，不修改轨迹数据和指标。 */
  setAltitudeScale(multiplier: number): void
  /** 切换三维相机预设。 */
  setViewPreset(preset: TrajectoryViewPreset): void
  /** 突出显示被定位的误差轨迹；null 清除。 */
  setHighlightTrack(trackId: string | null): void
  dispose(): void
}
