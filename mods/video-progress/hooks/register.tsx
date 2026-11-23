import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Job, Stage } from '../types'

// The pane reads the file helpers/progress.py (skill video-use) writes:
// ~/.claude/video-use/progress.json. Estimates come from timings.json there,
// so the pane only does arithmetic on what the scripts wrote.
const PANE = 'video-progress'
const TITLE = 'Progresso do vídeo'
const job = atom({ plugin: 'video-progress', key: 'job' } as const, null, { shape: 'job-1' })
const now = atom({ plugin: 'video-progress', key: 'now' } as const, 0)

// Module variables start over on a reload; they only avoid repeat work.
let lastMtime = -1
let seenStart = -1
let lastStatus = ''
let progressPath = ''

const fmt = (s: number): string => {
  const t = Math.max(0, Math.round(s))
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const ss = String(t % 60).padStart(2, '0')
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`
}
const approx = (s: number): string => (s >= 90 ? `~${Math.round(s / 60)} min` : `~${Math.max(1, Math.round(s))} s`)

type Calc = { frac: number; elapsed: number; remaining: number }

/** Fraction, elapsed and remaining seconds of one stage at time t (epoch s). */
const calc = (st: Stage, t: number): Calc => {
  const est = Math.max(1, st.estimate_s || 1)
  if (st.status === 'done' || st.status === 'skipped') {
    const elapsed = st.started_at && st.done_at ? st.done_at - st.started_at : 0
    return { frac: 1, elapsed, remaining: 0 }
  }
  if (st.status === 'pending') return { frac: 0, elapsed: 0, remaining: est }
  const elapsed = st.started_at ? Math.max(0, t - st.started_at) : 0
  if (st.status === 'failed') return { frac: st.fraction ?? 0, elapsed, remaining: 0 }
  // running: a real fraction from the script wins over the average
  if (st.fraction !== null && st.fraction > 0.02) {
    const f = Math.min(1, st.fraction)
    return { frac: f, elapsed, remaining: (elapsed / f) * (1 - f) }
  }
  const f = Math.min(0.95, elapsed / est)
  return { frac: f, elapsed, remaining: Math.max(est - elapsed, est * 0.05) }
}

const overall = (j: Job, t: number) => {
  let total = 0
  let doneW = 0
  let remaining = 0
  for (const st of j.stages) {
    const c = calc(st, t)
    const w = Math.max(1, st.estimate_s || 1)
    total += w
    doneW += w * c.frac
    remaining += c.remaining
  }
  return { frac: total > 0 ? doneW / total : 0, remaining }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'video-progresso',
      description: 'Abre o painel com o progresso do vídeo (skill video-use)',
    })
    const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? ''
    progressPath = `${home.replace(/\\/g, '/')}/.claude/video-use/progress.json`

    const tick = async (): Promise<void> => {
      let mtime = -1
      try {
        mtime = (await $.fs.stat(progressPath)).mtimeMs
      } catch {
        return
      }
      const t = await $.clock.now()
      if (mtime !== lastMtime) {
        lastMtime = mtime
        let parsed: Job | null = null
        try {
          parsed = JSON.parse(await $.fs.read(progressPath)) as Job
        } catch {
          return // mid-write: the next tick reads it
        }
        if (!parsed || !Array.isArray(parsed.stages)) return
        await update($, job, () => parsed)

        // a new job that is running now: bring the pane up
        const fresh = t / 1000 - parsed.updated_at < 600
        if (parsed.status === 'running' && parsed.started_at !== seenStart && fresh) {
          seenStart = parsed.started_at
          void $.ui.open({ id: PANE, title: TITLE })
        }
        if (parsed.status !== lastStatus) {
          if (lastStatus === 'running' && parsed.status === 'done') {
            $.ui.toast(`🎬 ${parsed.job}: pronto em ${fmt(parsed.updated_at - parsed.started_at)}`)
          }
          if (lastStatus === 'running' && parsed.status === 'failed') {
            $.ui.toast(`🎬 ${parsed.job}: uma etapa falhou`)
          }
          lastStatus = parsed.status
        }
      }
      const j = await read($, job)
      if (j && j.status === 'running') {
        await update($, now, () => t)
        const o = overall(j, t / 1000)
        $.ui.status(`🎬 ${Math.round(o.frac * 100)}% · falta ${approx(o.remaining)}`)
      } else {
        $.ui.status(undefined)
      }
    }

    await tick()
    $.clock.every(1000, tick)

    return next(e)
  })

  on('command.run', { command: 'video-progresso' }, async $ => {
    await $.ui.open({ id: PANE, title: TITLE })

    return { text: 'Painel de progresso do vídeo aberto.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Progress } = $.ui.resolve(e)
    const j = await read($, job)
    const nowMs = await read($, now)
    if (!j) {
      return (
        <Box flexDirection="column">
          <Text dimColor>Nenhum vídeo em andamento.</Text>
          <Text dimColor>Os scripts da skill video-use gravam o progresso aqui.</Text>
        </Box>
      )
    }
    const t = j.status === 'running' ? Math.max(nowMs / 1000, j.updated_at) : (j.finished_at ?? j.updated_at)
    const o = overall(j, t)
    const total = t - j.started_at
    const stale = j.status === 'running' && t - j.updated_at > 900
    const headline =
      j.status === 'done'
        ? `Pronto em ${fmt(total)}`
        : j.status === 'failed'
          ? `Parou com erro após ${fmt(total)}`
          : `${Math.round(o.frac * 100)}% · falta ${approx(o.remaining)} · ${fmt(total)} até agora`

    return (
      <Box flexDirection="column" gap={1}>
        <Box flexDirection="column">
          <Text weight="bold">{j.job}</Text>
          <Text dimColor>{j.dir}</Text>
        </Box>
        <Progress
          label="Geral"
          value={j.status === 'done' ? 1 : o.frac}
          valueText={headline}
          color={j.status === 'failed' ? 'error' : j.status === 'done' ? 'success' : 'claude'}
        />
        {stale && <Text color="warning">Sem atualização há {approx(t - j.updated_at)}: o processo pode ter parado.</Text>}
        <Box flexDirection="column" gap={1}>
          {j.stages.map(st => {
            const c = calc(st, t)
            const mark =
              st.status === 'done' ? '✓ ' : st.status === 'failed' ? '✗ ' : st.status === 'running' ? '▶ ' : st.status === 'skipped' ? '– ' : '· '
            const text =
              st.status === 'done'
                ? `${fmt(c.elapsed)} (média ${fmt(st.estimate_s)})`
                : st.status === 'running'
                  ? `${fmt(c.elapsed)} · falta ${approx(c.remaining)}${st.fraction !== null ? '' : ' (pela média)'}`
                  : st.status === 'failed'
                    ? `falhou${st.note ? `: ${st.note}` : ''}`
                    : st.status === 'skipped'
                      ? 'pulada'
                      : `estimado ${approx(st.estimate_s)}`
            return (
              <Progress
                label={`${mark}${st.label}`}
                value={c.frac}
                valueText={text}
                color={st.status === 'failed' ? 'error' : st.status === 'done' ? 'success' : 'claude'}
              />
            )
          })}
        </Box>
      </Box>
    )
  })
}
