import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from flask_cors import CORS
import mercadopago
import requests as req
from supabase import create_client
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__)
CORS(app)

# ============================================================
# CONFIGURAÇÃO DE AMBIENTE E DIRETÓRIOS
# ============================================================
ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"

# Pasta de replays salvos localmente na máquina
PASTA_VIDEOS_LOCAL = BASE_DIR / "replays_locais"
BUCKET_NOME = "replays"

if not ACCESS_TOKEN:
    raise RuntimeError("MP_ACCESS_TOKEN não configurado no .env")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL ou SUPABASE_KEY não configurados no .env")

sdk = mercadopago.SDK(ACCESS_TOKEN)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def obter_preco_atual():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
                return float(dados.get("preco_replay", 0.15))
        except Exception as e:
            print(f"[Aviso] Falha ao ler config.json no server: {e}", flush=True)
    return float(os.getenv("PRECO_REPLAY", "0.15"))


# ============================================================
# ROTINA AUTOMÁTICA DE LIMPEZA (28H SUPABASE / 7 DIAS LOCAL)
# ============================================================
def rotina_limpeza_automatica():
    """
    Roda continuamente em background a cada 1 hora.
    - Supabase (Storage + Tabela): apaga arquivos e registros criados há mais de 28 horas.
    - Disco Local: apaga arquivos .mp4 salvos há mais de 7 dias.
    """
    while True:
        try:
            agora = datetime.now(timezone.utc)

            # ----------------------------------------------------
            # 1. LIMPEZA NO SUPABASE (LIMITE: 28 HORAS)
            # ----------------------------------------------------
            limite_supabase_iso = (agora - timedelta(hours=28)).isoformat()

            res = (
                supabase.table("replays")
                .select("id, nome, preview_nom, criado_em")
                .lt("criado_em", limite_supabase_iso)
                .execute()
            )

            registros_expirados = res.data or []

            if registros_expirados:
                arquivos_storage = []
                ids_tabela = []

                for r in registros_expirados:
                    ids_tabela.append(r["id"])
                    if r.get("nome"):
                        arquivos_storage.append(r["nome"])
                    if r.get("preview_nom"):
                        arquivos_storage.append(r["preview_nom"])

                # Remove arquivos físicos do bucket replays
                if arquivos_storage:
                    try:
                        supabase.storage.from_(BUCKET_NOME).remove(arquivos_storage)
                        print(f"[Auto-Limpeza] {len(arquivos_storage)} arquivos removidos do Supabase Storage.", flush=True)
                    except Exception as e_stor:
                        print(f"[Erro Auto-Limpeza Storage]: {e_stor}", flush=True)

                # Remove linhas da tabela replays
                if ids_tabela:
                    try:
                        supabase.table("replays").delete().in_("id", ids_tabela).execute()
                        print(f"[Auto-Limpeza] {len(ids_tabela)} registros removidos da tabela replays.", flush=True)
                    except Exception as e_db:
                        print(f"[Erro Auto-Limpeza DB]: {e_db}", flush=True)

            # ----------------------------------------------------
            # 2. LIMPEZA NO DISCO LOCAL (LIMITE: 7 DIAS)
            # ----------------------------------------------------
            if PASTA_VIDEOS_LOCAL.exists():
                limite_local_ts = time.time() - (7 * 86400)  # 7 dias em segundos
                removidos_local = 0

                for video_local in PASTA_VIDEOS_LOCAL.glob("*.mp4"):
                    try:
                        if video_local.stat().st_mtime < limite_local_ts:
                            video_local.unlink(missing_ok=True)
                            removidos_local += 1
                    except OSError as e_os:
                        print(f"[Aviso Local] Falha ao deletar {video_local.name}: {e_os}", flush=True)

                if removidos_local > 0:
                    print(f"[Auto-Limpeza Local] {removidos_local} vídeos antigos apagados do disco.", flush=True)

        except Exception as e_loop:
            print(f"[Erro no loop de limpeza]: {e_loop}", flush=True)

        # Aguarda 1 hora até a próxima checagem
        time.sleep(3600)


# Inicia a thread de auto-limpeza em modo daemon
threading.Thread(target=rotina_limpeza_automatica, daemon=True).start()


# ============================================================
# HELPERS
# ============================================================
def obter_dados_video(video_id):
    try:
        resultado = (
            supabase.table("replays")
            .select("id, nome, url")
            .eq("id", video_id)
            .single()
            .execute()
        )
        return resultado.data
    except Exception as e:
        print(f"[Erro DB] Falha ao buscar vídeo {video_id}: {e}", flush=True)
        return None


