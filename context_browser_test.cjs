// Read-only regression: initial template, no old chats, and measured-usage UI.
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('./runtime/browser/node_modules/playwright-core');
(async()=>{
  const browser=await chromium.launch({executablePath:'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',headless:true});
  try{
    const context=await browser.newContext({viewport:{width:1500,height:1050}});
    const page=await context.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:6185/#/chatbox',{waitUntil:'networkidle'});
    const sessions=await (await context.request.get('http://127.0.0.1:6185/api/v1/chat/sessions')).json();
    await page.getByRole('link',{name:'Bot 设置',exact:true}).click();
    await page.locator('iframe').waitFor({state:'attached'});
    const frame=page.frames().find(f=>f!==page.mainFrame()&&f.url().includes('plugin'));
    await frame.waitForFunction(()=>document.querySelector('#notice')?.textContent.includes('载入'));
    if(await frame.locator('#bot-name').inputValue()!=='小伴')throw Error('Initial template changed');
    if(await frame.locator('#memory-budget').inputValue()!=='30000')throw Error('Wrong memory budget');
    if(await frame.locator('#history-budget').inputValue()!=='240000')throw Error('Wrong history budget');
    const close=page.getByRole('button',{name:'关闭',exact:true});
    if(await close.isVisible())await close.click();
    await frame.locator('[data-tab="metrics"]').click();
    await frame.locator('#usage-monthly table').waitFor({state:'visible'});
    if(await frame.locator('#usage-monthly tr').count()<2)throw Error('No actual usage rows');
    if(await frame.locator('#cost-forecast tr').count()!==10)throw Error('Missing normal cost scenarios');
    if(await frame.locator('#cost-continuous tr').count()!==10)throw Error('Missing deep-chat scenarios');
    const policy=JSON.parse(await frame.locator('#context-metrics').textContent());
    if(policy.count_method!=='official_deepseek_v4'||policy.policy.random_budget!==2200)throw Error('Wrong tokenizer or migrated policy');
    await frame.locator('#refresh-metrics').click();
    await frame.waitForFunction(()=>document.querySelector('#notice').textContent.includes('已刷新'));
    if(errors.length)throw Error(errors.join(';'));
    fs.mkdirSync(path.join(__dirname,'runtime/qa'),{recursive:true});
    await page.screenshot({path:path.join(__dirname,'runtime/qa/context-metrics.png'),fullPage:true});
    const evidence={template:'小伴',memory_budget:30000,history_budget:240000,monthly_usage_rows:await frame.locator('#usage-monthly tr').count()-1,cost_scenarios:18,errors,sessions};
    fs.writeFileSync(path.join(__dirname,'runtime/qa/context-browser-evidence.json'),JSON.stringify(evidence,null,2));
    console.log('PASS: template, budgets, official tokenizer, measured usage, 18 cost scenarios, refresh, no JS errors');
  }finally{await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
