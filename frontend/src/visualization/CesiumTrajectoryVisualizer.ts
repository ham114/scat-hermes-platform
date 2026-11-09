/** CesiumJS 实现的 TrajectoryVisualizer（M3-V3 最小原型）。 */

// Cesium 从 window.CESIUM_BASE_URL 解析 Workers/Assets/Widgets 静态资源，
// 必须在任何 Cesium 代码执行前设置（vite.config.ts 已把这些目录拷贝到 /cesium）。
declare global {
  interface Window {
    CESIUM_BASE_URL: string | undefined
  }
}
window.CESIUM_BASE_URL = window.CESIUM_BASE_URL ?? '/cesium'

import * as Cesium from 'cesium'
import 'cesium/Build/Cesium/Widgets/widgets.css'

import { estimateAircraftAttitude } from '@/utils/trajectoryPlayback'
import type { BasemapPreset, Track3D, TrajectoryViewPreset, TrajectoryVisualizer } from './types'

interface TrackRenderState {
  track: Track3D
  positions: Cesium.Cartesian3[]
  times: number[]
  fullLine: Cesium.Entity
  groundLine: Cesium.Entity
  altitudeStem: Cesium.Entity
  marker: Cesium.Entity
  trail: Cesium.Entity
}

/**
 * 底图预设列表。高德免 key 接口真实数据到 18 级（19 级返回灰色占位图）；
 * Esri 卫星影像官方公开服务到 19 级，放大细节最好。正式验收前仍按计划切换自建瓦片。
 */
export interface CesiumBasemapPreset extends BasemapPreset {
  url: string
  subdomains?: string[]
  minLevel?: number
  credit: string
}

export const BASEMAPS: CesiumBasemapPreset[] = [
  {
    id: 'osm',
    label: 'OpenStreetMap 街道',
    url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    minLevel: 0,
    maxLevel: 19,
    credit:
      '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>',
  },
  {
    id: 'esri',
    label: 'Esri 卫星影像',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    minLevel: 0,
    maxLevel: 19,
    credit: '影像 © Esri, Maxar, Earthstar Geographics',
  },
  {
    id: 'amap',
    label: '高德街道（仅内部调试）',
    url: 'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
    subdomains: ['1', '2', '3', '4'],
    minLevel: 3,
    maxLevel: 18,
    credit: '底图 © 高德地图（仅内部科研调试，正式验收前切换自建瓦片）',
  },
]

export const DEFAULT_BASEMAP_ID = 'osm'

function basemapById(id: string): CesiumBasemapPreset {
  return BASEMAPS.find((preset) => preset.id === id) ?? BASEMAPS[0]!
}

function imageryProviderFor(preset: CesiumBasemapPreset): Cesium.UrlTemplateImageryProvider {
  return new Cesium.UrlTemplateImageryProvider({
    url: preset.url,
    subdomains: preset.subdomains,
    minimumLevel: preset.minLevel,
    maximumLevel: preset.maxLevel,
    credit: new Cesium.Credit(preset.credit, true),
  })
}

function pointPosition(point: Track3D['points'][number], altitudeScale: number): Cesium.Cartesian3 {
  return Cesium.Cartesian3.fromDegrees(
    point.longitude,
    point.latitude,
    (point.altitudeMetres ?? 0) * altitudeScale,
  )
}

function groundPosition(point: Track3D['points'][number]): Cesium.Cartesian3 {
  return Cesium.Cartesian3.fromDegrees(point.longitude, point.latitude, 0)
}

/** 项目内置低多边形客机，离线可用；模型机头沿局部 +X、机顶沿 +Z。 */
export const DEFAULT_AIRCRAFT_MODEL_URL = '/models/aircraft-low-poly.gltf'

interface SampledState {
  position: Cesium.Cartesian3
  /** 航向（北向顺时针弧度）。 */
  heading: number
  pitch: number
  roll: number
}

/** 在升序 times 中找 time 所在区间并线性插值；越界钳制到端点。 */
function samplePosition(state: TrackRenderState, time: number): SampledState {
  const { times, positions } = state
  const attitude = estimateAircraftAttitude(state.track, time)
  const sampledAttitude = {
    heading: attitude?.headingRadians ?? 0,
    pitch: attitude?.pitchRadians ?? 0,
    roll: attitude?.rollRadians ?? 0,
  }
  if (time <= times[0]!) {
    return { position: positions[0]!.clone(), ...sampledAttitude }
  }
  if (time >= times[times.length - 1]!) {
    return { position: positions[positions.length - 1]!.clone(), ...sampledAttitude }
  }
  let low = 0
  let high = times.length - 1
  while (high - low > 1) {
    const mid = (low + high) >> 1
    if (times[mid]! <= time) low = mid
    else high = mid
  }
  const t0 = times[low]!
  const t1 = times[high]!
  const ratio = (time - t0) / Math.max(t1 - t0, Number.EPSILON)
  const position = Cesium.Cartesian3.lerp(
    positions[low]!,
    positions[high]!,
    ratio,
    new Cesium.Cartesian3(),
  )
  return { position, ...sampledAttitude }
}

