const path = require('node:path')

function unpackedPathFor(filePath) {
  return String(filePath || '').replace(/app\.asar(?=$|[\\/])/, 'app.asar.unpacked')
}

function unique(paths) {
  return [...new Set(paths.filter(Boolean))]
}

function appIconPathCandidates({ appRoot, resourcesPath = appRoot ? path.dirname(appRoot) : '', platform = process.platform }) {
  const nativeResourceCandidates = []

  if (resourcesPath) {
    if (platform === 'darwin') {
      nativeResourceCandidates.push(path.join(resourcesPath, 'icon.icns'))
    }
    nativeResourceCandidates.push(path.join(resourcesPath, 'icon.png'))
  }

  return unique([
    ...nativeResourceCandidates,
    appRoot ? path.join(unpackedPathFor(appRoot), 'dist', 'apple-touch-icon.png') : null,
    appRoot ? path.join(appRoot, 'assets', 'icon.png') : null,
    appRoot ? path.join(appRoot, 'public', 'apple-touch-icon.png') : null,
    appRoot ? path.join(appRoot, 'dist', 'apple-touch-icon.png') : null
  ])
}

function resolveAppIconPath({ appRoot, resourcesPath, platform, exists = () => false }) {
  return appIconPathCandidates({ appRoot, resourcesPath, platform }).find(exists)
}

module.exports = { appIconPathCandidates, resolveAppIconPath, unpackedPathFor }
