// 설치된 Codex sharp 런타임만 사용한다. 운영 자료·계정·네트워크에 접근하지 않는다.
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {dirname,resolve} from 'node:path';
const require=createRequire(import.meta.url);
// 신뢰할 수 있는 기존 Node 의존성만 사용한다. 자동 설치·다운로드는 하지 않는다.
const cli=process.argv.slice(2),moduleAt=cli.indexOf('--node-modules');
let nodeModules=null;
if(moduleAt>=0){
  const value=cli[moduleAt+1];
  if(!value||value.startsWith('--'))throw Error('--node-modules requires a directory');
  nodeModules=resolve(value);cli.splice(moduleAt,2);
}
const dependency=name=>require(nodeModules===null?name:resolve(nodeModules,name));

const sharp=dependency('sharp');
const media=dirname(fileURLToPath(import.meta.url));
const brand=resolve(media,'../../../desktop/assets/brand');
await mkdir(brand,{recursive:true});
const svg=await readFile(resolve(brand,'gukgam-live.svg'));
await sharp(svg).resize(1024,1024).png().toFile(resolve(brand,'gukgam-live-1024.png'));
const sizes=[16,24,32,48,64,128,256],images=[];
for(const size of sizes){const image=await sharp(svg).resize(size,size).png().toBuffer();images.push(image);await writeFile(resolve(brand,`gukgam-live-${size}.png`),image);}
const header=Buffer.alloc(6+16*sizes.length);header.writeUInt16LE(1,2);header.writeUInt16LE(sizes.length,4);
let offset=header.length;
sizes.forEach((size,index)=>{const at=6+16*index;header[at]=size===256?0:size;header[at+1]=size===256?0:size;header.writeUInt16LE(1,at+4);header.writeUInt16LE(32,at+6);header.writeUInt32LE(images[index].length,at+8);header.writeUInt32LE(offset,at+12);offset+=images[index].length;});
await writeFile(resolve(brand,'gukgam-live.ico'),Buffer.concat([header,...images]));
const preview=`<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="420"><rect width="1200" height="420" fill="#F1F5FB"/><text x="36" y="48" font-family="Malgun Gothic" font-size="24" fill="#0A1831">국감 라이브 · 아이콘 크기 검수</text></svg>`;
const layers=[];let x=40;
for(let index=0;index<sizes.length;index++){const size=sizes[index];layers.push({input:images[index],left:x,top:92});const label=Buffer.from(`<svg xmlns="http://www.w3.org/2000/svg" width="160" height="45"><text x="0" y="30" font-family="Arial" font-size="20" fill="#102B56">${size}px</text></svg>`);layers.push({input:label,left:x,top:360});x+=size+36;}
await sharp(Buffer.from(preview)).composite(layers).png().toFile(resolve(brand,'icon-size-review.png'));
const iconB64=(await readFile(resolve(brand,'gukgam-live-256.png'))).toString('base64');
const poster=`<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1920" viewBox="0 0 1080 1920">
<defs><linearGradient id="bg" x2="1" y2="1"><stop stop-color="#071329"/><stop offset="1" stop-color="#12396D"/></linearGradient><linearGradient id="glass" x2="1" y2="1"><stop stop-color="#183C69"/><stop offset="1" stop-color="#10243F"/></linearGradient><radialGradient id="glow"><stop stop-color="#297DCD" stop-opacity=".55"/><stop offset="1" stop-color="#12396D" stop-opacity="0"/></radialGradient></defs>
<rect width="1080" height="1920" fill="url(#bg)"/><circle cx="940" cy="500" r="640" fill="url(#glow)"/>
<g font-family="Malgun Gothic, sans-serif"><image href="data:image/png;base64,${iconB64}" x="86" y="112" width="86" height="86"/><text x="196" y="171" fill="#FFF" font-size="44" font-weight="700">국감 라이브</text><rect x="84" y="246" width="452" height="60" rx="30" fill="#173B61"/><text x="110" y="287" font-size="28" font-weight="700" fill="#70E8D9">WINDOWS · 국감 모니터링</text>
<text x="80" y="450" font-size="106" font-weight="700" fill="#FFF">코딩을 몰라도,</text><text x="80" y="588" font-size="106" font-weight="700" fill="#FFF">내 ChatGPT</text><text x="80" y="726" font-size="106" font-weight="700" fill="#64E4D3">계정으로.</text>
<text x="86" y="814" font-size="34" fill="#B1C7DE">설정부터 기록·검토·보고까지 한 흐름으로</text>
<rect x="76" y="900" width="928" height="622" rx="38" fill="url(#glass)" stroke="#42648B" stroke-width="2"/>
<circle cx="116" cy="946" r="7" fill="#54E4CE"/><text x="140" y="958" font-size="28" fill="#D8EDFC">회의 기록</text><rect x="828" y="926" width="142" height="46" rx="23" fill="#204E65"/><text x="848" y="958" font-size="23" fill="#83EEDC">가상 데모</text>
<rect x="112" y="1006" width="850" height="140" rx="22" fill="#0C203A"/><text x="140" y="1050" font-size="26" fill="#68DACA">질의 의원</text><text x="140" y="1104" font-size="33" fill="#FFF">자료 제출 계획을 설명해 주세요.</text>
<rect x="112" y="1170" width="850" height="140" rx="22" fill="#163F63"/><text x="140" y="1214" font-size="26" fill="#94C9FC">답변 기관</text><text x="140" y="1268" font-size="33" fill="#FFF">검토 후 제출 일정을 안내하겠습니다.</text>
<rect x="112" y="1334" width="850" height="148" rx="22" fill="#20495F"/><text x="140" y="1378" font-size="26" fill="#7AEDD9">AI 요약 초안 · 원문 확인 필요</text><text x="140" y="1436" font-size="31" fill="#FFF">제출 일정과 추가 자료 요청을 검토합니다.</text>
<text x="84" y="1623" font-size="42" font-weight="700" fill="#FFF">자막 수집 → 문답 정리 → 요약 → 내보내기</text><text x="84" y="1690" font-size="30" fill="#A7C2DC">내 ChatGPT 계정 · 로컬 모델·API 연결 선택</text>
<line x1="84" y1="1742" x2="996" y2="1742" stroke="#3A5879"/><text x="84" y="1798" font-size="26" fill="#AFC3D8">비공식 업무 지원 앱 · 실제 운영 자료를 사용하지 않았습니다.</text><text x="84" y="1846" font-size="26" fill="#AFC3D8">AI 결과에는 오류가 있을 수 있어 원문 검토가 필요합니다.</text></g></svg>`;
await writeFile(resolve(media,'poster.svg'),poster);
await sharp(Buffer.from(poster)).png().toFile(resolve(media,'poster.png'));
console.log(JSON.stringify({brand,media,icon_sizes:sizes,poster:[1080,1920],network:false}));