/**
 * 最小原型实现：
 * - 每条轨迹渲染 3D 折线 + 一个带估计姿态的 glTF 飞机 + 已飞过的尾迹；
 * - 时间游标由 TimeCursorStore 驱动（setTime），Cesium Clock 不参与播放，
 *   保证地图与高度、地速曲线共享同一时间真源（M7-05）。
 */
export class CesiumTrajectoryVisualizer implements TrajectoryVisualizer {
  private viewer: Cesium.Viewer | null = null
  private container: HTMLElement | null = null
  private states: TrackRenderState[] = []
  private basemapId: string
  private followEnabled = false
  private followTargetId: string | null = null
  private altitudeScale = 1
  private viewPreset: TrajectoryViewPreset = 'oblique'
  private highlightTrackId: string | null = null
  private currentTime = 0
  private aircraftModelUrl = DEFAULT_AIRCRAFT_MODEL_URL
  private readonly onBasemapFailed: ((error: unknown) => void) | undefined
  private readonly dimension: '2d' | '3d'

  constructor(
    basemapId: string = DEFAULT_BASEMAP_ID,
    onBasemapFailed?: (error: unknown) => void,
    dimension: '2d' | '3d' = '3d',
  ) {
    this.basemapId = basemapId
    this.onBasemapFailed = onBasemapFailed
    this.dimension = dimension
  }

  mount(container: HTMLElement): void {
    if (this.viewer) return
    const viewer = new Cesium.Viewer(container, {
      animation: false,
      baseLayerPicker: false,
      fullscreenButton: false,
      geocoder: false,
      homeButton: false,
      infoBox: false,
      navigationHelpButton: false,
      sceneModePicker: false,
      selectionIndicator: false,
      timeline: false,
      sceneMode: this.dimension === '2d' ? Cesium.SceneMode.SCENE2D : Cesium.SceneMode.SCENE3D,
      baseLayer: new Cesium.ImageryLayer(imageryProviderFor(basemapById(this.basemapId))),
    })
    viewer.scene.globe.depthTestAgainstTerrain = false
    // 底图瓦片加载失败（如访问被阻断、图片无法解码）会让 Cesium 停止渲染。
    // 降级：移除全部影像层并请求恢复渲染；轨迹 entity 不依赖底图，可继续回放。
    viewer.scene.renderError.addEventListener((_scene, error) => {
      if (viewer.imageryLayers.length > 0) {
        viewer.imageryLayers.removeAll()
        viewer.scene.requestRender()
        this.onBasemapFailed?.(error)
      }
    })
    this.viewer = viewer
    this.container = container
    viewer.camera.changed.addEventListener(() => this.updateViewMetadata())
  }

  setTracks(tracks: Track3D[]): void {
    const viewer = this.viewer
    if (!viewer) return
    this.clearEntities()
    this.states = tracks
      .filter((track) => track.points.length > 0)
      .map((track) => {
        const positions = track.points.map((point) => pointPosition(point, this.altitudeScale))
        const groundPositions = track.points.map(groundPosition)
        const times = track.points.map((point) => point.time)
        const origin = positions[0]!.clone()
        const trackColor = Cesium.Color.fromCssColorString(track.color)
        // 整条轨迹线（半透明），让回放前也能看到完整轨迹形状
        const fullLine = viewer.entities.add({
          polyline: {
            positions: positions.map((position) => position.clone()),
            width: 3,
            material: new Cesium.PolylineOutlineMaterialProperty({
              color: trackColor.withAlpha(
                track.timeBasis === 'sequence' || this.dimension === '2d' ? 1 : 0.6,
              ),
              outlineColor: Cesium.Color.BLACK.withAlpha(0.65),
              outlineWidth: 1,
            }),
          },
        })
        // 地面投影与当前高度垂线提供明确的三维高度参照；只影响显示。
        const groundLine = viewer.entities.add({
          polyline: {
            positions: groundPositions,
            width: 1,
            material: new Cesium.PolylineDashMaterialProperty({
              color: trackColor.withAlpha(0.35),
              dashLength: 12,
            }),
          },
        })
        const altitudeStem = viewer.entities.add({
          polyline: {
            positions: [groundPositions[0]!.clone(), origin.clone()],
            width: 1,
            material: trackColor.withAlpha(0.55),
          },
        })
        const initialSample = samplePosition(
          { track, positions, times } as TrackRenderState,
          times[0]!,
        )
        // 真实 glTF 网格；minimumPixelSize 只保证远景可辨，近景仍按模型米制尺寸显示。
        const marker = viewer.entities.add({
          position: origin,
          orientation: Cesium.Transforms.headingPitchRollQuaternion(
            origin,
            new Cesium.HeadingPitchRoll(
              initialSample.heading,
              initialSample.pitch,
              initialSample.roll,
            ),
          ),
          model: {
            show: track.timeBasis !== 'sequence' && this.dimension !== '2d',
            uri: this.aircraftModelUrl,
            minimumPixelSize: 30,
            maximumScale: 120,
            color: trackColor,
            colorBlendMode: Cesium.ColorBlendMode.MIX,
            colorBlendAmount: 0.72,
            silhouetteColor: Cesium.Color.WHITE.withAlpha(0.9),
            silhouetteSize: 0,
            runAnimations: false,
          },
          point: {
            show: track.timeBasis === 'sequence' || this.dimension === '2d',
            pixelSize: 8,
            color: trackColor,
            outlineColor: Cesium.Color.WHITE,
            outlineWidth: 2,
          },
        })
        // 已飞过的尾迹（实色，覆盖在半透明全轨迹线上）
        const trail = viewer.entities.add({
          polyline: {
            positions: [origin.clone()],
            width: 2,
            material: trackColor.withAlpha(0.85),
          },
        })
        return { track, positions, times, fullLine, groundLine, altitudeStem, marker, trail }
      })
    this.flyToTracks()
    this.updateViewMetadata()
    this.applyTrackHighlight()
  }

