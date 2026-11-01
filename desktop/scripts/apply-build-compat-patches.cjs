'use strict';

// 정상 빌드의 고정 의존성 API만 보완한다. 앱 소스·package/lock은 바꾸지 않는다.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { createRequire } = require('node:module');

const EXPECTED_ROOT = path.resolve(__dirname, '..');
const VERSIONS = {
  '@electron/asar': '4.3.1', filelist: '2.0.2', glob: '14.0.3',
  rimraf: '6.1.3', minimatch: '11.0.2', tar: '7.5.22',
  'dir-compare': '4.2.0', temp: '0.9.4',
};
const PACKAGE_SHA = {
  'dir-compare': '3a7fe9db9dcd63e1f503b7e137e515fe14741c7c1f0181cdf95b53489e6e7ec0',
  temp: 'd26998b8c650d2917e65bcd5397b53b30a22c5a96e44d75e5a2fbf254680def0',
};
const TARGETS = [
  {
    file: 'node_modules/dir-compare/build/src/FilterHandler/defaultFilterHandler.js',
    old: '6b753f018576fc270dc93e2be35d98a50431bd7f1e373b98acbec5e7acf381d2',
    patched: 'e1e53e006bb50be27de25da7e159434630ec842b1aa748e5b86766e538ba56aa',
    replacements: [
      ['const minimatch_1 = __importDefault(require("minimatch"));', 'const minimatch_1 = require("minimatch");'],
      ['minimatch_1.default', 'minimatch_1.minimatch'],
    ],
  },
  {
    file: 'node_modules/temp/lib/temp.js',
    old: 'ab00e9972ccbd639a4558519f40ad6839cb2c616b3da6cb9c12dea2828c6c9c9',
    patched: '985f2836396d8aaf8aab89ca716689c436532df26761c2edfadb26c7a05869b7',
    replacements: [["let rimraf     = require('rimraf');", "let rimraf     = require('./gukgam-rimraf6-cleanup-adapter.cjs');"]],
  },
];

// 프로젝트가 작성한 명시 adapter이며 upstream 버전·라이선스 파일을 바꾸지 않는다.
const ADAPTER = String.raw`'use strict';

// temp 0.9.4의 추적 경로 정리 호출만 rimraf 6.1.3의 명시 API로 연결한다.
const path = require('node:path');
const { rimraf, rimrafSync } = require('rimraf');
if (typeof rimraf !== 'function' || typeof rimrafSync !== 'function') {
  throw new Error('ERR_GUKGAM_TEMP_RIMRAF_API');
}

function checkedPath(value) {
  if (typeof value !== 'string' || value.includes('\0') || !path.isAbsolute(value)
      || path.normalize(value) === path.parse(value).root) {
    throw new TypeError('ERR_GUKGAM_TEMP_LITERAL_PATH');
  }
  return value;
}

function cleanupOptions(options) {
  if (!options || typeof options !== 'object' || Array.isArray(options)
      || Object.keys(options).join(',') !== 'maxBusyTries' || options.maxBusyTries !== 6) {
    throw new TypeError('ERR_GUKGAM_TEMP_094_OPTIONS');
  }
  // retryDelay는 native 구현에만 적용된다. Windows JS 구현은 자체 backoff를 쓴다.
  return { maxRetries: 6, retryDelay: 100, glob: false, preserveRoot: true };
}

function completeRemoval(removed) {
  if (removed !== true) {
    const error = new Error('ERR_GUKGAM_TEMP_CLEANUP_INCOMPLETE');
    error.code = 'ERR_GUKGAM_TEMP_CLEANUP_INCOMPLETE';
    return error;
  }
  return null;
}

function cleanupTrackedPath(value, options, callback) {
  if (typeof callback !== 'function') {
    throw new TypeError('ERR_GUKGAM_TEMP_CALLBACK');
  }
  const target = checkedPath(value);
  const removalOptions = cleanupOptions(options);
  // callback 예외를 Promise 거절로 다시 잡아 중복 호출하지 않는다.
  rimraf(target, removalOptions).then(
    (removed) => { queueMicrotask(() => callback(completeRemoval(removed))); },
    (error) => { queueMicrotask(() => callback(error)); },
  );
  return undefined;
}

cleanupTrackedPath.sync = function cleanupTrackedPathSync(value, options) {
  const target = checkedPath(value);
  const removalOptions = cleanupOptions(options);
  const error = completeRemoval(rimrafSync(target, removalOptions));
  if (error) throw error;
  return undefined;
};

module.exports = cleanupTrackedPath;` + '\n';
const ADAPTER_FILE = 'node_modules/temp/lib/gukgam-rimraf6-cleanup-adapter.cjs';

