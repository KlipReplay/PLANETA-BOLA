(async function testarSegurancaSupabase() {
  const SUPABASE_URL = "https://jqjdfbcdtldhabisumgle.supabase.co"; // URL com 'jqj' corrigida
  const ANON_KEY = "COLE_SUA_ANON_PUBLIC_KEY_AQUI"; // Pegue a anon key em Project Settings > API

  console.log("%c=== INICIANDO AUDITORIA DE SEGURANÇA (RLS) ===", "color: #38bdf8; font-weight: bold; font-size: 14px;");

  const testes = [
    { nome: "Tabela restrita: VENDAS", url: `${SUPABASE_URL}/rest/v1/vendas?select=*`, method: "GET", deveFalhar: true },
    { nome: "Tabela restrita: CUPONS", url: `${SUPABASE_URL}/rest/v1/cupons?select=*`, method: "GET", deveFalhar: true },
    { nome: "Injeção de Cupom: CUPONS (POST)", url: `${SUPABASE_URL}/rest/v1/cupons`, method: "POST", body: JSON.stringify({ codigo: "HACK100", tipo: "porcentagem", valor: 100, ativo: true }), deveFalhar: true },
    { nome: "Tabela restrita: TOKENS_DOWNLOAD", url: `${SUPABASE_URL}/rest/v1/tokens_download?select=*`, method: "GET", deveFalhar: true },
    { nome: "Deleção de logs: LOG_COMPRAS (DELETE)", url: `${SUPABASE_URL}/rest/v1/log_compras?id=gt.0`, method: "DELETE", deveFalhar: true },
    { nome: "Tabela pública: REPLAYS", url: `${SUPABASE_URL}/rest/v1/replays?select=id,nome`, method: "GET", deveFalhar: false }
  ];

  for (const teste of testes) {
    try {
      const res = await fetch(teste.url, {
        method: teste.method,
        headers: { "apikey": ANON_KEY, "Authorization": `Bearer ${ANON_KEY}`, "Content-Type": "application/json" },
        body: teste.body || undefined
      });
      const data = await res.json().catch(() => null);

      if (teste.deveFalhar) {
        const protegido = !res.ok || (Array.isArray(data) && data.length === 0);
        if (protegido) {
          console.log(`%c[PROTEGIDO] %c${teste.nome}`, "color: #4ade80; font-weight: bold;", "color: #cbd5e1;");
        } else {
          console.error(`%c[VULNERÁVEL] %c${teste.nome} -> Exposto!`, "color: #ef4444; font-weight: bold;", "color: #f87171;", data);
        }
      } else {
        if (res.ok) {
          console.log(`%c[OK - PÚBLICO] %c${teste.nome}`, "color: #38bdf8; font-weight: bold;", "color: #cbd5e1;");
        } else {
          console.warn(`[AVISO] Replays inacessível:`, data);
        }
      }
    } catch (err) {
      console.log(`%c[ERRO DE CONEXÃO] %c${teste.nome}`, "color: #facc15; font-weight: bold;", "color: #cbd5e1;");
    }
  }

  console.log("%c=== AUDITORIA FINALIZADA ===", "color: #38bdf8; font-weight: bold; font-size: 14px;");
})();