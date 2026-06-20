// alertador_valor.js v2 — sinal de aposta AO VIVO bet365 (SOMENTE LEITURA).
// Tiers WATCH -> ARM -> GREEN. NAO aposta; voce decide e clica.
//
// Cole no Console (F12) com a pagina #/IP/B1 aberta:
//   iniciarValor()                                  // defaults
//   iniciarValor({armMin:88, probMin:0.80})         // mais exigente
//   iniciarValor({greenBuffer:0.5})                 // GREEN um pouco mais cedo
//   pararValor()
//
// O QUE MUDOU vs v1 (ver MAPEAMENTO-BET365.md / plano):
//  - Modelo de probabilidade calibrado no futebol.db: hazard por minuto (curva),
//    acrescimo como distribuicao, multiplicador de placar, banda de confianca.
//  - Relogio lido em MM:SS; guardas anti relogio-fantasma (congelado / >teto).
//  - Tiers + AND-gate: GREEN so fundo nos acrescimos, mercado aberto, relogio
//    fresco, criterios batidos e confirmado por N scans. (GREEN = "ultima boa
//    janela", NAO "faltam 40s" — o apito e imprevisivel.)
//  - Gancho SofaScore: se window.__ssCross[chave] existir (preenchido pelo
//    caminho Playwright), corrobora o minuto e mata verde se status=finished.
//
// CAL embutido vem de hazard_cal.json (gerado por calibrar_hazard.py). Pra
// re-calibrar: rode o .py e cole os novos valores no objeto CAL abaixo.

