const bridge=window.AstrBotPluginView;
const $=id=>document.getElementById(id);
await bridge.ready();
let current='', memories=[], filter='all', personaState, profileVersion, profileDirty=false;
const profileFields={name:'bot-name',personality:'persona',speech_style:'speech-style',boundaries:'boundaries',worldview:'worldview'};
const statuses={checking:'检查中',pending:'待你批准',approved:'已批准',rejected:'已拒绝',blocked:'检查未通过',stale:'已过期，需重新申请'};
$('day').value=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
function unwrap(value){if(value.status==='error')throw new Error(value.message);return value.data??value;}
function notice(text,error=false){$('notice').textContent=text;$('notice').className=error?'error':'';}
function node(tag,text,className){const n=document.createElement(tag);n.textContent=text;if(className)n.className=className;return n;}
// Native confirm() is blocked by AstrBot's sandboxed plugin iframe. Use a real
// in-page dialog so authorization requires a visible second user action.
function askConfirmation(text){
  if(document.querySelector('#confirmation-dialog[open]'))return Promise.resolve(false);
  const dialog=document.createElement('dialog');dialog.id='confirmation-dialog';dialog.setAttribute('aria-labelledby','confirmation-title');
  const heading=node('h3','确认操作');heading.id='confirmation-title';const actions=node('div','','actions');
  const cancel=node('button','取消'),accept=node('button','确认执行','primary');
  actions.append(cancel,accept);dialog.append(heading,node('p',text,'confirmation-text'),actions);document.body.append(dialog);
  return new Promise(resolve=>{dialog.onclose=()=>{const accepted=dialog.returnValue==='confirm';dialog.remove();resolve(accepted);};dialog.oncancel=e=>{e.preventDefault();dialog.close('cancel');};cancel.onclick=()=>dialog.close('cancel');accept.onclick=()=>dialog.close('confirm');dialog.showModal();cancel.focus();});
}
function sourceDetails(sources){const detail=document.createElement('details');detail.append(node('summary','来源证据'),node('pre',(sources||[]).map(s=>`消息 #${s.message_id}${s.day?' · '+s.day:''}：${s.quote}`).join('\n')));return detail;}
function fieldLabel(p){return (p.layer==='L0'?personaState.l0_fields:personaState.l1_fields)[p.field]||p.field;}
function showTab(name){for(const tab of document.querySelectorAll('[data-tab]')){const active=tab.dataset.tab===name;tab.classList.toggle('active',active);tab.setAttribute('aria-selected',String(active));$(tab.dataset.tab+'-panel').hidden=!active;}if(name==='approvals'&&current)loadPersona().catch(e=>notice(e.message,true));}
for(const button of document.querySelectorAll('[data-tab]'))button.onclick=()=>showTab(button.dataset.tab);
async function busy(button,action){button.disabled=true;try{await action();}catch(e){notice(e.message,true);}finally{button.disabled=false;}}
function readProfile(){return Object.fromEntries(Object.entries(profileFields).map(([field,id])=>[field,$(id).value]));}
function sizeHint(){const text=Object.values(readProfile()).join('\n');$('profile-size').textContent=`当前草稿 ${text.length} 字 · 已保存人设 ${personaState?.estimated_tokens??'…'} Token（本地分词估算） · v${profileVersion??'…'}${profileDirty?' · 尚未保存':''}`;}
for(const id of Object.values(profileFields))$(id).oninput=()=>{profileDirty=true;sizeHint();};
function renderTraits(){
  $('habits').replaceChildren();
  if(!personaState.traits.length)$('habits').append(node('p','还没有已批准的长期习惯。可以先慢慢聊，让相处形成依据。','empty'));
  for(const t of personaState.traits){
    const card=node('article','','memory-card'+(!t.active?' invalid':''));card.dataset.traitId=t.id;
    card.append(node('div',`#${t.id} · ${personaState.l1_fields[t.field]} · ${!t.active?'已撤回':t.needs_review?'暂停，待重新检查':'已生效 · 不衰减'}`,'meta'),node('div',t.content,'content'));
    const proposal=personaState.proposals.find(p=>p.id===t.proposal_id);if(t.sources||proposal)card.append(sourceDetails(t.sources||proposal.sources));
    if(t.active&&t.needs_review){const recheck=node('button','重新申请检查');recheck.onclick=()=>busy(recheck,async()=>{notice('正在重新检查原习惯…');const result=unwrap(await bridge.apiPost('persona/recheck',{person:current,id:t.id}));await loadPersona();showTab('approvals');notice(`已重新申请：${statuses[result.status]}。批准前仍保持暂停。`);});card.append(recheck);}
    if(t.active){const revoke=node('button','撤回习惯');revoke.onclick=()=>busy(revoke,async()=>{if(!await askConfirmation(`撤回这条习惯？\n${t.content}`))return;unwrap(await bridge.apiPost('persona/trait',{person:current,id:t.id,action:'revoke'}));await loadPersona();notice('已撤回，后续回复不再注入这条习惯。');});card.append(revoke);}
    $('habits').append(card);
  }
}
function renderProposals(){
  $('proposals').replaceChildren();const pending=personaState.proposals.filter(p=>p.status==='pending').length;$('pending-count').textContent=pending||'';
  if(!personaState.proposals.length)$('proposals').append(node('p','暂时没有申请。Bot 提出具体修改后，会在这里等待你处理。','empty'));
  for(const p of personaState.proposals){
    const card=node('article','','memory-card proposal '+p.status);card.dataset.proposalId=p.id;
    card.append(node('div',`#${p.id} · ${p.layer} ${fieldLabel(p)} · ${statuses[p.status]} · 基于 v${p.base_version}`,'meta'));
    const diff=node('div','','diff');for(const [label,value] of [['修改前',p.old_value||'（新增）'],['拟修改为',p.new_value]]){const cell=node('div');cell.append(node('strong',label),node('p',value,'content'));diff.append(cell);}card.append(diff);
    card.append(node('p',`修改理由：${p.reason}`),node('p',`一致性检查：${p.check_result.reason||'尚未完成'}`,'hint'),sourceDetails(p.sources));
    if(p.status==='pending'){
      const actions=node('div','','actions');for(const [action,label] of [['approve','批准这项修改'],['reject','拒绝']]){const button=node('button',label,action==='approve'?'primary':'');button.onclick=()=>busy(button,async()=>{
        const question=action==='approve'?`确认授权这项修改？\n${p.layer} ${fieldLabel(p)}\n${p.old_value||'（新增）'}\n→ ${p.new_value}${p.layer==='L0'?'\n这会使旧申请过期，并暂停已有 L1。':''}`:'拒绝这项修改申请？';
        if(!await askConfirmation(question))return;
        unwrap(await bridge.apiPost('persona/decide',{person:current,id:p.id,action}));await loadPersona();notice(action==='approve'?'已批准并执行，下一条回复开始生效。':'已拒绝，人设未改变。');
      });actions.append(button);}card.append(actions);
    }
    $('proposals').append(card);
  }
}
function renderMemories(){
  $('counts').textContent=`核心 ${memories.filter(m=>m.kind==='core'&&m.status!=='invalid').length} · 活跃 ${memories.filter(m=>m.kind==='ordinary'&&m.status==='active').length} · 归档 ${memories.filter(m=>m.status==='archived').length}`;
  $('memories').replaceChildren();
  const visible=memories.filter(m=>filter==='all'||(filter==='core'?m.kind==='core'&&m.status!=='invalid':filter==='active'?m.kind==='ordinary'&&m.status==='active':m.status===filter));
  if(!visible.length)$('memories').append(node('p',memories.length?'这个分类暂时没有记忆。':'还没有记忆。先聊几句，随后整理今天或等待每日整理。','empty'));
  for(const m of visible){
    const article=node('article','','memory-card '+m.status);
    article.append(node('div',`#${m.id} · ${m.kind==='core'?'用户核心长期事实':`Lv.${m.level}`} · ${m.status==='active'?'活跃':m.status==='archived'?'归档':'已失效'}`,'meta'),node('div',m.content,'content'),sourceDetails(m.sources));
    if(m.status!=='invalid'){const b=node('button','标记失效');b.onclick=()=>busy(b,async()=>{if(!await askConfirmation('将这条记忆标记为失效？原始记录会保留。'))return;unwrap(await bridge.apiPost('invalidate',{person:current,id:m.id}));await loadMemories();notice('记忆已标记失效。');});article.append(b);}
    $('memories').append(article);
  }
}
for(const button of document.querySelectorAll('[data-filter]'))button.onclick=()=>{filter=button.dataset.filter;for(const b of document.querySelectorAll('[data-filter]'))b.classList.toggle('active',b===button);renderMemories();};
async function loadMemories(){
  const data=unwrap(await bridge.apiGet('state',{person:current}));current=data.person;memories=data.memories;
  $('people').replaceChildren(...data.people.map(p=>{const o=node('option',p.id==='webchat:owner'?'我（本地聊天）':p.id);o.value=p.id;return o;}));$('people').value=current;
  $('jobs').textContent=JSON.stringify({jobs:data.jobs,recent_contexts:data.runs},null,2);renderMemories();
}
async function loadPersona(){
  personaState=unwrap(await bridge.apiGet('persona',{person:current}));
  if(!profileDirty){for(const [key,id] of Object.entries(profileFields))$(id).value=personaState.profile[key]||'';profileVersion=personaState.version;}
  sizeHint();renderTraits();renderProposals();
}
async function loadSettings(){const data=unwrap(await bridge.apiGet('settings')),s=data.settings;$('memory-budget').value=s.memory_budget;$('history-budget').value=s.history_budget;$('memory-instructions').textContent=data.memory_instructions;$('model').textContent=data.model;}
async function loadWeb(updateSettings=true){const data=unwrap(await bridge.apiGet('web',{person:current}));if(updateSettings){$('web-enabled').checked=data.settings.enabled;$('web-calls').value=data.settings.max_calls;$('web-budget').value=data.settings.context_budget;}$('web-history').replaceChildren();if(!data.history.length)$('web-history').append(node('p','还没有联网记录。聊到需要核实的信息时，它会自主使用搜索。','empty'));for(const item of data.history){const card=node('article','','memory-card');card.append(node('div',`${item.action==='search'?'搜索':'读取网页'} · ${item.success?'成功':'未成功'} · ${item.created_at}`,'meta'),node('div',item.query_or_url,'content'));if(item.error)card.append(node('p',item.error,'hint'));$('web-history').append(card);}}
async function loadQQ(){const data=unwrap(await bridge.apiGet('qq'));const phases={qrcode_scanned:'已扫描，请在手机确认登录',initializing:'已确认，QQ 正在准备上线',reconnecting:'正在重新连接 QQ'};$('qq-state').textContent=data.online?`QQ 已在线${data.bot_qq?' · Bot 账号 '+data.bot_qq:''}`:phases[data.phase]||(data.state==='waiting_scan'?'等待手机 QQ 扫码':data.state==='disabled'?'QQ 接入尚未启用':data.error||'正在启动 QQ 客户端…');$('qq-qr').hidden=!data.qr_image||data.online;if(data.qr_image&&$('qq-qr').getAttribute('src')!==data.qr_image)$('qq-qr').src=data.qr_image;$('qq-pairing').hidden=!data.bind_command;$('qq-bind').textContent=data.bind_command||'';$('qq-owner').textContent=data.owner_qq?`已绑定主号：${data.owner_qq} · 与本地 Web 共享记忆`:'尚未绑定主号';$('refresh-qq').disabled=!data.enabled||data.online;}
$('refresh-qq').onclick=()=>busy($('refresh-qq'),async()=>{unwrap(await bridge.apiPost('qq/refresh',{}));await loadQQ();notice('QQ 二维码已刷新。');});
if(new URLSearchParams(location.search).get('tab')==='qq')showTab('qq');
let qqPolling=false;
let approvalPolling=false;
setInterval(async()=>{if($('approvals-panel').hidden||approvalPolling||document.querySelector('dialog[open]'))return;approvalPolling=true;try{await loadPersona();}catch(e){notice('修改审批暂时无法刷新：'+e.message,true);}finally{approvalPolling=false;}},15000);
function contactTime(value){return value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';}
async function loadContact(updateSettings=true){
  const data=unwrap(await bridge.apiGet('proactive')),s=data.settings,state=data.state;
  if(updateSettings){$('contact-enabled').checked=s.enabled;$('contact-min').value=s.min_hours;$('contact-max').value=s.max_hours;$('contact-quiet').checked=s.quiet_enabled;$('contact-start').value=s.quiet_start;$('contact-end').value=s.quiet_end;}
  $('contact-state').textContent=!s.enabled?'主动联系已关闭':data.worker_error?'调度器正在重试连接':state?.awaiting_reply?'等待你的回复 · 暂停主动联系':!data.qq_ready?'等待 QQ 上线并绑定主号':!state?'等你发来第一条消息':state.current_run?'正在准备主动消息':state.attempts>=3?'准备消息连续失败 · 等你回复后重新开始':`下次最早联系：${contactTime(state.due_at)}`;
  $('contact-detail').textContent=`${state?'上次收到你的消息：'+contactTime(state.last_user_at)+'。 ':''}间隔 ${s.min_hours}～${s.max_hours} 小时${s.quiet_enabled?'；免打扰 '+s.quiet_start+'～'+s.quiet_end:'；未开启免打扰'}。${!data.qq_ready?' QQ 尚未就绪时不会生成或发送。':''}${data.worker_error?' '+data.worker_error:''}`;
  const labels={generating:'正在准备',sending:'正在发送',sent:'已发送 · 等待回复',unknown:'发送结果未确认 · 等待回复',skipped:'本次保持安静',cancelled:'已取消',failed:'准备未完成'};
  $('contact-deliveries').replaceChildren();
  if(!data.deliveries.length)$('contact-deliveries').append(node('p','还没有主动联系记录。','empty'));
  for(const item of data.deliveries){const card=node('article','','memory-card');card.append(node('div',`${labels[item.status]||item.status} · ${contactTime(item.sent_at||item.created_at)}`,'meta'));if(item.text)card.append(node('div',item.text,'content'));if(item.reason||item.error)card.append(node('p',[item.reason,item.error].filter(Boolean).join(' · '),'hint'));$('contact-deliveries').append(card);}
}
$('save-contact').onclick=()=>busy($('save-contact'),async()=>{unwrap(await bridge.apiPost('proactive',{enabled:$('contact-enabled').checked,min_hours:Number($('contact-min').value),max_hours:Number($('contact-max').value),quiet_enabled:$('contact-quiet').checked,quiet_start:$('contact-start').value,quiet_end:$('contact-end').value}));await loadContact();notice('主动联系设置已保存。');});
$('refresh-contact').onclick=()=>busy($('refresh-contact'),async()=>{await loadContact(false);notice('排期已刷新，未改变计时。');});
$('preview-contact').onclick=()=>busy($('preview-contact'),async()=>{notice('正在结合人设、记忆和聊天生成预览…');const data=unwrap(await bridge.apiPost('proactive/preview',{})),r=data.preview;const card=node('article','','memory-card');card.append(node('div',r.send?'这次想说（仅预览）':'这次选择保持安静','meta'),node('div',r.send?r.text:r.reason,'content'));$('contact-preview').replaceChildren(card);notice('预览完成，没有向 QQ 发送。');});
let contactPolling=false;
setInterval(async()=>{if($('proactive-panel').hidden||contactPolling)return;contactPolling=true;try{await loadContact(false);}catch(e){$('contact-state').textContent='正在等待 Bot 重新连接…';}finally{contactPolling=false;}},15000);
setInterval(async()=>{if($('qq-panel').hidden||qqPolling)return;qqPolling=true;try{await loadQQ();}catch(e){$('qq-state').textContent='正在等待 Bot 重新连接…';}finally{qqPolling=false;}},5000);

function table(target,headers,rows){const t=document.createElement('table'),head=document.createElement('tr');for(const h of headers)head.append(node('th',h));t.append(head);for(const values of rows){const tr=document.createElement('tr');for(const value of values)tr.append(node('td',value));t.append(tr);}$(target).replaceChildren(t);}
function number(value){return value==null?'—':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:0});}
function dollars(low,high){return low==null?'—':`$${Number(low).toFixed(4)}${Math.abs(Number(high)-Number(low))>.00001?'～$'+Number(high).toFixed(4):''}`;}
async function loadMetrics(){const data=unwrap(await bridge.apiGet('metrics',{person:current}));$('usage-totals').textContent=`已记录 ${data.context_health.builds} 次上下文构建，静默截断 ${data.context_health.silent_truncations} 次。${data.monthly.length?'费用为 API 用量按已核实价格计算的估算，缺失 usage 会单独标记。':'尚无新调用，之后会显示 API 返回的真实统计。'}`;
table('usage-monthly',['月份','调用','输入','输出','缓存命中率','费用估算','缺失用量'],data.monthly.map(r=>[r.month,r.calls,number(r.input_tokens),number(r.output_tokens),r.input_tokens?(100*r.cache_hit_tokens/r.input_tokens).toFixed(1)+'%':'—',dollars(r.usd_low,r.usd_high),r.missing_usage]));
table('usage-web',['月份','搜索 / 读网页成功次数','费用估算'],data.web_fees_monthly.map(r=>[r.month,r.billable_calls,dollars(r.usd,r.usd)]));
table('usage-daily',['日期','分类','调用','输入 / 输出','缓存 Token','总耗时 / 首 Token','费用估算','缺失用量 / 输出截断'],data.daily.map(r=>[r.day,r.category,r.calls,number(r.input_tokens)+' / '+number(r.output_tokens),number(r.cache_hit_tokens),number(r.latency_ms)+' / '+number(r.first_token_ms)+' ms',dollars(r.usd_low,r.usd_high),r.missing_usage+' / '+r.output_truncations]));
$('context-metrics').textContent=JSON.stringify({policy:data.policy,count_method:data.count_method,recent:data.recent_contexts},null,2);$('continuity-summaries').textContent=JSON.stringify(data.summaries,null,2);
for(const [id,rows] of [['cost-forecast',data.forecast.scenarios],['cost-continuous',data.forecast.continuous_scenarios]])table(id,['每天回复','缓存命中','聊天（月）','每日整理（月，峰）','摘要（月，峰）','合计（月）'],rows.map(r=>[r.replies_per_day,r.cache_hit_fraction*100+'%',dollars(r.chat_offpeak_usd,r.chat_peak_usd),'$'+r.daily_memory_peak_usd.toFixed(3),'$'+r.summary_peak_usd.toFixed(3),dollars(r.total_offpeak_usd,r.total_peak_usd)]));}
$('refresh-metrics').onclick=()=>busy($('refresh-metrics'),async()=>{await loadMetrics();notice('用量与上下文统计已刷新。');});

