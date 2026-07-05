const assert = require('node:assert/strict')
const test = require('node:test')

const { focusOrCreateMainWindow } = require('./window-focus.cjs')

function createWindowDouble({ destroyed = false, minimized = false, visible = true } = {}) {
  const calls = []
  return {
    calls,
    isDestroyed: () => destroyed,
    isMinimized: () => minimized,
    isVisible: () => visible,
    restore: () => calls.push('restore'),
    show: () => calls.push('show'),
    focus: () => calls.push('focus')
  }
}

test('focusOrCreateMainWindow creates a replacement instead of touching a destroyed window', () => {
  const win = createWindowDouble({ destroyed: true, minimized: true, visible: false })
  const created = []

  const result = focusOrCreateMainWindow(win, () => {
    created.push('create')
  })

  assert.equal(result, 'created')
  assert.deepEqual(created, ['create'])
  assert.deepEqual(win.calls, [])
})

test('focusOrCreateMainWindow restores and focuses a live minimized window', () => {
  const win = createWindowDouble({ minimized: true })
  const created = []

  const result = focusOrCreateMainWindow(win, () => {
    created.push('create')
  })

  assert.equal(result, 'focused')
  assert.deepEqual(created, [])
  assert.deepEqual(win.calls, ['restore', 'focus'])
})
