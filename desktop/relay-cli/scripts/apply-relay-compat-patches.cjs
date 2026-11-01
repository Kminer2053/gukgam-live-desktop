'use strict';

// 정상 빌드용 고정 의존성 API 보완. 앱 소스·선언·lock·상위 metadata는 바꾸지 않는다.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { createRequire } = require('node:module');
const EXPECTED_ROOT = path.resolve(__dirname, '..');
const VERSIONS = {
  vercel: '59.4.0', '@ts-morph/common': '0.11.1',
  '@vercel/python-analysis': '0.14.0', zod: '4.6.6', minimatch: '11.0.2',
};
const OLD_MM = 'var minimatch__default = /*#__PURE__*/_interopDefaultLegacy(minimatch);';
const NEW_MM = "var minimatch__default = /*#__PURE__*/_interopDefaultLegacy(typeof minimatch === 'function' ? minimatch : minimatch.minimatch);";
const TARGETS = [
  {
    file: 'node_modules/@ts-morph/common/dist/ts-morph-common.js', kind: 'minimatch',
    old: '3184b1f03ea81d1ba8fafc5fb67663dc0e2dded6bc5d77dc185815e5900050fa',
    patched: 'd767c2b1f8162516aa8ad0f06ca0236615b2ad21d8feeccc4ce31653bb31577c',
    changes: 1,
  },
  {
    file: 'node_modules/@vercel/python-analysis/dist/index.js', kind: 'records',
    old: 'ca5f8e54fdb53f8539823004370d02f7e30e9ce7a1e0b52986d1598fa50170b9',
    patched: '6f9b1d717aaf5d027c81021be048b412dfa0911102ff4720b0349942090c7aac',
    aliases: { z: 10, z3: 1, z4: 6 }, changes: 17,
  },
  {
    file: 'node_modules/@vercel/python-analysis/dist/index.cjs', kind: 'records',
    old: '17eaa6add98e9de2ddf308faac5b97d04d25197f5e31d6f6979307d50b7364ed',
    patched: 'ddc222fe459b3a51097f665a5ad5cfa8c562e64746fab79419a59246ffd7adac',
    aliases: { 'import_zod.z': 10, 'import_zod3.z': 1, 'import_zod4.z': 6 }, changes: 17,
  },
];

