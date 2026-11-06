"""The page `life ui` (alias `life review`) serves on localhost: review what agents want to change, and explore / fix your graph.
Everything stored or written by an agent is put on the page with textContent only, never as HTML."""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Life graph</title>
<style>
:root{--bg:#fafaf8;--panel:#fff;--ink:#1c1c1a;--mute:#6b6b66;--line:#dcdcd5;--new:#1f8f5f;--newbg:#e3f4ec;--del:#c2402f;--delbg:#fbe8e4;--old:#8a8a82;--oldbg:#f1f1ec;--accent:#2a5bd7}
@media (prefers-color-scheme:dark){:root{--bg:#141412;--panel:#1c1c1a;--ink:#ecece6;--mute:#9a9a92;--line:#34342f;--new:#4fc08d;--newbg:#173226;--del:#ef7a68;--delbg:#3a1d19;--old:#8f8f86;--oldbg:#262622;--accent:#7aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif}
header{padding:0 20px;border-bottom:1px solid var(--line);display:flex;gap:22px;align-items:center;height:52px}
header h1{font-size:17px;margin:0}
.tabs{display:flex;gap:4px;height:100%}.tab{padding:0 14px;display:flex;align-items:center;cursor:pointer;color:var(--mute);border-bottom:2px solid transparent}
.tab.on{color:var(--ink);border-color:var(--accent)}.badge{background:var(--accent);color:#fff;border-radius:99px;font-size:12px;padding:0 7px;margin-left:7px}
main{display:grid;grid-template-columns:310px 1fr;min-height:calc(100vh - 53px)}
@media(max-width:800px){main{grid-template-columns:1fr}}
nav{border-right:1px solid var(--line);padding:12px;overflow:auto;max-height:calc(100vh - 53px)}
nav h2{font-size:12px;color:var(--mute);margin:14px 6px 6px;font-weight:600}
.item{padding:8px 10px;border-radius:8px;cursor:pointer;border:1px solid transparent}
.item:hover{background:var(--oldbg)}.item.sel{background:var(--panel);border-color:var(--line)}
.item b{display:block;font-weight:600}.item small{color:var(--mute)}
section{padding:18px 22px;max-width:1040px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.card h3{margin:0 0 8px;font-size:14px}
ul{margin:0;padding-left:20px}li{margin:4px 0}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px;align-items:center}
button{font:inherit;padding:7px 14px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer}
button.sm{padding:1px 9px;font-size:13px;margin-left:6px}
button.yes{background:var(--new);border-color:var(--new);color:#fff}button.no{color:var(--del);border-color:var(--del)}
button:disabled{opacity:.5;cursor:default}
input{font:inherit;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--ink);width:100%}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line);color:var(--mute);margin-left:6px}
.tag.new{color:var(--new);border-color:var(--new)}.tag.removed{color:var(--del);border-color:var(--del)}
svg{width:100%;height:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px}
svg .nd{cursor:pointer}
.legend{display:flex;gap:16px;color:var(--mute);font-size:13px;margin:8px 2px 0;flex-wrap:wrap}
.empty{color:var(--mute);padding:30px 6px}.mute{color:var(--mute)}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:10px 16px;border-radius:10px;display:none;gap:12px;align-items:center}
#toast button{background:transparent;color:var(--bg);border-color:var(--bg)}
</style></head><body>
<header><h1>Life graph</h1><div class="tabs"><div class="tab" id="t-review">Review<span class="badge" id="badge" style="display:none"></span></div><div class="tab" id="t-explore">Explore</div></div></header>
<main><nav id="nav"></nav><section id="main"></section></main><div id="toast"></div>
<script>
const $=(t,a={},...kids)=>{const e=document.createElement(t);for(const k in a){if(k==='class')e.className=a[k];else if(k.startsWith('on'))e[k]=a[k];else e.setAttribute(k,a[k])}for(const x of kids)e.append(x);return e};
const S=(t,a={},txt)=>{const e=document.createElementNS('http://www.w3.org/2000/svg',t);for(const k in a)e.setAttribute(k,a[k]);if(txt!==undefined)e.textContent=txt;return e};
const api=async(p,body)=>{const r=await fetch('api/'+p,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});return r.json()};
let tab=location.hash.startsWith('#e')?'explore':'review',data={pending:[],recent:[]},sel=null,busy=false,ex={q:'',hits:[],graph:null,cur:null,detail:null};
function setTab(t){tab=t;render()}document.getElementById('t-review').onclick=()=>setTab('review');document.getElementById('t-explore').onclick=()=>setTab('explore');

