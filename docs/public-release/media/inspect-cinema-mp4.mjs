import {createHash} from 'node:crypto';
const sha=b=>createHash('sha256').update(b).digest('hex');
function boxes(bytes,start=0,end=bytes.length){const result=[];for(let p=start;p<end;){if(p+8>end)throw Error('Truncated MP4 box');const size=bytes.readUInt32BE(p);if(size<8||p+size>end)throw Error('Invalid MP4 box size');result.push({type:bytes.toString('ascii',p+4,p+8),start:p,data:p+8,end:p+size});p+=size;}return result;}
function one(bytes,parent,type){const found=boxes(bytes,parent.data,parent.end).find(x=>x.type===type);if(!found)throw Error('Missing MP4 '+type);return found;}
export function inspectCinemaMp4(bytes){
 const top=boxes(bytes),moov=top.find(x=>x.type==='moov'),mdat=top.find(x=>x.type==='mdat');if(!moov||!mdat||moov.start>mdat.start)throw Error('Fast-start MP4 required');
 const mvhd=one(bytes,moov,'mvhd'),movieTimescale=bytes.readUInt32BE(mvhd.data+12),movieDuration=bytes.readUInt32BE(mvhd.data+16),tracks=[];
 for(const trak of boxes(bytes,moov.data,moov.end).filter(x=>x.type==='trak')){
  const tkhd=one(bytes,trak,'tkhd'),mdia=one(bytes,trak,'mdia'),mdhd=one(bytes,mdia,'mdhd'),hdlr=one(bytes,mdia,'hdlr'),minf=one(bytes,mdia,'minf'),stbl=one(bytes,minf,'stbl'),stts=one(bytes,stbl,'stts'),stsz=one(bytes,stbl,'stsz'),stco=one(bytes,stbl,'stco'),stsc=one(bytes,stbl,'stsc');
  const kind=bytes.toString('ascii',hdlr.data+8,hdlr.data+12),timescale=bytes.readUInt32BE(mdhd.data+12),durationTicks=bytes.readUInt32BE(mdhd.data+16),entries=bytes.readUInt32BE(stts.data+4),deltas=[];let sampleCount=0,totalTicks=0;
  for(let i=0;i<entries;i++){const count=bytes.readUInt32BE(stts.data+8+i*8),delta=bytes.readUInt32BE(stts.data+12+i*8);if(!count||!delta)throw Error('Invalid sample timing');sampleCount+=count;totalTicks+=count*delta;deltas.push({count,delta});}
  if(totalTicks!==durationTicks||bytes.readUInt32BE(stsz.data+4)!==0||bytes.readUInt32BE(stsz.data+8)!==sampleCount||bytes.readUInt32BE(stco.data+4)!==1||bytes.readUInt32BE(stsc.data+4)!==1||bytes.readUInt32BE(stsc.data+12)!==sampleCount)throw Error('Inconsistent MP4 sample tables');
  const offset=bytes.readUInt32BE(stco.data+8);let length=0;for(let i=0;i<sampleCount;i++){const size=bytes.readUInt32BE(stsz.data+12+i*4);if(!size)throw Error('Empty encoded sample');length+=size;}if(offset<mdat.data||offset+length>mdat.end)throw Error('MP4 sample data outside mdat');
  const edts=boxes(bytes,trak.data,trak.end).find(x=>x.type==='edts');let edit=null;if(edts){const elst=one(bytes,edts,'elst');if(bytes.readUInt32BE(elst.data+4)!==1)throw Error('Unexpected edit count');edit={movie_duration_ticks:bytes.readUInt32BE(elst.data+8),media_start_ticks:bytes.readInt32BE(elst.data+12)};if(edit.movie_duration_ticks!==movieDuration||edit.media_start_ticks<0)throw Error('Invalid edit interval');}
  tracks.push({kind,timescale,duration_ticks:durationTicks,sample_count:sampleCount,deltas,edit,width:bytes.readUInt32BE(tkhd.end-8)/65536,height:bytes.readUInt32BE(tkhd.end-4)/65536,encoded_bytes:length,encoded_sha256:sha(bytes.subarray(offset,offset+length))});
 }
 if(tracks.length!==2||!tracks.some(x=>x.kind==='vide')||!tracks.some(x=>x.kind==='soun'))throw Error('Expected one video and one audio track');
 return{movie_seconds:movieDuration/movieTimescale,fast_start:true,tracks};
}

// 이 렌더러의 완성본에서 AAC를 추출한다. 재구성 때 음성을 다시 인코딩하지 않는다.
export function extractCinemaAudio(bytes){
 const structure=inspectCinemaMp4(bytes),audio=structure.tracks.find(x=>x.kind==='soun'),video=structure.tracks.find(x=>x.kind==='vide');
 if(audio.timescale!==48000||audio.deltas.some(x=>x.delta!==1024))throw Error('Use the completed introduction AAC track.');
 const moov=boxes(bytes).find(x=>x.type==='moov'),trak=boxes(bytes,moov.data,moov.end).filter(x=>x.type==='trak').find(x=>{const mdia=one(bytes,x,'mdia'),hdlr=one(bytes,mdia,'hdlr');return bytes.toString('ascii',hdlr.data+8,hdlr.data+12)==='soun';});
 const mdia=one(bytes,trak,'mdia'),stbl=one(bytes,one(bytes,mdia,'minf'),'stbl'),stsd=one(bytes,stbl,'stsd'),entry=boxes(bytes,stsd.data+8,stsd.end)[0];
 if(entry.type!=='mp4a'||bytes.readUInt16BE(entry.data+16)!==2)throw Error('Stereo AAC sample entry required.');
 const esds=boxes(bytes,entry.data+28,entry.end).find(x=>x.type==='esds');if(!esds)throw Error('Missing AAC decoder configuration.');
 function descriptor(offset,end){const tag=bytes[offset++];let length=0,count=0,b;do{if(offset>=end||count++>=4)throw Error('Invalid ES descriptor');b=bytes[offset++];length=length*128+(b&127);}while(b&128);if(offset+length>end)throw Error('Truncated ES descriptor');return{tag,data:offset,end:offset+length};}
 const es=descriptor(esds.data+4,esds.end);if(es.tag!==3||bytes[es.data+2]!==0)throw Error('Unsupported ES flags');const decoder=descriptor(es.data+3,es.end);if(decoder.tag!==4||bytes[decoder.data]!==0x40)throw Error('AAC decoder descriptor required');const specific=descriptor(decoder.data+13,decoder.end);if(specific.tag!==5||specific.end<=specific.data)throw Error('Missing AAC AudioSpecificConfig');
 const stsz=one(bytes,stbl,'stsz'),stco=one(bytes,stbl,'stco'),trim=audio.edit?.media_start_ticks??0,samples=[];let offset=bytes.readUInt32BE(stco.data+8);
 for(let i=0;i<audio.sample_count;i++){const size=bytes.readUInt32BE(stsz.data+12+i*4);samples.push({data:bytes.subarray(offset,offset+size).toString('base64'),timestamp:Math.round((i*1024-trim)*1e6/48000),duration:1024,key:true});offset+=size;}
 return{channels:2,timescale:48000,description:bytes.subarray(specific.data,specific.end).toString('base64'),samples,video_seconds:video.duration_ticks/video.timescale,encoded_sha256:audio.encoded_sha256};
}