  setTime(seconds: number): void {
    this.currentTime = seconds
    for (const state of this.states) {
      const time = state.track.timeBasis === 'sequence' ? state.times.at(-1)! : seconds
      const sampled = samplePosition(state, time)
      ;(state.marker.position as Cesium.ConstantPositionProperty).setValue(sampled.position)
      state.marker.orientation = new Cesium.ConstantProperty(
        Cesium.Transforms.headingPitchRollQuaternion(
          sampled.position,
          new Cesium.HeadingPitchRoll(sampled.heading, sampled.pitch, sampled.roll),
        ),
      )
      const high = upperBound(state.times, time)
      const traveled = state.positions.slice(0, high + 1)
      if (high + 1 < state.positions.length) {
        traveled.push(sampled.position)
      }
      ;(state.trail.polyline as Cesium.PolylineGraphics).positions = new Cesium.ConstantProperty(
        traveled,
      )
      const cartographic = Cesium.Cartographic.fromCartesian(sampled.position)
      const ground = Cesium.Cartesian3.fromRadians(cartographic.longitude, cartographic.latitude, 0)
      ;(state.altitudeStem.polyline as Cesium.PolylineGraphics).positions =
        new Cesium.ConstantProperty([ground, sampled.position])
    }
    this.updateFollowCamera(seconds)
    if (this.viewer) this.viewer.scene.requestRender()
  }

  setFollow(enabled: boolean): void {
    this.followEnabled = enabled
    if (enabled) this.updateFollowCamera(this.currentTime)
    // 退出跟随时恢复自由视角，避免相机被锁在低空
    if (!enabled && this.viewer && this.states.length > 0) {
      this.flyToTracks()
    }
  }

  setFollowTarget(trackId: string | null): void {
    this.followTargetId = trackId
    if (this.followEnabled) this.updateFollowCamera(this.currentTime)
  }

  setAltitudeScale(multiplier: number): void {
    if (![1, 2, 5, 10].includes(multiplier) || multiplier === this.altitudeScale) return
    this.altitudeScale = multiplier
    for (const state of this.states) {
      state.positions = state.track.points.map((point) => pointPosition(point, multiplier))
      ;(state.fullLine.polyline as Cesium.PolylineGraphics).positions = new Cesium.ConstantProperty(
        state.positions,
      )
    }
    this.setTime(this.currentTime)
    if (!this.followEnabled) this.flyToTracks()
  }

  setViewPreset(preset: TrajectoryViewPreset): void {
    this.viewPreset = preset
    this.followEnabled = false
    this.flyToTracks()
  }

  resize(): void {
    this.viewer?.forceResize()
    if (!this.followEnabled) this.flyToTracks()
  }

  setHighlightTrack(trackId: string | null): void {
    this.highlightTrackId = trackId
    this.applyTrackHighlight()
  }

  setBasemap(id: string): void {
    const viewer = this.viewer
    if (!viewer) {
      this.basemapId = id
      return
    }
    this.basemapId = id
    viewer.imageryLayers.removeAll()
    viewer.imageryLayers.addImageryProvider(imageryProviderFor(basemapById(id)))
  }

