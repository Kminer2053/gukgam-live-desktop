import {build} from 'esbuild';
import {mkdir, copyFile} from 'node:fs/promises';
await mkdir('dist/renderer', {recursive: true});
await build({entryPoints:['src/main/index.ts'], outfile:'dist/main/index.cjs', bundle:true, platform:'node', target:'node22', external:['electron'], format:'cjs'});
await build({entryPoints:['src/preload/index.ts'], outfile:'dist/preload/index.cjs', bundle:true, platform:'node', target:'node22', external:['electron'], format:'cjs'});
await build({entryPoints:['src/renderer/index.ts'], outfile:'dist/renderer/app.js', bundle:true, platform:'browser', target:'chrome130', format:'iife'});
await Promise.all(['index.html','style.css'].map(name=>copyFile(`src/renderer/${name}`, `dist/renderer/${name}`)));
await mkdir('dist/renderer/brand', {recursive: true});
await copyFile('assets/brand/gukgam-live.svg','dist/renderer/brand/gukgam-live.svg');