/* ---------- graph drawing (shared) ---------- */
function layout(g,W,H,iters){const n=g.nodes.map((x,i)=>({...x,x:W/2+Math.cos(i*6.283/g.nodes.length)*W*.3,y:H/2+Math.sin(i*6.283/g.nodes.length)*H*.3,vx:0,vy:0}));
  const ix={};n.forEach(x=>ix[x.id]=x);const L=g.links.filter(l=>ix[l.s]&&ix[l.t]).map(l=>({...l,a:ix[l.s],b:ix[l.t]}));const k=Math.sqrt(W*H/Math.max(n.length,1))*.8;
  for(let it=0;it<iters;it++){const t=1-it/iters;
    for(const a of n)for(const b of n){if(a===b)continue;let dx=a.x-b.x,dy=a.y-b.y,d=Math.hypot(dx,dy)||.1,f=k*k/d;a.vx+=dx/d*f*.02;a.vy+=dy/d*f*.02}
    for(const l of L){let dx=l.b.x-l.a.x,dy=l.b.y-l.a.y,d=Math.hypot(dx,dy)||.1,f=d*d/k*.02;l.a.vx+=dx/d*f;l.a.vy+=dy/d*f;l.b.vx-=dx/d*f;l.b.vy-=dy/d*f}
    for(const a of n){a.vx+=(W/2-a.x)*.01;a.vy+=(H/2-a.y)*.01;a.x+=Math.max(-20,Math.min(20,a.vx))*t;a.y+=Math.max(-20,Math.min(20,a.vy))*t;a.vx*=.6;a.vy*=.6;a.x=Math.max(70,Math.min(W-70,a.x));a.y=Math.max(40,Math.min(H-95,a.y))}}
  return {n,L}}
function draw(g,opt={}){const big=g.nodes.length>60,W=860,H=opt.H||Math.min(620,340+g.nodes.length*(big?3:14)),svg=S('svg',{viewBox:`0 0 ${W} ${H}`});
  if(!g.nodes.length){svg.append(S('text',{x:W/2,y:H/2,'text-anchor':'middle',fill:'var(--mute)'},'Nothing to draw'));return svg}
  const {n,L}=layout(g,W,H,g.nodes.length>150?120:400);const col={new:'var(--new)',removed:'var(--del)',existing:'var(--old)'};
  const rank=[...n].sort((a,b)=>(b.size||0)-(a.size||0)),labelled=new Set(big?rank.slice(0,35).map(x=>x.id):n.map(x=>x.id));
  for(const l of L){const c=col[l.state]||col.existing;svg.append(S('line',{x1:l.a.x,y1:l.a.y,x2:l.b.x,y2:l.b.y,stroke:c,'stroke-width':big?1:2,'stroke-opacity':big?.45:1,'stroke-dasharray':l.state==='removed'?'6 4':'0'}));
    if(!big)svg.append(S('text',{x:(l.a.x+l.b.x)/2,y:(l.a.y+l.b.y)/2-4,'text-anchor':'middle','font-size':11,fill:c},l.rel))}
  for(const x of n){const c=col[x.state]||col.existing,bg=x.state==='new'?'var(--newbg)':x.state==='removed'?'var(--delbg)':'var(--oldbg)',r=big?5+Math.min(10,(x.size||0)):14;
    const g2=S('g',{class:opt.onclick?'nd':''});if(opt.onclick)g2.onclick=()=>opt.onclick(x);
    const shape=x.kind==='event'?S('rect',{x:x.x-9,y:x.y-9,width:18,height:18,rx:3,transform:`rotate(45 ${x.x} ${x.y})`}):S('circle',{cx:x.x,cy:x.y,r});
    shape.setAttribute('fill',opt.hl===x.id?'var(--accent)':bg);shape.setAttribute('stroke',c);shape.setAttribute('stroke-width',2);if(x.state==='removed')shape.setAttribute('stroke-dasharray','5 3');g2.append(shape);
    if(labelled.has(x.id)){const nm=x.name.length>26?x.name.slice(0,25)+'…':x.name;
      const t=S('text',{x:x.x,y:x.y+r+14,'text-anchor':'middle','font-size':big?11:13,fill:'var(--ink)'},nm);if(x.state==='removed')t.setAttribute('text-decoration','line-through');g2.append(t);
      if(!big){g2.append(S('text',{x:x.x,y:x.y+r+28,'text-anchor':'middle','font-size':10,fill:'var(--mute)'},x.kind+(x.state==='new'?' · new':x.state==='removed'?' · deleted':'')));
        let dy=r+42;for(const a of (x.adds||[]).slice(0,2)){g2.append(S('text',{x:x.x,y:x.y+dy,'text-anchor':'middle','font-size':10,fill:'var(--new)'},'+ '+(a.length>34?a.slice(0,33)+'…':a)));dy+=12}
        for(const a of (x.removes||[]).slice(0,2)){g2.append(S('text',{x:x.x,y:x.y+dy,'text-anchor':'middle','font-size':10,fill:'var(--del)'},'− '+(a.length>34?a.slice(0,33)+'…':a)));dy+=12}}}
    svg.append(g2)}
  return svg}
