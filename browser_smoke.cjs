const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('./runtime/browser/node_modules/playwright-core');
(async()=>{
  const browser=await chromium.launch({executablePath:'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',headless:true});
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000}});
    const page=await context.newPage();
    const errors=[];
    let bridgeResponse;
    page.on('response',async r=>{if(r.url().includes('bridge-sdk.js')){const body=await r.text();bridgeResponse={status:r.status(),head:body.slice(0,120),length:body.length,headers:r.headers(),origin:r.request().headers().origin};}});
    page.on('pageerror',e=>errors.push(e.message));
    page.on('console',m=>{if(m.type()==='error')console.log('Browser console:',m.text().replace(/eyJ[A-Za-z0-9_.-]+/g,'[scope]'));});
    page.on('response',r=>{if(r.status()>=400){const h=r.request().headers();console.log('HTTP error:',r.status(),new URL(r.url()).pathname.replace(/\/_t\/[^/]+/,'/_t/[scope]'),h.origin||'no-origin',h['sec-fetch-site']||'');}});
    fs.mkdirSync(path.join(__dirname,'runtime/qa'),{recursive:true});
    await page.goto('http://127.0.0.1:6185/#/chatbox',{waitUntil:'networkidle'});
    await page.getByRole('link',{name:'Bot 设置',exact:true}).waitFor();
    if(page.url().includes('/auth/'))throw new Error('Unexpected account login');
    console.log('Chat URL:',page.url());
    console.log('Chat text:',(await page.locator('body').innerText()).slice(0,1400));
    if(process.argv.includes('--chat')) {
      const input=page.locator('textarea').first();
      await input.waitFor({state:'visible'});
      const sent=page.waitForResponse(r=>r.request().method()==='POST'&&(r.headers()['content-type']||'').includes('text/event-stream'),{timeout:60000});
      await input.fill('这是浏览器界面验收，请只回复“界面连接正常”，不要记录为用户事实。');
      await input.press('Enter');
      const response=await sent; await response.finished();
      await page.getByText('界面连接正常',{exact:true}).first().waitFor({timeout:30000});
      console.log('Browser input -> live reply: PASS');
    }
    await page.screenshot({path:path.join(__dirname,'runtime/qa/web-chat.png'),fullPage:true});
    await page.getByRole('link',{name:'Bot 设置',exact:true}).click();
    await page.locator('iframe').waitFor({state:'attached',timeout:15000});
    console.log('Memory page URL:',page.url());
    const frame=page.frames().find(f=>f!==page.mainFrame()&&f.url().includes('plugin'));
    if(frame){await frame.locator('#notice').waitFor({state:'visible'});try{await frame.waitForFunction(()=>document.getElementById('notice').textContent.includes('载入'),{},{timeout:10000});}catch(e){console.log('Settings notice:',await frame.locator('#notice').textContent());console.log('JS errors:',errors);console.log('Bridge response:',bridgeResponse);throw e;}console.log('Bot settings loaded:',await frame.locator('#bot-name').inputValue());}
    else { console.log('Page text:',(await page.locator('body').innerText()).slice(0,1400)); }
    const closeNotice=page.getByRole('button',{name:'关闭',exact:true});
    if(await closeNotice.isVisible())await closeNotice.click();
    if(process.argv.includes('--settings')) {
      const endpoint='http://127.0.0.1:6185/api/v1/plugins/extensions/astrbot_plugin_companion_memory/settings';
      const original=(await (await context.request.get(endpoint)).json()).settings;
      try {
        await frame.locator('#bot-name').fill('界面设置验证');
        await frame.getByRole('button',{name:'保存人设',exact:true}).click();
        await frame.getByRole('status').filter({hasText:'已保存'}).waitFor();
        if((await (await context.request.get(endpoint)).json()).settings.bot_name!=='界面设置验证')throw new Error('Persona save failed');
        await page.reload({waitUntil:'networkidle'});
        const reloaded=page.frames().find(f=>f!==page.mainFrame()&&f.url().includes('plugin'));
        await reloaded.waitForFunction(()=>document.getElementById('bot-name')?.value==='界面设置验证');
        if(await closeNotice.isVisible())await closeNotice.click();
        await reloaded.locator('[data-tab="memory"]').click();
        await reloaded.locator('#counts').waitFor({state:'visible'});
        await reloaded.locator('[data-tab="rules"]').click();
        await reloaded.locator('#memory-budget').waitFor({state:'visible'});
        console.log('Persona save, reload persistence, memory tab and rules tab: PASS');
      } finally {await context.request.post(endpoint,{data:original});}
      await page.reload({waitUntil:'networkidle'});
    }
    const finalFrame=page.frames().find(f=>f!==page.mainFrame()&&f.url().includes('plugin'));
    if(finalFrame)await finalFrame.waitForFunction(()=>document.getElementById('notice')?.textContent.includes('载入'));
    if(await closeNotice.isVisible())await closeNotice.click();
    await page.screenshot({path:path.join(__dirname,'runtime/qa/memory-view.png'),fullPage:true});
    console.log('Browser errors:',JSON.stringify(errors));
    if(errors.length)throw new Error('Browser runtime errors');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e.message);process.exitCode=1;});
