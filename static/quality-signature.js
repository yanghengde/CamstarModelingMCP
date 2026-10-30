(() => {
  'use strict';
  let challenge=location.hash.slice(1),csrf='',expiryTimer,participants=[];
  history.replaceState(null,'',location.pathname);
  const $=id=>document.getElementById(id);
  const clear=()=>{for(const input of document.querySelectorAll('input[type=password]'))input.value='';};
  const disable=()=>{for(const input of document.querySelectorAll('input[type=password]'))input.disabled=true;};
  const message=text=>{$('status').textContent=text;};
  const arm=seconds=>{clearTimeout(expiryTimer);expiryTimer=setTimeout(()=>{clear();disable();$('save').disabled=true;$('cancel').disabled=true;message('签名输入已过期，请重新准备计划。');},Math.max(0,seconds)*1000);};
  const headers=()=>({'Content-Type':'application/json','X-Signature-Challenge':challenge,'X-Signature-CSRF':csrf});
  const call=async(path,body)=>{
    const response=await fetch('/api/quality-signature/'+path,{method:'POST',headers:headers(),credentials:'same-origin',cache:'no-store',body:body});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'签名请求不可用');return data;
  };
  $('capture').addEventListener('submit',async event=>{
    event.preventDefault();$('save').disabled=true;
    const body=participants.length?{signatures:participants.map((p,n)=>({slot_id:p.slot_id,password:$('password-'+n).value,approved:$('confirm-'+n).checked})),approved:$('approved').checked}:{password:$('password').value,approved:$('approved').checked};
    const payload=JSON.stringify(body);clear();for(const value of body.signatures||[])value.password='';body.password='';
    try{const data=await call('capture',payload);message(data.note);$('capture').reset();disable();arm(data.expires_in);}
    catch(error){message(error.message);$('save').disabled=false;}
  });
  $('cancel').addEventListener('click',async()=>{clear();try{await call('cancel');disable();$('save').disabled=true;$('cancel').disabled=true;message('签名输入已取消。');}catch(error){message(error.message);}});
  addEventListener('pagehide',clear);
  (async()=>{
    try{
      if(!challenge)throw new Error('缺少签名请求，请从已准备计划的链接进入。');
      const data=await call('open');csrf=data.csrf;
      for(const [key,value] of Object.entries(data.display)){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=String(value);$('details').append(dt,dd);}
      participants=data.participants||[];
      if(participants.length){
        $('single-password').hidden=true;$('password').required=false;
        for(const [n,p] of participants.entries()){
          const section=document.createElement('fieldset'),legend=document.createElement('legend'),label=document.createElement('label'),input=document.createElement('input'),confirmLabel=document.createElement('label'),confirm=document.createElement('input');
          legend.textContent=`${p.position}：${p.employee_name}（${p.employee_id}） · ${p.requirement} / ${p.meaning}`;
          label.htmlFor='password-'+n;label.textContent='本人MES签名口令';input.id='password-'+n;input.type='password';input.required=true;input.maxLength=128;input.autocomplete='off';input.spellcheck=false;input.disabled=data.state!=='awaiting_input';
          confirm.type='checkbox';confirm.id='confirm-'+n;confirm.required=true;confirmLabel.append(confirm,document.createTextNode(`我是${p.employee_name}，已核对本计划、签名含义和岗位，亲自输入并授权本次提交。`));
          section.append(legend,label,input,confirmLabel);$('participants').append(section);
        }
      }
      $('save').disabled=data.state!=='awaiting_input';$('cancel').disabled=false;
      $('password').disabled=participants.length>0||data.state!=='awaiting_input';arm(data.expires_in);
      message(data.state==='captured'?'此计划已输入签名，请返回聊天确认同一计划。':`请在 ${data.expires_in} 秒内完成输入。`);
    }catch(error){message(error.message);}
  })();
})();
