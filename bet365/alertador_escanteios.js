// alertador_escanteios.js — sinal AO VIVO de ESCANTEIOS Over/Under no bet365
// (SOMENTE LEITURA). Porta o modelo de bet365/prob_aovivo_mercado.py (Fase 3).
// NAO aposta — voce decide e clica.
//
// DIFERENCA p/ alertador_valor.js (1X2/gols):
//  - O overview do bet365 mostra PLACAR DE GOLS (.ovm-ScorePill), nao a contagem
//    de escanteios. A contagem de escanteios so aparece no PAINEL do jogo ABERTO
//    (rodas .ml1-WheelChartAdvanced). Logo, este alertador opera no JOGO ABERTO:
//    abra a partida, deixe o painel de estatisticas visivel e (p/ ter odds) a aba
//    "Escanteios"/"Cantos" do mercado.
//  - Mercado e Over/Under de um TOTAL (nao 1/X/2). Math: P(total final > linha).
//
// ⚠️ SELETORES DE DOM: os de escanteios (contagem + odds Over/Under) variam por
//    versao/idioma do bet365. Os defaults abaixo sao best-effort; confirme com
//    `escDebug()` na pagina e ajuste cfg.* se necessario. O MODELO e exato
//    (paridade testada vs Python); o que pode falhar e a LEITURA do DOM.
//
// Uso no Console (F12), com a partida aberta:
//   iniciarEscanteios()                      // le linha+odds da tela
//   iniciarEscanteios({linha:9.5})           // forca a linha (se nao achar odds)
//   iniciarEscanteios({lado:'casa', linha:5.5})  // total do mandante
//   escDebug()                               // imprime o que conseguiu ler do DOM
//   pararEscanteios()
//
// CAL embutido (MERCADO_CAL) vem de mercado_cal.json (calibrar_mercado.py).
// Troque media_total/home_share pelos do SEU banco; o h_reg e derivado da forma.

