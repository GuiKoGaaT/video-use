import { expect, test } from 'claude-code/testing'

const PANE = {
  plugin: 'video-progress',
  component: 'Pane',
  requestId: 'video-progress',
  props: {
    title: 'Progresso do vídeo',
    isFocused: false,
    bodyColumns: 50,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 30 },
    view: {},
  },
} as const

const NOW = 1_800_000_100 // epoch s
const JOB = {
  job: 'piloto',
  dir: 'C:/videos/meu-video/edit',
  started_at: NOW - 100,
  updated_at: NOW - 1,
  status: 'running',
  finished_at: null,
  stages: [
    { id: 'transcribe', label: 'Transcrição', key: 'transcribe.local.cpu', units: 60, estimate_s: 110,
      status: 'done', started_at: NOW - 100, done_at: NOW - 40, fraction: 1, note: null },
    { id: 'matte', label: 'Recorte', key: 'matte.rvm.cpu', units: 60, estimate_s: 400,
      status: 'running', started_at: NOW - 40, done_at: null, fraction: 0.25, note: null },
    { id: 'composite', label: 'Composição', key: 'composite.1920', units: 14, estimate_s: 56,
      status: 'pending', started_at: null, done_at: null, fraction: null, note: null },
  ],
}

test('the pane draws the overall bar and one bar per stage, on every surface', async ($, on) => {
  on('command.register', () => ({ value: { command: 'video-progresso' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('env.get', (_, e) => ({ value: e.name === 'USERPROFILE' ? 'C:/Users/x' : undefined }))
  on('fs.stat', () => ({ value: { kind: 'file', size: 10, mtimeMs: 1, isLink: false } }))
  on('fs.read', () => ({ value: JSON.stringify(JOB) }))
  on('clock.now', () => ({ value: NOW * 1000 }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ text: /Geral/ })).toBeTruthy()
    expect(await ui.find({ text: /falta/ })).toBeTruthy()
    expect(await ui.find({ text: /Transcrição/ })).toBeTruthy()
    expect(await ui.find({ text: /Recorte/ })).toBeTruthy()
    expect(await ui.find({ text: /Composição/ })).toBeTruthy()
    await ui.unmount()
  }
})

test('with no job file the pane says nothing is running', async ($, on) => {
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await ui.find({ text: /Nenhum vídeo em andamento/ })).toBeTruthy()
  await ui.unmount()
})
