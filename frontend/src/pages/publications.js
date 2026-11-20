import {api,post} from '../api/client.js';import {state} from '../stores/session.js';import {esc} from '../utils/html.js';import {table} from '../components/common/table.js';import {badge} from '../components/common/status.js';import {field,select} from '../components/common/fields.js';import {MODELS} from '../constants/enums.js';
const STATE_LABEL={pending:'待处理',claimable:'可领取',withdrawn:'已撤回',failed:'失败'};
const STATE_HINT={pending:'预算已扣账，后台正在重算并加噪。刷新可看进度。',claimable:'扰动后的七日整数预测可领取；原值、回测真值和随机种子不外传。',withdrawn:'已撤回，已领取预算不退还。',failed:'本次处理失败，同号重试沿用原记录，不重复扣账。'};
function detail(p){
 if(p.state==='claimable'&&p.forecasts){
  let f=p.forecasts;
  return `<div class="metrics">${Object.entries(f).map(([m,v])=>`<div><strong>${v.join(' / ')}</strong>${MODELS[m]} · 未来7日扰动后整数</div>`).join('')}</div>
  <div class="compare"><div><h3>七日预测 / 两法</h3>${table(['日期','周季节朴素','Holt'],p.days.map((d,i)=>[d,f.seasonal_naive[i],f.holt[i]]))}</div><div><h3>可核验的隐私界</h3>${table(['项目','数值'],[['机制',p.mechanism.name],['界',`ε=${p.mechanism.epsilon_total}，δ=${p.mechanism.delta}`],['单日裁剪上限',p.mechanism.clip_upper],['两模型 ε 分配',`${p.mechanism.epsilon_per_model} + ${p.mechanism.epsilon_per_model}`],['季节法 L1 灵敏度',p.mechanism.l1_sensitivity.seasonal_naive],['Holt L1 灵敏度',p.mechanism.l1_sensitivity.holt],['噪声尺度（季节/Holt）',`${p.mechanism.noise_scale.seasonal_naive.toFixed(2)} / ${p.mechanism.noise_scale.holt.toFixed(2)}`],['每次发布 ε',p.mechanism.epsilon_total],['逐日累计上限',p.mechanism.daily_cap],['训练窗口',`${p.mechanism.training_window.first_day} → ${p.mechanism.training_window.last_day}`],['扣账训练日',p.mechanism.charged_days]])}</div></div>
  <p class="notice">${esc(p.mechanism.neighboring)}。${esc(p.mechanism.composition)}。${esc(p.mechanism.rounding)}。灵敏度只依赖裁剪上限、窗口长度与 α/β，可离线复算。</p>
  <button id="claim" data-number="${esc(p.number)}">领取并下载 JSON</button>`;
 }
 return `<p class="notice">${esc(STATE_HINT[p.state]||p.state)}${p.error?'：'+esc(p.error):''}</p>`;
}
export function render(){
 let rows=state.publications||[];
 let sel=state.publication||rows[0]||null;
 return `<div class="split"><section class="panel"><h2>对外共享预测 / ${rows.length} 条</h2><p>对外只发布两法的七日扰动整数预测与机制说明。发布前由后台从冻结日量重算并加噪；每次发布按训练日共同扣账，预算不足即拒绝。</p>
 ${rows.map(p=>`<button class="study" data-publication="${esc(p.number)}">${esc(p.number)} · ${esc(p.branch)} · ${badge(STATE_LABEL[p.state]||p.state)}${p.epsilon!=null?' · ε='+p.epsilon:''}</button>`).join('')||'<p>暂无对外发布。先在右侧提交一份。</p>'}
 ${sel?`<article class="rec"><h3>${esc(sel.number)} ${badge(STATE_LABEL[sel.state]||sel.state)}</h3>${detail(sel)}${sel.state==='pending'||sel.state==='failed'?`<button id="refresh-publication">刷新状态</button>`:''}${sel.state==='claimable'&&state.user.role!=='viewer'?`<button data-withdraw="${esc(sel.number)}">撤回（预算不退）</button>`:''}</article>`:''}
 <p class="eyebrow">内部预测与排班仍保留在「预测对比」「排班建议」。</p></section>
 ${state.user.role==='viewer'?'<aside class="side"><h2>只读</h2><p>观察员可查看与领取可领取的发布，不能提交或撤回。</p></aside>':'<aside class="side"><h2>提交对外发布</h2><form id="publication">'
 ${field('发布编号（幂等号）','number','text','required minlength=6 value="pub-'+Date.now().toString(36)+'"')}
 ${select('成功预测版本（含网点）','run_id',state.runs.filter(r=>r.state==='done').map(r=>[r.id,`#${r.id} ${r.name} / ${r.branch}`]))}
 ${field('每次发布预算 ε（0.01–10）','epsilon','number','required min=0.01 max=10 step=0.01 value=1')}
 ${field('逐日累计上限 ε','daily_cap','number','required min=0.01 max=50 step=0.01 value=3')}
 ${field('日量裁剪上限','clip_upper','number','required min=1 max=1000 step=1 value=200')}
 <button class="primary">提交并扣账</button></form>
 <p class="notice">两个模型各分一半 ε；重叠训练日共同扣账；发布编号与内容绑定，同号重试一致即复用原记录。裁剪上限越小，隐私噪声越小。</p></aside>'}</div>`;
}
