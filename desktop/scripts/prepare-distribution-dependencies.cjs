'use strict';
// 배포 전 고정 의존성·온라인 설치 무결성·선택 다운로드 목록을 확인한다.
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const root = path.resolve(__dirname, '..');
for (const script of [
  path.join(__dirname, 'apply-build-compat-patches.cjs'),
  path.join(root, 'relay-cli', 'scripts', 'apply-relay-compat-patches.cjs'),
  path.join(__dirname, 'apply-web-installer-security.cjs'),
  path.join(__dirname, 'check-runtime-pack-catalog.cjs'),
]) {
  const result = spawnSync(process.execPath, [script], {
    cwd: root, shell: false, windowsHide: true, stdio: 'inherit', timeout: 30000,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
