#!/usr/bin/env node

import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const root = process.cwd()
const dist = path.join(root, 'dist')

const runtimeFiles = [
  ['LICENSE', 'LICENSE'],
  [
    'node_modules/@coreui/coreui/dist/js/coreui.bundle.min.js',
    'js/coreui.bundle.min.js'
  ],
  ['node_modules/simplebar/dist/simplebar.css', 'simplebar/simplebar.css'],
  ['node_modules/simplebar/dist/simplebar.min.js', 'simplebar/simplebar.min.js']
]

const expectedDistFiles = [
  'LICENSE',
  'css/style.min.css',
  'js/coreui.bundle.min.js',
  'simplebar/simplebar.css',
  'simplebar/simplebar.min.js'
]

const readJson = file => JSON.parse(fs.readFileSync(path.join(root, file), 'utf8'))

const assertVersion = (packagePath, expectedVersion) => {
  const { version } = readJson(packagePath)
  if (version !== expectedVersion) {
    throw new Error(`${packagePath} must resolve to ${expectedVersion}, got ${version}`)
  }
}

const copyRuntime = () => {
  assertVersion('node_modules/@coreui/coreui/package.json', '5.9.0')
  assertVersion('node_modules/simplebar/package.json', '6.3.3')

  for (const [source, destination] of runtimeFiles) {
    const sourcePath = path.join(root, source)
    const destinationPath = path.join(dist, destination)
    fs.mkdirSync(path.dirname(destinationPath), { recursive: true })
    if (destination === 'js/coreui.bundle.min.js') {
      const sourceText = fs.readFileSync(sourcePath, 'utf8')
      const runtimeText = sourceText.replace(/\r?\n?\/\/# sourceMappingURL=.*$/u, '')
      fs.writeFileSync(destinationPath, runtimeText)
    } else {
      fs.copyFileSync(sourcePath, destinationPath)
    }
  }
}

const listFiles = directory => {
  if (!fs.existsSync(directory)) {
    return []
  }

  return fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const entryPath = path.join(directory, entry.name)
    if (entry.isDirectory()) {
      return listFiles(entryPath)
    }
    return [path.relative(dist, entryPath).replaceAll('\\', '/')]
  })
}

const verifyRuntime = () => {
  const actualFiles = listFiles(dist).sort()
  const expectedFiles = [...expectedDistFiles].sort()

  if (JSON.stringify(actualFiles) !== JSON.stringify(expectedFiles)) {
    throw new Error(
      `Unexpected runtime asset set.\nExpected: ${expectedFiles.join(', ')}\n` +
      `Actual: ${actualFiles.join(', ')}`
    )
  }

  for (const file of actualFiles) {
    const stat = fs.statSync(path.join(dist, file))
    if (stat.size === 0) {
      throw new Error(`Runtime asset is empty: ${file}`)
    }
  }

  const css = fs.readFileSync(path.join(dist, 'css/style.min.css'), 'utf8')
  if (!css.includes('--cui-body-color') || !css.includes('.sidebar')) {
    throw new Error('Compiled CoreUI stylesheet is missing required shell classes')
  }

  const coreui = fs.readFileSync(path.join(dist, 'js/coreui.bundle.min.js'), 'utf8')
  if (!coreui.includes('CoreUI') || !coreui.includes('Modal')) {
    throw new Error('CoreUI runtime bundle failed its content check')
  }

  if (css.includes('sourceMappingURL') || coreui.includes('sourceMappingURL')) {
    throw new Error('Production assets must not reference omitted source maps')
  }
}

const normalizeText = file => fs.readFileSync(file, 'utf8').replaceAll('\r\n', '\n')

const verifyVendor = () => {
  verifyRuntime()

  const vendorRoot = path.resolve(root, '../../web/static/vendor/coreui-template')
  for (const file of expectedDistFiles) {
    const builtPath = path.join(dist, file)
    const vendorPath = path.join(vendorRoot, file)
    if (!fs.existsSync(vendorPath)) {
      throw new Error(`Production vendor asset is missing: ${file}`)
    }
    if (normalizeText(builtPath) !== normalizeText(vendorPath)) {
      throw new Error(`Production vendor asset differs from reproducible build: ${file}`)
    }
  }

  for (const sourceMap of ['css/style.min.css.map', 'js/coreui.bundle.min.js.map']) {
    if (fs.existsSync(path.join(vendorRoot, sourceMap))) {
      throw new Error(`Unused production source map must be removed: ${sourceMap}`)
    }
  }
}

const command = process.argv[2]

if (command === 'copy') {
  copyRuntime()
} else if (command === 'verify') {
  verifyRuntime()
} else if (command === 'verify-vendor') {
  verifyVendor()
} else {
  throw new Error('Usage: node build/runtime-assets.mjs <copy|verify|verify-vendor>')
}