  setSpeed(): void {
    // 原型由页面 RAF 循环驱动；本接口为 M7 引擎内置时钟模式保留。
  }

  play(): void {
    // 原型由页面 RAF 循环驱动；本接口为 M7 引擎内置时钟模式保留。
  }

  pause(): void {
    // 原型由页面 RAF 循环驱动；本接口为 M7 引擎内置时钟模式保留。
  }

  setAircraftModel(url: string | null): void {
    this.aircraftModelUrl = url?.trim() || DEFAULT_AIRCRAFT_MODEL_URL
    for (const state of this.states) {
      ;(state.marker.model as Cesium.ModelGraphics).uri = new Cesium.ConstantProperty(
        this.aircraftModelUrl,
      )
    }
    this.viewer?.scene.requestRender()
  }

  dispose(): void {
    if (this.viewer) {
      this.clearEntities()
      this.viewer.destroy()
      this.viewer = null
    }
    this.states = []
    this.container = null
  }

  private clearEntities(): void {
    if (!this.viewer) return
    for (const state of this.states) {
      this.viewer.entities.remove(state.fullLine)
      this.viewer.entities.remove(state.groundLine)
      this.viewer.entities.remove(state.altitudeStem)
      this.viewer.entities.remove(state.marker)
      this.viewer.entities.remove(state.trail)
    }
    this.states = []
  }

  private applyTrackHighlight(): void {
    for (const state of this.states) {
      const highlighted = state.track.id === this.highlightTrackId
      ;(state.marker.model as Cesium.ModelGraphics).silhouetteSize = new Cesium.ConstantProperty(
        highlighted ? 3 : 0,
      )
      ;(state.trail.polyline as Cesium.PolylineGraphics).width = new Cesium.ConstantProperty(
        highlighted ? 5 : 2,
      )
    }
    this.viewer?.scene.requestRender()
  }

  /** 跟随视角：相机锁定用户选择的轨迹；未选择时使用第一条。 */
  private updateFollowCamera(seconds: number): void {
    const viewer = this.viewer
    if (!viewer || !this.followEnabled || this.states.length === 0) return
    const target =
      this.states.find((state) => state.track.id === this.followTargetId) ?? this.states[0]!
    const sampled = samplePosition(target, seconds)
    viewer.camera.lookAt(
      sampled.position,
      new Cesium.HeadingPitchRange(sampled.heading, -Cesium.Math.toRadians(12), 600),
    )
  }

  private flyToTracks(): void {
    const viewer = this.viewer
    if (!viewer || this.states.length === 0) return
    const all: Cesium.Cartesian3[] = []
    for (const state of this.states) {
      all.push(...state.positions)
    }
    const bounds = Cesium.BoundingSphere.fromPoints(all)
    const offsetByPreset: Record<TrajectoryViewPreset, Cesium.HeadingPitchRange> = {
      oblique: new Cesium.HeadingPitchRange(
        0,
        -Cesium.Math.PI_OVER_THREE,
        bounds.radius < 1 ? 2_000 : 0,
      ),
      top: new Cesium.HeadingPitchRange(0, -Cesium.Math.PI_OVER_TWO, bounds.radius < 1 ? 2_000 : 0),
      side: new Cesium.HeadingPitchRange(
        Cesium.Math.PI_OVER_TWO,
        -Cesium.Math.toRadians(12),
        bounds.radius < 1 ? 2_000 : 0,
      ),
    }
    viewer.camera.cancelFlight()
    viewer.camera.viewBoundingSphere(
      bounds,
      offsetByPreset[this.dimension === '2d' ? 'top' : this.viewPreset],
    )
    viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY)
    viewer.scene.requestRender()
    this.updateViewMetadata()
  }

  private updateViewMetadata(): void {
    if (!this.viewer || !this.container) return
    const position = this.viewer.camera.positionCartographic
    this.container.dataset.trackCount = String(this.states.length)
    this.container.dataset.pointCount = String(
      this.states.reduce((n, state) => n + state.track.points.length, 0),
    )
    this.container.dataset.cameraLongitude = String(Cesium.Math.toDegrees(position.longitude))
    this.container.dataset.cameraLatitude = String(Cesium.Math.toDegrees(position.latitude))
    this.container.dataset.sceneDimension = this.dimension
  }
}

/** 返回 times 中最后一个 <= time 的下标；全部大于 time 时返回 -1。 */
function upperBound(times: number[], time: number): number {
  let low = 0
  let high = times.length - 1
  let result = -1
  while (low <= high) {
    const mid = (low + high) >> 1
    if (times[mid]! <= time) {
      result = mid
      low = mid + 1
    } else {
      high = mid - 1
    }
  }
  return result
}