;(function () {
  const VERSAO = 'esc-v1.0';
  const hasDOM = (typeof window !== 'undefined') && (typeof document !== 'undefined');

  // ---------------- calibracao (de mercado_cal.json -> "escanteios") ----------
  // Edite media_total e home_share com os valores do SEU banco (calibrar_mercado.py).
  const MERCADO_CAL = {
    media_total: 10.0,          // media de escanteios/jogo (REAL, do seu banco)
    home_share: 0.55,           // fracao dos escanteios feita pelo mandante (REAL)
    // FORMA da curva (prior): pesos relativos por bucket de 5 min (0..89).
    // "subida" = leve aumento no 2T e fim de jogo. O blend de ritmo domina ao vivo.
    shape_weights: [0.85,0.90,0.95,1.00,1.00,1.00,1.05,1.05,1.05,
                    1.00,1.05,1.05,1.10,1.10,1.15,1.20,1.25,1.35],
    fator_acrescimo: 1.4,
    stoppage_extra_pmf: [0.45,0.28,0.15,0.08,0.04],
    pace: { K: 25.0, w_max: 0.85 },     // shrinkage do blend de ritmo: w=min(w_max,m/(m+K))
    mult: { sigma: 0.30, d0: 1.5, clampLo: 0.5, clampHi: 1.8,
            red_down: 0.80, red_up: 1.20 },
    sigma_log: { base: 0.25, sem_pace: 0.10 },
  };

  function buildCal(c) {                 // deriva h_reg_buckets da forma + nivel real
    const soma = c.shape_weights.reduce((a, b) => a + b, 0);
    const h = c.shape_weights.map(w => c.media_total * w / soma / 5.0);
    return Object.assign({}, c, { h_reg_buckets: h });
  }
  const CAL = buildCal(MERCADO_CAL);

  // ---------------- config ----------------
  const DEF = {
    lado: 'total',           // 'total' | 'casa' | 'fora'
    linha: null,             // null = tenta ler da tela; senao usa este valor
    watchMin: 35,            // escanteios resolvem cedo; entra em WATCH antes
    probMin: 0.62,           // prob minima do lado dominante p/ ARM
    margemValor: 0.0,        // exige odd_tela > odd_justa*(1+margem)
    acrescimoDefault: 4,     // acrescimo do 2T quando desconhecido
    intervaloMs: 4000,
    confirmScans: 2,         // ARM/GREEN persiste N scans (anti-flicker)
    // ---- seletores de DOM (AJUSTAVEIS) ----
    reEscanteio: 'escanteio|corner|canto',     // label da roda / nome do mercado
    reOver: 'mais|over|acima',
    reUnder: 'menos|under|abaixo',
  };
  const COR = { WATCH:'#6aa0ff', ARM:'#ffb300', GREEN:'#2bd24f', OFF:'#888' };

  // ---------------- modelo (porta de prob_aovivo_mercado.py) ----------------
  const fat = n => { let f = 1; for (let i = 2; i <= n; i++) f *= i; return f; };
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  function poisPmf(lam, k) { return lam <= 0 ? (k === 0 ? 1 : 0)
                                   : Math.exp(-lam) * Math.pow(lam, k) / fat(k); }
  function poisCdf(lam, k) { k = Math.floor(k); if (k < 0) return 0;
    let s = 0; for (let i = 0; i <= k; i++) s += poisPmf(lam, i); return s; }

  // P(N>linha), P(N<=linha) para N~Poisson(lam). Porta de mercados.over_under.
  function overUnder(lam, linha) {
    const base = Math.floor(linha);
    if (Math.abs(linha - base - 0.5) < 1e-9) {            // meia-linha (sem push)
      const pu = poisCdf(lam, base); return [1 - pu, pu];
    }
    const k = Math.round(linha);                          // linha inteira: tira push
    const pPush = poisPmf(lam, k), pu = poisCdf(lam, k - 1);
    let po = 1 - pu - pPush; const s = (po + pu) || 1;
    return [po / s, pu / s];
  }

  function lamRegular(m, h) { if (m >= 90) return 0; let s = 0;
    for (let b = 0; b < h.length; b++) { const lo = 5*b, hi = 5*b+5,
      ov = Math.min(90, hi) - Math.max(m, lo); if (ov > 0) s += h[b]*ov; } return s; }
  function lamEsperadoAte(m, h) { const mm = Math.min(m, 90); let s = 0;
    for (let b = 0; b < h.length; b++) { const lo = 5*b, hi = 5*b+5,
      ov = Math.min(mm, hi) - Math.max(0, lo); if (ov > 0) s += h[b]*ov; } return s; }

  function mults(gc, gf, m, cal) {       // placar: lider cria menos, perdedor mais
    const mu = cal.mult, d = gc - gf;
    const push = 0.5 + 0.5*(Math.min(m, 90)/90), t = Math.tanh(d/mu.d0);
    return [clamp(1 + mu.sigma*(-t*push), mu.clampLo, mu.clampHi),
            clamp(1 + mu.sigma*( t*push), mu.clampLo, mu.clampHi)];
  }
  function multVermelho(redC, redF, restante, cal) {
    const mu = cal.mult, net = (redC||0) - (redF||0);
    const neff = Math.abs(net) * Math.min(1, Math.max(0, restante)/30);
    if (net > 0) return [Math.pow(mu.red_down, neff), Math.pow(mu.red_up, neff)];
    if (net < 0) return [Math.pow(mu.red_up, neff), Math.pow(mu.red_down, neff)];
    return [1, 1];
  }

  function lamRestanteLados(m, cCasa, cFora, cal, A, gc, gf, redC, redF, escala) {
    const h = cal.h_reg_buckets, pace = cal.pace, shareBase = cal.home_share;
    const C = cCasa + cFora;
    const lamRegRem = lamRegular(m, h);
    const hStop = (cal.media_total/90) * (cal.fator_acrescimo || 1.4);
    const espAte = lamEsperadoAte(m, h);
    const w = m > 0 ? Math.min(pace.w_max, m/(m + pace.K)) : 0;
    const paceFactor = (espAte > 0 && C > 0) ? C/espAte : 1;
    const pfEff = 1 + w*(paceFactor - 1);
    const shareObs = C > 0 ? cCasa/C : shareBase;
    const shareEff = (1 - w)*shareBase + w*shareObs;
    const restante = Math.max(0, 90 - m) + A;
    const [Mc, Mf] = mults(gc, gf, m, cal);
    const [Rc, Rf] = multVermelho(redC, redF, restante, cal);
    const pmf = cal.stoppage_extra_pmf, elapsedStop = Math.max(0, m - 90);
    let lamC = 0, lamF = 0, wsum = 0;
    for (let x = 0; x < pmf.length; x++) {
      const S = A + x, remStop = Math.max(0, S - elapsedStop);
      const lamT = (lamRegRem + hStop*remStop) * pfEff * (escala || 1);
      lamC += pmf[x]*lamT*shareEff*Mc*Rc;
      lamF += pmf[x]*lamT*(1 - shareEff)*Mf*Rf; wsum += pmf[x];
    }
    if (wsum > 0) { lamC /= wsum; lamF /= wsum; }
    return { lamC, lamF, det: { paceFactor, pfEff, shareEff, w } };
  }

  function probOverUnder(m, cCasa, cFora, linha, cal, lado, o) {
    const r = lamRestanteLados(m, cCasa, cFora, cal, o.A, o.gc, o.gf,
                               o.redC, o.redF, o.escala);
    let ou;
    if (lado === 'casa') ou = overUnder(r.lamC, linha - cCasa);
    else if (lado === 'fora') ou = overUnder(r.lamF, linha - cFora);
    else ou = overUnder(r.lamC + r.lamF, linha - (cCasa + cFora));
    return { over: ou[0], under: ou[1], det: r.det };
  }
  function banda(m, cCasa, cFora, linha, cal, lado, o) {
    const sl = cal.sigma_log;
    const s = sl.base + ((cCasa + cFora) === 0 ? (sl.sem_pace || 0.1) : 0);
    const hi = probOverUnder(m, cCasa, cFora, linha, cal, lado, Object.assign({}, o, {escala:Math.exp(+s)}));
    const lo = probOverUnder(m, cCasa, cFora, linha, cal, lado, Object.assign({}, o, {escala:Math.exp(-s)}));
    return [Math.min(lo.over, hi.over), Math.max(lo.over, hi.over)];
  }

  // ---------------- (Node) export do modelo p/ teste de paridade ----------------
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { buildCal, overUnder, probOverUnder, banda, lamRestanteLados };
  }
  if (!hasDOM) return;   // fora do browser: so o modelo, sem DOM/UI

  // ---------------- parsing DOM (best-effort, AJUSTAVEL) ----------------
  const txt = e => (e && typeof e.innerText === 'string') ? e.innerText.trim() : '';
  const num = o => { const n = parseFloat(String(o).replace(',', '.')); return isNaN(n) ? null : n; };
  const SEL_LISTA = '.ovm-FixtureList, .ovm-SortedFixtureList';
  function jogoAberto() {   // .ovm-Fixture FORA de qualquer lista = jogo aberto/painel
    return [...document.querySelectorAll('.ovm-Fixture')].find(f => !f.closest(SEL_LISTA));
  }
  function parseRelogio(s) { const m = String(s||'').match(/(\d+):(\d+)/);
    return m ? +m[1] + (+m[2])/60 : null; }

  function lerJogo(cfg) {     // {casa,fora,gc,gf,min,cCasa,cFora} do jogo aberto, ou null
    const fx = jogoAberto(); if (!fx) return null;
    const nomes = [...fx.querySelectorAll('[class*="TeamName"]')].map(txt).filter(Boolean);
    if (nomes.length < 2) return null;
    const pl = [...fx.querySelectorAll('.ovm-ScorePill')].map(e => parseInt(e.innerText, 10));
    const min = parseRelogio((fx.querySelector('.ovm-InPlayTimer')||{}).innerText);
    // contagem de escanteios: roda do painel cujo label casa reEscanteio
    const reE = new RegExp(cfg.reEscanteio, 'i');
    let cCasa = null, cFora = null;
    document.querySelectorAll('.ml1-WheelChartAdvanced').forEach(wch => {
      const lab = txt(wch.querySelector('.ml1-WheelChartAdvanced_Text'));
      if (lab && reE.test(lab)) {
        const ns = [...wch.querySelectorAll('*')].map(txt).filter(x => /^\d+$/.test(x)).map(Number);
        if (ns.length >= 2) { cCasa = ns[0]; cFora = ns[1]; }
      }
    });
    return { casa:nomes[0], fora:nomes[1], gc:pl[0]||0, gf:pl[1]||0,
             min, cCasa, cFora };
  }

  function lerOddsEscanteios(cfg) {   // {linha, over, under} da aba de escanteios, ou null
    const reE = new RegExp(cfg.reEscanteio, 'i');
    // procura um container de mercado cujo cabecalho mencione escanteios
    const heads = [...document.querySelectorAll('*')].filter(e =>
      e.children.length <= 3 && reE.test(e.textContent || '') &&
      (e.textContent || '').length < 40);
    for (const h of heads) {
      const cont = h.closest('[class*="Market"]') || h.parentElement;
      if (!cont) continue;
      // pega numeros: linha (x.5) e duas odds (>1). Heuristica simples e robusta.
      // ordem usual da tela: 1a odd = Over, 2a = Under (confirme com escDebug()).
      const linhaM = (cont.textContent || '').match(/(\d+\.5|\d+,5)/);
      const odds = [...cont.querySelectorAll('*')].map(e => txt(e))
        .filter(t => /^\d+\.\d{2}$|^\d+,\d{2}$/.test(t)).map(num).filter(v => v && v > 1);
      if (linhaM && odds.length >= 2) {
        return { linha: num(linhaM[0]), over: odds[0], under: odds[1] };
      }
    }
    return null;
  }

  window.escDebug = function (over) {
    const cfg = Object.assign({}, DEF, over || {});
    const j = lerJogo(cfg), od = lerOddsEscanteios(cfg);
    console.log('%c[escDebug] jogo:', 'color:#6aa0ff', j);
    console.log('%c[escDebug] odds escanteios:', 'color:#6aa0ff', od);
    if (!j) console.log('  -> nao achei o jogo aberto. Abra a partida (painel visivel).');
    if (j && (j.cCasa == null)) console.log('  -> nao achei a contagem de escanteios. Ajuste cfg.reEscanteio ou deixe o painel de stats visivel.');
    if (!od) console.log('  -> nao achei odds de escanteios. Abra a aba "Escanteios"/"Cantos" ou passe {linha:9.5}.');
    return { jogo: j, odds: od };
  };

  // ---------------- UX ----------------
  let timer = null;
  const ST = (window.__escState = window.__escState || {});
  function barra(html) { let b = document.getElementById('__escBar');
    if (!b) { b = document.createElement('div'); b.id = '__escBar';
      b.style.cssText = 'position:fixed;bottom:10px;left:10px;z-index:99999;background:#111;'
        + 'color:#fff;border:1px solid #444;font:bold 12px sans-serif;padding:8px 12px;'
        + 'border-radius:6px;line-height:1.5;max-width:520px';
      document.body.appendChild(b); }
    b.innerHTML = html;
  }

  function scan(cfg) {
    const j = lerJogo(cfg);
    if (!j || j.min == null) { barra(`<b style="color:#33d17a">${VERSAO}</b><br>`
      + `<span style="color:${COR.OFF}">sem jogo aberto / sem relogio — abra a partida</span>`); return; }
    if (j.cCasa == null) { barra(`<b style="color:#33d17a">${VERSAO}</b><br>`
      + `<span style="color:${COR.OFF}">nao li a contagem de escanteios — rode escDebug()</span>`); return; }

    const od = (cfg.linha == null) ? lerOddsEscanteios(cfg) : null;
    const linha = (cfg.linha != null) ? cfg.linha : (od ? od.linha : null);
    if (linha == null) { barra(`<b style="color:#33d17a">${VERSAO}</b><br>`
      + `${j.casa} x ${j.fora} ${j.min.toFixed(0)}' · escanteios ${j.cCasa}-${j.cFora}<br>`
      + `<span style="color:${COR.OFF}">sem linha (sem odds na tela) — passe {linha:9.5}</span>`); return; }

    const o = { A: cfg.acrescimoDefault, gc: j.gc, gf: j.gf, redC: 0, redF: 0 };
    const r = probOverUnder(j.min, j.cCasa, j.cFora, linha, CAL, cfg.lado, o);
    const [blo, bhi] = banda(j.min, j.cCasa, j.cFora, linha, CAL, cfg.lado, o);
    const domOver = r.over >= r.under;
    const prob = domOver ? r.over : r.under;
    const lab = domOver ? 'OVER' : 'UNDER';
    const justa = prob > 0 ? 1/prob : Infinity;

    // valor: precisa de odds na tela do lado dominante
    let oddTela = null, valor = false;
    if (od) { oddTela = domOver ? od.over : od.under;
      valor = (oddTela != null) && (oddTela > justa * (1 + cfg.margemValor)); }

    const st = ST[cfg.lado + '|' + linha] = ST[cfg.lado + '|' + linha] || {};
    const armOk = j.min >= cfg.watchMin && prob >= cfg.probMin && (od ? valor : true);
    st.arm = armOk ? (st.arm || 0) + 1 : 0;
    const tier = (st.arm >= cfg.confirmScans && od && valor) ? 'GREEN'
               : armOk ? 'ARM' : 'WATCH';

    const telaTxt = oddTela != null ? oddTela.toFixed(2) : (od ? 'susp' : 's/odds');
    barra(`<b style="color:#33d17a">${VERSAO}</b> · `
      + `<b style="color:${COR[tier]}">${tier}</b> ${cfg.lado} escanteios<br>`
      + `${j.casa} x ${j.fora} · ${j.min.toFixed(0)}' · placar gols ${j.gc}-${j.gf}<br>`
      + `escanteios ${j.cCasa}-${j.cFora} (=${j.cCasa+j.cFora}) · ritmo x${r.det.pfEff.toFixed(2)} · share casa ${(r.det.shareEff*100).toFixed(0)}%<br>`
      + `<b>${lab} ${linha}</b>: ${(prob*100).toFixed(1)}% · justa ${justa.toFixed(2)} · `
      + `tela ${telaTxt} ${od ? (valor?'✓':'✗') : ''} · banda ${(blo*100).toFixed(0)}-${(bhi*100).toFixed(0)}%`
      + (tier==='GREEN' ? '<br>💚 valor confirmado — confira o jogo antes de clicar' : ''));
    if (tier === 'GREEN' && st.arm === cfg.confirmScans) beep();
  }

  function beep() { try { const a = new (window.AudioContext||window.webkitAudioContext)();
    const o = a.createOscillator(); o.connect(a.destination); o.frequency.value = 880;
    o.start(); setTimeout(() => o.stop(), 180); } catch (e) {} }

  // ---------------- API ----------------
  window.iniciarEscanteios = function (over) {
    const cfg = Object.assign({}, DEF, over || {});
    window.pararEscanteios();
    const tick = () => scan(cfg);
    tick(); timer = setInterval(tick, cfg.intervaloMs);
    console.log('%cAlertador Escanteios ON', 'color:#2bd24f;font-weight:bold', cfg);
    console.log('Abra a partida (painel de stats + aba Escanteios). Rode escDebug() se nao ler o DOM.');
    return cfg;
  };
  window.pararEscanteios = function () {
    if (timer) clearInterval(timer); timer = null;
    const b = document.getElementById('__escBar'); if (b) b.remove();
    for (const k in ST) delete ST[k];
    console.log('%cAlertador Escanteios OFF', 'color:#888');
  };
})();