def registrar_compra(video_id, payment_id, nome_video, valor=None):
    try:
        supabase.table("log_compras").insert({
            "video_id": video_id,
            "nome_video": nome_video,
            "payment_id": str(payment_id)
        }).execute()
        print(f"[LOG] Gravado em log_compras: {payment_id}", flush=True)
    except Exception as e:
        print(f"[Erro Log] Falha ao registrar log_compras: {e}", flush=True)

    try:
        existe = supabase.table("vendas").select("id").eq("payment_id", str(payment_id)).execute()
        if not existe.data:
            valor_final = float(valor if valor is not None else obter_preco_atual())
            supabase.table("vendas").insert({
                "replay_id": str(video_id),
                "replay_nome": str(nome_video),
                "valor": valor_final,
                "payment_id": str(payment_id),
                "status": "approved",
                "criado_em": datetime.now(timezone.utc).isoformat()
            }).execute()
            print(f"[Financeiro SUCESSO] Venda gravada na tabela vendas: ID {payment_id} | Valor: R$ {valor_final}", flush=True)
        else:
            print(f"[Financeiro] Venda já existia na tabela vendas: ID {payment_id}", flush=True)
    except Exception as e:
        print(f"[ERRO CRÍTICO BANCO VENDAS]: {e}", flush=True)


# ============================================================
# ROTAS DA API
# ============================================================
@app.route("/config", methods=["GET"])
def obter_config_publica():
    try:
        return jsonify({"preco": obter_preco_atual()}), 200
    except Exception as e:
        return jsonify({"preco": float(os.getenv("PRECO_REPLAY", "0.15")), "erro": str(e)}), 200


@app.route("/replays", methods=["GET"])
def listar_replays():
    try:
        resultado = (
            supabase.table("replays")
            .select("id, nome, criado_em, preview_url")
            .order("id", desc=True)
            .limit(50)
            .execute()
        )
        resp = jsonify(resultado.data or [])
        # Impede que o frontend e o navegador guardem cache de replays já excluídos
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp, 200
    except Exception as e:
        print(f"[Erro /replays]: {e}", flush=True)
        return jsonify({"erro": "Erro ao listar replays"}), 500


@app.route("/validar-cupom", methods=["POST"])
def validar_cupom():
    try:
        dados = request.get_json(silent=True) or {}
        codigo = dados.get("codigo", "").strip().upper()

        if not codigo:
            return jsonify({"valido": False, "erro": "Informe o cupom"}), 400

        res = supabase.table("cupons").select("*").eq("codigo", codigo).eq("ativo", True).execute()
        if not res.data:
            return jsonify({"valido": False, "erro": "Cupom inválido ou expirado"}), 404

        cupom = res.data[0]
        preco_original = obter_preco_atual()

        if cupom["tipo"] == "porcentagem":
            desconto = (preco_original * float(cupom["valor"])) / 100.0
            preco_final = max(0.10, preco_original - desconto)
        else:
            preco_final = max(0.10, preco_original - float(cupom["valor"]))

        return jsonify({
            "valido": True,
            "codigo": cupom["codigo"],
            "tipo": cupom["tipo"],
            "valor_desconto": float(cupom["valor"]),
            "preco_original": preco_original,
            "preco_final": round(preco_final, 2)
        }), 200
    except Exception as e:
        return jsonify({"valido": False, "erro": str(e)}), 500


@app.route("/criar-pix", methods=["POST"])
def criar_pix():
    try:
        body = request.get_json(silent=True) or {}
        video_id = body.get("video_id")
        cupom_codigo = body.get("cupom", "").strip().upper()

        if not video_id:
            return jsonify({"erro": "video_id é obrigatório"}), 400

        video = obter_dados_video(video_id)
        if not video:
            return jsonify({"erro": "Vídeo não encontrado"}), 404

        nome_video = video.get("nome", f"Replay #{video_id}")
        valor_cobrar = obter_preco_atual()

        if cupom_codigo:
            cup_res = supabase.table("cupons").select("*").eq("codigo", cupom_codigo).eq("ativo", True).execute()
            if cup_res.data:
                c = cup_res.data[0]
                if c["tipo"] == "porcentagem":
                    valor_cobrar = max(0.10, valor_cobrar * (1 - float(c["valor"]) / 100.0))
                else:
                    valor_cobrar = max(0.10, valor_cobrar - float(c["valor"]))

                try:
                    usos_atuais = c.get("usos") or 0
                    supabase.table("cupons").update({"usos": usos_atuais + 1}).eq("id", c["id"]).execute()
                except Exception as e_cup:
                    print(f"[Aviso] Falha ao incrementar uso do cupom: {e_cup}", flush=True)

        valor_cobrar = round(valor_cobrar, 2)

        payment_data = {
            "transaction_amount": valor_cobrar,
            "description": f"Download {nome_video}",
            "payment_method_id": "pix",
            "external_reference": str(video_id),
            "metadata": {
                "video_id": str(video_id),
                "cupom": cupom_codigo
            },
            "payer": {
                "email": "cliente@quadra.com"
            }
        }

        result = sdk.payment().create(payment_data)
        resp = result.get("response", {})

        if result.get("status") not in (200, 201):
            return jsonify({"erro": "Falha ao gerar cobrança Pix", "detalhes": resp}), 500

        tx_data = resp["point_of_interaction"]["transaction_data"]

        return jsonify({
            "payment_id": resp["id"],
            "qr_code": tx_data["qr_code"],
            "qr_code_base64": tx_data["qr_code_base64"],
            "valor": valor_cobrar,
            "expira_em": int(time.time() + 600)
        }), 200

    except Exception as e:
        print(f"[Erro /criar-pix]: {e}", flush=True)
        return jsonify({"erro": "Erro interno ao processar pagamento"}), 500