function demand(value, code) { if (!value) throw new Error(code); }
function sha(data) { return crypto.createHash('sha256').update(data).digest('hex'); }
function samePath(a, b) { return process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b; }
function inside(root, file) {
  const relative = path.relative(root, file);
  return relative === '' || (relative !== '..' && !path.isAbsolute(relative) && !relative.startsWith('..' + path.sep));
}
function unlinked(file) {
  for (let current = path.resolve(file); ; current = path.dirname(current)) {
    try { demand(!fs.lstatSync(current).isSymbolicLink(), 'ERR_SCHEMA_LINKED_PATH'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (current === path.dirname(current)) break;
  }
}
function under(root, relative) {
  demand(relative && typeof relative === 'string' && !path.isAbsolute(relative)
    && !relative.includes('\\') && !relative.includes(':') && !relative.split('/').includes('..'), 'ERR_SCHEMA_RELATIVE_PATH');
  const file = path.resolve(root, ...relative.split('/'));
  demand(file !== root && inside(root, file), 'ERR_SCHEMA_PATH_ESCAPE');
  unlinked(file);
  return file;
}
function read(file) {
  unlinked(file);
  demand(fs.lstatSync(file).isFile(), 'ERR_SCHEMA_INPUT_NOT_FILE');
  return fs.readFileSync(file);
}
function metadata(file, name) {
  const raw = read(file);
  const value = JSON.parse(raw.toString('utf8'));
  demand(value.name === name && value.version === VERSIONS[name], 'ERR_SCHEMA_PACKAGE_VERSION:' + name);
  return { file, raw, name, version: value.version, sha256: sha(raw) };
}
function resolved(root, fromFile, name) {
  const entry = createRequire(fromFile).resolve(name);
  demand(inside(root, entry), 'ERR_SCHEMA_RESOLUTION_ESCAPE');
  unlinked(entry);
  for (let folder = path.dirname(entry); folder !== root; folder = path.dirname(folder)) {
    demand(folder !== path.dirname(folder), 'ERR_SCHEMA_RESOLUTION_METADATA');
    const candidate = path.join(folder, 'package.json');
    if (fs.existsSync(candidate)) {
      const value = JSON.parse(read(candidate).toString('utf8'));
      if (value.name === name) return { ...metadata(candidate, name), entry };
    }
  }
  throw new Error('ERR_SCHEMA_RESOLUTION_METADATA');
}
function replaceOne(text, oldValue, newValue) {
  demand(text.split(oldValue).length === 2, 'ERR_SCHEMA_SINGLE_LITERAL');
  return text.replace(oldValue, newValue);
}

// 알려진 코드에서 괄호·문자열·주석을 구분하여 record의 최상위 인수 수를 검사한다.
function arity(text, offset) {
  const stack = [')'];
  let commas = 0;
  let quote = null;
  let escaped = false;
  let content = false;
  for (let i = offset; i < text.length; i++) {
    const char = text[i];
    if (quote) {
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === quote) quote = null;
      continue;
    }
    if (char === '"' || char === "'" || char === '`') { quote = char; content = true; continue; }
    if (char === '/' && text[i + 1] === '/') {
      const end = text.indexOf('\n', i + 2);
      demand(end !== -1, 'ERR_SCHEMA_UNTERMINATED_COMMENT');
      i = end;
      continue;
    }
    if (char === '/' && text[i + 1] === '*') {
      const end = text.indexOf('*/', i + 2);
      demand(end !== -1, 'ERR_SCHEMA_UNTERMINATED_COMMENT');
      i = end + 1;
      continue;
    }
    if ('([{'.includes(char)) {
      stack.push({ '(': ')', '[': ']', '{': '}' }[char]);
      content = true;
    } else if (')]}'.includes(char)) {
      demand(stack.pop() === char, 'ERR_SCHEMA_BRACKET_MISMATCH');
      if (stack.length === 0) return content ? commas + 1 : 0;
    } else if (char === ',' && stack.length === 1) commas++;
    else if (!/\s/.test(char)) content = true;
  }
  throw new Error('ERR_SCHEMA_UNTERMINATED_RECORD');
}
function records(text, spec, reverse = false) {
  const matches = [...text.matchAll(/\b(z\d*|import_zod\d*\.z)\.record\(/g)];
  demand(matches.length === spec.changes && [...text.matchAll(/\.record\(/g)].length === spec.changes,
    'ERR_SCHEMA_RECORD_COUNT');
  const counts = {};
  for (const match of matches) {
    const alias = match[1];
    demand(Object.hasOwn(spec.aliases, alias), 'ERR_SCHEMA_RECORD_ALIAS');
    counts[alias] = (counts[alias] || 0) + 1;
    demand(arity(text, match.index + match[0].length) === (reverse ? 2 : 1), 'ERR_SCHEMA_RECORD_ARITY');
  }
  demand(Object.entries(spec.aliases).every(([alias, count]) => counts[alias] === count)
    && Object.keys(counts).length === Object.keys(spec.aliases).length, 'ERR_SCHEMA_ALIAS_COUNT');
  for (const match of matches.reverse()) {
    const offset = match.index + match[0].length;
    const key = match[1] + '.string(), ';
    if (reverse) {
      demand(text.slice(offset, offset + key.length) === key, 'ERR_SCHEMA_INSERTED_KEY_DRIFT');
      text = text.slice(0, offset) + text.slice(offset + key.length);
    } else text = text.slice(0, offset) + key + text.slice(offset);
  }
  return text;
}
function plan(root, spec) {
  const file = under(root, spec.file);
  const current = read(file);
  const currentSha = sha(current);
  demand(currentSha === spec.old || currentSha === spec.patched, 'ERR_SCHEMA_UPSTREAM_SHA:' + spec.file);
  let original = current.toString('utf8');
  demand(Buffer.from(original).equals(current), 'ERR_SCHEMA_SOURCE_ENCODING');
  if (currentSha === spec.patched) original = spec.kind === 'minimatch'
    ? replaceOne(original, NEW_MM, OLD_MM) : records(original, spec, true);
  const originalData = Buffer.from(original);
  demand(sha(originalData) === spec.old, 'ERR_SCHEMA_RESTORATION_SHA');
  const patched = spec.kind === 'minimatch' ? replaceOne(original, OLD_MM, NEW_MM) : records(original, spec);
  const desired = Buffer.from(patched);
  demand(sha(desired) === spec.patched, 'ERR_SCHEMA_GENERATED_SHA');
  return { ...spec, absolute: file, originalData, desired, alreadyPatched: currentSha === spec.patched };
}
function writeNewOrVerify(file, data) {
  unlinked(file);
  if (fs.existsSync(file)) { demand(read(file).equals(data), 'ERR_SCHEMA_EVIDENCE_DRIFT'); return; }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  unlinked(file);
  fs.writeFileSync(file, data, { flag: 'wx' });
}
function replaceAtomically(file, data) {
  unlinked(file);
  const temporary = file + '.gukgam-schema-patch.tmp';
  unlinked(temporary);
  demand(!fs.existsSync(temporary), 'ERR_SCHEMA_TEMPORARY_EXISTS');
  fs.writeFileSync(temporary, data, { flag: 'wx' });
  fs.renameSync(temporary, file);
  demand(read(file).equals(data), 'ERR_SCHEMA_WRITTEN_SHA');
}

function main() {
  const arguments_ = process.argv.slice(2);
  demand(arguments_.length === 0 || (arguments_.length === 2 && arguments_[0] === '--root'), '사용법: node apply-relay-compat-patches.cjs [--root <relay-cli-dir>]');
  demand(Number.parseInt(process.versions.node.split('.')[0], 10) >= 24, 'ERR_SCHEMA_REQUIRES_NODE_24');
  const root = arguments_.length === 0 ? EXPECTED_ROOT : path.resolve(arguments_[1]);
  demand(samePath(root, EXPECTED_ROOT), 'ERR_RELAY_ROOT_MUST_MATCH_SCRIPT_LOCATION');
  unlinked(root);
  demand(fs.lstatSync(root).isDirectory() && samePath(fs.realpathSync(root), root), 'ERR_SCHEMA_ROOT_IDENTITY');
  const packages = Object.fromEntries(Object.keys(VERSIONS).map((name) => [name,
    metadata(under(root, 'node_modules/' + name + '/package.json'), name)]));
  const mm = resolved(root, under(root, TARGETS[0].file), 'minimatch');
  const zod = resolved(root, under(root, TARGETS[2].file), 'zod');
  const zodRelative = path.relative(root, zod.entry).split(path.sep).join('/');
  demand(!/(?:^|\/)v3(?:\/|$)/.test(zodRelative), 'ERR_SCHEMA_NATIVE_ZOD4_REQUIRED');
  const plans = TARGETS.map((target) => plan(root, target));
  const evidence = 'node_modules/.cache/gukgam-native-zod4-compat';
  const receiptFile = under(root, evidence + '/applied.json');
  const roots = ['package.json', 'package-lock.json'].map((name) => ({ file: name, sha256: sha(read(under(root, name))) }));
  const licenses = [];
  for (const name of ['@ts-morph/common', '@vercel/python-analysis']) {
    for (const leaf of ['LICENSE', 'LICENSE.md', 'LICENSE.txt']) {
      const file = 'node_modules/' + name + '/' + leaf;
      if (fs.existsSync(under(root, file))) licenses.push({ file, sha256: sha(read(under(root, file))) });
    }
  }
  const unchanged = [
    ...Object.values(packages).map((item) => ({ file: path.relative(root, item.file).split(path.sep).join('/'), sha256: item.sha256 })),
    ...roots, ...licenses,
  ];
  function verifyUnchanged() {
    for (const item of unchanged) demand(sha(read(under(root, item.file))) === item.sha256, 'ERR_SCHEMA_METADATA_OR_LICENSE_CHANGED');
  }
  const receipt = {
    schema_version: 1, kind: 'normal_native_zod4_build_compat', package_versions: VERSIONS,
    targets: plans.map((item) => ({ file: item.file, original_sha256: item.old, patched_sha256: item.patched, changes: item.changes })),
    resolved_dependencies: {
      minimatch: { version: mm.version, entry: path.relative(root, mm.entry).split(path.sep).join('/') },
      zod: { version: zod.version, module_specifier: 'zod', entry: zodRelative },
    },
    upstream_license_files: licenses, original_directory: evidence + '/originals',
    application_sources_written: 0, dependency_sources_patched: 3, source_changes: 35,
    dependency_metadata_changed: false, target_package_code_executed_by_patcher: 0,
  };
  const backups = plans.map((item) => ({ file: under(root, evidence + '/originals/' + item.file), data: item.originalData }));
  for (const item of backups) if (fs.existsSync(item.file)) demand(read(item.file).equals(item.data), 'ERR_SCHEMA_BACKUP_DRIFT');
  if (fs.existsSync(receiptFile)) {
    demand(plans.every((item) => item.alreadyPatched) && backups.every((item) => fs.existsSync(item.file))
      && read(receiptFile).equals(Buffer.from(JSON.stringify(receipt, null, 2) + '\n')), 'ERR_SCHEMA_RECEIPT_DRIFT');
    verifyUnchanged();
    console.log(JSON.stringify({ ok: true, already_applied: true, dependency_sources_patched_this_run: 0 }));
    return;
  }
  for (const item of backups) writeNewOrVerify(item.file, item.data);
  for (const item of plans) if (!item.alreadyPatched) replaceAtomically(item.absolute, item.desired);
  for (const item of plans) demand(sha(read(item.absolute)) === item.patched, 'ERR_SCHEMA_FINAL_SHA');
  verifyUnchanged();
  writeNewOrVerify(receiptFile, Buffer.from(JSON.stringify(receipt, null, 2) + '\n'));
  console.log(JSON.stringify({ ok: true, already_applied: false,
    dependency_sources_patched_this_run: plans.filter((item) => !item.alreadyPatched).length,
    targets: receipt.targets, resolved_dependencies: receipt.resolved_dependencies,
    evidence: receiptFile }));
}

try { main(); }
catch (error) { console.error(error && error.message ? error.message : 'ERR_NATIVE_ZOD4_COMPAT'); process.exitCode = 1; }