function toast(msg,undoId){const t=document.getElementById('toast');t.replaceChildren(msg);if(undoId)t.append($('button',{onclick:async()=>{await api('decide',{id:undoId,action:'undo'});t.style.display='none';await refresh()}},'Undo'));
  t.style.display='flex';clearTimeout(toast.h);toast.h=setTimeout(()=>t.style.display='none',9000)}

/* ---------- review ---------- */
async function loadReview(){data=await api('changes');const b=document.getElementById('badge');b.textContent=data.pending.length;b.style.display=data.pending.length?'inline':'none';
  if(!sel||![...data.pending,...data.recent].some(c=>c.changeset===sel))sel=(location.hash.startsWith('#c:')?location.hash.slice(1):(data.pending[0]||data.recent[0]||{}).changeset||null)}
async function decide(id,action){if(busy)return;busy=true;render();const j=await api('decide',{id,action});busy=false;if(!j.ok&&j.message)alert(j.message);await refresh()}
function renderReview(nav,main){
  const item=c=>$('div',{class:'item'+(c.changeset===sel?' sel':''),onclick:()=>{sel=c.changeset;location.hash=sel;render()}},$('b',{},c.agent+' · '+c.changeset),$('small',{},(c.status!=='pending'?c.status+' · ':'')+c.when+' · '+(c.summary[0]||'').slice(0,60)));
  nav.append($('h2',{},'WAITING'));if(!data.pending.length)nav.append($('div',{class:'empty'},'Nothing is waiting.'));data.pending.forEach(c=>nav.append(item(c)));
  if(data.pending.length>1)nav.append($('div',{class:'row'},$('button',{class:'yes',onclick:async()=>{for(const c of data.pending)await decide(c.changeset,'approve')}},'Approve all')));
  nav.append($('h2',{},'RECENT'));data.recent.forEach(c=>nav.append(item(c)));
  const c=[...data.pending,...data.recent].find(x=>x.changeset===sel);if(!c){main.append($('div',{class:'empty'},'Pick a change on the left.'));return}
  main.append($('h2',{style:'margin:0 0 4px'},c.agent+' wants to:'),$('div',{class:'mute',style:'margin-bottom:12px'},c.changeset+' · '+(c.source||'')+' · '+c.when+' · '+c.status+(c.note?' · '+c.note:'')));
  const list=$('ul');c.summary.forEach(s=>list.append($('li',{},s)));main.append($('div',{class:'card'},$('h3',{},'What happens'),list));
  const tg=(c.targets||[]);if(tg.length){const u=$('ul');tg.forEach(t=>u.append($('li',{},t.as+' ',$('span',{class:'tag '+(t.status==='new'?'new':'')},t.status==='new'?'new':'existing '+t.ref))));main.append($('div',{class:'card'},$('h3',{},'Where it attaches'),u))}
  main.append(draw(c.graph||{nodes:[],links:[]}));
  main.append($('div',{class:'legend'},...['green = added','red dashed = deleted','grey = already in your graph','diamond = event'].map(t=>$('span',{},t))));
  const row=$('div',{class:'row'});
  if(c.status==='pending'){row.append($('button',{class:'yes',...(busy?{disabled:''}:{}),onclick:()=>decide(c.changeset,'approve')},'Yes, apply'),$('button',{class:'no',...(busy?{disabled:''}:{}),onclick:()=>decide(c.changeset,'reject')},'No, reject'))}
  else if(c.status==='applied'){row.append($('button',{class:'no',...(busy?{disabled:''}:{}),onclick:()=>decide(c.changeset,'undo')},'Undo this'))}
  main.append(row)}

