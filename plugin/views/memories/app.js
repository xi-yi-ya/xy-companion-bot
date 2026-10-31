const bridge=window.AstrBotPluginView;
const $=id=>document.getElementById(id);
await bridge.ready();
let current='', memories=[], filter='all';
$('day').value=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
function unwrap(value){if(value.status==='error')throw new Error(value.message);return value.data??value;}
function notice(text,error=false){$('notice').textContent=text;$('notice').className=error?'error':'';}
for(const button of document.querySelectorAll('[data-tab]'))button.onclick=()=>{
  for(const tab of document.querySelectorAll('[data-tab]')){const active=tab===button;tab.classList.toggle('active',active);tab.setAttribute('aria-selected',String(active));$(tab.dataset.tab+'-panel').hidden=!active;}
};
function renderMemories(){
  $('counts').textContent=`核心 ${memories.filter(m=>m.kind==='core'&&m.status!=='invalid').length} · 活跃 ${memories.filter(m=>m.kind==='ordinary'&&m.status==='active').length} · 归档 ${memories.filter(m=>m.status==='archived').length}`;
  $('memories').replaceChildren();
  const visible=memories.filter(m=>filter==='all'||(filter==='core'?m.kind==='core'&&m.status!=='invalid':filter==='active'?m.kind==='ordinary'&&m.status==='active':m.status===filter));
  if(!visible.length){const empty=document.createElement('p');empty.className='empty';empty.textContent=memories.length?'这个分类暂时没有记忆。':'还没有记忆。先聊几句，随后整理今天或等待每日整理。';$('memories').append(empty);}
  for(const m of visible){
    const article=document.createElement('article');article.className='memory-card '+m.status;
    const meta=document.createElement('div');meta.className='meta';meta.textContent=`#${m.id} · ${m.kind==='core'?'核心长期记忆':`Lv.${m.level}`} · ${m.status==='active'?'活跃':m.status==='archived'?'归档':'已失效'}`;
    const content=document.createElement('div');content.className='content';content.textContent=m.content;
    const sources=document.createElement('details'),title=document.createElement('summary'),evidence=document.createElement('pre');title.textContent='来源证据';evidence.textContent=(m.sources||[]).map(s=>`消息 #${s.message_id}：${s.quote}`).join('\n');sources.append(title,evidence);article.append(meta,content,sources);
    if(m.status!=='invalid'){const b=document.createElement('button');b.textContent='标记失效';b.onclick=async()=>{if(!confirm('将这条记忆标记为失效？原始记录会保留。'))return;try{unwrap(await bridge.apiPost('invalidate',{person:current,id:m.id}));await loadMemories();}catch(e){notice(e.message,true);}};article.append(b);}
    $('memories').append(article);
  }
}
for(const button of document.querySelectorAll('[data-filter]'))button.onclick=()=>{filter=button.dataset.filter;for(const b of document.querySelectorAll('[data-filter]'))b.classList.toggle('active',b===button);renderMemories();};
async function loadMemories(){
  const data=unwrap(await bridge.apiGet('state',{person:current}));current=data.person;memories=data.memories;
  $('people').replaceChildren(...data.people.map(p=>{const o=document.createElement('option');o.value=p.id;o.textContent=p.id==='webchat:owner'?'我（本地聊天）':p.id;return o;}));$('people').value=current;
  $('jobs').textContent=JSON.stringify({jobs:data.jobs,recent_contexts:data.runs},null,2);renderMemories();
}
async function loadSettings(){
  const data=unwrap(await bridge.apiGet('settings')),s=data.settings;
  $('bot-name').value=s.bot_name;$('persona').value=s.persona;$('memory-budget').value=s.memory_budget;$('history-budget').value=s.history_budget;
  $('memory-instructions').textContent=data.memory_instructions;$('model').textContent=data.model;
}
for(const button of document.querySelectorAll('.save-settings'))button.onclick=async()=>{
  const buttons=document.querySelectorAll('.save-settings');buttons.forEach(b=>b.disabled=true);
  try{unwrap(await bridge.apiPost('settings',{bot_name:$('bot-name').value,persona:$('persona').value,memory_budget:Number($('memory-budget').value),history_budget:Number($('history-budget').value)}));notice('已保存，下一条回复开始生效。');}catch(e){notice(e.message,true);}finally{buttons.forEach(b=>b.disabled=false);}
};
$('refresh').onclick=async()=>{try{await loadMemories();notice('记忆已刷新，等级未重置。');}catch(e){notice(e.message,true);}};
$('people').onchange=async()=>{current=$('people').value;try{await loadMemories();}catch(e){notice(e.message,true);}};
$('consolidate').onclick=async()=>{if(!current){notice('先聊几句，再整理记忆。');return;}$('consolidate').disabled=true;notice('正在整理，将调用 DeepSeek…');try{const result=unwrap(await bridge.apiPost('consolidate',{person:current,day:$('day').value}));await loadMemories();notice(result.status==='complete'?'整理完成。':result.status==='up_to_date'?'这一天没有尚未整理的新消息。':`整理结果：${result.status}`);}catch(e){notice(e.message,true);}finally{$('consolidate').disabled=false;}};
try{await Promise.all([loadSettings(),loadMemories()]);notice('设置与记忆已从本地数据库载入。');}catch(e){notice(e.message,true);}
