#!/usr/bin/env node
// Public native-compiler feedback only. No grader imports, stubs, props, or assertions.
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

const [filename, modules, mode, ...extra] = process.argv.slice(2)
if (!filename || !modules || extra.length || (mode && mode !== '--script-only')) {
  throw new Error('usage: vue_public_check.mjs SOURCE NODE_MODULES [--script-only]')
}
const require = createRequire(import.meta.url)
const { parse, compileScript, compileTemplate } = require(path.join(path.resolve(modules), '@vue/compiler-sfc'))
const absolute = path.resolve(filename)
const source = fs.readFileSync(absolute, 'utf8')
const { descriptor, errors } = parse(source, { filename: absolute, sourceMap: true })
if (errors.length) throw errors[0]
const scriptOnly = mode === '--script-only'
const compiled = (descriptor.script || descriptor.scriptSetup)
  ? compileScript(descriptor, {
    id: 'reviewbench-public',
    inlineTemplate: !scriptOnly,
    defineModel: true,
    sourceMap: true
  })
  : null
if (!scriptOnly && descriptor.template && !descriptor.scriptSetup) {
  const template = compileTemplate({
    id: 'reviewbench-public',
    filename: absolute,
    source: descriptor.template.content,
    sourceMap: true,
    compilerOptions: { bindingMetadata: compiled?.bindings ?? {} }
  })
  if (template.errors.length) throw template.errors[0]
}
console.log(`VUE_SFC_COMPILE_OK ${absolute}`)
