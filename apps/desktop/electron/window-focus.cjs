function focusOrCreateMainWindow(win, createWindow) {
  if (!win || win.isDestroyed()) {
    createWindow()
    return 'created'
  }

  if (win.isMinimized()) win.restore()
  win.focus()
  return 'focused'
}

module.exports = { focusOrCreateMainWindow }
