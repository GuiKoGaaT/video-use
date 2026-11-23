/** One stage as helpers/progress.py writes it. */
export type Stage = {
  id: string
  label: string
  key: string | null
  units: number | null
  estimate_s: number
  status: 'pending' | 'running' | 'done' | 'failed' | 'skipped'
  started_at: number | null
  done_at: number | null
  fraction: number | null
  note: string | null
}

/** The job in ~/.claude/video-use/progress.json (times in epoch seconds). */
export type Job = {
  job: string
  dir: string
  started_at: number
  updated_at: number
  status: 'running' | 'done' | 'failed'
  finished_at: number | null
  stages: Stage[]
}

declare module 'claude-code' {
  interface PluginState {
    'video-progress': {
      job: Shaped<Job | null>
      /** Clock of the last tick, epoch ms: redraws the time estimates each second. */
      now: number
    }
  }
}
