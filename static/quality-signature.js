(() => {
  'use strict';
  let challenge=location.hash.slice(1),csrf='',expiryTimer;
  history.replaceState(null,'',location.pathname);
  const $=id=>document.getElementById(id);
  const clear=()=>{$('password').value='';};
  const message=text=>{$('status').textContent=text;};
  const arm=seconds=>{clearTimeout(expiryTimer);expiryTimer=setTimeout(()=>{clear();$('password').disabled=true;$('save').disabled=true;$('cancel').disabled=true;message('签名输入已过期，请重新准备计划。');},Math.max(0,seconds)*1000);};
  const headers=()=>({'Content-Type':'application/json','X-Signature-Challenge':challenge,'X-Signature-CSRF':csrf});
  const call=async(path,body)=>{
    const response=await fetch('/api/quality-signature/'+path,{method:'POST',headers:headers(),credentials:'same-origin',cache:'no-store',body:body});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'签名请求不可用');return data;
  };
  $('capture').addEventListener('submit',async event=>{
    event.preventDefault();$('save').disabled=true;
    const payload=JSON.stringify({password:$('password').value,approved:$('approved').checked});clear();
    try{const data=await call('capture',payload);message(data.note);$('capture').reset();$('password').disabled=true;arm(data.expires_in);}
    catch(error){message(error.message);$('save').disabled=false;}
  });
  $('cancel').addEventListener('click',async()=>{clear();try{await call('cancel');$('save').disabled=true;$('cancel').disabled=true;message('签名输入已取消。');}catch(error){message(error.message);}});
  addEventListener('pagehide',clear);
  (async()=>{
    try{
      if(!challenge)throw new Error('缺少签名请求，请从已准备计划的链接进入。');
      const data=await call('open');csrf=data.csrf;
      for(const [key,value] of Object.entries(data.display)){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=String(value);$('details').append(dt,dd);}
      $('save').disabled=data.state!=='awaiting_input';$('cancel').disabled=false;
      $('password').disabled=data.state!=='awaiting_input';arm(data.expires_in);
      message(data.state==='captured'?'此计划已输入签名，请返回聊天确认同一计划。':`请在 ${data.expires_in} 秒内完成输入。`);
    }catch(error){message(error.message);}
  })();
})();