$('save-profile').onclick=()=>busy($('save-profile'),async()=>{unwrap(await bridge.apiPost('persona/profile',{profile:readProfile(),version:profileVersion}));profileDirty=false;await loadPersona();notice('人设已保存，下一条回复开始生效。');});
$('save-budgets').onclick=()=>busy($('save-budgets'),async()=>{unwrap(await bridge.apiPost('budgets',{memory_budget:Number($('memory-budget').value),history_budget:Number($('history-budget').value)}));notice('预算已保存，下一条回复开始生效。');});
for(const button of document.querySelectorAll('.refresh-persona'))button.onclick=()=>busy(button,async()=>{await loadPersona();notice(profileDirty?'申请与习惯已刷新，保留尚未保存的人设草稿。':'人设、习惯与申请已刷新。');});
$('refresh').onclick=()=>busy($('refresh'),async()=>{await loadMemories();notice('记忆已刷新，等级未重置。');});
$('people').onchange=async()=>{current=$('people').value;try{await loadMemories();await loadPersona();await loadWeb(false);await loadMetrics();notice('已切换互动用户。');}catch(e){notice(e.message,true);}};
$('save-web').onclick=()=>busy($('save-web'),async()=>{unwrap(await bridge.apiPost('web',{enabled:$('web-enabled').checked,max_calls:Number($('web-calls').value),context_budget:Number($('web-budget').value)}));notice('联网设置已保存，下一条回复生效。');});
$('refresh-web').onclick=()=>busy($('refresh-web'),async()=>{await loadWeb(false);notice('联网记录已刷新。');});
$('discover').onclick=()=>busy($('discover'),async()=>{if(!current)throw new Error('先聊几句，让系统有交流依据。');notice('正在检查长期习惯候选，将按需调用 DeepSeek…');const result=unwrap(await bridge.apiPost('persona/discover',{person:current}));await loadPersona();notice(result.status==='insufficient_evidence'?'跨日证据还不够，暂时不生成长期习惯。':result.candidates!==undefined?`检查完成，生成 ${result.candidates} 条候选；批准前不生效。`:'今天的候选检查已处理或正在进行，不重复生成。');});
$('consolidate').onclick=()=>busy($('consolidate'),async()=>{if(!current)throw new Error('先聊几句，再整理记忆。');notice('正在整理，将调用 DeepSeek…');const result=unwrap(await bridge.apiPost('consolidate',{person:current,day:$('day').value}));await loadMemories();notice(result.status==='complete'?'整理完成。':result.status==='up_to_date'?'这一天没有尚未整理的新消息。':`整理结果：${result.status}`);});
try{await Promise.all([loadSettings(),loadMemories()]);await Promise.all([loadPersona(),loadWeb(),loadMetrics(),loadQQ(),loadContact()]);notice('人设、习惯与记忆已从本地数据库载入。');}catch(e){notice(e.message,true);}