@app.route("/status/<int:payment_id>", methods=["GET"])
def status_pagamento(payment_id):
    try:
        checar_log = (
            supabase.table("log_compras")
            .select("video_id")
            .eq("payment_id", str(payment_id))
            .execute()
        )

        if checar_log.data:
            vid = checar_log.data[0]["video_id"]

            try:
                checar_venda = supabase.table("vendas").select("id").eq("payment_id", str(payment_id)).execute()
                if not checar_venda.data:
                    video = obter_dados_video(vid)
                    nome_video = video.get("nome") if video else f"Replay #{vid}"
                    registrar_compra(vid, payment_id, nome_video, obter_preco_atual())
            except Exception as e_venda:
                print(f"[Erro fallback vendas]: {e_venda}", flush=True)

            token_existente = (
                supabase.table("tokens_download")
                .select("token, expira_em")
                .eq("video_id", vid)
                .order("id", desc=True)
                .limit(1)
                .execute()
            )

            if token_existente.data:
                exp_dt = datetime.fromisoformat(token_existente.data[0]["expira_em"].replace("Z", "+00:00"))
                if datetime.now(timezone.utc) < exp_dt:
                    return jsonify({"status": "approved", "download_token": token_existente.data[0]["token"]}), 200

        result = sdk.payment().get(payment_id)
        resp = result.get("response", {})
        status_atual = resp.get("status")

        if status_atual == "approved":
            video_id = resp.get("external_reference") or (resp.get("metadata") or {}).get("video_id")

            if not video_id:
                video_id = request.args.get("video_id")

            if not video_id:
                return jsonify({"erro": "ID do vídeo não vinculado ao pagamento"}), 400

            video = obter_dados_video(video_id)
            nome_video = video.get("nome") if video else "Replay"
            valor_pago = resp.get("transaction_amount", obter_preco_atual())

            registrar_compra(video_id, payment_id, nome_video, valor=valor_pago)

            token = secrets.token_urlsafe(32)
            limite_expiracao = datetime.now(timezone.utc) + timedelta(minutes=5)

            supabase.table("tokens_download").insert({
                "token": token,
                "video_id": video_id,
                "expira_em": limite_expiracao.isoformat()
            }).execute()

            return jsonify({"status": "approved", "download_token": token}), 200

        return jsonify({"status": status_atual}), 200

    except Exception as e:
        print(f"[Erro /status/{payment_id}]: {e}", flush=True)
        return jsonify({"erro": "Erro ao consultar transação"}), 500


@app.route("/download/<token>", methods=["GET"])
def download_video(token):
    try:
        resultado_token = (
            supabase.table("tokens_download")
            .select("*")
            .eq("token", token)
            .execute()
        )

        if not resultado_token.data:
            return jsonify({"erro": "Link inválido ou expirado"}), 403

        dados_sessao = resultado_token.data[0]
        video_id = dados_sessao["video_id"]
        expira_em_str = dados_sessao["expira_em"]

        expira_em = datetime.fromisoformat(expira_em_str.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expira_em:
            supabase.table("tokens_download").delete().eq("token", token).execute()
            return jsonify({"erro": "Link de download expirado"}), 403

        video = obter_dados_video(video_id)
        if not video or not video.get("url"):
            return jsonify({"erro": "Arquivo de vídeo não localizado"}), 404

        nome_raw = video.get("nome") or f"replay_{video_id}.mp4"
        nome_arquivo = secure_filename(nome_raw)
        if not nome_arquivo.endswith(".mp4"):
            nome_arquivo += ".mp4"

        resposta_stream = req.get(video["url"], stream=True, timeout=30)
        if resposta_stream.status_code != 200:
            return jsonify({"erro": "Falha ao acessar armazenamento do vídeo"}), 502

        # ----------------------------------------------------
        # REGISTRO NO LOG DE DOWNLOADS
        # ----------------------------------------------------
        try:
            ip_cliente = (
                request.headers.get("CF-Connecting-IP")
                or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or request.remote_addr
            )
            supabase.table("log_downloads").insert({
                "video_id": str(video_id),
                "nome_video": str(nome_raw),
                "baixado_em": datetime.now(timezone.utc).isoformat(),
                "ip_origem": ip_cliente
            }).execute()
            print(f"[LOG DOWNLOAD] Registrado: {nome_raw} para IP {ip_cliente}", flush=True)
        except Exception as e_log:
            print(f"[Aviso Log Download] Falha ao registrar log_downloads: {e_log}", flush=True)

        supabase.table("tokens_download").delete().eq("token", token).execute()

        def gerar_fluxo():
            for chunk in resposta_stream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        headers = {
            "Content-Disposition": f'attachment; filename="{nome_arquivo}"',
            "Content-Type": "video/mp4"
        }

        return Response(gerar_fluxo(), headers=headers)

    except Exception as e:
        print(f"[Erro /download]: {e}", flush=True)
        return jsonify({"erro": "Erro durante a transferência do arquivo"}), 500


# ============================================================
# INICIALIZAÇÃO
# ============================================================
if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta)