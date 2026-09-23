(() => {
'use strict';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const escAttr = value => esc(value).replace(/\n/g, '&#10;');
const n = value => Number(value || 0);
const fmt = value => {
  const v=n(value), a=Math.abs(v);
  if(a>=1e9)return (v/1e9).toFixed(a>=1e10?1:2)+'B';
  if(a>=1e6)return (v/1e6).toFixed(a>=1e7?1:2)+'M';
  if(a>=1e3)return (v/1e3).toFixed(a>=1e4?1:2)+'K';
  return Math.round(v).toLocaleString();
};
const fmtExact = value => n(value).toLocaleString(undefined,{maximumFractionDigits:0});
const fmtCredits = value => n(value).toLocaleString(undefined,{minimumFractionDigits:n(value)<10?2:1,maximumFractionDigits:n(value)<10?2:1});
const fmtUsd = value => n(value).toLocaleString('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:2});
const pct = value => `${n(value).toFixed(1)}%`;
const fmtMs = value => {const v=n(value);if(!v)return'—';if(v>=60000)return`${(v/60000).toFixed(1)}m`;if(v>=1000)return`${(v/1000).toFixed(1)}s`;return`${Math.round(v)}ms`;};
const axisDate = value => {const [y,m,d]=String(value||'').split('-').map(Number), mons=['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];return d&&m?`${String(d).padStart(2,'0')}-${mons[m-1]}`:String(value||'');};
const fmtDate = (value,time=false) => {if(!value)return'—';const d=new Date(value);if(Number.isNaN(d.getTime()))return String(value).slice(0,time?16:10);return new Intl.DateTimeFormat('en-AU',time?{day:'2-digit',month:'short',year:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}:{day:'2-digit',month:'short',year:'2-digit'}).format(d);};
const uniq = values => [...new Set(values.filter(v=>v!==''&&v!=null))];
const hash = s => {let h=2166136261;for(const c of String(s||'')){h^=c.charCodeAt(0);h=Math.imul(h,16777619)}return h>>>0;};

const MODEL_COLORS={
  'gpt-5.6-luna':'#0876db','gpt-5.6-sol':'#ee8726','gpt-5.6-terra':'#37ae69',
  'codex-auto-review':'#e85d9e','gpt-6-astra':'#7c5ce5','gpt-6-sol':'#3c8fd6',
  'gpt-6-luna':'#4d9a9a','gpt-5.5':'#159a8c','gpt-5.4':'#c07d12','gpt-5.3-codex':'#d64f5f'
};
const COLOR_POOL=['#0876db','#ee8726','#37ae69','#e85d9e','#7c5ce5','#159a8c','#c07d12','#d64f5f','#3c8fd6','#8e6c4c','#4d9a9a','#a661c2'];
const AGENT_COLORS={'Main':'#5f6b78','executor':'#0876db','guardian_review':'#7c5ce5','reviewer':'#e85d9e','planner':'#ee8726','researcher':'#37ae69','security_review':'#d64f5f'};
const modelColor = name => MODEL_COLORS[String(name||'Unknown').toLowerCase()] || COLOR_POOL[hash('model:'+name)%COLOR_POOL.length];
const agentColor = name => AGENT_COLORS[String(name||'Subagent')] || COLOR_POOL[hash('agent:'+name)%COLOR_POOL.length];
const effortColor = name => ({medium:'#0876db',high:'#7c5ce5',low:'#37ae69'}[String(name||'').toLowerCase()] || COLOR_POOL[hash('effort:'+name)%COLOR_POOL.length]);

let meta=null;
let dashboard=null;
let activeQuickRange=0;
let dashboardController=null;
let dashboardLoaded=false;
let refreshPoll=null;
let refreshMode='days';
let sortState={key:'total',dir:-1};

function showLoading(title='Loading Codex usage…',sub='Reading local analytics database'){
  $('loadingTitle').textContent=title;$('loadingSub').textContent=sub;$('loadingOverlay').classList.add('show');
}
function hideLoading(){$('loadingOverlay').classList.remove('show');}
function setOptions(id,values,label){
  $(id).innerHTML=`<option value="">${esc(label)}</option>`+values.map(v=>`<option value="${escAttr(v)}">${esc(v)}</option>`).join('');
}
function shiftIsoDate(iso,days){if(!iso)return'';const [y,m,d]=iso.split('-').map(Number),dt=new Date(Date.UTC(y,m-1,d+days));return dt.toISOString().slice(0,10);}
function clampDate(value){if(!value)return value;if(meta?.data_min&&value<meta.data_min)return meta.data_min;if(meta?.data_max&&value>meta.data_max)return meta.data_max;return value;}
function filters(){
  return {
    from:$('fromDate').value,to:$('toDate').value,model:$('modelFilter').value,
    agent:$('agentFilter').value,effort:$('effortFilter').value,project:$('projectFilter').value
  };
}
function queryString(obj){const q=new URLSearchParams();Object.entries(obj).forEach(([k,v])=>{if(v)q.set(k,v)});return q.toString();}
async function api(url,options={}){
  const res=await fetch(url,{cache:'no-store',...options});
  const data=await res.json().catch(()=>({error:`HTTP ${res.status}`}));
  if(!res.ok)throw new Error(data.error||data.message||`HTTP ${res.status}`);
  return data;
}

function quickRangeStart(days){
  if(!meta?.data_max)return'';
  const wanted=shiftIsoDate(meta.data_max,-(days-1));
  return meta.data_min&&wanted<meta.data_min?meta.data_min:wanted;
}
function updateQuickButtons(){document.querySelectorAll('.quick-range').forEach(btn=>btn.classList.toggle('active',Number(btn.dataset.days||0)===activeQuickRange));}
function updateRangeStatus(){
  const f=filters(), bits=[];
  if(meta?.data_min&&meta?.data_max)bits.push(`Available ${axisDate(meta.data_min)} → ${axisDate(meta.data_max)}`);
  if(f.from&&f.to)bits.push(`Selected ${axisDate(f.from)} → ${axisDate(f.to)}`);
  if(dashboard?.summary)bits.push(`${fmtExact(dashboard.summary.responses)} responses`);
  $('rangeStatus').textContent=bits.join(' · ');
}

function initFilters(preserve=false){
  const previous=preserve?filters():null;
  setOptions('modelFilter',meta?.models||[],'All models');
  setOptions('agentFilter',meta?.agents||[],'All roles');
  setOptions('effortFilter',meta?.efforts||[],'All efforts');
  setOptions('projectFilter',meta?.projects||[],'All projects');
  const from=$('fromDate'),to=$('toDate');
  from.min=meta?.data_min||'';from.max=meta?.data_max||'';to.min=meta?.data_min||'';to.max=meta?.data_max||'';
  if(previous){
    from.value=clampDate(previous.from||meta?.data_min||'');to.value=clampDate(previous.to||meta?.data_max||'');
    $('modelFilter').value=(meta.models||[]).includes(previous.model)?previous.model:'';
    $('agentFilter').value=(meta.agents||[]).includes(previous.agent)?previous.agent:'';
    $('effortFilter').value=(meta.efforts||[]).includes(previous.effort)?previous.effort:'';
    $('projectFilter').value=(meta.projects||[]).includes(previous.project)?previous.project:'';
  }else{
    from.value=meta?.data_min||'';to.value=meta?.data_max||'';
  }
  updateQuickButtons();updateRangeStatus();
}

async function loadMeta(preserve=false){
  meta=await api('/api/meta');
  initFilters(preserve);
  $('refreshFrom').value=filters().from||meta?.data_min||'';
  $('refreshTo').value=filters().to||meta?.data_max||'';
  if(meta?.refresh?.status==='running')startRefreshPolling(meta.refresh);
}

async function loadDashboard(showOverlay=true){
  if(!meta?.ready){
    dashboard=null;
    $('content').innerHTML=`<div class="panel empty"><b>No analytics database yet.</b><br><br>Use <b>Refresh data</b> to build it from your mounted .codex history.</div>`;
    $('sourceNote').textContent='No local analytics data loaded';
    $('updatedNote').textContent='';
    updateRangeStatus();hideLoading();return;
  }
  if(dashboardController)dashboardController.abort();
  dashboardController=new AbortController();
  if(showOverlay)showLoading(dashboardLoaded?'Updating dashboard…':'Loading Codex usage…','Querying the local SQLite database');
  try{
    const res=await fetch('/api/dashboard?'+queryString(filters()),{cache:'no-store',signal:dashboardController.signal});
    const data=await res.json();
    if(!res.ok)throw new Error(data.error||`HTTP ${res.status}`);
    dashboard=data;dashboardLoaded=true;render();updateRangeStatus();
  }catch(err){
    if(err.name==='AbortError')return;
    $('content').innerHTML=`<div class="panel error-panel"><b>Could not load dashboard data.</b><br><br>${esc(err.message)}</div>`;
  }finally{if(showOverlay)hideLoading();}
}

function kpi(label,value,sub,color='var(--blue)',warn=false){return`<div class="card ${warn?'warn':''}" style="--card-accent:${color}"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="sub">${esc(sub||'')}</div></div>`;}
function mini(label,value,sub=''){return`<div class="mini"><div class="m-label">${esc(label)}</div><div class="m-value">${esc(value)}</div><div class="m-sub">${esc(sub)}</div></div>`;}
function tipJson(title,items){return escAttr(JSON.stringify({title,items}));}
function legend(names,colorFn){return`<div class="legend">${names.map(name=>`<span class="legend-item"><span class="dot" style="background:${colorFn(name)}"></span>${esc(name)}</span>`).join('')}</div>`;}
function hbars(entries,total,colorFn,valueFmt=fmt,maxRows=12){
  if(!entries.length)return'<div class="empty">No data for this selection.</div>';
  const shown=entries.slice(0,maxRows),mx=Math.max(...shown.map(x=>n(x.total_tokens??x.value)),1);
  return`<div class="hbars">${shown.map(x=>{const name=x.name||'Unknown',v=n(x.total_tokens??x.value),share=total?pct(v/total*100):'',color=colorFn(name);return`<div class="hrow" data-tip-json="${tipJson(name,[{label:'Value',value:valueFmt(v),color},{label:'Share',value:share,color}])}"><div class="hname" title="${esc(name)}">${esc(name)}</div><div class="track"><div class="fill" style="--fill:${color};width:${Math.max(1,v/mx*100)}%"></div></div><div class="hval">${valueFmt(v)}${share?` · ${share}`:''}</div></div>`}).join('')}</div>`;
}

function stackedDaily(data,valueKey='total_tokens',mode='tokens'){
  if(!data.length)return'<div class="empty">No data for this selection.</div>';
  const dates=uniq(data.map(x=>x.date)).sort(), models=uniq(data.map(x=>x.model));
  const by=new Map();data.forEach(r=>{const key=r.date+'|'+r.model;by.set(key,n(r[valueKey]));});
  const dateTotals=new Map();dates.forEach(d=>dateTotals.set(d,data.filter(x=>x.date===d).reduce((a,x)=>a+n(x[valueKey]),0)));
  const W=920,H=255,L=58,R=14,T=16,B=38,pw=W-L-R,ph=H-T-B,max=Math.max(...dateTotals.values(),1),step=pw/dates.length,bw=Math.min(55,step*.66);
  const y=v=>T+ph-v/max*ph;let svg=`<svg viewBox="0 0 ${W} ${H}">`;
  for(let i=0;i<=4;i++){const v=max*i/4,yy=y(v);svg+=`<line class="gridline" x1="${L}" x2="${W-R}" y1="${yy}" y2="${yy}"/><text class="axis-label" x="${L-7}" y="${yy+3}" text-anchor="end">${mode==='usd'?fmtUsd(v):(mode==='credits'?fmtCredits(v):fmt(v))}</text>`;}
  dates.forEach((d,i)=>{
    const x=L+i*step+(step-bw)/2;let acc=0;
    models.forEach(m=>{const v=by.get(d+'|'+m)||0;if(!v)return;const h=v/max*ph,yy=T+ph-(acc+v)/max*ph;svg+=`<rect x="${x}" y="${yy}" width="${bw}" height="${Math.max(h,.7)}" fill="${modelColor(m)}"/>`;acc+=v;});
    const rows=data.filter(x=>x.date===d),total=rows.reduce((a,x)=>a+n(x[valueKey]),0);
    const items=mode==='tokens'?
      [{label:'Total',value:fmtExact(total),color:'#5f6b78'},{label:'Cached input',value:fmtExact(rows.reduce((a,x)=>a+n(x.cached_input_tokens),0)),color:'#159a8c'},{label:'Fresh input',value:fmtExact(rows.reduce((a,x)=>a+n(x.fresh_input_tokens),0)),color:'#ee8726'},{label:'Output',value:fmtExact(rows.reduce((a,x)=>a+n(x.output_tokens),0)),color:'#e85d9e'},...rows.map(x=>({label:x.model,value:fmtExact(x[valueKey]),color:modelColor(x.model)}))]:
      [{label:mode==='usd'?'API-equivalent cost':'Estimated credits',value:mode==='usd'?fmtUsd(total):fmtCredits(total),color:'var(--green)'},...rows.map(x=>({label:x.model,value:mode==='usd'?fmtUsd(x[valueKey]):fmtCredits(x[valueKey]),color:modelColor(x.model)}))];
    svg+=`<rect x="${L+i*step}" y="${T}" width="${step}" height="${ph}" fill="transparent" data-tip-json="${tipJson(axisDate(d),items)}"/><text class="axis-label" x="${L+i*step+step/2}" y="${H-11}" text-anchor="middle">${axisDate(d)}</text>`;
  });
  svg+='</svg>';return svg+legend(models,modelColor);
}

function lineChart(data,nameKey,valueKey,colorFn){
  if(!data.length)return'<div class="empty">No data for this selection.</div>';
  const dates=uniq(data.map(x=>x.date)).sort(), names=uniq(data.map(x=>x[nameKey]||'Unknown'));
  const top=names.map(name=>[name,data.filter(x=>(x[nameKey]||'Unknown')===name).reduce((a,x)=>a+n(x[valueKey]),0)]).sort((a,b)=>b[1]-a[1]).slice(0,6).map(x=>x[0]);
  const shown=data.filter(x=>top.includes(x[nameKey]||'Unknown')), by=new Map();shown.forEach(x=>by.set(x.date+'|'+(x[nameKey]||'Unknown'),n(x[valueKey])));
  const max=Math.max(...shown.map(x=>n(x[valueKey])),1),W=920,H=245,L=58,R=14,T=16,B=38,pw=W-L-R,ph=H-T-B;
  const x=(d,i)=>dates.length===1?L+pw/2:L+i*pw/(dates.length-1),y=v=>T+ph-v/max*ph;let svg=`<svg viewBox="0 0 ${W} ${H}">`;
  for(let i=0;i<=4;i++){const v=max*i/4,yy=y(v);svg+=`<line class="gridline" x1="${L}" x2="${W-R}" y1="${yy}" y2="${yy}"/><text class="axis-label" x="${L-7}" y="${yy+3}" text-anchor="end">${fmt(v)}</text>`;}
  top.forEach(name=>{const pts=dates.map((d,i)=>`${x(d,i)},${y(by.get(d+'|'+name)||0)}`).join(' ');svg+=`<polyline points="${pts}" fill="none" stroke="${colorFn(name)}" stroke-width="2" vector-effect="non-scaling-stroke"/>`;});
  dates.forEach((d,i)=>{const left=dates.length===1?L:L+(i===0?0:(i-.5)*pw/(dates.length-1)),right=dates.length===1?W-R:L+(i===dates.length-1?dates.length-1:(i+.5))*pw/(dates.length-1),items=top.map(name=>({label:name,value:fmtExact(by.get(d+'|'+name)||0),color:colorFn(name)}));svg+=`<rect x="${left}" y="${T}" width="${Math.max(1,right-left)}" height="${ph}" fill="transparent" data-tip-json="${tipJson(axisDate(d),items)}"/>`;if(dates.length<=12||i===0||i===dates.length-1||i%Math.ceil(dates.length/10)===0)svg+=`<text class="axis-label" x="${x(d,i)}" y="${H-10}" text-anchor="middle">${axisDate(d)}</text>`;});
  svg+='</svg>';return svg+legend(top,colorFn);
}

function simpleBars(data,valueKey='value',color='var(--blue)',label='Value'){
  if(!data.length)return'<div class="empty">No data for this selection.</div>';
  const W=920,H=245,L=58,R=14,T=16,B=38,pw=W-L-R,ph=H-T-B,max=Math.max(...data.map(x=>n(x[valueKey])),1),step=pw/data.length,bw=Math.min(55,step*.66);
  let svg=`<svg viewBox="0 0 ${W} ${H}">`;
  for(let i=0;i<=4;i++){const v=max*i/4,yy=T+ph-v/max*ph;svg+=`<line class="gridline" x1="${L}" x2="${W-R}" y1="${yy}" y2="${yy}"/><text class="axis-label" x="${L-7}" y="${yy+3}" text-anchor="end">${fmt(v)}</text>`;}
  data.forEach((row,i)=>{const v=n(row[valueKey]),h=v/max*ph,x=L+i*step+(step-bw)/2,y=T+ph-h;svg+=`<rect x="${x}" y="${y}" width="${bw}" height="${Math.max(h,.7)}" rx="4" fill="${color}" data-tip-json="${tipJson(axisDate(row.date),[{label,value:fmtExact(v),color}])}"/><text class="axis-label" x="${L+i*step+step/2}" y="${H-10}" text-anchor="middle">${axisDate(row.date)}</text>`;});return svg+'</svg>';
}

function modelPill(model){return`<span class="model-pill" style="--pill-color:${modelColor(model)}">${esc(model||'Unknown')}</span>`;}
function agentPills(items){
  if(!items?.length)return'<div class="note">No configured agents were found in the mounted .codex/agents folder.</div>';
  return`<div class="agent-pills">${items.map(x=>`<span class="agent-pill ${x.status==='used_selected'?'used':x.status==='used_historical'?'range':'never'}">${esc(x.name)}</span>`).join('')}</div><div class="agent-summary"><span><b>Green</b> used in selection</span><span><b>Amber</b> used historically</span><span><b>Pink</b> not observed</span></div>`;
}

function sessionTable(sessions){
  const val=(x,key)=>({session:x.name,start:x.start,latest:x.latest,model:x.top_model,agents:x.agents,turns:x.turns,responses:x.responses,compactions:x.compactions,credits:x.credits,creditCoverage:x.credit_coverage,apiCost:x.api_cost,apiCoverage:x.api_coverage,fresh:x.fresh,cached:x.cached,output:x.output,total:x.total,duration:x.duration,maxContext:x.max_context,failures:x.failures})[key];
  const data=[...(sessions||[])].sort((a,b)=>{let x=val(a,sortState.key),y=val(b,sortState.key);if(typeof x==='string'||typeof y==='string')return sortState.dir*String(x||'').localeCompare(String(y||''));return sortState.dir*(n(x)-n(y));});
  if(!data.length)return'<div class="empty">No sessions for this selection.</div>';
  const cols=[['session','Session',''],['start','Start',''],['latest','Latest update',''],['model','Top model',''],['agents','Agents','num'],['turns','Turns','num'],['responses','Responses','num'],['compactions','Compacts','num'],['credits','Est. credits','num'],['creditCoverage','Credit cov.','num'],['apiCost','API cost','num'],['apiCoverage','API cov.','num'],['fresh','Fresh input','num'],['cached','Cached input','num'],['output','Output','num'],['total','Total','num'],['duration','Turn time','num'],['maxContext','Max ctx','num'],['failures','Failed','num']];
  const head=cols.map(([k,l,c])=>`<th class="sortable ${c}" data-sort="${k}">${l}<span class="sort-arrow">${sortState.key===k?(sortState.dir<0?'▼':'▲'):''}</span></th>`).join('');
  const body=data.map(x=>`<tr><td title="${esc(x.name)}">${esc(String(x.name||'').length>62?String(x.name).slice(0,59)+'…':x.name)}</td><td>${fmtDate(x.start,true)}</td><td>${fmtDate(x.latest,true)}</td><td>${modelPill(x.top_model)}</td><td class="num">${fmtExact(x.agents)}</td><td class="num">${fmtExact(x.turns)}</td><td class="num">${fmtExact(x.responses)}</td><td class="num">${fmtExact(x.compactions)}</td><td class="num">${fmtCredits(x.credits)}</td><td class="num">${pct(x.credit_coverage)}</td><td class="num">${fmtUsd(x.api_cost)}</td><td class="num">${pct(x.api_coverage)}</td><td class="num">${fmt(x.fresh)}</td><td class="num">${fmt(x.cached)}</td><td class="num">${fmt(x.output)}</td><td class="num"><b>${fmt(x.total)}</b></td><td class="num">${fmtMs(x.duration)}</td><td class="num">${x.max_context?pct(x.max_context):'—'}</td><td class="num">${fmtExact(x.failures)}</td></tr>`).join('');
  return`<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function render(){
  if(!dashboard?.ready)return;
  const s=dashboard.summary||{}, t=dashboard.turn_summary||{}, a=dashboard.activity_summary||{}, total=n(s.total_tokens);
  const toolByDay=dashboard.tool_activity||[],skillByDay=dashboard.skill_activity||[],patch=dashboard.patch_daily||[];
  const latest=dashboard.latest_rate_limit;
  $('content').innerHTML=`
    <div class="kpis">
      ${kpi('Total tokens',fmt(total),`${fmtExact(s.responses)} model responses`,'var(--blue)')}
      ${kpi('API-equivalent cost',fmtUsd(s.api_cost),`${pct(s.api_coverage_pct)} coverage · current Standard API rates`,'var(--green)',n(s.api_coverage_pct)<95)}
      ${kpi('Estimated credits',fmtCredits(s.estimated_credits),`${pct(s.credit_coverage_pct)} token coverage`,'var(--green)',n(s.credit_coverage_pct)<95)}
      ${kpi('Fresh input',fmt(s.fresh_input_tokens),`${pct(n(s.input_tokens)?n(s.fresh_input_tokens)/n(s.input_tokens)*100:0)} of input`,'var(--orange)')}
      ${kpi('Cached input',fmt(s.cached_input_tokens),`${pct(s.cache_hit_pct)} cache share`,'var(--teal)')}
      ${kpi('Output',fmt(s.output_tokens),`${fmt(s.reasoning_output_tokens)} reasoning subset`,'var(--pink)')}
      ${kpi('Sessions',fmtExact(s.sessions),`${fmtExact(s.threads)} threads`,'var(--purple)')}
      ${kpi('Subagent share',pct(s.subagent_share_pct),`${fmtExact(s.compactions)} compaction responses`,'var(--slate)')}
    </div>

    <div class="section-title"><h2>Token usage</h2><p>Dedicated colours stay consistent for each model and agent throughout the dashboard.</p></div>
    <div class="grid-2">
      <div class="panel"><h3>Daily token mix</h3><div class="desc">Stacked by model. Hover a day to see cached input, fresh input, output and the model split.</div><div class="chart">${stackedDaily(dashboard.daily_models||[])}</div></div>
      <div class="panel"><h3>Models</h3><div class="desc">Total tokens processed by the model recorded on each turn.</div>${hbars(dashboard.models||[],total,modelColor)}</div>
    </div>
    <div class="grid-3 mt">
      <div class="panel"><h3>Agents / roles</h3><div class="desc">Main thread and recorded subagent role labels.</div>${hbars(dashboard.agents||[],total,agentColor)}${agentPills(dashboard.configured_agents||[])}</div>
      <div class="panel"><h3>Reasoning effort</h3><div class="desc">Total token traffic grouped by recorded turn effort.</div>${hbars(dashboard.efforts||[],total,effortColor)}</div>
      <div class="panel"><h3>Response volume</h3><div class="desc">Model responses by main thread vs subagent.</div>${hbars((dashboard.agent_types||[]).map(x=>({...x,value:x.responses,total_tokens:x.responses})),n(s.responses),agentColor,fmtExact)}</div>
    </div>

    <div class="section-title"><h2>Subscription value · API-equivalent token cost</h2><p>Counterfactual cost at the current embedded Standard OpenAI API token rates.</p></div>
    <div class="grid-3">
      <div class="panel span-2"><h3>API-equivalent cost by day</h3><div class="desc">USD token cost by model for the current selection.</div><div class="chart">${stackedDaily(dashboard.daily_models||[],'api_cost','usd')}</div></div>
      <div class="panel"><h3>Cost by model</h3><div class="desc">API-equivalent token cost by model.</div>${hbars((dashboard.models||[]).map(x=>({...x,total_tokens:x.api_cost})),n(s.api_cost),modelColor,fmtUsd)}</div>
    </div>
    <div class="grid-2 equal mt">
      <div class="panel"><h3>Cost by agent</h3><div class="desc">API-equivalent token cost attributed to recorded agent roles.</div>${hbars((dashboard.agents||[]).map(x=>({...x,total_tokens:x.api_cost})),n(s.api_cost),agentColor,fmtUsd)}</div>
      <div class="panel"><h3>Estimate coverage</h3><div class="desc">Coverage is the token volume mapped to a published API model price.</div>
        ${mini('Priced token coverage',pct(s.api_coverage_pct),`${fmt(total-n(s.api_priced_tokens))} tokens excluded`)}
        <div style="height:8px"></div>
        ${mini('Long-context responses',fmtExact(s.long_context_responses),'Requests above the configured long-context threshold')}
      </div>
    </div>

    <div class="section-title"><h2>Useful insights</h2><p>Context, compaction, latency, failure and activity measures that help explain where capacity is going.</p></div>
    <div class="insight-cards">
      ${mini('Avg input / response',fmt(s.avg_input),'Average request context')}
      ${mini('P95 input / response',fmt(s.p95_input),'Large-request threshold')}
      ${mini('Context amplification',n(s.context_amplification).toFixed(1)+'×','Total input ÷ fresh input')}
      ${mini('Compaction overhead',fmt(s.compaction_tokens),`${fmtCredits(s.compaction_credits)} est. credits`)}
      ${mini('Avg turn duration',fmtMs(t.avg_duration_ms),`P95 ${fmtMs(t.p95_duration_ms)}`)}
      ${mini('Time to first token',fmtMs(t.avg_ttft_ms),`P95 ${fmtMs(t.p95_ttft_ms)}`)}
      ${mini('Failed / aborted',pct(t.failure_rate_pct),`${fmtExact(t.failed_turns)} turns`)}
      ${mini('P95 context use',pct(t.p95_context_pct),'Of recorded model context window')}
      ${mini('Tool calls',fmtExact(a.tool_calls),`${fmtExact(a.distinct_plugins)} plugins`)}
      ${mini('Skills used',fmtExact(a.skill_uses),`${fmtExact(a.distinct_skills)} distinct skills`)}
      ${mini('Patch lines changed',fmtExact(a.patch_lines_changed),'Persisted patch activity')}
      ${mini('Rate-limit pressure',latest?pct(latest.primary_used_pct):'—',latest?'Latest observed primary window':'No persisted rate-limit sample')}
    </div>

    <div class="section-title"><h2>Activity over time</h2><p>OpenAI-style views backed by local rollout telemetry.</p></div>
    <div class="grid-2 equal">
      <div class="panel"><h3>Tokens by model</h3><div class="desc">Total tokens by model over time.</div><div class="chart">${lineChart(dashboard.daily_models||[],'model','total_tokens',modelColor)}</div></div>
      <div class="panel"><h3>Turns by model</h3><div class="desc">Recorded turns by model over time.</div><div class="chart">${lineChart(dashboard.turns_by_model||[],'model','turns',modelColor)}</div></div>
    </div>
    <div class="grid-3 mt">
      <div class="panel"><h3>Tool activity</h3><div class="desc">Persisted tool/plugin calls over time.</div><div class="chart">${lineChart(toolByDay,'name','value',x=>COLOR_POOL[hash('tool:'+x)%COLOR_POOL.length])}</div></div>
      <div class="panel"><h3>Skills used</h3><div class="desc">Best-effort local skill detection from persisted rollouts.</div><div class="chart">${skillByDay.length?lineChart(skillByDay,'name','value',x=>COLOR_POOL[hash('skill:'+x)%COLOR_POOL.length]):'<div class="empty">No attributable skill invocations were found in this selection.</div>'}</div></div>
      <div class="panel"><h3>Patch lines changed</h3><div class="desc">Persisted patch activity; not a Git lines-of-code metric.</div><div class="chart">${simpleBars(patch,'value','var(--blue)','Lines changed')}</div></div>
    </div>

    <div class="section-title"><h2>Highest-usage sessions</h2><p>Sortable session-level token, cost and efficiency metrics for the current filters.</p></div>
    <div class="panel">${sessionTable(dashboard.sessions||[])}</div>
  `;
  $('sourceNote').textContent=`SQLite · ${fmtExact(s.responses)} responses in current filters`;
  const md=dashboard.metadata||{};
  $('updatedNote').textContent=md.generated_at?`Updated ${fmtDate(md.generated_at,true)} · API rates ${md.api_price_as_of||'current'} · credit rates ${md.credit_rate_as_of||'current'}`:'';
  document.querySelectorAll('th.sortable').forEach(th=>th.addEventListener('click',()=>{const key=th.dataset.sort;if(sortState.key===key)sortState.dir*=-1;else{sortState.key=key;sortState.dir=-1;}render();}));
}

async function applyFilters(){
  activeQuickRange=0;updateQuickButtons();
  const from=$('fromDate'),to=$('toDate');from.value=clampDate(from.value);to.value=clampDate(to.value);
  if(from.value&&to.value&&from.value>to.value)to.value=from.value;
  await loadDashboard(true);
}
function setQuickRange(days){activeQuickRange=days;updateQuickButtons();$('fromDate').value=quickRangeStart(days);$('toDate').value=meta.data_max||'';loadDashboard(true);}
function resetFilters(){activeQuickRange=0;initFilters(false);loadDashboard(true);}

function renderTip(raw){
  try{const data=JSON.parse(raw||'{}'),items=Array.isArray(data.items)?data.items:[];$('tooltip').innerHTML=`${data.title?`<div class="tip-title">${esc(data.title)}</div>`:''}${items.map(item=>`<div class="tip-row"><span class="tip-swatch" style="--tip-color:${escAttr(item.color||'var(--muted)')}"></span><span>${esc(item.label||'')}</span><span class="tip-value">${esc(item.value??'')}</span></div>`).join('')}`;return true;}catch{return false;}
}
document.addEventListener('mousemove',e=>{const t=e.target.closest?.('[data-tip-json]'),tip=$('tooltip');if(!t){tip.style.display='none';return;}renderTip(t.getAttribute('data-tip-json'));tip.style.display='block';let x=e.clientX+14,y=e.clientY+14;tip.style.left=x+'px';tip.style.top=y+'px';const r=tip.getBoundingClientRect();if(r.right>innerWidth-8)tip.style.left=(e.clientX-r.width-14)+'px';if(r.bottom>innerHeight-8)tip.style.top=(e.clientY-r.height-14)+'px';});
document.addEventListener('mouseleave',()=>{$('tooltip').style.display='none';});

function openRefreshModal(){
  $('refreshModal').classList.add('show');
  $('refreshModalStatus').hidden=true;
  $('refreshDays').value=activeQuickRange||7;
  $('refreshFrom').value=filters().from||meta?.data_min||'';
  $('refreshTo').value=filters().to||meta?.data_max||'';
}
function closeRefreshModal(){$('refreshModal').classList.remove('show');}
function setRefreshMode(mode){refreshMode=mode;document.querySelectorAll('.mode-btn').forEach(btn=>btn.classList.toggle('active',btn.dataset.refreshMode===mode));$('refreshDaysPanel').hidden=mode!=='days';$('refreshRangePanel').hidden=mode!=='range';}
async function startRefresh(){
  const body={mode:refreshMode,scan_all:$('refreshScanAll').checked};
  if(refreshMode==='range'){body.from=$('refreshFrom').value;body.to=$('refreshTo').value;}else body.days=Number($('refreshDays').value||7);
  $('startRefreshBtn').disabled=true;$('refreshModalStatus').hidden=false;$('refreshModalStatus').textContent='Starting background refresh…';
  try{
    const state=await api('/api/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    closeRefreshModal();startRefreshPolling(state);
  }catch(err){$('refreshModalStatus').textContent=err.message;}finally{$('startRefreshBtn').disabled=false;}
}
function showRefreshBanner(state){
  const b=$('refreshBanner'),text=$('refreshBannerText');b.className='refresh-banner show '+(state.status==='completed'?'done':state.status==='failed'?'failed':'');
  if(state.status==='running'){b.querySelector('.spinner').style.display='block';text.textContent=state.message||'Refreshing Codex telemetry in the background…';}
  else{b.querySelector('.spinner').style.display='none';text.textContent=state.message||state.status;}
}
function startRefreshPolling(initial){
  if(initial)showRefreshBanner(initial);
  if(refreshPoll)clearInterval(refreshPoll);
  const poll=async()=>{
    try{
      const state=await api('/api/refresh/status');showRefreshBanner(state);
      if(state.status!=='running'){
        clearInterval(refreshPoll);refreshPoll=null;
        if(state.status==='completed'){
          await loadMeta(true);await loadDashboard(false);
          setTimeout(()=>{$('refreshBanner').classList.remove('show');},5000);
        }
      }
    }catch{}
  };
  refreshPoll=setInterval(poll,1500);poll();
}

function bind(){
  document.querySelectorAll('.quick-range').forEach(btn=>btn.addEventListener('click',()=>setQuickRange(Number(btn.dataset.days))));
  ['fromDate','toDate','modelFilter','agentFilter','effortFilter','projectFilter'].forEach(id=>$(id).addEventListener('change',applyFilters));
  $('resetBtn').addEventListener('click',resetFilters);
  $('refreshDataBtn').addEventListener('click',openRefreshModal);
  $('closeRefreshModal').addEventListener('click',closeRefreshModal);
  $('cancelRefreshBtn').addEventListener('click',closeRefreshModal);
  $('refreshModal').addEventListener('click',e=>{if(e.target===$('refreshModal'))closeRefreshModal();});
  document.querySelectorAll('.mode-btn').forEach(btn=>btn.addEventListener('click',()=>setRefreshMode(btn.dataset.refreshMode)));
  $('startRefreshBtn').addEventListener('click',startRefresh);
}

async function boot(){
  bind();showLoading();
  try{await loadMeta(false);await loadDashboard(false);}
  catch(err){$('content').innerHTML=`<div class="panel error-panel"><b>Could not start dashboard.</b><br><br>${esc(err.message)}</div>`;}
  finally{hideLoading();}
}

boot();
})();