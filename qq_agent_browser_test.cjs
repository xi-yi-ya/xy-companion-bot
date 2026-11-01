const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('./runtime/browser/node_modules/playwright-core');
const API='http://127.0.0.1:6185/api/v1/plugins/extensions/astrbot_plugin_companion_memory/';
(async()=>{
 const browser=await chromium.launch({executablePath:'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1100}}),page=await context.newPage(),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const get=async name=>(await context.request.get(API+name)).json(),post=async(name,data)=>(await context.request.post(API+name,{data})).json();
 const original=(await get('qq/agent')).settings,publication=(await get('qzone/publishing')).settings;
 let frame;
 async function open(){await page.goto('http://127.0.0.1:6185/#/plugin-view/astrbot_plugin_companion_memory/memories',{waitUntil:'networkidle'});frame=page.frames().find(f=>f!==page.mainFrame()&&f.url().includes('plugin'));await frame.waitForFunction(()=>document.getElementById('notice')?.textContent.includes('载入'));const close=page.getByRole('button',{name:'关闭',exact:true});if(await close.isVisible())await close.click();}
 try{
  await open();await frame.locator('[data-tab="qq"]').click();
  await frame.locator('#qq-send-images').setChecked(!original.send_images);await frame.locator('#save-qq-agent').click();await frame.waitForFunction(()=>document.getElementById('notice').textContent.includes('账号权限已保存'));
  assert.equal((await get('qq/agent')).settings.send_images,!original.send_images);
  await page.reload({waitUntil:'networkidle'});await open();await frame.locator('[data-tab="qq"]').click();assert.equal(await frame.locator('#qq-send-images').isChecked(),!original.send_images);
  await frame.locator('#qq-emergency').click();await frame.waitForFunction(()=>document.getElementById('qq-agent-state').textContent.includes('已急停'));assert.equal((await get('qq/agent')).settings.paused,true);
  await post('qq/agent',original);await frame.locator('#refresh-qq-agent').click();
  await page.screenshot({path:'runtime/qa/qq-agent-settings.png',fullPage:true});
  await frame.locator('[data-tab="qzone"]').click();await frame.locator('#publish-enabled').setChecked(false);await frame.locator('#publish-hours').fill('13');await frame.locator('#publish-limit').fill('3');await frame.locator('#save-publication').click();await frame.waitForFunction(()=>document.getElementById('notice').textContent.includes('自主发布设置已保存'));assert.deepEqual((await get('qzone/publishing')).settings,{enabled:false,min_hours:13,daily_limit:3});
  await frame.locator('#publish-hours').fill('14');await page.waitForTimeout(16000);assert.equal(await frame.locator('#publish-hours').inputValue(),'14');
  await page.reload({waitUntil:'networkidle'});await open();await frame.locator('[data-tab="qzone"]').click();assert.equal(await frame.locator('#publish-hours').inputValue(),'13');assert.equal(await frame.locator('#publish-limit').inputValue(),'3');
  await page.screenshot({path:'runtime/qa/qzone-publishing-settings.png',fullPage:true});
  assert.deepEqual(errors,[]);fs.writeFileSync('runtime/qa/qq-agent-ui.json',JSON.stringify({permissions_save_reload:true,emergency_switch:true,publication_save_reload:true,draft_survives_poll:true,errors},null,2));console.log('PASS: QQ permissions, emergency stop, publishing settings persistence and draft preservation; browser errors 0');
 }finally{await post('qq/agent',original);await post('qzone/publishing',publication);await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
