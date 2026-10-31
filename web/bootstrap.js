const session = await fetch('/companion/local-session', {cache:'no-store'});
if(!session.ok)throw new Error('本地服务连接失败');
const {username,token}=await session.json();
localStorage.setItem('user',username);
localStorage.setItem('token',token);
for(const key of ['change_pwd_hint','md5_pwd_hint','password_upgrade_required'])localStorage.removeItem(key);
if(!location.hash || location.hash.startsWith('#/auth/'))location.hash='/chatbox';
const entry=document.querySelector('script[data-entry]').dataset.entry;
await import(entry);
const settings=document.createElement('a');
settings.id='companion-bot-settings';
settings.href='#/plugin-view/astrbot_plugin_companion_memory/memories';
settings.textContent='⚙ Bot 设置';
settings.setAttribute('aria-label','Bot 设置');
document.body.append(settings);
function update(){settings.hidden=!location.hash.startsWith('#/chatbox');}
window.addEventListener('hashchange',update);
update();
