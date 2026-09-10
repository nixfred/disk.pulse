const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const ctx={Qt:{rgba:(r,g,b,a)=>[r,g,b,a]}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('Model.js','utf8').replace('.pragma library',''),ctx);

const root={mount:'/',fstype:'btrfs',device:'nvme0n1',total:500*2**30,free:160*2**30,used:340*2**30,freePct:32,usedPct:68};
const boot={mount:'/boot',fstype:'vfat',device:'nvme0n1',total:2*2**30,free:1.5*2**30,used:0.5*2**30,freePct:75,usedPct:25};
const drive={name:'nvme0n1',temp:63.4,tempMax:84.85,rates:{util:12}};
const m={warm:true,filesystems:[root,boot],disks:[drive],rates:{read:37.2e6,write:554e3},psi:{some:{avg10:0.6}}};

// Readouts: one decimal, explicit units, the strip form for throughput.
assert.equal(ctx.readout(m,0),'32.0%');assert.equal(ctx.readout(m,1),'68.0%');
assert.equal(ctx.readout(m,2),'160.0 GiB');assert.equal(ctx.readout(m,3),'340.0 GiB');
assert.equal(ctx.readout(m,4),'R 37.2M');assert.equal(ctx.modeTag(m,4),'W 554K');
assert.equal(ctx.readout(m,5),'63°C');assert.equal(ctx.modeTag(m,5),'DRIVE');
assert.equal(ctx.modeTag(m,0),'FREE');assert.equal(ctx.modeTag(m,1),'USED');
assert.equal(ctx.readout({},0),'—');assert.equal(ctx.readout({warm:true,filesystems:[]},0),'—');
// The chip follows the mount point it is told to, and falls back to root.
assert.equal(ctx.readout(m,0,'/boot'),'75.0%');
assert.equal(ctx.primaryOf(m,'/nowhere').mount,'/');
assert.equal(ctx.driveOf(m,root).name,'nvme0n1');
assert.equal(ctx.driveOf({disks:[{name:'a',rates:{util:3}},{name:'b',rates:{util:9}}]},{device:''}).name,'b');

// The ramp: dark red with nothing left, yellow at half, green with room.
for(const [p,c] of [[0,[133,13,41]],[50,[239,204,69]],[100,[67,242,161]]]){
  const result=ctx.ramp(p);c.forEach((v,i)=>assert.equal(Math.round(result[i]*255),v));
}

// Units: capacity is binary like df, throughput and drive totals are decimal.
assert.equal(ctx.size(1048576),'1.0 MiB');assert.equal(ctx.size(1.5*2**40),'1.50 TiB');assert.equal(ctx.size(2048),'2 KiB');
assert.deepEqual({...ctx.heroAmount(160*2**30)},{value:'160.0',unit:'GiB'});
assert.deepEqual({...ctx.heroAmount(2*2**40)},{value:'2.00',unit:'TiB'});
assert.equal(ctx.rate(1e6),'1.0 MB/s');assert.equal(ctx.rate(2.5e9),'2.50 GB/s');assert.equal(ctx.rate(0),'0 B/s');
assert.equal(ctx.dec(8937012736000),'8.94 TB');assert.equal(ctx.dec(1.5e6),'1.5 MB');
// The strip holds every rate to five characters; 999.9K rolls up, never "1000K".
assert.equal(ctx.tight(999950),'1.0M');assert.equal(ctx.tight(99.96e6),'100M');assert.equal(ctx.tight(254200),'254K');assert.equal(ctx.tight(12),'12B');
assert.equal(ctx.shortRate(5e7),'50M');assert.equal(ctx.shortRate(2.5e6),'2.5M');
assert.equal(ctx.temp(null),'—');assert.equal(ctx.temp(63.4),'63°C');
assert.equal(ctx.hours(3170),'132 days');assert.equal(ctx.hours(20),'20 h');assert.equal(ctx.hours(24*365*2),'2.0 years');assert.equal(ctx.hours(undefined),'—');
assert.equal(ctx.ms(0.51),'0.5 ms');assert.equal(ctx.ms(12.4),'12 ms');assert.equal(ctx.ms(null),'—');
assert.equal(ctx.pct(null),'—');assert.equal(ctx.whole(8.6),'9%');
assert.equal(ctx.perSec(917.2),'917/s');assert.equal(ctx.count(12977),'13.0k');

// Health: fullness and stalling outrank a warm drive.
assert.equal(ctx.healthLabel(m,root,drive,true),'WAITING FOR TELEMETRY');
assert.equal(ctx.healthLabel(m,root,drive,false),'ROOM TO GROW');
assert.equal(ctx.healthLabel(m,{...root,freePct:12},drive,false),'LOW HEADROOM');
assert.equal(ctx.healthLabel(m,{...root,freePct:3},drive,false),'DISK IS NEARLY FULL');
assert.equal(ctx.healthLabel({...m,psi:{some:{avg10:15}}},root,drive,false),'STORAGE IS STALLING');
assert.equal(ctx.healthLabel(m,root,{...drive,temp:85},false),'DRIVE IS RUNNING HOT');
assert.equal(ctx.healthLabel(m,root,{...drive,rates:{util:90}},false),'DRIVE IS SATURATED');

// Width reservations: every mode whose text changes each sample reserves the
// widest string it can produce; the amount modes reserve nothing.
assert.equal(ctx.widestReadout(0),'100.0%');assert.equal(ctx.widestReadout(4),'R 99.9M');assert.equal(ctx.widestReadout(5),'99°C');
assert.equal(ctx.widestReadout(2),'');assert.equal(ctx.widestTag(3),'');assert.equal(ctx.widestTag(4),'W 99.9M');
for(let mode=0;mode<6;mode++) assert.ok(ctx.modeName(mode).length>0);
assert.equal(ctx.modeName(9),'% free');

// The graph axis never magnifies a quiet drive into noise.
assert.equal(ctx.niceMax(0),1e5);assert.equal(ctx.niceMax(37e6),5e7);assert.equal(ctx.niceMax(9e8),2e9);

for(const [xdg,expected] of [[undefined,'/home/test/.local/state/disk-pulse'],['','/home/test/.local/state/disk-pulse'],
    ['relative','/home/test/.local/state/disk-pulse'],['./relative','/home/test/.local/state/disk-pulse'],
    ['/state','/state/disk-pulse']]){
  assert.equal(ctx.stateDir('/home/test',xdg),expected);
}
const panel=fs.readFileSync('Panel.qml','utf8');
const expression=panel.match(/readonly property string stateDir:\s*([^\n]+)/)[1];
for(const [xdg,expected] of [['','/home/test/.local/state/disk-pulse'],['relative','/home/test/.local/state/disk-pulse'],
    ['/state','/state/disk-pulse']]){
  assert.equal(vm.runInNewContext(expression,{Model:ctx,Quickshell:{env:n=>n==='HOME'?'/home/test':xdg}}),expected);
}
// The picker offers exactly the modes the readout knows, and the keys match.
assert.ok(panel.includes('model:6'),'the picker must list six modes');
assert.ok(panel.includes('event.key<=Qt.Key_6'),'keys 1-6 must pick a mode');
assert.ok(panel.includes("Model.clamp(setting('displayMode',0),0,5)"),'the mode setting must clamp to six modes');
console.log('Readout formats, mount following, units, health labels, width reservations, axis ceilings and state-directory agreement pass.');
