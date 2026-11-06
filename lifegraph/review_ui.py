"""The page `life review` serves on localhost: what agents want to change, drawn as a graph diff, with Yes / No.
Everything an agent wrote is put on the page with textContent only, never as HTML."""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Life graph: review changes</title>
<style>
:root{--bg:#fafaf8;--panel:#fff;--ink:#1c1c1a;--mute:#6b6b66;--line:#dcdcd5;--new:#1f8f5f;--newbg:#e3f4ec;--del:#c2402f;--delbg:#fbe8e4;--old:#8a8a82;--oldbg:#f1f1ec;--accent:#2a5bd7}
@media (prefers-color-scheme:dark){:root{--bg:#141412;--panel:#1c1c1a;--ink:#ecece6;--mute:#9a9a92;--line:#34342f;--new:#4fc08d;--newbg:#173226;--del:#ef7a68;--delbg:#3a1d19;--old:#8f8f86;--oldbg:#262622;--accent:#7aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:baseline}
header h1{font-size:17px;margin:0}header span{color:var(--mute)}
main{display:grid;grid-template-columns:300px 1fr;min-height:calc(100vh - 54px)}
@media(max-width:800px){main{grid-template-columns:1fr}}
nav{border-right:1px solid var(--line);padding:12px;overflow:auto}
nav h2{font-size:12px;letter-spacing:.04em;text-transform:none;color:var(--mute);margin:14px 6px 6px;font-weight:600}
.item{padding:9px 10px;border-radius:8px;cursor:pointer;border:1px solid transparent}
.item:hover{background:var(--oldbg)}.item.sel{background:var(--panel);border-color:var(--line)}
.item b{display:block;font-weight:600}.item small{color:var(--mute)}
section{padding:18px 22px;max-width:1000px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.card h3{margin:0 0 8px;font-size:14px}
ul{margin:0;padding-left:20px}li{margin:3px 0}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
button{font:inherit;padding:8px 16px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer}
button.yes{background:var(--new);border-color:var(--new);color:#fff}button.no{color:var(--del);border-color:var(--del)}
button:disabled{opacity:.5;cursor:default}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line);color:var(--mute)}
.tag.new{color:var(--new);border-color:var(--new)}.tag.removed{color:var(--del);border-color:var(--del)}
svg{width:100%;height:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px}
.legend{display:flex;gap:16px;color:var(--mute);font-size:13px;margin:8px 2px 0;flex-wrap:wrap}
.empty{color:var(--mute);padding:30px 6px}.err{color:var(--del)}
</style></head><body>
<header><h1>Review changes</h1><span id="sub"></span></header>
<main><nav id="nav"></nav><section id="main"></section></main>
<script>
const $=(t,a={},...kids)=>{const e=document.createElement(t);for(const k in a){if(k==='class')e.className=a[k];else if(k.startsWith('on'))e[k]=a[k];else e.setAttribute(k,a[k])}for(const x of kids)e.append(x);return e};
const S=(t,a={},txt)=>{const e=document.createElementNS('http://www.w3.org/2000/svg',t);for(const k in a)e.setAttribute(k,a[k]);if(txt!==undefined)e.textContent=txt;return e};
let data={pending:[],recent:[]},sel=null,busy=false;
async function load(){const r=await fetch('api/changes');data=await r.json();
  if(!sel||![...data.pending,...data.recent].some(c=>c.changeset===sel))sel=(location.hash.slice(1)||(data.pending[0]||data.recent[0]||{}).changeset||null);render()}
function layout(g,W,H){const n=g.nodes.map((x,i)=>({...x,x:W/2+Math.cos(i*6.283/g.nodes.length)*W*.3,y:H/2+Math.sin(i*6.283/g.nodes.length)*H*.3,vx:0,vy:0}));
  const ix={};n.forEach(x=>ix[x.id]=x);const L=g.links.filter(l=>ix[l.s]&&ix[l.t]).map(l=>({...l,a:ix[l.s],b:ix[l.t]}));const k=Math.sqrt(W*H/Math.max(n.length,1))*.8;
  for(let it=0;it<400;it++){const t=1-it/400;
    for(const a of n)for(const b of n){if(a===b)continue;let dx=a.x-b.x,dy=a.y-b.y,d=Math.hypot(dx,dy)||.1,f=k*k/d;a.vx+=dx/d*f*.02;a.vy+=dy/d*f*.02}
    for(const l of L){let dx=l.b.x-l.a.x,dy=l.b.y-l.a.y,d=Math.hypot(dx,dy)||.1,f=d*d/k*.02;l.a.vx+=dx/d*f;l.a.vy+=dy/d*f;l.b.vx-=dx/d*f;l.b.vy-=dy/d*f}
    for(const a of n){a.vx+=(W/2-a.x)*.01;a.vy+=(H/2-a.y)*.01;a.x+=Math.max(-20,Math.min(20,a.vx))*t;a.y+=Math.max(-20,Math.min(20,a.vy))*t;a.vx*=.6;a.vy*=.6;a.x=Math.max(70,Math.min(W-70,a.x));a.y=Math.max(40,Math.min(H-95,a.y))}}
  return {n,L}}
function draw(g){const W=860,H=Math.min(620,340+g.nodes.length*14),svg=S('svg',{viewBox:`0 0 ${W} ${H}`});
  if(!g.nodes.length){svg.append(S('text',{x:W/2,y:H/2,'text-anchor':'middle',fill:'var(--mute)'},'Nothing to draw'));return svg}
  const {n,L}=layout(g,W,H);const col={new:'var(--new)',removed:'var(--del)',existing:'var(--old)'};
  for(const l of L){const c=col[l.state]||col.existing;svg.append(S('line',{x1:l.a.x,y1:l.a.y,x2:l.b.x,y2:l.b.y,stroke:c,'stroke-width':2,'stroke-dasharray':l.state==='removed'?'6 4':'0'}));
    svg.append(S('text',{x:(l.a.x+l.b.x)/2,y:(l.a.y+l.b.y)/2-4,'text-anchor':'middle','font-size':11,fill:c},l.rel))}
  for(const x of n){const c=col[x.state]||col.existing,bg=x.state==='new'?'var(--newbg)':x.state==='removed'?'var(--delbg)':'var(--oldbg)';
    const shape=x.kind==='event'?S('rect',{x:x.x-9,y:x.y-9,width:18,height:18,rx:3,transform:`rotate(45 ${x.x} ${x.y})`}):S('circle',{cx:x.x,cy:x.y,r:14});
    shape.setAttribute('fill',bg);shape.setAttribute('stroke',c);shape.setAttribute('stroke-width',2);if(x.state==='removed')shape.setAttribute('stroke-dasharray','5 3');svg.append(shape);
    const nm=x.name.length>26?x.name.slice(0,25)+'…':x.name;
    const t=S('text',{x:x.x,y:x.y+30,'text-anchor':'middle','font-size':13,fill:'var(--ink)'},nm);if(x.state==='removed')t.setAttribute('text-decoration','line-through');svg.append(t);
    svg.append(S('text',{x:x.x,y:x.y+44,'text-anchor':'middle','font-size':10,fill:'var(--mute)'},x.kind+(x.state==='new'?' · new':x.state==='removed'?' · deleted':'')));
    let dy=58;for(const a of (x.adds||[]).slice(0,2)){svg.append(S('text',{x:x.x,y:x.y+dy,'text-anchor':'middle','font-size':10,fill:'var(--new)'},'+ '+(a.length>34?a.slice(0,33)+'…':a)));dy+=12}
    for(const a of (x.removes||[]).slice(0,2)){svg.append(S('text',{x:x.x,y:x.y+dy,'text-anchor':'middle','font-size':10,fill:'var(--del)'},'− '+(a.length>34?a.slice(0,33)+'…':a)));dy+=12}}
  return svg}
async function decide(id,action){if(busy)return;busy=true;render();
  const r=await fetch('api/decide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,action})});const j=await r.json();busy=false;
  if(!j.ok)alert(j.message||'failed');await load()}
function render(){const nav=document.getElementById('nav'),main=document.getElementById('main');nav.replaceChildren();main.replaceChildren();
  document.getElementById('sub').textContent=data.pending.length?data.pending.length+' waiting for you':'nothing waiting';
  const item=c=>$('div',{class:'item'+(c.changeset===sel?' sel':''),onclick:()=>{sel=c.changeset;location.hash=sel;render()}},$('b',{},c.agent+' · '+c.changeset),$('small',{},(c.status!=='pending'?c.status+' · ':'')+c.when+' · '+(c.summary[0]||'').slice(0,60)));
  nav.append($('h2',{},'WAITING'));if(!data.pending.length)nav.append($('div',{class:'empty'},'Nothing is waiting.'));data.pending.forEach(c=>nav.append(item(c)));
  if(data.pending.length>1)nav.append($('div',{class:'row'},$('button',{class:'yes',onclick:async()=>{for(const c of data.pending)await decide(c.changeset,'approve')}},'Approve all')));
  nav.append($('h2',{},'RECENT'));data.recent.forEach(c=>nav.append(item(c)));
  const c=[...data.pending,...data.recent].find(x=>x.changeset===sel);if(!c){main.append($('div',{class:'empty'},'Pick a change on the left.'));return}
  main.append($('h2',{style:'margin:0 0 4px'},c.agent+' wants to:'),$('div',{style:'color:var(--mute);margin-bottom:12px'},c.changeset+' · '+c.when+' · '+c.status+(c.note?' · '+c.note:'')));
  const list=$('ul');c.summary.forEach(s=>list.append($('li',{},s)));main.append($('div',{class:'card'},$('h3',{},'What happens'),list));
  const tg=(c.targets||[]);if(tg.length){const u=$('ul');tg.forEach(t=>u.append($('li',{},t.as+' ',$('span',{class:'tag '+(t.status==='new'?'new':'')},t.status==='new'?'new':'existing '+t.ref))));
    main.append($('div',{class:'card'},$('h3',{},'Where it attaches'),u))}
  main.append(draw(c.graph||{nodes:[],links:[]}));
  main.append($('div',{class:'legend'},...['green = added','red dashed = deleted','grey = already in your graph','diamond = event'].map(t=>$('span',{},t))));
  const row=$('div',{class:'row'});
  if(c.status==='pending'){row.append($('button',{class:'yes',...(busy?{disabled:''}:{}),onclick:()=>decide(c.changeset,'approve')},'Yes, apply'),$('button',{class:'no',...(busy?{disabled:''}:{}),onclick:()=>decide(c.changeset,'reject')},'No, reject'))}
  else if(c.status==='applied'){row.append($('button',{class:'no',...(busy?{disabled:''}:{}),onclick:()=>decide(c.changeset,'undo')},'Undo this'))}
  main.append(row)}
load();setInterval(load,4000);
</script></body></html>
"""