function requireCondition(value, code) {
  if (!value) throw new Error(code);
}
function sha(data) { return crypto.createHash('sha256').update(data).digest('hex'); }
function samePath(left, right) {
  return process.platform === 'win32' ? left.toLowerCase() === right.toLowerCase() : left === right;
}
function unlinked(file) {
  for (let current = path.resolve(file); ; current = path.dirname(current)) {
    try {
      requireCondition(!fs.lstatSync(current).isSymbolicLink(), 'ERR_LINKED_PATCH_PATH');
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    const parent = path.dirname(current);
    if (parent === current) break;
  }
}
function under(root, relative) {
  requireCondition(typeof relative === 'string' && relative && !path.isAbsolute(relative)
    && !relative.includes('\\') && !relative.includes(':') && !relative.split('/').includes('..'), 'ERR_PATCH_RELATIVE_PATH');
  const resolved = path.resolve(root, ...relative.split('/'));
  const inside = path.relative(root, resolved);
  requireCondition(inside && inside !== '..' && !path.isAbsolute(inside)
    && !inside.startsWith('..' + path.sep), 'ERR_PATCH_ESCAPE');
  unlinked(resolved);
  return resolved;
}
function readFile(file) {
  unlinked(file);
  requireCondition(fs.lstatSync(file).isFile(), 'ERR_PATCH_INPUT_NOT_FILE');
  return fs.readFileSync(file);
}
function readPackage(file, name, version) {
  const raw = readFile(file);
  const json = JSON.parse(raw.toString('utf8'));
  requireCondition(json.name === name && json.version === version, 'ERR_PATCH_PACKAGE_VERSION:' + name);
  if (PACKAGE_SHA[name]) requireCondition(sha(raw) === PACKAGE_SHA[name], 'ERR_PATCH_PACKAGE_JSON_SHA:' + name);
  return { file, sha256: sha(raw), name, version };
}
function resolvedPackage(root, fromFile, name, version) {
  const entry = createRequire(fromFile).resolve(name);
  const inside = path.relative(root, entry);
  requireCondition(inside && inside !== '..' && !path.isAbsolute(inside)
    && !inside.startsWith('..' + path.sep), 'ERR_RESOLVED_DEPENDENCY_ESCAPE');
  unlinked(entry);
  for (let folder = path.dirname(entry); folder !== root; folder = path.dirname(folder)) {
    requireCondition(folder !== path.dirname(folder), 'ERR_RESOLVED_PACKAGE_NOT_FOUND');
    const candidate = path.join(folder, 'package.json');
    if (fs.existsSync(candidate)) {
      const raw = readFile(candidate);
      const json = JSON.parse(raw.toString('utf8'));
      if (json.name === name) return readPackage(candidate, name, version);
    }
  }
  throw new Error('ERR_RESOLVED_PACKAGE_NOT_FOUND');
}
function replaceOne(text, oldValue, newValue) {
  requireCondition(text.split(oldValue).length === 2, 'ERR_PATCH_LITERAL_COUNT');
  return text.replace(oldValue, newValue);
}
function planTarget(root, target) {
  const file = under(root, target.file);
  const current = readFile(file);
  const currentHash = sha(current);
  requireCondition(currentHash === target.old || currentHash === target.patched, 'ERR_PATCH_UPSTREAM_SHA:' + target.file);
  let original = current.toString('utf8');
  requireCondition(Buffer.from(original).equals(current), 'ERR_PATCH_UPSTREAM_ENCODING');
  if (currentHash === target.patched) {
    for (const [oldValue, newValue] of [...target.replacements].reverse()) {
      original = replaceOne(original, newValue, oldValue);
    }
  }
  const originalData = Buffer.from(original);
  requireCondition(sha(originalData) === target.old, 'ERR_PATCH_RESTORATION_SHA');
  let patched = original;
  for (const [oldValue, newValue] of target.replacements) patched = replaceOne(patched, oldValue, newValue);
  const desired = Buffer.from(patched);
  requireCondition(sha(desired) === target.patched, 'ERR_PATCH_GENERATION_SHA');
  return { ...target, absolute: file, originalData, desired, alreadyPatched: currentHash === target.patched };
}
function writeNewOrVerify(file, data) {
  unlinked(file);
  if (fs.existsSync(file)) {
    requireCondition(readFile(file).equals(data), 'ERR_PATCH_EVIDENCE_CHANGED');
    return;
  }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  unlinked(file);
  fs.writeFileSync(file, data, { flag: 'wx' });
}
function replaceAtomically(file, data) {
  unlinked(file);
  const temporary = file + '.gukgam-patch.tmp';
  unlinked(temporary);
  requireCondition(!fs.existsSync(temporary), 'ERR_PATCH_TEMP_EXISTS');
  fs.writeFileSync(temporary, data, { flag: 'wx' });
  fs.renameSync(temporary, file);
  requireCondition(readFile(file).equals(data), 'ERR_PATCH_WRITTEN_SHA');
}

function main() {
  const arguments_ = process.argv.slice(2);
  requireCondition(arguments_.length === 0 || (arguments_.length === 2 && arguments_[0] === '--root'), '사용법: node apply-build-compat-patches.cjs [--root <desktopdir>]');
  requireCondition(Number.parseInt(process.versions.node.split('.')[0], 10) >= 24, 'ERR_BUILD_COMPAT_REQUIRES_NODE_24');
  const root = arguments_.length === 0 ? EXPECTED_ROOT : path.resolve(arguments_[1]);
  requireCondition(samePath(root, EXPECTED_ROOT), 'ERR_BUILD_ROOT_MUST_MATCH_SCRIPT_PARENT');
  unlinked(root);
  requireCondition(fs.lstatSync(root).isDirectory() && samePath(fs.realpathSync(root), root), 'ERR_BUILD_ROOT_IDENTITY');
  const packageFiles = {};
  for (const [name, version] of Object.entries(VERSIONS)) {
    packageFiles[name] = readPackage(under(root, 'node_modules/' + name + '/package.json'), name, version);
  }
  const dirEntry = under(root, TARGETS[0].file);
  const tempEntry = under(root, TARGETS[1].file);
  const resolutions = {
    minimatch: resolvedPackage(root, dirEntry, 'minimatch', VERSIONS.minimatch),
    rimraf: resolvedPackage(root, tempEntry, 'rimraf', VERSIONS.rimraf),
  };
  const plans = TARGETS.map((target) => planTarget(root, target));
  const adapterPath = under(root, ADAPTER_FILE);
  const adapterData = Buffer.from(ADAPTER);
  if (fs.existsSync(adapterPath)) requireCondition(readFile(adapterPath).equals(adapterData), 'ERR_PATCH_ADAPTER_DRIFT');
  const evidence = under(root, 'node_modules/.cache/gukgam-build-compat');
  const receiptPath = under(root, 'node_modules/.cache/gukgam-build-compat/applied.json');
  const packageLockFiles = ['package.json', 'package-lock.json'].map((name) => ({ name, sha256: sha(readFile(under(root, name))) }));
  const licenseFiles = [];
  for (const name of ['dir-compare', 'temp']) {
    for (const license of ['LICENSE', 'LICENSE.md']) {
      const relative = 'node_modules/' + name + '/' + license;
      const file = under(root, relative);
      if (fs.existsSync(file)) licenseFiles.push({ file: relative, sha256: sha(readFile(file)) });
    }
  }
  // 앱 버전 등 정상 metadata 변경을 허용하되 이번 실행이 입력을 바꾸지 않았는지 확인한다.
  function verifyUnchangedInputs() {
    const files = [
      ...Object.values(packageFiles).map((item) => ({ file: path.relative(root, item.file).split(path.sep).join('/'), sha256: item.sha256 })),
      ...packageLockFiles.map((item) => ({ file: item.name, sha256: item.sha256 })),
      ...licenseFiles,
    ];
    for (const item of files) {
      requireCondition(sha(readFile(under(root, item.file))) === item.sha256, 'ERR_PATCH_UNRELATED_INPUT_CHANGED');
    }
  }
  const receipt = {
    schema_version: 1, kind: 'normalbuildcompat',
    package_versions: VERSIONS,
    targets: plans.map((item) => ({ file: item.file, original_sha256: item.old, patched_sha256: item.patched })),
    generated_adapter: { file: ADAPTER_FILE, sha256: sha(adapterData) },
    upstream_license_files: licenseFiles,
    application_sources_written: 0, dependency_sources_patched: 2,
  };
  const backups = plans.map((item) => ({
    file: under(root, 'node_modules/.cache/gukgam-build-compat/originals/' + item.file), data: item.originalData,
  }));
  for (const item of backups) {
    if (fs.existsSync(item.file)) requireCondition(readFile(item.file).equals(item.data), 'ERR_PATCH_BACKUP_DRIFT');
  }
  if (fs.existsSync(receiptPath)) {
    requireCondition(plans.every((item) => item.alreadyPatched) && fs.existsSync(adapterPath)
      && backups.every((item) => fs.existsSync(item.file))
      && readFile(receiptPath).equals(Buffer.from(JSON.stringify(receipt, null, 2) + '\n')), 'ERR_EXISTING_PATCH_RECEIPT_MISMATCH');
    verifyUnchangedInputs();
    console.log(JSON.stringify({ ok: true, already_applied: true, dependency_sources_patched_this_run: 0, root_inputs_unchanged: true, evidence: path.relative(root, evidence) }));
    return;
  }
  for (const item of backups) writeNewOrVerify(item.file, item.data);
  writeNewOrVerify(adapterPath, adapterData);
  for (const item of plans) if (!item.alreadyPatched) replaceAtomically(item.absolute, item.desired);
  for (const item of plans) requireCondition(sha(readFile(item.absolute)) === item.patched, 'ERR_PATCH_FINAL_SHA');
  verifyUnchangedInputs();
  writeNewOrVerify(receiptPath, Buffer.from(JSON.stringify(receipt, null, 2) + '\n'));
  console.log(JSON.stringify({ ok: true, already_applied: false, dependency_sources_patched_this_run: plans.filter((item) => !item.alreadyPatched).length, root_inputs_unchanged: true, targets: receipt.targets, adapter_sha256: sha(adapterData), evidence: path.relative(root, evidence) }));
}

try { main(); }
catch (error) { console.error(error && error.message ? error.message : 'ERR_BUILD_COMPAT_PATCH'); process.exitCode = 1; }
