export async function initQQCustomUI({bridge,unwrap,node,busy,notice,askConfirmation}){
  const $=id=>document.getElementById(id);
  const names={avatar:'头像',nickname:'昵称',signature:'签名',collect:'本地收录表情',sync:'同步 QQ 收藏'};
  const states={pending:'待你确认',queued:'尚未执行',executing:'正在执行',sent:'已完成',blocked:'检查未通过',failed:'未成功',unknown:'结果未知，不重复执行',cancelled:'已取消',rate_limited:'间隔或频率限制',disabled:'已关闭',confirmed:'QQ 列表已确认',not_synced:'未同步'};
  for(const [key,title] of Object.entries(names)){
    const label=node('label',title),select=node('select');select.id='custom-mode-'+key;
    for(const [value,text] of [['off','关闭'],['confirm','由我批准'],['auto','Bot 自主执行']]){const option=node('option',text);option.value=value;select.append(option);}
    label.append(select);$('qq-custom-modes').append(label);
  }
  for(const key of ['avatar','nickname','signature']){
    const label=node('label',names[key]+'最短修改间隔（小时）'),input=node('input');input.id='custom-hours-'+key;input.type='number';input.min='.25';input.max='720';input.step='.25';label.append(input);$('qq-custom-cooldowns').append(label);
  }
  const act=async value=>unwrap(await bridge.apiPost('qq/custom/act',value));
  async function image(id,parent){try{const data=unwrap(await bridge.apiGet('qq/custom/image',{id})),img=node('img');img.src=data.image;img.alt='图片预览';img.className='sticker-preview';parent.prepend(img);}catch(e){parent.append(node('p','预览不可用：'+e.message,'hint'));}}
  function button(title,action,parent){const b=node('button',title);b.onclick=()=>busy(b,action);parent.append(b);}
  async function gallery(rows){
    $('sticker-gallery').replaceChildren();
    if(!rows.length)$('sticker-gallery').append(node('p','还没有匹配的表情。','hint'));
    for(const s of rows){
      const card=node('article','','memory-card');card.append(node('p',s.description),node('p',`${s.emotion} · ${s.tags.join(' / ')} · ${states[s.qq_state]||s.qq_state}${s.active?'':' · 已停用'}`,'hint'));
      const scope=node('select');for(const [value,text] of [['owner','仅主人私聊'],['all','全部已授权会话'],...(s.scope.startsWith('group:')?[[s.scope,'原群 '+s.scope.slice(6)]]:[])]){const o=node('option',text);o.value=value;scope.append(o);}scope.value=s.scope;scope.onchange=()=>busy(scope,async()=>{if(!await askConfirmation('调整这张表情的使用范围？')){scope.value=s.scope;return;}await act({action:'scope',asset_id:s.asset_id,scope:scope.value});await load(false);});card.append(scope);
      if(!['sent','confirmed','unknown','syncing'].includes(s.qq_state))button('同步 QQ 收藏',async()=>{const result=await act({action:'sync',asset_id:s.asset_id});await load(false);notice('QQ 收藏：'+(states[result.status]||result.status));},card);
      button(s.active?'停用表情':'启用表情',async()=>{await act({action:'active',asset_id:s.asset_id,active:!s.active});await load(false);},card);
      $('sticker-gallery').append(card);await image(s.asset_id,card);
    }
  }
  async function load(form=true){
    const data=unwrap(await bridge.apiGet('qq/custom')),s=data.settings;
    if(form){for(const key of Object.keys(names))$('custom-mode-'+key).value=s.modes[key];for(const key of Object.keys(s.cooldown_hours))$('custom-hours-'+key).value=s.cooldown_hours[key];$('sticker-auto').checked=s.auto_collect;$('sticker-hourly').value=s.collect_per_hour;$('sticker-capacity').value=s.capacity;}
    $('qq-custom-state').textContent=data.worker_error?'自动识别暂时异常：'+data.worker_error:'操作受 QQ 连接页的急停、发送图片和群附件权限约束。';
    $('qq-current-profile').textContent=data.current_profile?JSON.stringify(data.current_profile.payload,null,2):'尚未读取当前 QQ 资料';
    const selected=$('custom-asset').value;$('custom-asset').replaceChildren(...data.assets.filter(a=>a.kind==='image').map(a=>{const o=node('option',a.name);o.value=a.id;return o;}));if(selected)$('custom-asset').value=selected;
    $('qq-custom-actions').replaceChildren();
    if(!data.actions.length)$('qq-custom-actions').append(node('p','暂无操作申请。','hint'));
    for(const a of data.actions){const card=node('article','','memory-card');card.dataset.customActionId=a.id;card.append(node('p',`${names[a.kind]||a.kind} · ${states[a.status]||a.status} · ${a.created_at}`,'meta'),node('p',a.payload.text??a.payload.name??a.payload.label?.description??'', 'content'),node('p',a.reason,'hint'));if(a.error)card.append(node('p',a.error,'hint'));if(a.payload.check)card.append(node('p','公开资料检查：'+a.payload.check.reason,'hint'));if(a.status==='pending'||a.status==='queued'){for(const approve of [true,false])button(approve?'批准执行':'取消申请',async()=>{if(approve&&!await askConfirmation(`确认${names[a.kind]||a.kind}？\n${a.payload.text??a.payload.name??''}\n${a.reason}`))return;const result=await act({action:'decide',id:a.id,approve});await load(false);notice(result.cancelled?'申请已取消。':'操作结果：'+(states[result.status]||result.status));},card);}if(a.payload.asset_id&&a.status==='pending')await image(a.payload.asset_id,card);$('qq-custom-actions').append(card);}
    $('sticker-count').textContent=`本地共 ${data.sticker_count} 张；显示最近或搜索匹配的前 20 张。`;
    await gallery(data.stickers);
  }
  $('save-qq-custom').onclick=()=>busy($('save-qq-custom'),async()=>{await bridge.apiPost('qq/custom',{modes:Object.fromEntries(Object.keys(names).map(k=>[k,$('custom-mode-'+k).value])),cooldown_hours:Object.fromEntries(['avatar','nickname','signature'].map(k=>[k,Number($('custom-hours-'+k).value)])),auto_collect:$('sticker-auto').checked,collect_per_hour:Number($('sticker-hourly').value),capacity:Number($('sticker-capacity').value)}).then(unwrap);notice('QQ 外观与表情权限已保存；旧待审申请不会自动执行。');});
  $('refresh-qq-custom').onclick=()=>busy($('refresh-qq-custom'),()=>load(false));
  $('refresh-qq-profile').onclick=()=>busy($('refresh-qq-profile'),async()=>{const r=await act({action:'profile'});await load(false);notice('QQ 资料读取：'+(states[r.status]||r.status));});
  $('read-qq-favorites').onclick=()=>busy($('read-qq-favorites'),async()=>{const r=await act({action:'favorites'});$('qq-favorites-result').textContent=JSON.stringify(r.favorites,null,2);$('qq-favorites-result').parentElement.open=true;notice(`读取到 ${r.favorites.length} 张 QQ 收藏表情。`);});
  $('propose-qq-profile').onclick=()=>busy($('propose-qq-profile'),async()=>{const kind=$('custom-kind').value;const r=await act({action:'propose',kind,value:kind==='avatar'?$('custom-asset').value:$('custom-text').value});await load(false);notice('申请：'+(states[r.status]||r.status));});
  $('collect-qq-sticker').onclick=()=>busy($('collect-qq-sticker'),async()=>{const r=await act({action:'collect',asset_id:$('custom-asset').value});await load(false);notice('本地收录：'+(states[r.status]||r.status));});
  $('search-stickers').onclick=()=>busy($('search-stickers'),async()=>{const r=await act({action:'search',query:$('sticker-query').value});await gallery(r.stickers);});
  await load();
}
