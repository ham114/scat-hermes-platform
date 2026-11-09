import { defineStore } from 'pinia'

import type { Track3D } from '@/visualization/types'

export const PLAYBACK_SPEEDS = [0.25, 0.5, 1, 2, 4, 8, 16] as const

interface TimeCursorState {
  tracks: Track3D[]
  minTime: number
  maxTime: number
  currentTime: number
  playing: boolean
  speed: number
}

const initialState = (): TimeCursorState => ({
  tracks: [],
  minTime: 0,
  maxTime: 0,
  currentTime: 0,
  playing: false,
  speed: 1,
})

function trackBounds(tracks: Track3D[]): { min: number; max: number } {
  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY
  for (const track of tracks) {
    for (const point of track.points) {
      if (point.time < min) min = point.time
      if (point.time > max) max = point.time
    }
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { min: 0, max: 0 }
  return { min, max }
}

/**
 * 全局时间游标：二维/三维地图与高度、地速曲线共享的唯一时间真源。
 * 播放越过 maxTime 自动停止（单次播放语义）；speed 限制在白名单倍速内。
 */
export const useTimeCursorStore = defineStore('timeCursor', {
  state: initialState,
  getters: {
    hasTracks: (state) => state.tracks.length > 0,
    duration: (state) => Math.max(state.maxTime - state.minTime, 0),
  },
  actions: {
    setTracks(tracks: Track3D[]) {
      this.tracks = tracks
      const bounds = trackBounds(tracks)
      this.minTime = bounds.min
      this.maxTime = bounds.max
      this.playing = false
      this.currentTime = this.clampTime(bounds.min)
    },
    /** 把时间钳制到当前轨迹范围内。 */
    clampTime(seconds: number): number {
      if (!this.hasTracks) return 0
      return Math.min(Math.max(seconds, this.minTime), this.maxTime)
    },
    setTime(seconds: number) {
      this.currentTime = this.clampTime(seconds)
    },
    setSpeed(multiplier: number) {
      this.speed = (PLAYBACK_SPEEDS as readonly number[]).includes(multiplier) ? multiplier : 1
    },
    play() {
      if (!this.hasTracks) return
      if (this.currentTime >= this.maxTime) this.currentTime = this.minTime
      this.playing = true
    },
    pause() {
      this.playing = false
    },
    toggle() {
      if (this.playing) this.pause()
      else this.play()
    },
    /** 由渲染循环按真实流逝时间推进；越过终点自动停止并停在终点。 */
    advance(deltaSeconds: number) {
      if (!this.playing || !this.hasTracks) return
      const next = this.currentTime + deltaSeconds * this.speed
      if (next >= this.maxTime) {
        this.currentTime = this.maxTime
        this.playing = false
      } else {
        this.currentTime = next
      }
    },
    reset() {
      Object.assign(this, initialState())
    },
  },
})
