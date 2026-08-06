const assert = require('node:assert/strict')
const path = require('node:path')
const test = require('node:test')

const { appIconPathCandidates, resolveAppIconPath } = require('./app-icon-path.cjs')

test('appIconPathCandidates prefers native-loadable packaged icons before asar-internal pngs', () => {
  const appRoot = path.join('/Applications/Hermes.app', 'Contents', 'Resources', 'app.asar')
  const resourcesPath = path.dirname(appRoot)

  assert.deepEqual(appIconPathCandidates({ appRoot, resourcesPath, platform: 'darwin' }).slice(0, 3), [
    path.join(resourcesPath, 'icon.icns'),
    path.join(resourcesPath, 'icon.png'),
    path.join(resourcesPath, 'app.asar.unpacked', 'dist', 'apple-touch-icon.png')
  ])
})

test('resolveAppIconPath skips asar-internal candidates when an unpacked icon exists', () => {
  const appRoot = path.join('/Applications/Hermes.app', 'Contents', 'Resources', 'app.asar')
  const resourcesPath = path.dirname(appRoot)
  const existing = new Set([
    path.join(appRoot, 'public', 'apple-touch-icon.png'),
    path.join(resourcesPath, 'app.asar.unpacked', 'dist', 'apple-touch-icon.png')
  ])

  assert.equal(
    resolveAppIconPath({
      appRoot,
      resourcesPath,
      exists: filePath => existing.has(filePath),
      platform: 'darwin'
    }),
    path.join(resourcesPath, 'app.asar.unpacked', 'dist', 'apple-touch-icon.png')
  )
})
