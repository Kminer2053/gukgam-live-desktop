// WebCodecs AVC/AAC 출력만 묶는다. 네트워크·외부 패키지·미디어 실행물을 사용하지 않는다.
(function(global){
 const concat=parts=>{const result=new Uint8Array(parts.reduce((n,p)=>n+p.length,0));let offset=0;for(const p of parts){result.set(p,offset);offset+=p.length;}return result;};
 const str=value=>new TextEncoder().encode(value),u8=(...v)=>new Uint8Array(v),u16=value=>u8(value>>>8,value),u24=value=>u8(value>>>16,value>>>8,value),u32=value=>u8(value>>>24,value>>>16,value>>>8,value),zeros=n=>new Uint8Array(n);
 const box=(name,...parts)=>{const body=concat(parts);return concat([u32(body.length+8),str(name),body]);},full=(name,version,flags,...parts)=>box(name,u8(version),u24(flags),...parts);
 const matrix=()=>concat([u32(0x10000),u32(0),u32(0),u32(0),u32(0x10000),u32(0),u32(0),u32(0),u32(0x40000000)]);
 function descriptor(tag,data){let size=data.length,encoded=[size&127];while((size>>>=7)>0)encoded.unshift((size&127)|128);return concat([u8(tag,...encoded),data]);}
 function sampleDescription(track){
  if(track.kind==='video'){
   const compressor=new Uint8Array(32),label=str('WebCodecs AVC');compressor[0]=label.length;compressor.set(label,1);
   return box('avc1',zeros(6),u16(1),zeros(16),u16(track.width),u16(track.height),u32(0x480000),u32(0x480000),u32(0),u16(1),compressor,u16(24),u16(0xffff),box('avcC',track.description));
  }
  const decoder=descriptor(4,concat([u8(0x40,0x15),u24(0),u32(192000),u32(192000),descriptor(5,track.description)]));
  const esds=full('esds',0,0,descriptor(3,concat([u16(track.id),u8(0),decoder,descriptor(6,u8(2))])));
  return box('mp4a',zeros(6),u16(1),zeros(8),u16(track.channels),u16(16),u16(0),u16(0),u32(track.timescale*65536),esds);
 }
 function timing(samples){const entries=[];for(const sample of samples){const last=entries.at(-1);if(last&&last.delta===sample.duration)last.count++;else entries.push({count:1,delta:sample.duration});}return full('stts',0,0,u32(entries.length),...entries.map(x=>concat([u32(x.count),u32(x.delta)])));}
 function trackBox(track,offset,movieDuration){
  const duration=track.samples.reduce((n,s)=>n+s.duration,0),video=track.kind==='video';
  const tkhd=full('tkhd',0,7,u32(0),u32(0),u32(track.id),u32(0),u32(movieDuration),zeros(8),u16(0),u16(0),u16(video?0:0x100),u16(0),matrix(),u32((track.width||0)*65536),u32((track.height||0)*65536));
  const mdhd=full('mdhd',0,0,u32(0),u32(0),u32(track.timescale),u32(duration),u16(0x55c4),u16(0));
  const hdlr=full('hdlr',0,0,u32(0),str(video?'vide':'soun'),zeros(12),str(video?'VideoHandler\0':'SoundHandler\0'));
  const stsd=full('stsd',0,0,u32(1),sampleDescription(track));
  const stsc=full('stsc',0,0,u32(1),u32(1),u32(track.samples.length),u32(1));
  const stsz=full('stsz',0,0,u32(0),u32(track.samples.length),...track.samples.map(s=>u32(s.data.length)));
  const stco=full('stco',0,0,u32(1),u32(offset));
  const sync=video?full('stss',0,0,u32(track.samples.filter(x=>x.key).length),...track.samples.flatMap((x,i)=>x.key?[u32(i+1)]:[])):new Uint8Array();
  const stbl=box('stbl',stsd,timing(track.samples),stsc,stsz,stco,sync);
  const dinf=box('dinf',full('dref',0,0,u32(1),full('url ',0,1)));
  const minf=box('minf',video?full('vmhd',0,1,u16(0),zeros(6)):full('smhd',0,0,u16(0),u16(0)),dinf,stbl);
  const trim=Math.max(0,Math.round(-(track.samples[0].timestamp||0)*track.timescale/1e6));
  const edit=(!video||trim)?box('edts',full('elst',0,0,u32(1),u32(movieDuration),u32(trim),u16(1),u16(0))):new Uint8Array();
  return box('trak',tkhd,edit,box('mdia',mdhd,hdlr,minf));
 }
 global.muxCinemaMp4=function({video,audio,seconds}){
  if(!video.description?.length||!audio.description?.length||!video.samples.length||!audio.samples.length)throw Error('Missing AVC/AAC configuration or samples.');
  for(const track of [video,audio])for(let i=1;i<track.samples.length;i++)if(track.samples[i].timestamp<track.samples[i-1].timestamp)throw Error('Reordered samples are unsupported.');
  const firstAudio=audio.samples[0].timestamp,trim=Math.max(0,Math.round(-firstAudio*audio.timescale/1e6));if(firstAudio>2)throw Error('AAC starts after the movie timeline.');
  for(let i=0;i<audio.samples.length;i++){const sample=audio.samples[i],expected=firstAudio+i*1024*1e6/audio.timescale;if(!sample.data.length||sample.duration!==1024||Math.abs(sample.timestamp-expected)>2)throw Error('AAC sample gap, duplicate, or invalid duration.');}
  if(audio.samples.length*1024<Math.round(seconds*audio.timescale)+trim)throw Error('AAC output does not cover the movie.');
  video={...video,id:1,kind:'video'};audio={...audio,id:2,kind:'audio'};
  const movieDuration=Math.round(seconds*1000),ftyp=box('ftyp',str('isom'),u32(0x200),str('isomiso2avc1mp41'));
  const mvhd=full('mvhd',0,0,u32(0),u32(0),u32(1000),u32(movieDuration),u32(0x10000),u16(0x100),zeros(10),matrix(),zeros(24),u32(3));
  const makeMoov=(videoOffset,audioOffset)=>box('moov',mvhd,trackBox(video,videoOffset,movieDuration),trackBox(audio,audioOffset,movieDuration));
  const videoData=concat(video.samples.map(s=>s.data)),audioData=concat(audio.samples.map(s=>s.data)),provisional=makeMoov(0,0),videoOffset=ftyp.length+provisional.length+8,moov=makeMoov(videoOffset,videoOffset+videoData.length);
  if(moov.length!==provisional.length)throw Error('Unstable MP4 offsets.');
  return concat([ftyp,moov,box('mdat',videoData,audioData)]);
 };
})(globalThis);