;(function () {
  const VERSAO = 'v2.9f';  // <- aparece na barra; se nao mostrar isso, e a versao ANTIGA (f = filtro de odd minima)
  // ---------------- calibracao (de hazard_cal.json) ----------------
  const CAL = {
    home_share: 0.5489,
    gols_por_jogo: 2.2682,
    h_reg_buckets: [0.010366,0.023221,0.016172,0.016586,0.023221,0.026123,
                    0.021562,0.026538,0.025709,0.02405,0.026953,0.026123,
                    0.022392,0.029855,0.022392,0.022806,0.031099,0.016586],
    h_stop_2h: 0.03133,
    stoppage_extra_pmf: [0.45,0.28,0.15,0.08,0.04],
    red: { down: 0.74, up: 1.30 },
    mult: { sigma: 0.35, d0: 1.5, clampLo: 0.45, clampHi: 2.2 },
    sigma_log: { base: 0.30, sem_stats: 0.10, por_mult: 0.15 },
    forca: { minJogos: 3, k: 5.0, wMax: 0.85 },   // shrinkage do blend forca-time
  };

  // ---------------- config / thresholds ----------------
  const DEF = {
    watchMin: 80,            // entra em WATCH
    armMin: 88,              // pode ARMar
    probMin: 0.70,           // prob minima do resultado dominante
    greenBuffer: 1.0,        // GREEN comeca esse tanto ANTES do fim anunciado
    unknownStoppageFloor: 2, // acrescimo desconhecido -> GREEN a partir de 92'
    greenOvershoot: 2,       // teto do GREEN acima do anunciado
    hardCeiling: 8,          // teto do GREEN quando acrescimo desconhecido
    stallScans: 2,           // relogio congelado por N scans -> STALE
    surgeScans: 3,           // sem mudanca de placar/flap nesses scans
    confirmScans: 2,         // GREEN precisa persistir N scans (anti-flicker)
    clockSkewTol: 2.0,       // tolerancia de minuto bet365 vs SofaScore
    intervaloMs: 3000,
    exigeOddAtiva: true,
    ssUrl: null,             // ex.: 'http://localhost:8765' (sofascore_live.py servir)
    ssRefreshMs: 10000,      // de quanto em quanto puxa o cross do SofaScore
    painel: true,            // momentum pelo painel do bet365 (jogo aberto) — sem API, mesma fonte
    stakeTotal: 10,          // R$ total p/ o calculo de Dutching (cobrir 2 resultados)
    dutch: true,             // mostra o plano de Dutching no badge dos ARM/GREEN
    usarBanda: true,         // (B) gateia pelo limite INFERIOR da banda (cenario pessimista)
    vermelho: true,          // (A) le cartao vermelho do DOM e alimenta o modelo
    valorTier: true,         // (C) habilita o tier VALOR (cedo, mais variancia)
    valorMin: 75,            // (C) a partir de que minuto o VALOR pode aparecer
    valorMargin: 0.06,       // (C) prob exigida = probMin + isto (colchao maior que ARM)
    valorEdge: 0.08,         // (C) margem de valor minima: oddTela/justo >= 1+isto
    dutchTier: true,         // sinaliza TAMBEM quando cobrir as 2 opcoes for +EV (acende as 2 celulas + quanto em cada)
    dutchEvMin: 0,           // EV minimo (%) do Dutching p/ disparar (0 = qualquer lucro esperado pelo modelo)
    minOdd: 1.20,            // NAO sinaliza com odd abaixo disto: upside irrisorio (ex.: 1.03) e zona
                             // onde o modelo fica superconfiante (100%) -> evita sinal de lixo de fim de jogo
  };

  const COR = { WATCH:'#6aa0ff', VALOR:'#a06bff', DUTCH:'#00c2b8', ARM:'#ffb300', GREEN:'#2bd24f', STALE:'#e23b3b' };
  let timer = null;
  const ST = (window.__avState = window.__avState || {}); // estado por fixture

  // ---------------- cross-check SofaScore (opcional, via localhost) ----------
  const _RUIDO = new Set(['fc','cf','sc','ac','afc','cd','ca','club','clube',
    'calcio','ssd','ssc','us','as','if','sk','fk','bk','ik','il','de','do','da','the','fa']);
  function normalizarTime(n){            // espelha sofascore_live.normalizar
    if(!n) return '';
    let s=n.normalize('NFKD').replace(/[̀-ͯ]/g,'').toLowerCase();
    for(const ch of "._-/'") s=s.split(ch).join(' ');
    return s.split(/\s+/).filter(t=>t && !_RUIDO.has(t)).join(' ');
  }
  const ssCache = (window.__ssCache = window.__ssCache || {data:{}, ts:0, loading:false});
  function ssFetch(url, now){
    if(!url || ssCache.loading || (now - ssCache.ts) < (DEF.ssRefreshMs)) return;
    ssCache.loading = true;
    fetch(url, {cache:'no-store'}).then(r=>r.json()).then(d=>{
      ssCache.data = d || {}; ssCache.ts = Date.now(); ssCache.loading = false;
    }).catch(()=>{ ssCache.loading = false; });
  }
  function ssLookup(home, away){
    const k = normalizarTime(home)+'|'+normalizarTime(away);
    return ssCache.data[k] || (window.__ssCross && window.__ssCross[k]) || null;
  }
  function logarSinal(url, rec){              // POST best-effort pro servidor (Stage 3)
    if(!url) return;
    try{ fetch(url+'/log', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(rec)}).catch(()=>{}); }catch(e){}
  }
  // forca-time do FBref via servidor local (/forca), por nome; cache por jogo (muda devagar)
  const forcaCache = (window.__forcaCache = window.__forcaCache || {});
  function forcaLocal(url, casa, fora){
    if(!url) return null;
    const k = normalizarTime(casa)+'|'+normalizarTime(fora);
    if(k in forcaCache) return forcaCache[k];           // resolvido: obj {lam_casa,...} ou null
    forcaCache[k] = null;                                // em andamento -> nao re-busca
    fetch(url+'/forca?casa='+encodeURIComponent(casa)+'&fora='+encodeURIComponent(fora))
      .then(r=>r.json()).then(d=>{ forcaCache[k] = (d && d.lam_casa) ? d : null; }).catch(()=>{});
    return null;
  }
  // correcao de calibracao (loop E): mapa P_raw->P_calib do recalibrar_sinais.py, servido em /correcao.
  // Identidade ate ter dados (n_settled<40). Monotono -> nao muda o argmax (qual resultado domina).
  const corrCache = (window.__avCorr = window.__avCorr || {data:null, ts:0, loading:false});
  function corrFetch(url, now){
    if(!url || corrCache.loading || (corrCache.data && (now-corrCache.ts)<300000)) return;  // 5 min
    corrCache.loading=true;
    fetch(url+'/correcao', {cache:'no-store'}).then(r=>r.json()).then(d=>{
      corrCache.data=d||null; corrCache.ts=Date.now(); corrCache.loading=false;
    }).catch(()=>{ corrCache.loading=false; });
  }
  function aplicarCorrecao(p){
    const c=corrCache.data;
    if(!c || !c.metodo || c.metodo==='identidade') return p;
    if(((c._meta&&c._meta.n_settled)||0) < 40) return p;
    let q=p;
    if(c.metodo==='platt' && c.a!=null){ q=1/(1+Math.exp(Math.max(-30,Math.min(30, c.a*p+c.b)))); }
    else if(c.metodo==='isotonic' && c.pontos && c.pontos.length){
      const pts=c.pontos;
      if(p<=pts[0][0]) q=pts[0][1];
      else if(p>=pts[pts.length-1][0]) q=pts[pts.length-1][1];
      else for(let i=1;i<pts.length;i++){ if(p<=pts[i][0]){
        const x0=pts[i-1][0], y0=pts[i-1][1], x1=pts[i][0], y1=pts[i][1];
        q=y0+(y1-y0)*(p-x0)/((x1-x0)||1); break; } }
    }
    const cap=(c.cap!=null?c.cap:0.15);     // calibracao e nudge: nao afasta mais que isto do cru
    q=Math.max(p-cap, Math.min(p+cap, q));
    return Math.max(0.01, Math.min(0.99, q));
  }

  // ---------------- momentum pelo PAINEL do bet365 (jogo aberto, sem API/SofaScore) ----------
  // Pesos das metricas do painel (sem xG; "Ataques Perigosos" = o sinal que a casa usa).
  // pesos das stats reais do painel bet365 (mesma fonte das odds). Somam 1.
  // SÓ as rodas (ordem casa/fora confiavel). A barra de chutes vem com ordem
  // ambigua (inverte o sinal) -> fora. "Ataques Perigosos" e o sinal principal.
  const PAINEL_W = { "Ataques Perigosos":0.65, "Ataques":0.20, "% de Posse":0.15 };
  const SEL_LISTA = '.ovm-FixtureList, .ovm-SortedFixtureList';  // agrupada OU ordenada
  function _painelFixture(){            // o jogo ABERTO = .ovm-Fixture FORA de qualquer lista
    return [...document.querySelectorAll('.ovm-Fixture')].find(f => !f.closest(SEL_LISTA));
  }
  function lerPainel(){                  // {casa,fora,stats} do jogo aberto, ou null
    const fx=_painelFixture(); if(!fx) return null;
    const nomes=[...fx.querySelectorAll('[class*="TeamName"]')].map(txt).filter(Boolean);
    if(nomes.length<2) return null;
    const s={};
    document.querySelectorAll('.ml1-WheelChartAdvanced').forEach(w=>{  // rodas: so existem no painel
      const lab=txt(w.querySelector('.ml1-WheelChartAdvanced_Text'));
      const ns=[...w.querySelectorAll('*')].map(txt).filter(x=>/^\d+$/.test(x)).map(Number);
      if(lab&&ns.length>=2) s[lab]=[ns[0],ns[1]];
    });
    const sb=document.querySelector('.ml1-StatsLowerAdvanced_ShotsDualBar');
    if(sb){ const n=(txt(sb).match(/\d+/g)||[]).map(Number);
      if(n.length>=4){ s["Finalizacoes"]=[n[0],n[1]]; s["ChutesAoGol"]=[n[2],n[3]]; } }
    return { casa:nomes[0], fora:nomes[1], stats:s };
  }
  function momentumPainel(stats){        // {casa,fora} a partir das stats do painel; 1/1 se vazio
    let sw=0, ss=0;
    for(const k in PAINEL_W){ const v=stats[k];
      if(!v||(v[0]+v[1])<=0) continue;
      const sh=Math.max(0.15,Math.min(0.85, v[0]/(v[0]+v[1])));
      ss+=PAINEL_W[k]*sh; sw+=PAINEL_W[k];
    }
    if(sw===0) return {casa:1, fora:1, ph:0.5};
    const ph=ss/sw;
    return { casa:Math.max(0.6,Math.min(1.7,Math.exp(0.5*2*(ph-0.5)))),
             fora:Math.max(0.6,Math.min(1.7,Math.exp(0.5*2*(0.5-ph)))), ph:+ph.toFixed(3) };
  }
  function lerAcrescimoPainel(){         // (D) "90+N" real do relogio do PAINEL (jogo aberto)
    const fx=_painelFixture(); if(!fx) return null;
    const t = txt(fx.querySelector('.ovm-InPlayTimer')) || '';
    const m = t.match(/90\s*\+\s*(\d+)/);   // so 2T; quem chama ja garante tot>=90
    return m ? +m[1] : null;
  }

  // ---------------- modelo (porta do prob_aovivo.py) ----------------
  const fatorial = n => { let f=1; for (let i=2;i<=n;i++) f*=i; return f; };
  const pois = (k,l) => l<=0 ? (k===0?1:0) : Math.exp(-l)*Math.pow(l,k)/fatorial(k);
  const clamp = (v,lo,hi) => Math.max(lo, Math.min(hi, v));

  function lamRegular(m) {
    if (m >= 90) return 0;
    let s = 0;
    for (let b=0;b<CAL.h_reg_buckets.length;b++) {
      const lo=5*b, hi=5*b+5, ov=Math.min(90,hi)-Math.max(m,lo);
      if (ov>0) s += CAL.h_reg_buckets[b]*ov;
    }
    return s;
  }
  function mults(gc,gf,m,restante,momC,momF,redC,redF) {
    const mu=CAL.mult, rd=CAL.red;
    const d=gc-gf, push=0.5+0.5*(Math.min(m,90)/90), t=Math.tanh(d/mu.d0);
    let msC=1+mu.sigma*(-t*push), msF=1+mu.sigma*(t*push);
    const net=(redC||0)-(redF||0), neff=Math.abs(net)*Math.min(1,Math.max(0,restante)/30);
    let mrC=1, mrF=1;
    if (net>0){ mrC=Math.pow(rd.down,neff); mrF=Math.pow(rd.up,neff); }
    else if (net<0){ mrC=Math.pow(rd.up,neff); mrF=Math.pow(rd.down,neff); }
    return [clamp((momC||1)*msC*mrC,mu.clampLo,mu.clampHi),
            clamp((momF||1)*msF*mrF,mu.clampLo,mu.clampHi)];
  }
  function convolui(gc,gf,lc,lf,kmax=8){
    let pc=0,pe=0,pf=0;
    for(let a=0;a<=kmax;a++){ const pa=pois(a,lc);
      for(let b=0;b<=kmax;b++){ const pb=pois(b,lf); const fc=gc+a,ff=gf+b;
        if(fc>ff)pc+=pa*pb; else if(fc<ff)pf+=pa*pb; else pe+=pa*pb; }}
    const s=pc+pe+pf||1; return [pc/s,pe/s,pf/s];
  }
  function blendForca(forca){ // -> [r_eff, sh_eff]; sem forca -> [1, home_share] (cai no global)
    const sh0=CAL.home_share; let r=1, sh=sh0;
    if(forca && forca.lam_casa && forca.lam_fora && forca.n>=CAL.forca.minJogos){
      const lcf=forca.lam_casa, laf=forca.lam_fora, tot=lcf+laf;
      if(tot>0){ const w=Math.min(CAL.forca.wMax, forca.n/(forca.n+CAL.forca.k));
        r = 1 + w*((tot/CAL.gols_por_jogo)-1);
        sh = sh0 + w*((lcf/tot)-sh0); }
    }
    return [r, Math.min(0.95,Math.max(0.05,sh))];
  }
  function probs(m,gc,gf,A,o){ // o = {momC,momF,redC,redF,escala,forca}
    const lreg=lamRegular(m), elapsed=Math.max(0,m-90);
    const [rEff,shEff]=blendForca(o.forca);
    const [Mc,Mf]=mults(gc,gf,m,Math.max(0,90-m)+A,o.momC,o.momF,o.redC,o.redF);
    const pmf=CAL.stoppage_extra_pmf; let pc=0,pe=0,pf=0,w=0;
    for(let x=0;x<pmf.length;x++){
      const S=A+x, remStop=Math.max(0,S-elapsed);
      const lt=(lreg+CAL.h_stop_2h*remStop)*(o.escala||1)*rEff;
      const [a,e,f]=convolui(gc,gf,lt*shEff*Mc,lt*(1-shEff)*Mf);
      pc+=pmf[x]*a; pe+=pmf[x]*e; pf+=pmf[x]*f; w+=pmf[x];
    }
    return w>0?[pc/w,pe/w,pf/w]:[pc,pe,pf];
  }
  function dominante(gc,gf,pc,pe,pf){ // -> {lab,prob,idx} (idx: 0=1,1=X,2=2)
    const c=[['CASA',pc,0],['EMPATE',pe,1],['FORA',pf,2]].sort((a,b)=>b[1]-a[1]);
    return {lab:c[0][0],prob:c[0][1],idx:c[0][2]};
  }
  function mantemProb(gc,gf,pc,pe,pf){ return gc>gf?pc : gc<gf?pf : pe; }
  // (B) banda de confianca — porta de prob_aovivo.banda_confianca: escala Λ por exp(±sigma_log).
  // sigma_log cresce quando falta momentum e quando o multiplicador e forte -> banda mais larga.
  function bandaSigmaLog(o,Mc,Mf){
    const sl=CAL.sigma_log; let s=sl.base;
    if((o.momC||1)===1 && (o.momF||1)===1) s+=(sl.sem_stats||0);
    if(Math.abs(Math.log(Mc||1))>0.2 || Math.abs(Math.log(Mf||1))>0.2) s+=(sl.por_mult||0);
    return s;
  }
  function bandaProbsIdx(m,gc,gf,A,o,idx){   // -> [pLo,pHi] do resultado idx (0=1,1=X,2=2)
    const [Mc,Mf]=mults(gc,gf,m,Math.max(0,90-m)+A,o.momC,o.momF,o.redC,o.redF);
    const s=bandaSigmaLog(o,Mc,Mf);
    const lo=probs(m,gc,gf,A,Object.assign({},o,{escala:(o.escala||1)*Math.exp(+s)}));  // Λ maior -> P menor
    const hi=probs(m,gc,gf,A,Object.assign({},o,{escala:(o.escala||1)*Math.exp(-s)}));
    return [lo[idx], hi[idx]];
  }
  // Dutching: cobre os 2 resultados de MENOR odd, dividindo `stake` pra retorno igual
  // se qualquer um dos 2 sair. Voce PERDE tudo se sair o 3o. Por isso o que importa
  // NAO e "lucro se cobrir", e o EV REAL = (1 - P_excluido)*retorno - stake (P do modelo).
  function dutch(odds, p3, stake){          // odds=[o1,oX,o2], p3=[pCasa,pEmpate,pFora]
    const LAB=['CASA','EMPATE','FORA'];
    if(odds.filter(o=>o!=null).length<3) return null;
    const ord=[0,1,2].sort((a,b)=>odds[a]-odds[b]);
    const a=ord[0], b=ord[1], ex=ord[2];
    const inv=1/odds[a]+1/odds[b], ret=stake/inv, pExcl=p3[ex];
    return { a:{lab:LAB[a], idx:a, stake:stake*(1/odds[a])/inv},
             b:{lab:LAB[b], idx:b, stake:stake*(1/odds[b])/inv},
             excl:LAB[ex], exclIdx:ex, pExcl, ret, seCobrePct:(ret/stake-1)*100,
             evPct:((1-pExcl)*ret/stake-1)*100 };   // EV real (negativo = nao vale)
  }

  // ---------------- parsing DOM ----------------
  const txt = e => (e&&typeof e.innerText==='string')?e.innerText.trim():'';
  const num = o => { const n=parseFloat(String(o).replace(',','.')); return isNaN(n)?null:n; };
  function parseRelogio(s){ const m=String(s||'').match(/(\d+):(\d+)/); // MM:SS
    return m ? {min:+m[1], seg:+m[2], tot:+m[1]+(+m[2])/60} : null; }
  function lerAcrescimo(fx){ // tenta "+N" no DOM da fixture (raro no overview)
    for(const e of fx.querySelectorAll('*')){ if(e.children.length) continue;
      const t=txt(e); const m=t.match(/^\+\s?(\d+)/); if(m) return +m[1]; }
    return null;
  }
  // (A) cartao vermelho por lado. Layout bet365: casa=linha de cima, fora=linha de baixo;
  // separa o marcador pela posicao vertical (top) relativa aos 2 TeamName. Defensivo:
  // se nao da p/ separar com confianca, devolve 0/0 (= comportamento atual, nunca chuta lado).
  function lerVermelhos(fx){
    const tn=[...fx.querySelectorAll('[class*="TeamName"]')];
    if(tn.length<2 || !fx.getBoundingClientRect) return {redC:0,redF:0};
    const yc=tn[0].getBoundingClientRect().top, yf=tn[1].getBoundingClientRect().top;
    if(yc===yf) return {redC:0,redF:0};
    const meio=(yc+yf)/2, casaEmCima=yc<yf;
    const lado=(el)=>{ const r=el.getBoundingClientRect&&el.getBoundingClientRect();
      if(!r||!r.height) return null; return ((r.top<=meio)===casaEmCima)?'c':'f'; };
    const sig=(el)=>{ const cls=(el.className&&el.className.baseVal!==undefined)?el.className.baseVal:(''+(el.className||''));
      const al=((el.getAttribute&&(el.getAttribute('aria-label')||el.getAttribute('title')))||'');
      return /red.?card|card.?red|redcard/i.test(cls) || /red card|cart[aã]o vermelho|expuls/i.test(al); };
    let rc=0, rf=0; const vistos=new Set();
    const add=(el,n)=>{ const L=lado(el); if(L==='c') rc+=n; else if(L==='f') rf+=n; };
    fx.querySelectorAll('[class],[aria-label],[title]').forEach(el=>{
      if(vistos.has(el)||!sig(el)) return; vistos.add(el); add(el,1); });
    fx.querySelectorAll('*').forEach(el=>{ if(el.children.length||vistos.has(el)) return;
      if(el.closest('[class*="TeamName"]')) return;
      if([...vistos].some(v=>v.contains&&v.contains(el))) return;   // ja contado via ancestral -> nao duplica
      const n=((el.textContent||'').match(/🟥/g)||[]).length; if(n){ vistos.add(el); add(el,n); } });
    return {redC:Math.min(2,rc), redF:Math.min(2,rf)};
  }

  // ---------------- freshness ----------------
  function freshness(st, relogio, oddsCount, A, cfg, ss){
    const teto = (A!=null) ? 90+A+4 : 98;
    if (relogio && relogio.tot > teto) return {ok:false, strict:false, stale:'TETO'};
    // cross-check SofaScore (se disponivel)
    if (ss){
      if (ss.status==='finished') return {ok:false, strict:false, stale:'SS-FIM'};
      if (relogio && ss.min!=null && Math.abs(relogio.tot - ss.min) > cfg.clockSkewTol)
        return {ok:true, strict:false, stale:null, ssOk:false};
    }
    // congelado? compara segundos com o ultimo scan
    let advanced=true;
    if (st && st.lastTot!=null){
      if (relogio && relogio.tot === st.lastTot){
        st.stall = (st.stall||0)+1; advanced=false;
        if (st.stall >= cfg.stallScans) return {ok:false, strict:false, stale:'CONGELADO'};
      } else st.stall=0;
    }
    const mercadoOk = oddsCount>=3;
    return {ok:true, strict: advanced && mercadoOk && (!ss || ss.status!=='finished'),
            stale:null, ssOk: ss?true:undefined};
  }

  // ---------------- UX ----------------
  function ensureCSS(){
    if (document.getElementById('__avCSS')) return;
    const s=document.createElement('style'); s.id='__avCSS';
    s.textContent='@keyframes avPulse{0%{box-shadow:0 0 6px '+COR.GREEN+'aa}'
      +'50%{box-shadow:0 0 18px '+COR.GREEN+'}100%{box-shadow:0 0 6px '+COR.GREEN+'aa}}'
      +'.__avGreen{animation:avPulse 1s infinite}';
    document.head.appendChild(s);
  }
  function limpaCels(fx){ fx.querySelectorAll('.ovm-ParticipantOddsOnly').forEach(c=>{c.style.outline='';c.style.boxShadow='';}); }
  function limpa(fx){
    if(!fx.__avPainted) return;                      // PERF: nunca pintada -> nada a limpar
    fx.__avPainted=false;
    fx.style.outline=''; fx.style.boxShadow=''; fx.classList.remove('__avGreen');
    limpaCels(fx); const b=fx.querySelector('.__avBadge'); if(b)b.remove(); }
  function pinta(fx, cor, pulse, texto, cellIdx){
    fx.style.outline='3px solid '+cor; fx.style.outlineOffset='-3px'; fx.style.position='relative';
    fx.classList.toggle('__avGreen', !!pulse);
    if(!pulse) fx.style.boxShadow='0 0 12px '+cor+'aa';
    limpaCels(fx);                                   // acende a CELULA exata p/ clicar (ARM/GREEN)
    const cells=fx.querySelectorAll('.ovm-ParticipantOddsOnly');
    const idxs = Array.isArray(cellIdx) ? cellIdx : (cellIdx>=0 ? [cellIdx] : []);
    idxs.forEach(ci=>{ if(cells[ci]){ cells[ci].style.outline='3px solid '+cor;
      cells[ci].style.boxShadow='inset 0 0 16px '+cor; } });
    let b=fx.querySelector('.__avBadge');
    if(!b){ b=document.createElement('div'); b.className='__avBadge';
      b.style.cssText='position:absolute;top:2px;left:2px;z-index:9;font:bold 10px sans-serif;'
        +'padding:1px 5px;border-radius:3px;color:#000;pointer-events:none;white-space:nowrap';
      fx.prepend(b); }
    b.style.background=cor; b.innerHTML=String(texto).replace(/\n/g,'<br>');  // suporta 2 linhas (Dutch)
    fx.__avPainted=true;                             // marca p/ o limpa() só agir no que foi pintado
  }

  // ---------------- scan ----------------
  function scan(cfg, now){
    let cont={IDLE:0,WATCH:0,VALOR:0,DUTCH:0,ARM:0,GREEN:0,STALE:0};
    ssFetch(cfg.ssUrl, now);                 // atualiza o cross do SofaScore (async)
    corrFetch(cfg.ssUrl, now);               // (E) atualiza o mapa de correcao de calibracao
    const pn = cfg.painel ? lerPainel() : null;          // momentum do jogo ABERTO no painel
    const pnMom = pn ? momentumPainel(pn.stats) : null;
    const pnC = pn ? normalizarTime(pn.casa) : '', pnF = pn ? normalizarTime(pn.fora) : '';
    // TRAVA DE MERCADO: o modelo so vale p/ "Resultado Final" (1/X/2). Em "Proximo Gol"
    // ou "Partida - Gols" as 3 odds significam outra coisa -> nao sinaliza (evita erro).
    const tabAtiva=((document.querySelector('.ovm-ClassificationMarketSwitcherMenu_Item-active')||{}).textContent||'').trim();
    if(tabAtiva && !/Resultado Final/i.test(tabAtiva)){
      document.querySelectorAll('.ovm-Fixture').forEach(limpa);
      barra(cont, cfg, 'mercado "'+tabAtiva+'" — troque p/ "Resultado Final"');
      return;
    }
    document.querySelectorAll('.ovm-Fixture').forEach(fx=>{        // agrupada OU ordenada
      try {                                                         // blindagem: 1 fixture com erro
                                                                    // NUNCA derruba o scan inteiro
        if(!fx.closest(SEL_LISTA)) return;                          // pula o jogo aberto (painel)
        const comp=fx.closest('.ovm-Competition');
        const liga=comp?(txt(comp.querySelector('.ovm-CompetitionHeader'))||'').split('\n')[0].trim():'';
        const nomes=[...fx.querySelectorAll('[class*="TeamName"]')].map(txt).filter(Boolean);
        const key=liga+'|'+nomes.join('|');
        // guard por-jogo: as vezes a fixture troca o 1X2 por "Marcar o Xo Gol" /
        // "Proximo Gol". Ai as 3 odds NAO sao 1/X/2 -> ignora (nao sinaliza errado).
        if(/Marcar o\s*\d|Pr[oó]ximo Gol/i.test(fx.textContent||'')){ limpa(fx); cont.IDLE++; return; }
        // relogio/placar via innerText (comportamento original, robusto ao layout
        // do bet365 — textContent quebrava o parse do relogio em alguns renders).
        const relogio=parseRelogio((fx.querySelector('.ovm-InPlayTimer')||{}).innerText);
        const pl=[...fx.querySelectorAll('.ovm-ScorePill')].map(e=>parseInt(e.innerText,10));

        if(!relogio || pl.length<2 || relogio.tot<cfg.watchMin){ limpa(fx); cont.IDLE++;
          if(ST[key]) ST[key].lastTot = relogio?relogio.tot:ST[key].lastTot; return; }
        // PERF: só lê odds (e roda o modelo) dos jogos na janela ativa (>=watchMin).
        // A maioria dos jogos ao vivo está abaixo disso e já saiu acima, sem custo.
        const odds=[...fx.querySelectorAll('.ovm-ParticipantOddsOnly')].map(e=>num(e.innerText));
        const oddsCount=odds.filter(v=>v!=null).length;

        const st = ST[key] = ST[key] || {};
        st.tick = (st.tick||0)+1;
        // PERF: lerAcrescimo/lerVermelhos varrem a subárvore inteira (caro). Recalcula
        // a cada scan só na janela crítica (>=88', poucos jogos); antes, a cada 4 scans
        // (acréscimo e cartão mudam devagar — cache não muda o sinal na prática).
        const recomputarPesado = relogio.tot>=88 || (st.tick%4)===1;
        const ss = ssLookup(nomes[0], nomes[1]);
        let A;
        if(ss && ss.injury!=null){ A = ss.injury; }
        else { if(recomputarPesado || st.Acache===undefined) st.Acache = lerAcrescimo(fx); A = st.Acache; }
        // (D) se for o jogo ABERTO no painel e ja passou de 90', le o "90+N" real do painel
        if(A==null && pn && relogio.tot>=90 && pnC===normalizarTime(nomes[0]) && pnF===normalizarTime(nomes[1]))
          A = lerAcrescimoPainel();
        const fr = freshness(st, relogio, oddsCount, A, cfg, ss);

        if(fr.stale){ limpa(fx); pinta(fx, COR.STALE, false, 'STALE '+fr.stale, -1);
          st.lastTot=relogio.tot; st.green=0; st.dutch=0; cont.STALE++; return; }

        // modelo
        // momentum: painel do bet365 (jogo aberto) tem prioridade; senao SofaScore; senao 1.0
        let momC=(ss&&ss.mom)?ss.mom.casa:1, momF=(ss&&ss.mom)?ss.mom.fora:1;
        if(pnMom && pnC===normalizarTime(nomes[0]) && pnF===normalizarTime(nomes[1])){
          momC=pnMom.casa; momF=pnMom.fora;
        }
        // (A) cartao vermelho: lido do DOM (auditavel no badge 🟥); fallback SofaScore
        let redC=0, redF=0;
        if(cfg.vermelho){
          if(recomputarPesado || st.redCache===undefined) st.redCache = lerVermelhos(fx);
          redC=st.redCache.redC; redF=st.redCache.redF;
          if(!redC && !redF && ss && ss.redC!=null){ redC=ss.redC; redF=ss.redF; } }
        const forca = forcaLocal(cfg.ssUrl, nomes[0], nomes[1]) || (ss?ss.forca:null);  // FBref local > SofaScore
        const Aeff = A!=null?A:cfg.unknownStoppageFloor;
        const oModel = {momC, momF, redC, redF, forca};
        const [pc,pe,pf]=probs(relogio.tot, pl[0], pl[1], Aeff, oModel);
        const dom=dominante(pl[0],pl[1],pc,pe,pf);
        // (B) banda de confianca p/ o resultado dominante; (E) correcao de calibracao (monotona)
        let pLo=dom.prob, pHi=dom.prob;
        if(cfg.usarBanda){ const bb=bandaProbsIdx(relogio.tot, pl[0], pl[1], Aeff, oModel, dom.idx);
          pLo=bb[0]; pHi=bb[1]; }
        const domProb=aplicarCorrecao(dom.prob); pLo=aplicarCorrecao(pLo); pHi=aplicarCorrecao(pHi);
        const be=1/domProb, oddTela=odds[dom.idx];
        const valor=(oddTela!=null)&&(oddTela>be);
        const mercadoOk=oddsCount>=3 && (!cfg.exigeOddAtiva || oddTela!=null);
        const gateProb=cfg.usarBanda?pLo:domProb;   // (B) gating pelo limite inferior se banda ON

        // tiers
        const greenMin = (A!=null) ? 90+Math.max(0,A-cfg.greenBuffer) : 90+cfg.unknownStoppageFloor;
        const greenCeil = (A!=null) ? 90+A+cfg.greenOvershoot : 90+cfg.hardCeiling;
        const noSurge = (st.lastScore===undefined || st.lastScore===pl.join('-'))
                        && (st.lastOdds===undefined || !(st.lastOdds<3 && oddsCount>=3)); // sem flap
        const valeOdd = oddTela!=null && oddTela>=cfg.minOdd;   // upside minimo p/ valer a pena
        const armOk = relogio.tot>=cfg.armMin && gateProb>=cfg.probMin && valor && valeOdd && mercadoOk && fr.ok;
        const greenCond = armOk && relogio.tot>=greenMin && relogio.tot<=greenCeil
                          && fr.strict && noSurge && mercadoOk;
        st.green = greenCond ? (st.green||0)+1 : 0;
        const isGreen = st.green>=cfg.confirmScans;
        // (C) tier VALOR — cedo, mais variancia: so ANTES da janela ARM, exige colchao + margem de valor
        const edgeOk = (oddTela!=null) && be>0 && (oddTela/be)>=(1+cfg.valorEdge);
        const valorOk = cfg.valorTier && !armOk && !isGreen && relogio.tot>=cfg.valorMin
                        && gateProb>=(cfg.probMin+cfg.valorMargin) && edgeOk && valeOdd && mercadoOk && fr.ok;

        // DUTCHING como SINAL proprio: cobrir as 2 opcoes de MENOR odd quando isso
        // for +EV pelo modelo. Usa a banda PESSIMISTA no resultado excluido (mais
        // provavel do que o ponto) p/ nao sobre-sinalizar. Acende as 2 celulas.
        let dt=null;
        if(cfg.dutch || cfg.dutchTier){
          dt = dutch(odds, [pc,pe,pf], cfg.stakeTotal);
          if(dt){
            let pExclEff = dt.pExcl;
            if(cfg.usarBanda){ const bx=bandaProbsIdx(relogio.tot, pl[0], pl[1], Aeff, oModel, dt.exclIdx);
              pExclEff = aplicarCorrecao(bx[1]); }      // limite SUPERIOR do excluido = cenario pessimista
            dt.evEff = ((1-pExclEff)*dt.ret/cfg.stakeTotal - 1)*100;
          }
        }
        const dutchGate = cfg.dutchTier && dt && relogio.tot>=cfg.armMin
                          && dt.evEff>=cfg.dutchEvMin && mercadoOk && fr.ok && noSurge;
        st.dutch = (dutchGate && !isGreen) ? (st.dutch||0)+1 : 0;
        const isDutch = st.dutch>=cfg.confirmScans;

        let tier = isGreen?'GREEN' : isDutch?'DUTCH' : armOk?'ARM' : valorOk?'VALOR' : 'WATCH';
        const tela = oddTela!=null?oddTela.toFixed(2):'susp';
        const aTxt = A!=null?('+'+A):'+?';
        const motivo = !mercadoOk?' SUSPENSO' : (!noSurge?' GOL?' : '');
        const banda = cfg.usarBanda?` [${(pLo*100).toFixed(0)}–${(pHi*100).toFixed(0)}%]`:'';
        const reds = (redC||redF)?` 🟥${redC}-${redF}`:'';
        const linhaDutch = dt ? (()=>{               // "quanto jogar em cada" — sempre que houver plano
            const pA=Math.round(dt.a.stake/cfg.stakeTotal*100), pB=Math.round(dt.b.stake/cfg.stakeTotal*100);
            const lucroCob=dt.ret-cfg.stakeTotal;
            return `\n💰 R$${dt.a.stake.toFixed(2)} ${dt.a.lab} (${pA}%) + R$${dt.b.stake.toFixed(2)} ${dt.b.lab} (${pB}%)`
              + ` → volta R$${dt.ret.toFixed(2)} de R$${cfg.stakeTotal} = ${lucroCob>=0?'LUCRO +':'PERDE '}R$${lucroCob.toFixed(2)} se cobrir`
              + ` · EV ${dt.evEff>=0?'+':''}${dt.evEff.toFixed(1)}%${dt.evEff>0?'✅':''} · perde tudo se ${dt.excl}`;
          })() : '';
        let txtBadge, cellsToPaint;
        if(tier==='DUTCH'){                            // sinal das DUAS opcoes
          txtBadge = `DUTCH ✅ cobre ${dt.a.lab}+${dt.b.lab} · EV +${dt.evEff.toFixed(1)}% | ${aTxt}${reds}` + linhaDutch;
          cellsToPaint = [dt.a.idx, dt.b.idx];
        } else {                                       // sinal de UMA opcao (dominante)
          txtBadge = `${tier}${tier==='ARM'?motivo:''} ${dom.lab} ${(domProb*100).toFixed(0)}%${banda}`
                     + ` | just ${be.toFixed(2)} | tela ${tela} ${valor?'✓':'✗'} | ${aTxt}${reds}`;
          if(tier==='VALOR') txtBadge += ` · valor +${((oddTela/be-1)*100).toFixed(0)}% (cedo, +variância)`;
          if(tier==='WATCH' && gateProb>=cfg.probMin && valor && !valeOdd)
            txtBadge += ` · odd ${tela}<${cfg.minOdd} (sem upside, nao sinaliza)`;  // explica por que ficou em WATCH
          if(cfg.dutch && (tier==='ARM'||tier==='GREEN')) txtBadge += linhaDutch;   // dutch como info auxiliar
          cellsToPaint = tier==='WATCH'?-1:dom.idx;
        }
        pinta(fx, COR[tier], isGreen||isDutch, txtBadge, cellsToPaint);
        if(isGreen && st.green===cfg.confirmScans){          // transicao p/ GREEN (1 opcao) -> loga
          beep();
          logarSinal(cfg.ssUrl, {liga, casa:nomes[0], fora:nomes[1],
            event_id: ss?ss.event_id:null, minuto:+relogio.tot.toFixed(1),
            placar:pl.join('-'), resultado:dom.lab, prob:+dom.prob.toFixed(4),
            breakeven:+be.toFixed(3), odd_tela:oddTela, acrescimo:A});
        } else if(isDutch && st.dutch===cfg.confirmScans){   // transicao p/ DUTCH (2 opcoes) -> beep
          beep();                                            // sem log: settlement do dutch != 1X2 do sinal_log
        }
        cont[tier]++;

        st.lastTot=relogio.tot; st.lastScore=pl.join('-'); st.lastOdds=oddsCount;
      } catch(e){ if(!window.__avErr){ window.__avErr=e;       // loga 1x; nao trava o scan
          console.warn('alertador: erro numa fixture (ignorada) —', e); } }
    });
    barra(cont, cfg);
  }

  function beep(){ try{ const a=new (window.AudioContext||window.webkitAudioContext)();
    const o=a.createOscillator(); o.connect(a.destination); o.frequency.value=920;
    o.start(); setTimeout(()=>o.stop(),200);}catch(e){} }

  function barra(c,cfg,aviso){ let bar=document.getElementById('__avBar');
    if(!bar){ bar=document.createElement('div'); bar.id='__avBar';
      bar.style.cssText='position:fixed;bottom:10px;right:10px;z-index:99999;background:#111;'
        +'color:#fff;border:1px solid #444;font:bold 12px sans-serif;padding:8px 12px;border-radius:6px;line-height:1.4';
      // linha de status (atualizada a cada scan)
      const status=document.createElement('div'); status.id='__avBarStatus';
      // linha de AJUSTES (criada UMA vez — nao reescreve a cada scan, senao perde o foco/valor)
      const row=document.createElement('div'); row.id='__avBarCfg';
      row.style.cssText='margin-top:6px;padding-top:6px;border-top:1px solid #333;font-weight:normal;'
        +'display:flex;gap:10px;align-items:center;flex-wrap:wrap';
      const mkInput=(val,w,title)=>{ const i=document.createElement('input');
        i.type='number'; i.value=val; i.title=title||'';
        i.style.cssText='width:'+w+';background:#222;color:#fff;border:1px solid #555;border-radius:3px;'
          +'padding:1px 4px;font:bold 11px sans-serif;text-align:right'; return i; };
      // 💰 valor total p/ Dutching
      const sStake=document.createElement('span'); sStake.style.cssText='display:inline-flex;align-items:center;gap:3px';
      const iStake=mkInput(cfg.stakeTotal,'52px','Valor total p/ o Dutching. As % valem p/ QUALQUER valor.');
      iStake.min='1'; iStake.step='5';
      iStake.onchange=()=>{ const v=parseFloat(iStake.value); if(v>0){ cfg.stakeTotal=v; saveLS(cfg); } else iStake.value=cfg.stakeTotal; };
      sStake.innerHTML='💰 R$'; sStake.appendChild(iStake);
      // ⏱ a partir de que minuto avisa (menor = mais cedo = mais tempo)
      const sArm=document.createElement('span'); sArm.style.cssText='display:inline-flex;align-items:center;gap:3px';
      const iArm=mkInput(cfg.armMin,'38px','A partir de que minuto pode avisar. Menor = mais cedo = mais tempo (88 normal, 84 mais cedo).');
      iArm.min='70'; iArm.max='95'; iArm.step='1';
      iArm.onchange=()=>{ let v=parseInt(iArm.value,10); if(!(v>=70&&v<=95)){ iArm.value=cfg.armMin; return; }
        cfg.armMin=v; cfg.watchMin=Math.min(DEF.watchMin,v); cfg.greenBuffer=v<88?2.5:1.0; cfg.unknownStoppageFloor=v<88?1:2; saveLS(cfg); };
      sArm.innerHTML='⏱ avisa ≥'; sArm.appendChild(iArm); sArm.appendChild(document.createTextNode("'"));
      // ✓ banda — gating pelo cenario pessimista (B)
      const sBanda=document.createElement('span'); sBanda.style.cssText='display:inline-flex;align-items:center;gap:3px';
      const iBanda=document.createElement('input'); iBanda.type='checkbox'; iBanda.checked=!!cfg.usarBanda;
      iBanda.title='Trava pelo limite INFERIOR da banda — mais preciso; exige colchao maior quanto mais cedo.';
      iBanda.style.accentColor='#a06bff';
      iBanda.onchange=()=>{ cfg.usarBanda=iBanda.checked; saveLS(cfg); };
      sBanda.appendChild(iBanda); sBanda.appendChild(document.createTextNode('banda'));
      // 🎯 margem do tier VALOR cedo (C), em pontos %
      const sVal=document.createElement('span'); sVal.style.cssText='display:inline-flex;align-items:center;gap:3px';
      const iVal=mkInput(Math.round(cfg.valorMargin*100),'34px','Tier VALOR cedo: prob exigida = probMin + isto (pontos %). Maior = mais exigente.');
      iVal.min='0'; iVal.max='25'; iVal.step='2';
      iVal.onchange=()=>{ let v=parseInt(iVal.value,10); if(!(v>=0&&v<=25)){ iVal.value=Math.round(cfg.valorMargin*100); return; }
        cfg.valorMargin=v/100; saveLS(cfg); };
      sVal.innerHTML='🎯 +'; sVal.appendChild(iVal); sVal.appendChild(document.createTextNode('pp'));
      // 🏷 odd minima p/ sinalizar (filtra os spots de upside irrisorio, ex.: 1.03)
      const sMin=document.createElement('span'); sMin.style.cssText='display:inline-flex;align-items:center;gap:3px';
      const iMin=mkInput(cfg.minOdd,'42px','Odd minima p/ sinalizar. Abaixo disto o upside e irrisorio (ex.: 1.20). Mata sinal de lixo de fim de jogo.');
      iMin.min='1'; iMin.max='5'; iMin.step='0.05';
      iMin.onchange=()=>{ const v=parseFloat(iMin.value); if(v>=1){ cfg.minOdd=v; saveLS(cfg); } else iMin.value=cfg.minOdd; };
      sMin.innerHTML='🏷 odd≥'; sMin.appendChild(iMin);
      // ↺ volta ao padrao
      const reset=document.createElement('span'); reset.textContent='↺ padrão';
      reset.style.cssText='color:#6aa0ff;cursor:pointer;font-size:11px';
      reset.title='Limpa os ajustes salvos e volta ao padrao';
      reset.onclick=()=>{ try{localStorage.removeItem(LS_KEY);}catch(e){}
        cfg.stakeTotal=DEF.stakeTotal; cfg.armMin=DEF.armMin; cfg.watchMin=DEF.watchMin;
        cfg.greenBuffer=DEF.greenBuffer; cfg.unknownStoppageFloor=DEF.unknownStoppageFloor;
        cfg.usarBanda=DEF.usarBanda; cfg.valorMargin=DEF.valorMargin; cfg.minOdd=DEF.minOdd;
        iStake.value=DEF.stakeTotal; iArm.value=DEF.armMin;
        iBanda.checked=DEF.usarBanda; iVal.value=Math.round(DEF.valorMargin*100); iMin.value=DEF.minOdd; };
      row.appendChild(sStake); row.appendChild(sArm); row.appendChild(sBanda);
      row.appendChild(sVal); row.appendChild(sMin); row.appendChild(reset);
      bar.appendChild(status); bar.appendChild(row);
      document.body.appendChild(bar); }
    let status=bar.querySelector('#__avBarStatus');
    if(!status){ bar.remove(); return barra(c,cfg,aviso); }   // barra velha (reinjecao s/ F5) -> reconstroi
    const corr = (corrCache.data && corrCache.data.metodo && corrCache.data.metodo!=='identidade'
                  && ((corrCache.data._meta&&corrCache.data._meta.n_settled)||0)>=40) ? ' · calib✓' : '';
    const linha2 = aviso
      ? `<span style="color:${COR.STALE}">⚠️ ${aviso}</span>`
      : `arm≥${cfg.armMin}' prob≥${cfg.probMin}${cfg.usarBanda?' (banda)':''}${corr} · SofaScore: `
        + (cfg.ssUrl ? (Object.keys(ssCache.data).length+' jogos') : 'off');
    status.innerHTML=`<span style="color:#33d17a;font-weight:bold">${VERSAO}</span>  `
      +`<span style="color:${COR.GREEN}">GREEN ${c.GREEN}</span> · `
      +`<span style="color:${COR.ARM}">ARM ${c.ARM}</span> · `
      +`<span style="color:${COR.DUTCH}">DUTCH ${c.DUTCH}</span> · `
      +`<span style="color:${COR.VALOR}">VALOR ${c.VALOR}</span> · `
      +`<span style="color:${COR.WATCH}">WATCH ${c.WATCH}</span> · `
      +`<span style="color:${COR.STALE}">STALE ${c.STALE}</span><br>`+linha2;
  }

  // ---------------- prefs do usuario (salvas no navegador, sobrevivem ao F5) --
  // O valor (R$) e o tempo (a partir de que minuto avisa) sao editaveis na
  // BARRINHA — nao precisa mexer no codigo nem regerar o bookmarklet.
  const LS_KEY = '__avCfg';
  function loadLS(){
    try{ const o=JSON.parse(localStorage.getItem(LS_KEY)||'{}'); const r={};
      if(o.stakeTotal>0) r.stakeTotal=+o.stakeTotal;
      if(o.armMin>=70 && o.armMin<=95){ r.armMin=+o.armMin;       // menor = avisa mais cedo
        r.watchMin = Math.min(DEF.watchMin, r.armMin);            // WATCH tem que descer junto, senao bloqueia
        r.greenBuffer = r.armMin<88 ? 2.5 : 1.0;                  // GREEN tambem entra mais cedo
        r.unknownStoppageFloor = r.armMin<88 ? 1 : 2; }
      if(typeof o.usarBanda==='boolean') r.usarBanda=o.usarBanda;        // (B) toggle da banda
      if(o.valorMargin>=0 && o.valorMargin<=0.25) r.valorMargin=+o.valorMargin;  // (C) margem VALOR
      if(o.minOdd>=1 && o.minOdd<=5) r.minOdd=+o.minOdd;                  // filtro de odd minima
      return r;
    }catch(e){ return {}; }
  }
  function saveLS(cfg){
    try{ localStorage.setItem(LS_KEY, JSON.stringify(
      {stakeTotal:cfg.stakeTotal, armMin:cfg.armMin, usarBanda:cfg.usarBanda,
       valorMargin:cfg.valorMargin, minOdd:cfg.minOdd})); }catch(e){}
  }

  // ---------------- API ----------------
  window.iniciarValor = function(over){
    const cfg=Object.assign({}, DEF, over||{}, loadLS());   // prefs da tela vencem o baked
    window.pararValor();
    ensureCSS();
    const tick=()=>scan(cfg, Date.now());
    tick(); timer=setInterval(tick, cfg.intervaloMs);
    console.log('%cAlertador v2 ON','color:#2bd24f;font-weight:bold', cfg);
    console.log('GREEN = fundo nos acrescimos + criterios + relogio fresco. NAO e "faltam 40s". Confira o jogo antes de clicar.');
    return cfg;
  };
  window.pararValor = function(){
    if(timer)clearInterval(timer); timer=null;
    document.querySelectorAll('.ovm-Fixture').forEach(limpa);
    const bar=document.getElementById('__avBar'); if(bar)bar.remove();
    for(const k in ST) delete ST[k];
    console.log('%cAlertador v2 OFF','color:#888');
  };
})();
