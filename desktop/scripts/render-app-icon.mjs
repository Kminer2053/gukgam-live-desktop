// 앱 아이콘만 생성한다. 홍보영상·매뉴얼·시연 자료에는 쓰지 않는다.
import {readFile,writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {dirname,resolve} from 'node:path';
import {createHash} from 'node:crypto';
const args=process.argv.slice(2);
if(args.length!==0&&(args.length!==2||args[0]!=='--node-modules'))throw Error('사용법: node scripts/render-app-icon.mjs [--node-modules 기존_설치_경로]');
const require=createRequire(import.meta.url);
const sharp=args.length?require(resolve(args[1],'sharp')):require('sharp');
const brand=resolve(dirname(fileURLToPath(import.meta.url)),'../assets/brand');
const source=await readFile(resolve(brand,'gukgam-live.svg'));
const sizes=[16,24,32,48,64,128,256],images=[];
const outputs=new Map([['gukgam-live.svg',source]]);
outputs.set('gukgam-live-1024.png',await sharp(source).resize(1024,1024).png().toBuffer());
for(const size of sizes){const png=await sharp(source).resize(size,size).png().toBuffer();images.push(png);outputs.set(`gukgam-live-${size}.png`,png);}
const header=Buffer.alloc(6+16*sizes.length);header.writeUInt16LE(1,2);header.writeUInt16LE(sizes.length,4);
let offset=header.length;
sizes.forEach((size,index)=>{const at=6+16*index;header[at]=size===256?0:size;header[at+1]=size===256?0:size;header.writeUInt16LE(1,at+4);header.writeUInt16LE(32,at+6);header.writeUInt32LE(images[index].length,at+8);header.writeUInt32LE(offset,at+12);offset+=images[index].length;});
outputs.set('gukgam-live.ico',Buffer.concat([header,...images]));
const sheet=Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800"><rect width="1280" height="800" fill="#F1F5FB"/><rect y="450" width="1280" height="350" fill="#101F35"/><g font-family="Malgun Gothic,sans-serif"><text x="40" y="54" font-size="29" font-weight="700" fill="#102B56">국감 라이브 데스크톱 · 앱 아이콘</text><text x="40" y="93" font-size="17" fill="#4E6380">회의장 돔 + 자막 기록 / Windows 7개 해상도 / 투명 모서리</text><text x="40" y="488" font-size="19" fill="#CBDDEE">어두운 바탕에서도 선명한 윤곽</text></g></svg>');
const layers=[];let x=40;
for(let i=0;i<sizes.length;i++){const size=sizes[i];layers.push({input:images[i],left:x,top:128},{input:images[i],left:x,top:510});const label=Buffer.from(`<svg xmlns="http://www.w3.org/2000/svg" width="140" height="32"><text x="0" y="23" font-family="Arial" font-size="17" fill="#3F5773">${size}px</text></svg>`);layers.push({input:label,left:x,top:397});x+=size+36;}
outputs.set('icon-size-review.png',await sharp(sheet).composite(layers).png().toBuffer());
for(const [name,data] of outputs)if(name!=='gukgam-live.svg')await writeFile(resolve(brand,name),data);
const manifest={design:'hearing-dome-and-captions',ico_entries:sizes.map(size=>({width:size,height:size,bits:32})),files:Object.fromEntries([...outputs].sort(([a],[b])=>a.localeCompare(b)).map(([name,data])=>[name,{bytes:data.length,sha256:createHash('sha256').update(data).digest('hex')}]))};
await writeFile(resolve(brand,'asset-manifest.json'),JSON.stringify(manifest,null,2)+'\n');
console.log(JSON.stringify({brand,files:outputs.size+1,ico_entries:sizes,network:false}));
