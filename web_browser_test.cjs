const path=require('node:path');
const {chromium}=require('./runtime/browser/node_modules/playwright-core');
const API='http://127.0.0.1:6185/api/v1/plugins/extensions/astrbot_plugin_companion_memory/';
(async()=>{
 const browser=await chromium.launch({executablePath:'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1100}}), page=await context.newPage(), errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 let original;
 try {
  original=(await (await context.request.get(API+'web')).json()).settings;
  await page.goto('http://127.0.0.1:6185/#/plugin-view/astrbot_plugin_companion_memory/memories',{waitUntil:'networkidle'});
  let frame=page.frames().find(f=>f!==page.mainFrame()&&f.url().includes('plugin'));
  await frame.waitForFunction(()=>document.getElementById('notice')?.textContent.includes('载入'));
  const close=page.getByRole('button',{name:'关闭',exact:true});if(await close.isVisible())await close.click();
  await frame.locator('[data-tab="web"]').click();
  await frame.locator('#web-enabled').setChecked(!original.enabled);
  await frame.locator('#save-web').click();
  await frame.getByRole('status').filter({hasText:'联网设置已保存'}).waitFor();
  if((await (await context.request.get(API+'web')).json()).settings.enabled===original.enabled)throw new Error('Toggle not saved');
  await page.reload({waitUntil:'networkidle'});
  frame=page.frames().find(f=>f!==page.mainFrame()&&f.url().includes('plugin'));
  await frame.waitForFunction(()=>document.getElementById('notice')?.textContent.includes('载入'));
  if(await close.isVisible())await close.click();
  await frame.locator('[data-tab="web"]').click();
  if(await frame.locator('#web-enabled').isChecked()===original.enabled)throw new Error('Toggle not persisted');
  await frame.locator('#web-enabled').setChecked(original.enabled);
  await frame.locator('#save-web').click();
  await frame.getByRole('status').filter({hasText:'联网设置已保存'}).waitFor();
  await frame.locator('#refresh-web').click();
  await frame.getByRole('status').filter({hasText:'联网记录已刷新'}).waitFor();
  await page.screenshot({path:path.join(__dirname,'runtime/qa/web-access-settings.png'),fullPage:true});
  if(errors.length)throw new Error(JSON.stringify(errors));
  console.log('Web toggle save -> reload -> restore; request history visible; browser errors 0: PASS');
 } finally {
  if(original)await context.request.post(API+'web',{data:original});
  await browser.close();
 }
})().catch(e=>{console.error(e.message);process.exitCode=1;});
