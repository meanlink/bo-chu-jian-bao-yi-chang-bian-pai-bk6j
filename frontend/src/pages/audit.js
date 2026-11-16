import {seals} from '../api/entities.js';
import {downloadSeal} from '../api/client.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,date,notify} from '../utils/dom.js';

const statusLabel={sealed:'已封存',pending:'封存中',failed:'封存失败'};

function fingerprints(pack){
 return '<div class="seal-hashes"><span>前包指纹 <code>'+esc((pack.prev_header_hash||'').slice(0,16))+'…</code></span><span>包头指纹 <code>'+esc((pack.header_hash||'').slice(0,16))+'…</code></span></div>';
}

function packRow(pack,canWrite){
 const actions=[];
 if(pack.downloadable)actions.push('<button class="text-button" data-seal-download="'+pack.id+'">下载 ↓</button>');
 if(canWrite&&pack.status!=='sealed'&&pack.status!=='failed')actions.push('<button class="text-button" data-seal-retry="'+pack.id+'">重试封存 ↻</button>');
 return '<article class="seal-pack '+esc(pack.status)+'"><div class="seal-pointer"><span class="seal-no">第 '+String(pack.pack_no).padStart(3,'0')+' 包</span><span class="status '+esc(pack.status)+'">'+esc(statusLabel[pack.status]||pack.status)+'</span>'+(pack.is_registered_head?'<span class="seal-head">当前封存头</span>':'')+'</div><div class="seal-body"><strong>审计 #'+pack.first_audit_id+' – #'+pack.last_audit_id+' · '+pack.item_count+' 条</strong><p>封存人 '+esc(pack.created_by)+' · '+(pack.sealed_at?date(pack.sealed_at):'未完成登记')+'</p>'+(pack.error?'<p class="warning">'+esc(pack.error)+'</p>':'')+fingerprints(pack)+'</div><div class="seal-actions">'+(actions.join('')||'<span class="hint">尚未完成，不可下载</span>')+'</div></article>';
}

function resultPanel(result){
 if(!result)return '<p class="empty">点击“整链验真”按已登记封存头核对全部已封存包。</p>';
 const head=result.registered_head||{};
 const packs=result.packs||[];
 return '<div class="verify-summary '+(!result.ok?'bad':'good')+'"><strong>'+(result.ok?'验真通过':'验真发现断点')+'</strong><span>已核对 '+result.checked_packs+' 包 / '+result.checked_records+' 条 · 待封存动作 '+result.unsealed_audit_count+' 条</span></div>'+(result.break?'<p class="warning">断点：'+esc(result.break)+'</p>':'')+(result.problems?.length?'<ol class="verify-problems">'+result.problems.map(p=>'<li>'+esc(p)+'</li>').join('')+'</ol>':'')+(result.warnings?.length?'<ul class="verify-warnings">'+result.warnings.map(p=>'<li>'+esc(p)+'</li>').join('')+'</ul>':'')+'<div class="registered-head"><span>已登记封存头</span><strong>第 '+head.last_pack_no+' 包</strong><code>'+esc(head.last_header_hash||'')+'</code><small>覆盖至审计 #'+(head.last_audit_id??0)+'</small></div>'+(packs.length?'<div class="verify-packs">'+packs.map(p=>'<div class="verify-pack '+(p.ok?'ok':'bad')+'"><span>第 '+p.pack_no+' 包</span><span>审计 #'+p.first_audit_id+' – #'+p.last_audit_id+' · '+p.item_count+' 条</span><span>'+(p.ok?'通过':'断点')+'</span></div>'+(p.problems.length?'<ul>'+p.problems.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul>':'')).join('')+'</div>':'');
}

export async function auditPage(){
 const data=await seals.list();
 const canWrite=session.user.role!=='viewer';
 const head=data.registered_head||{};
 const packs=data.packs||[];
 return heading('06 / AUDIT SEALS','连续审计封存与验真','按已提交顺序连续封存全部业务动作。每包列明审计范围、条数与前包指纹；封存期间的新动作留给下一包。验真依据已登记封存头核对，不使用包内自检指纹。','<button class="button" id="refresh-seals">刷新封存状态 ↻</button>')
 +'<section class="seal-stats"><div><span>已封存包</span><strong>'+packs.filter(p=>p.status==='sealed').length+'</strong></div><div><span>待封存动作</span><strong>'+data.unsealed_audit_count+'</strong></div><div><span>已登记封存头</span><strong>第 '+head.last_pack_no+' 包</strong></div><div><span>覆盖至审计</span><strong>#'+(head.last_audit_id??0)+'</strong></div><div><span>在途包</span><strong>'+(data.pending_pack_no?'第 '+data.pending_pack_no+' 包':'无')+'</strong></div></section>'
 +'<div class="seal-grid"><section class="panel"><div class="section-title"><h2>封存新动作</h2><span>连续、不可跳过</span></div><form id="seal-form"><label>封存备注（可选）<input name="note" maxlength="200" placeholder="例如：交班前封存 08:00–12:00 动作"></label><button type="submit" class="primary" '+(canWrite?'':'disabled')+'>开启第 '+(head.last_pack_no+1)+' 包</button><p class="hint">只封装已提交动作；封存期间产生的新动作自动进入下一包。'+(canWrite?'':'观察员只能读取与下载。')+'</p></form><div class="section-title"><h2>验真工具</h2></div><button class="button" id="verify-chain">整链验真</button><label class="seal-upload">验真单个包文件<input type="file" id="verify-file" accept=".json"></label><p class="hint">单个包验真会与已登记封存头、范围与前包指纹核对，并提示前后断点。</p></section><section class="panel"><div class="section-title"><h2>封存包</h2><span>'+packs.length+' 包 · 按包号倒序</span></div><div class="seal-list">'+(packs.length?packs.map(p=>packRow(p,canWrite)).join(''):'<p class="empty">还没有封存包。先完成业务动作，再点击“开启第 1 包”。</p>')+'</div></section></div>'
 +'<section class="panel"><div class="section-title"><h2>验真结果</h2><span>以已登记封存头为准</span></div><div id="verify-result">'+resultPanel(null)+'</div></section>';
}

export function bindAudit(refresh){
 document.querySelector('#refresh-seals').onclick=refresh;
 const canWrite=session.user.role!=='viewer';
 if(canWrite){
  document.querySelector('#seal-form').addEventListener('submit',async e=>{
   e.preventDefault();const button=e.target.querySelector('[type=submit]');button.disabled=true;
   try{await seals.create(new FormData(e.target).get('note')||'');notify('封存包已生成，可下载或验真');await refresh();}
   catch(error){notify(error.message,true);}finally{button.disabled=false;}
  });
 }
 document.querySelectorAll('[data-seal-download]').forEach(b=>b.onclick=()=>downloadSeal(b.dataset.sealDownload).catch(error=>notify(error.message,true)));
 document.querySelectorAll('[data-seal-retry]').forEach(b=>b.onclick=async()=>{try{await seals.retry(b.dataset.sealRetry);notify('封存包已重新写入并登记');await refresh();}catch(error){notify(error.message,true);}});
 const show=result=>{document.querySelector('#verify-result').innerHTML=resultPanel(result);};
 document.querySelector('#verify-chain').onclick=async()=>{try{show(await seals.verifyChain());}catch(error){notify(error.message,true);}};
 document.querySelector('#verify-file').addEventListener('change',async e=>{const file=e.target.files[0];if(!file)return;try{const document_=JSON.parse(await file.text());show(await seals.verifyDocument(document_));}catch(error){notify(error.message,true);}});
}