/* ---------- explore ---------- */
async function loadExplore(){if(!ex.graph){const g=await api('graph');ex.graph={nodes:(g.nodes||[]).map(n=>({id:'e:'+n.id,kind:n.kind,name:n.name,state:'existing',size:n.events+n.facts})),links:(g.links||[]).map(l=>({s:'e:'+l.s,t:'e:'+l.t,rel:l.rel,state:'existing'}))}}}
async function doSearch(q){ex.q=q;ex.hits=q.trim()?await api('search?q='+encodeURIComponent(q)):[];render()}
async function open_(ref){ex.cur=ref;ex.detail=await api('get?ref='+encodeURIComponent(ref));render()}
async function apply(ops,msg){const r=await api('apply',{ops});if(r.status==='applied'||r.status==='unchanged'){toast(msg||'Done.',r.changeset);ex.graph=null;await loadExplore();if(ex.cur&&!(r.status==='applied'&&ops.some(o=>o.op==='forget'&&o.ref===ex.cur))&&!ops.some(o=>o.op==='merge')){ex.detail=await api('get?ref='+encodeURIComponent(ex.cur))}else{ex.cur=null;ex.detail=null}render()}
  else alert((r.errors||[r.message||'failed']).join('\n'))}
function renderExplore(nav,main){
  const inp=$('input',{placeholder:'Search by words or meaning…',value:ex.q});inp.onkeydown=e=>{if(e.key==='Enter')doSearch(inp.value)};nav.append(inp);
  if(ex.q.trim()){nav.append($('h2',{},ex.hits.length?'RESULTS':'NO RESULTS'));ex.hits.forEach(h=>nav.append($('div',{class:'item'+(h.ref===ex.cur?' sel':''),onclick:()=>open_(h.ref.startsWith('e:')?h.ref:h.ref)},$('b',{},h.name,h.found_by?$('span',{class:'tag'},h.found_by==='meaning'?'by meaning':h.found_by):''),$('small',{},h.kind+(h.when?' · '+h.when:'')))))}
  else{nav.append($('h2',{},'MOST CONNECTED'));[...(ex.graph?.nodes||[])].sort((a,b)=>b.size-a.size).slice(0,40).forEach(n=>nav.append($('div',{class:'item'+(n.id===ex.cur?' sel':''),onclick:()=>open_(n.id)},$('b',{},n.name),$('small',{},n.kind+' · '+n.size+' items'))))}
  const d=ex.detail;
  if(!d||d.error||!ex.cur){main.append($('div',{class:'card'},$('h3',{},ex.graph?.nodes.length?'Your graph ('+ex.graph.nodes.length+' things)':'Your graph is empty'),$('div',{class:'mute'},'Click a node or search to open it. Anything you change here can be undone.')),draw(ex.graph||{nodes:[],links:[]},{onclick:n=>open_(n.id),H:560}));return}
  if(ex.cur.startsWith('v:')){main.append($('h2',{style:'margin:0 0 4px'},d.title),$('div',{class:'mute'},d.when+' · '+d.kind+' · '+ex.cur),$('div',{class:'card'},d.body||'(no details)'),
    $('div',{class:'row'},$('button',{class:'no',onclick:()=>{if(confirm('Delete this event?'))apply([{op:'forget',ref:ex.cur}],'Event deleted.')}},'Delete event')));return}
  main.append($('h2',{style:'margin:0 0 4px'},d.name,$('span',{class:'tag'},d.kind),$('span',{class:'tag'},d.ref)),$('div',{class:'mute'},(d.aliases.length?'also known as '+d.aliases.join(', ')+' · ':'')+'first seen '+d.first_seen));
  const facts=$('ul');d.facts.forEach(f=>facts.append($('li',{},f.text,$('button',{class:'sm',onclick:()=>{const t=prompt('Change this fact to:',f.text);if(t&&t!==f.text)apply([{op:'edit_fact',ref:f.ref,text:t}],'Fact changed.')}},'edit'),$('button',{class:'sm no',onclick:()=>apply([{op:'forget',ref:f.ref}],'Fact deleted.')},'delete'))));
  if(!d.facts.length)facts.append($('li',{class:'mute'},'no facts yet'));
  const add=$('input',{placeholder:'Add a fact…'});add.onkeydown=e=>{if(e.key==='Enter'&&add.value.trim())apply([{op:'fact',subject:ex.cur,text:add.value.trim()}],'Fact added.')};
  main.append($('div',{class:'card'},$('h3',{},'Facts'),facts,$('div',{style:'margin-top:10px'},add)));
  const rel=$('ul');d.relations.forEach(r=>rel.append($('li',{},(r.direction==='out'?'→ ':'← ')+r.rel+' '+r.with,$('button',{class:'sm no',onclick:()=>apply([{op:'forget',ref:r.ref}],'Link deleted.')},'delete'))));
  if(!d.relations.length)rel.append($('li',{class:'mute'},'no links'));main.append($('div',{class:'card'},$('h3',{},'Links'),rel));
  const ev=$('ul');d.events.forEach(v=>ev.append($('li',{},v.when+' · '+v.title+' ',$('button',{class:'sm',onclick:()=>open_(v.ref)},'open'),$('button',{class:'sm no',onclick:()=>{if(confirm('Delete this event?'))apply([{op:'forget',ref:v.ref}],'Event deleted.')}},'delete'))));
  if(!d.events.length)ev.append($('li',{class:'mute'},'no events'));main.append($('div',{class:'card'},$('h3',{},'Events'),ev));
  const nb=[{id:d.ref,kind:d.kind,name:d.name,state:'existing',size:9}],lk=[];d.relations.forEach((r,i)=>{const id='n'+i;nb.push({id,kind:r.with.split(':')[0],name:r.with.split(':').slice(1).join(':'),state:'existing',size:1});lk.push(r.direction==='out'?{s:d.ref,t:id,rel:r.rel,state:'existing'}:{s:id,t:d.ref,rel:r.rel,state:'existing'})});
  d.events.slice(0,6).forEach((v,i)=>{const id='v'+i;nb.push({id,kind:'event',name:v.title,state:'existing',size:1,ref:v.ref});lk.push({s:id,t:d.ref,rel:'involves',state:'existing'})});
  main.append(draw({nodes:nb,links:lk},{H:380,hl:d.ref,onclick:n=>{if(n.ref)open_(n.ref)}}));
  main.append($('div',{class:'row'},
    $('button',{onclick:()=>{const to=prompt('Merge this into which entity? (e.g. person:Sam). Everything attached moves there.');if(to&&confirm('Merge '+d.name+' into '+to+'? Merges can\'t be undone.'))apply([{op:'merge',from:ex.cur,to}],'Merged.')}},'Merge into…'),
    $('button',{class:'no',onclick:()=>{if(confirm('Delete '+d.name+' with all its facts and links? (Events stay.)'))apply([{op:'forget',ref:ex.cur}],d.name+' deleted.')}},'Delete '+d.name)))}

/* ---------- shell ---------- */
function render(){const nav=document.getElementById('nav'),main=document.getElementById('main');nav.replaceChildren();main.replaceChildren();
  document.getElementById('t-review').className='tab'+(tab==='review'?' on':'');document.getElementById('t-explore').className='tab'+(tab==='explore'?' on':'');
  document.title=data.pending.length?'('+data.pending.length+') Life graph':'Life graph';
  tab==='review'?renderReview(nav,main):renderExplore(nav,main)}
async function refresh(){await loadReview();if(tab==='explore')await loadExplore();render()}
refresh();setInterval(()=>{if(!busy&&tab==='review')loadReview().then(render)},4000);
</script></body></html>
"""
