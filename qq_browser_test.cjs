const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('./runtime/browser/node_modules/playwright-core');
(async()=>{
  const browser=await chromium.launch({executablePath:'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',headless:true});
  try{
    const context=await browser.newContext({viewport:{width:1440,height:1100}});
    const page=await context.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:6185/companion/qq',{waitUntil:'networkidle'});
    await page.locator('#qr').waitFor({state:'visible'});
    await page.waitForFunction(()=>document.querySelector('#qr').naturalWidth>0);
    if(!(await page.locator('#command').textContent()).startsWith('/绑定 '))throw Error('No initial pairing command');
    await page.locator('#refresh').click();
    await page.waitForFunction(()=>!document.querySelector('#refresh').disabled);
    const base='http://127.0.0.1:6185/api/v1/plugins/extensions/astrbot_plugin_companion_memory/';
    const state=await (await context.request.get(base+'qq')).json();
    if(!state.qr_image||state.online)throw Error('Expected real waiting-scan QR');
    const pixels=await page.locator('#qr').evaluate(img=>({w:img.naturalWidth,h:img.naturalHeight}));
    await page.screenshot({path:path.join(__dirname,'runtime/qa/qq-login.png'),fullPage:true});
    await page.goto('http://127.0.0.1:6185/#/plugin-view/astrbot_plugin_companion_memory/memories',{waitUntil:'networkidle'});
    await page.locator('iframe').waitFor({state:'attached'});
    const frame=page.frames().find(f=>f!==page.mainFrame()&&f.url().includes('plugin'));
    await frame.waitForFunction(()=>document.querySelector('#notice')?.textContent.includes('载入'));
    const close=page.getByRole('button',{name:'关闭',exact:true});
    if(await close.isVisible())await close.click();
    await frame.locator('[data-tab="qq"]').click();
    await frame.locator('#qq-qr').waitFor({state:'visible'});
    if(!(await frame.locator('#qq-state').textContent()).includes('等待手机'))throw Error('QQ tab failed');
    if(errors.length)throw Error(errors.join(';'));
    fs.writeFileSync(path.join(__dirname,'runtime/qa/qq-browser-evidence.json'),JSON.stringify({standalone_qr:true,settings_qq_tab:true,manual_refresh:true,pixels,errors},null,2));
    console.log('PASS: real QR displayed, refresh API, pairing instructions, QQ settings tab; no JS errors');
  }finally{await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
