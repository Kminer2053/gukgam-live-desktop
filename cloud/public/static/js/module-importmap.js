// 뒤따르는 모듈보다 먼저 고정된 같은 출처의 경로를 연결한다.
(() => {
  'use strict';
  const bootstrap = document.currentScript;
  const version = encodeURIComponent(bootstrap.dataset.version || '');
  const paths = [
    '/static/js/common.js',
    '/static/js/closed-block-view.js',
    '/static/js/report-view.js',
    '/static/js/audio-capture.js',
  ];
  const imports = Object.fromEntries(paths.map((path) => [path, `${path}?v=${version}`]));
  const importMap = document.createElement('script');
  importMap.type = 'importmap';
  importMap.textContent = JSON.stringify({imports});
  bootstrap.after(importMap);
})();
