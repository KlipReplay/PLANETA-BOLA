import os
import sys
import subprocess
import json
from datetime import datetime
import cv2
from flask import Flask, jsonify, request, render_template_string
from flask_cors import CORS
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

try:
    from pygrabber.dshow_graph import FilterGraph
    HAS_PYGRABBER = True
except ImportError:
    HAS_PYGRABBER = False

app = Flask(__name__)
CORS(app)

PYTHON_EXE = sys.executable
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

GRAVACOES_DIR = os.path.join(BASE_DIR, "Gravações")
REPLAYS_DIR   = os.path.join(BASE_DIR, "Replays")
PREVIEWS_DIR  = os.path.join(BASE_DIR, "Previews")
BANDWIDTH_FILE = os.path.join(BASE_DIR, "bandwidth.json")
CONFIG_FILE   = os.path.join(BASE_DIR, "config.json")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"[Aviso] Falha ao conectar Supabase no admin: {e}", flush=True)

STORAGE_LIMITE_BYTES = int(os.getenv("STORAGE_LIMITE_BYTES", str(1 * 1024 * 1024 * 1024)))

processos = {
    "server": None,
    "camera": None
}

def carregar_config_local():
    padrao = {"preco_replay": 0.15, "duracao_replay_segundos": 60}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
                padrao.update(dados)
        except Exception as e:
            print(f"[Aviso] Falha ao ler config.json: {e}", flush=True)
    return padrao

def salvar_config_local(dados):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(dados, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"[Erro] Falha ao salvar config.json: {e}", flush=True)
        return False

def formatar_bytes(b):
    if b >= 1024 * 1024 * 1024:
        return f"{b / (1024**3):.2f} GB"
    elif b >= 1024 * 1024:
        return f"{b / (1024**2):.1f} MB"
    elif b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b} B"

def calcular_pasta(caminho):
    qtd = 0
    tamanho = 0
    if os.path.exists(caminho):
        for entry in os.scandir(caminho):
            if entry.is_file():
                qtd += 1
                try:
                    tamanho += entry.stat().st_size
                except Exception:
                    pass
    return qtd, tamanho, formatar_bytes(tamanho)

def obter_metricas_supabase():
    if not supabase:
        return {"qtd": 0, "bytes": 0, "formatado": "Não configurado", "pct": 0}
    try:
        arquivos = supabase.storage.from_("replays").list()
        qtd = len(arquivos)
        total_bytes = sum(arq.get("metadata", {}).get("size", 0) for arq in arquivos)
        pct = round((total_bytes / STORAGE_LIMITE_BYTES) * 100, 1)
        return {
            "qtd": qtd,
            "bytes": total_bytes,
            "formatado": formatar_bytes(total_bytes),
            "pct": pct
        }
    except Exception as e:
        return {"qtd": 0, "bytes": 0, "formatado": f"Erro: {str(e)[:20]}", "pct": 0}

def obter_bandwidth():
    try:
        if os.path.exists(BANDWIDTH_FILE):
            with open(BANDWIDTH_FILE, "r", encoding="utf-8") as f:
                b = json.load(f).get("usado_bytes", 0)
                return formatar_bytes(b)
    except Exception:
        pass
    return "0 B"

def listar_cameras_disponiveis():
    cameras = []
    backend = cv2.CAP_MSMF if sys.platform == "win32" else cv2.CAP_ANY

    if HAS_PYGRABBER and sys.platform == "win32":
        try:
            graph = FilterGraph()
            nomes_dispositivos = graph.get_input_devices()
            for idx, nome in enumerate(nomes_dispositivos):
                cap = cv2.VideoCapture(idx, backend)
                res = "Ocupada / Indisponível"
                if cap.isOpened():
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    res = f"{w}x{h}"
                    cap.release()
                cameras.append({
                    "index": idx,
                    "nome": nome,
                    "resolucao": res
                })
            return cameras
        except Exception as e:
            print(f"[Aviso] Falha pygrabber: {e}", flush=True)

    for idx in range(6):
        cap = cv2.VideoCapture(idx, backend)
        if cap.isOpened():
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cameras.append({
                "index": idx,
                "nome": f"Câmera {idx}",
                "resolucao": f"{w}x{h}"
            })
            cap.release()
    return cameras

def status_processo(proc):
    if proc is None:
        return "OFFLINE"
    if proc.poll() is None:
        return "ONLINE"
    return "STOPPED"

@app.route("/")
def painel():
    return render_template_string(HTML_DASHBOARD)

@app.route("/api/status", methods=["GET"])
def get_status():
    return jsonify({
        "server": status_processo(processos["server"]),
        "camera": status_processo(processos["camera"])
    })

@app.route("/api/cameras", methods=["GET"])
def get_cameras():
    return jsonify(listar_cameras_disponiveis())

@app.route("/api/metricas", methods=["GET"])
def get_metricas():
    grav_qtd, _, grav_txt = calcular_pasta(GRAVACOES_DIR)
    rep_qtd, _, rep_txt   = calcular_pasta(REPLAYS_DIR)
    prev_qtd, _, prev_txt = calcular_pasta(PREVIEWS_DIR)
    supa = obter_metricas_supabase()
    bw = obter_bandwidth()

    return jsonify({
        "gravacoes": {"qtd": grav_qtd, "tamanho": grav_txt},
        "replays": {"qtd": rep_qtd, "tamanho": rep_txt},
        "previews": {"qtd": prev_qtd, "tamanho": prev_txt},
        "supabase": supa,
        "bandwidth": bw
    })

@app.route("/api/financeiro", methods=["GET"])
def get_financeiro():
    padrao = {"total": "R$ 0,00", "p70": "R$ 0,00", "p30": "R$ 0,00", "qtd": 0}
    if not supabase:
        return jsonify({"hoje": padrao, "mes": padrao, "geral": padrao})

    try:
        res = (
            supabase.table("vendas")
            .select("valor, criado_em")
            .eq("status", "approved")
            .execute()
        )
        vendas = res.data or []

        hoje_str = datetime.now().strftime("%Y-%m-%d")
        mes_str = datetime.now().strftime("%Y-%m")

        total_geral = 0.0
        total_hoje = 0.0
        qtd_hoje = 0
        total_mes = 0.0
        qtd_mes = 0

        for v in vendas:
            val = float(v.get("valor", 0))
            dt = v.get("criado_em", "")[:10]
            total_geral += val

            if dt == hoje_str:
                total_hoje += val
                qtd_hoje += 1
            if dt.startswith(mes_str):
                total_mes += val
                qtd_mes += 1

        def formatar_repasse(total, qtd):
            p70 = total * 0.70
            p30 = total * 0.30
            return {
                "total": f"R$ {total:.2f}".replace(".", ","),
                "p70": f"R$ {p70:.2f}".replace(".", ","),
                "p30": f"R$ {p30:.2f}".replace(".", ","),
                "qtd": qtd
            }

        return jsonify({
            "hoje": formatar_repasse(total_hoje, qtd_hoje),
            "mes": formatar_repasse(total_mes, qtd_mes),
            "geral": formatar_repasse(total_geral, len(vendas))
        })
    except Exception as e:
        print(f"[ERRO FINANCEIRO] {e}", flush=True)
        return jsonify({"hoje": padrao, "mes": padrao, "geral": padrao})

@app.route("/api/configuracoes", methods=["GET"])
def get_configuracoes():
    return jsonify(carregar_config_local()), 200

@app.route("/api/configuracoes", methods=["POST"])
def post_configuracoes():
    dados = request.get_json(silent=True) or {}
    try:
        preco = float(dados.get("preco_replay", 0.15))
        duracao = int(dados.get("duracao_replay_segundos", 60))

        if preco < 0.10:
            return jsonify({"erro": "O preço mínimo do replay é R$ 0,10"}), 400
        if duracao < 10 or duracao > 300:
            return jsonify({"erro": "A duração deve ficar entre 10 e 300 segundos"}), 400

        nova_config = {
            "preco_replay": round(preco, 2),
            "duracao_replay_segundos": duracao
        }

        if salvar_config_local(nova_config):
            return jsonify({"mensagem": "Configurações salvas com sucesso!", "config": nova_config}), 200
        return jsonify({"erro": "Falha ao gravar arquivo config.json"}), 500

    except (ValueError, TypeError) as e:
        return jsonify({"erro": f"Dados numéricos inválidos: {e}"}), 400

@app.route("/api/cupons", methods=["GET"])
def listar_cupons():
    if not supabase:
        return jsonify([]), 200
    try:
        res = supabase.table("cupons").select("*").order("criado_em", desc=True).execute()
        return jsonify(res.data or []), 200
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/api/cupons", methods=["POST"])
def criar_cupom():
    if not supabase:
        return jsonify({"erro": "Supabase indisponível"}), 500

    dados = request.get_json(silent=True) or {}
    codigo = dados.get("codigo", "").strip().upper()
    tipo = dados.get("tipo", "porcentagem")
    try:
        valor = float(dados.get("valor", 0))
    except (ValueError, TypeError):
        return jsonify({"erro": "Valor numérico inválido"}), 400

    if not codigo or valor <= 0:
        return jsonify({"erro": "Código e valor positivo são obrigatórios"}), 400

    try:
        res = supabase.table("cupons").insert({
            "codigo": codigo,
            "tipo": tipo,
            "valor": valor,
            "ativo": True,
            "usos": 0
        }).execute()
        return jsonify({"mensagem": "Cupom registrado", "dados": res.data}), 201
    except Exception as e:
        return jsonify({"erro": f"Erro ao criar: {str(e)}"}), 400

@app.route("/api/cupons/<id_cupom>", methods=["DELETE"])
def deletar_cupom(id_cupom):
    if not supabase:
        return jsonify({"erro": "Supabase indisponível"}), 500
    try:
        supabase.table("cupons").delete().eq("id", id_cupom).execute()
        return jsonify({"mensagem": "Cupom deletado com sucesso"}), 200
    except Exception as e:
        return jsonify({"erro": str(e)}), 400

@app.route("/api/iniciar/<servico>", methods=["POST"])
def iniciar_servico(servico):
    if servico not in processos:
        return jsonify({"erro": "Serviço inválido"}), 400

    if status_processo(processos[servico]) == "ONLINE":
        return jsonify({"mensagem": f"{servico} já está rodando"}), 200

    script = os.path.join(BASE_DIR, f"{servico}.py")
    if not os.path.exists(script):
        return jsonify({"erro": f"Arquivo {servico}.py não localizado"}), 404

    env = os.environ.copy()
    if servico == "camera":
        body = request.get_json(silent=True) or {}
        cam_idx = str(body.get("camera_index", "0"))
        env["CAMERA_INDEX"] = cam_idx

    proc = subprocess.Popen([PYTHON_EXE, script], cwd=BASE_DIR, env=env, shell=False)
    processos[servico] = proc
    return jsonify({"mensagem": f"{servico} iniciado com sucesso", "status": "ONLINE"})

@app.route("/api/parar/<servico>", methods=["POST"])
def parar_servico(servico):
    if servico not in processos:
        return jsonify({"erro": "Serviço inválido"}), 400

    proc = processos[servico]
    if proc is not None and proc.poll() is None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        processos[servico] = None
        return jsonify({"mensagem": f"{servico} finalizado", "status": "OFFLINE"})

    processos[servico] = None
    return jsonify({"mensagem": f"{servico} já estava parado", "status": "OFFLINE"})

HTML_DASHBOARD = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin - Gestão Replay</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0b0f19; color: #f1f5f9; margin: 0; padding: 28px 20px; }
  .container { max-width: 960px; margin: 0 auto; }
  .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 22px; }
  h1 { font-size: 22px; font-weight: 700; margin: 0; }
  
  .tabs { display: flex; gap: 8px; border-bottom: 1px solid #1e293b; margin-bottom: 20px; }
  .tab-btn { background: none; border: none; color: #94a3b8; padding: 10px 18px; font-size: 14px; font-weight: 600; cursor: pointer; border-bottom: 2px solid transparent; transition: all .2s; }
  .tab-btn:hover { color: #f1f5f9; }
  .tab-btn.active { color: #38bdf8; border-bottom-color: #38bdf8; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  .section-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; color: #94a3b8; font-weight: 700; margin: 20px 0 10px 0; }
  .grid-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
  .card-metric { background: #131b2e; border-radius: 10px; padding: 16px; border: 1px solid #1e293b; display: flex; flex-direction: column; justify-content: space-between; }
  .card-metric h4 { margin: 0 0 6px 0; color: #94a3b8; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
  .metric-val { font-size: 20px; font-weight: 700; color: #38bdf8; }
  .metric-sub { font-size: 12px; color: #64748b; margin-top: 4px; }
  
  .split-box { margin-top: 10px; border-top: 1px solid #1e293b; padding-top: 8px; font-size: 11px; display: flex; flex-direction: column; gap: 4px; }
  .split-row { display: flex; justify-content: space-between; align-items: center; }

  .card { background: #131b2e; border-radius: 10px; padding: 18px; margin-bottom: 16px; border: 1px solid #1e293b; }
  .card-title { font-size: 15px; font-weight: 600; margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center; }
  .status-badge { font-size: 11px; padding: 3px 9px; border-radius: 9999px; font-weight: 700; }
  .status-online { background: #064e3b; color: #34d399; }
  .status-offline { background: #7f1d1d; color: #f87171; }
  
  .btn { padding: 8px 14px; border-radius: 6px; border: none; font-weight: 600; font-size: 12px; cursor: pointer; transition: opacity .15s; }
  .btn:hover { opacity: .88; }
  .btn-start { background: #10b981; color: #022c22; }
  .btn-stop { background: #ef4444; color: white; }
  .btn-refresh { background: #1e293b; color: #94a3b8; border: 1px solid #334155; }
  .btn-save { background: #38bdf8; color: #082f49; font-weight: 700; }

  input, select { background: #090d16; border: 1px solid #334155; color: #f8fafc; padding: 9px 12px; border-radius: 6px; font-size: 13px; outline: none; }
  select { width: 100%; margin-bottom: 12px; }
  .progress-bg { width: 100%; height: 7px; background: #1e293b; border-radius: 4px; overflow: hidden; margin-top: 8px; }
  .progress-bar { height: 100%; background: #3b82f6; width: 0%; transition: width .3s; }

  .cupom-form { display: flex; gap: 10px; margin-bottom: 18px; flex-wrap: wrap; }
  .cupom-table { width: 100%; border-collapse: collapse; font-size: 13px; text-align: left; }
  .cupom-table th { padding: 10px; border-bottom: 1px solid #1e293b; color: #94a3b8; font-weight: 600; }
  .cupom-table td { padding: 12px 10px; border-bottom: 1px solid #1e293b; vertical-align: middle; }
  .tag-code { font-family: monospace; font-size: 13px; background: #1e293b; color: #38bdf8; padding: 4px 8px; border-radius: 4px; font-weight: bold; }

  .config-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
  .config-item label { display: block; font-size: 12px; color: #94a3b8; font-weight: 600; margin-bottom: 6px; }
  .config-item small { display: block; font-size: 11px; color: #64748b; margin-top: 4px; }
  .alert-box { padding: 10px 14px; border-radius: 6px; font-size: 12px; margin-top: 12px; display: none; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Painel de Controle - Quadra Replay</h1>
    <button class="btn btn-refresh" onclick="carregarTudo()">Atualizar Dados</button>
  </div>

  <div class="tabs">
    <button class="tab-btn active" onclick="trocarAba('visao-geral')">Visão Geral & Sistema</button>
    <button class="tab-btn" onclick="trocarAba('cupons')">Cupons de Desconto</button>
    <button class="tab-btn" onclick="trocarAba('configuracoes')">Configurações</button>
  </div>

  <!-- ABA 1: VISÃO GERAL -->
  <div id="tab-visao-geral" class="tab-content active">
    <div class="section-label">Controle Financeiro & Repasse (70% / 30%)</div>
    <div class="grid-metrics">
      <div class="card-metric">
        <div>
          <h4>Hoje</h4>
          <div id="fin-hoje-total" class="metric-val" style="color: #4ade80;">R$ 0,00</div>
          <div id="fin-hoje-qtd" class="metric-sub">0 replays vendidos</div>
        </div>
        <div class="split-box">
          <div class="split-row">
            <span style="color: #94a3b8;">70% (Sistema):</span>
            <strong id="fin-hoje-70" style="color: #38bdf8;">R$ 0,00</strong>
          </div>
          <div class="split-row">
            <span style="color: #94a3b8;">30% (Quadra):</span>
            <strong id="fin-hoje-30" style="color: #facc15;">R$ 0,00</strong>
          </div>
        </div>
      </div>

      <div class="card-metric">
        <div>
          <h4>Mês Atual</h4>
          <div id="fin-mes-total" class="metric-val" style="color: #4ade80;">R$ 0,00</div>
          <div id="fin-mes-qtd" class="metric-sub">0 replays vendidos</div>
        </div>
        <div class="split-box">
          <div class="split-row">
            <span style="color: #94a3b8;">70% (Sistema):</span>
            <strong id="fin-mes-70" style="color: #38bdf8;">R$ 0,00</strong>
          </div>
          <div class="split-row">
            <span style="color: #94a3b8;">30% (Quadra):</span>
            <strong id="fin-mes-30" style="color: #facc15;">R$ 0,00</strong>
          </div>
        </div>
      </div>

      <div class="card-metric">
        <div>
          <h4>Total Acumulado</h4>
          <div id="fin-geral-total" class="metric-val" style="color: #22d3ee;">R$ 0,00</div>
          <div id="fin-geral-qtd" class="metric-sub">0 vendas aprovadas</div>
        </div>
        <div class="split-box">
          <div class="split-row">
            <span style="color: #94a3b8;">70% (Sistema):</span>
            <strong id="fin-geral-70" style="color: #38bdf8;">R$ 0,00</strong>
          </div>
          <div class="split-row">
            <span style="color: #94a3b8;">30% (Quadra):</span>
            <strong id="fin-geral-30" style="color: #facc15;">R$ 0,00</strong>
          </div>
        </div>
      </div>

      <div class="card-metric">
        <div>
          <h4>Bandwidth Usado</h4>
          <div id="m-bw-total" class="metric-val" style="color: #e2e8f0;">0 B</div>
          <div class="metric-sub">Tráfego de upload local</div>
        </div>
      </div>
    </div>

    <div class="section-label">Armazenamento & Mídia</div>
    <div class="grid-metrics" style="margin-bottom: 20px;">
      <div class="card-metric">
        <h4>Gravações Contínuas</h4>
        <div id="m-grav-qtd" class="metric-val">0</div>
        <div id="m-grav-tam" class="metric-sub">0 B em disco</div>
      </div>
      <div class="card-metric">
        <h4>Replays Cortados</h4>
        <div id="m-rep-qtd" class="metric-val">0</div>
        <div id="m-rep-tam" class="metric-sub">0 B em disco</div>
      </div>
      <div class="card-metric">
        <h4>Previews Locais</h4>
        <div id="m-prev-qtd" class="metric-val">0</div>
        <div id="m-prev-tam" class="metric-sub">0 B em disco</div>
      </div>
      <div class="card-metric">
        <h4>Supabase Storage</h4>
        <div id="m-supa-tam" class="metric-val">0 MB</div>
        <div id="m-supa-sub" class="metric-sub">0 arquivos (0%)</div>
        <div class="progress-bg">
          <div id="m-supa-bar" class="progress-bar"></div>
        </div>
      </div>
    </div>

    <div class="section-label">Gerenciamento de Processos</div>
    <div class="card">
      <div class="card-title">
        <span>Servidor de Pagamento e API (server.py)</span>
        <span id="badge-server" class="status-badge status-offline">OFFLINE</span>
      </div>
      <button class="btn btn-start" onclick="iniciar('server')" style="margin-right: 6px;">Iniciar Servidor</button>
      <button class="btn btn-stop" onclick="parar('server')">Parar Servidor</button>
    </div>

    <div class="card">
      <div class="card-title">
        <span>Captura da Câmera e Gravação (camera.py)</span>
        <span id="badge-camera" class="status-badge status-offline">OFFLINE</span>
      </div>
      <label style="font-size:12px; color:#94a3b8; display:block; margin-bottom:6px;">Dispositivo de Vídeo:</label>
      <select id="camera-select">
        <option value="0">Detectando câmeras...</option>
      </select>
      <button class="btn btn-start" onclick="iniciarCamera()" style="margin-right: 6px;">Iniciar Câmera</button>
      <button class="btn btn-stop" onclick="parar('camera')">Parar Câmera</button>
    </div>
  </div>

  <!-- ABA 2: CUPONS -->
  <div id="tab-cupons" class="tab-content">
    <div class="card">
      <div class="card-title">Criar Novo Cupom Promocional</div>
      <div class="cupom-form">
        <input type="text" id="cupom-nome" placeholder="CÓDIGO (ex: AMIGOS10)" style="text-transform: uppercase; min-width: 200px;">
        <select id="cupom-tipo" style="width: auto; margin-bottom: 0;">
          <option value="porcentagem">% Porcentagem de Desconto</option>
          <option value="fixo">R$ Desconto Fixo em Reais</option>
        </select>
        <input type="number" step="0.01" id="cupom-valor" placeholder="Valor (ex: 20 ou 2.50)" style="width: 170px;">
        <button class="btn btn-start" onclick="adicionarCupom()">+ Adicionar Cupom</button>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Cupons Cadastrados no Sistema</div>
      <table class="cupom-table">
        <thead>
          <tr>
            <th>Código</th>
            <th>Tipo</th>
            <th>Desconto</th>
            <th>Uso Total</th>
            <th>Ação</th>
          </tr>
        </thead>
        <tbody id="lista-cupons">
          <tr><td colspan="5" style="color: #64748b;">Carregando cupons...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ABA 3: CONFIGURAÇÕES -->
  <div id="tab-configuracoes" class="tab-content">
    <div class="card">
      <div class="card-title">Ajustes do Replay e Cobrança</div>
      
      <div class="config-grid">
        <div class="config-item">
          <label>Preço Padrão do Replay (R$):</label>
          <input type="number" id="cfg-preco" step="0.01" min="0.10" style="width: 100%;">
          <small>Valor cobrado por padrão ao gerar o Pix (Mínimo: R$ 0,10).</small>
        </div>

        <div class="config-item">
          <label>Duração do Replay Cortado (Segundos):</label>
          <input type="number" id="cfg-duracao" min="10" max="300" style="width: 100%;">
          <small>Tempo retroativo salvo da jogada (ex: 20s, 60s ou 90s).</small>
        </div>
      </div>

      <button class="btn btn-save" onclick="salvarConfiguracoes()">Salvar Configurações</button>
      <div id="cfg-msg" class="alert-box"></div>
    </div>
  </div>
</div>

<script>
function trocarAba(nomeAba) {
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
  
  if (nomeAba === 'visao-geral') {
    document.querySelector("button[onclick*='visao-geral']").classList.add('active');
    document.getElementById('tab-visao-geral').classList.add('active');
  } else if (nomeAba === 'cupons') {
    document.querySelector("button[onclick*='cupons']").classList.add('active');
    document.getElementById('tab-cupons').classList.add('active');
    carregarCupons();
  } else if (nomeAba === 'configuracoes') {
    document.querySelector("button[onclick*='configuracoes']").classList.add('active');
    document.getElementById('tab-configuracoes').classList.add('active');
    carregarConfiguracoes();
  }
}

async function atualizarStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    configurarBadge('server', data.server);
    configurarBadge('camera', data.camera);
  } catch (e) {
    console.error(e);
  }
}

function configurarBadge(servico, status) {
  const badge = document.getElementById(`badge-${servico}`);
  badge.textContent = status;
  badge.className = `status-badge ${status === 'ONLINE' ? 'status-online' : 'status-offline'}`;
}

async function atualizarMetricas() {
  try {
    const res = await fetch('/api/metricas');
    const d = await res.json();

    document.getElementById('m-grav-qtd').textContent = d.gravacoes.qtd;
    document.getElementById('m-grav-tam').textContent = `${d.gravacoes.tamanho} em disco`;

    document.getElementById('m-rep-qtd').textContent = d.replays.qtd;
    document.getElementById('m-rep-tam').textContent = `${d.replays.tamanho} em disco`;

    document.getElementById('m-prev-qtd').textContent = d.previews.qtd;
    document.getElementById('m-prev-tam').textContent = `${d.previews.tamanho} em disco`;

    document.getElementById('m-supa-tam').textContent = d.supabase.formatado;
    document.getElementById('m-supa-sub').textContent = `${d.supabase.qtd} arquivos (${d.supabase.pct}% de 1GB)`;
    document.getElementById('m-bw-total').textContent = d.bandwidth;

    const bar = document.getElementById('m-supa-bar');
    bar.style.width = `${Math.min(d.supabase.pct, 100)}%`;
    bar.style.background = d.supabase.pct > 80 ? '#ef4444' : '#3b82f6';
  } catch (e) {
    console.error("Erro ao puxar métricas:", e);
  }
}

async function atualizarFinanceiro() {
  try {
    const res = await fetch('/api/financeiro');
    const d = await res.json();

    // Hoje
    document.getElementById('fin-hoje-total').textContent = d.hoje.total;
    document.getElementById('fin-hoje-qtd').textContent = `${d.hoje.qtd} replays vendidos`;
    document.getElementById('fin-hoje-70').textContent = d.hoje.p70;
    document.getElementById('fin-hoje-30').textContent = d.hoje.p30;

    // Mês Atual
    document.getElementById('fin-mes-total').textContent = d.mes.total;
    document.getElementById('fin-mes-qtd').textContent = `${d.mes.qtd} replays vendidos`;
    document.getElementById('fin-mes-70').textContent = d.mes.p70;
    document.getElementById('fin-mes-30').textContent = d.mes.p30;

    // Total Acumulado
    document.getElementById('fin-geral-total').textContent = d.geral.total;
    document.getElementById('fin-geral-qtd').textContent = `${d.geral.qtd} vendas aprovadas`;
    document.getElementById('fin-geral-70').textContent = d.geral.p70;
    document.getElementById('fin-geral-30').textContent = d.geral.p30;
  } catch (e) {
    console.error("Erro financeiro:", e);
  }
}

async function carregarCupons() {
  const tbody = document.getElementById('lista-cupons');
  try {
    const res = await fetch('/api/cupons');
    const cupons = await res.json();
    tbody.innerHTML = '';
    
    if (!cupons || cupons.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="color: #64748b; padding: 14px 10px;">Nenhum cupom ativo no momento.</td></tr>';
      return;
    }

    cupons.forEach(c => {
      const desc = c.tipo === 'porcentagem' ? `${c.valor}% OFF` : `R$ ${parseFloat(c.valor).toFixed(2).replace(".", ",")} OFF`;
      tbody.innerHTML += `
        <tr>
          <td><span class="tag-code">${c.codigo}</span></td>
          <td style="text-transform: capitalize; color: #cbd5e1;">${c.tipo}</td>
          <td style="color: #4ade80; font-weight: 600;">${desc}</td>
          <td style="color: #94a3b8;">${c.usos || 0} vezes</td>
          <td><button class="btn btn-stop" style="padding: 5px 10px; font-size: 11px;" onclick="removerCupom('${c.id}')">Excluir</button></td>
        </tr>
      `;
    });
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="5" style="color: #ef4444; padding: 14px 10px;">Falha ao carregar lista de cupons.</td></tr>';
  }
}

async function adicionarCupom() {
  const codigo = document.getElementById('cupom-nome').value.trim().toUpperCase();
  const tipo = document.getElementById('cupom-tipo').value;
  const valor = document.getElementById('cupom-valor').value;

  if (!codigo || !valor || valor <= 0) {
    alert("Informe um código e um valor de desconto válido.");
    return;
  }

  const res = await fetch('/api/cupons', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ codigo, tipo, valor })
  });

  const d = await res.json();
  if (res.ok) {
    document.getElementById('cupom-nome').value = '';
    document.getElementById('cupom-valor').value = '';
    carregarCupons();
  } else {
    alert(d.erro || "Falha ao cadastrar cupom");
  }
}

async function removerCupom(id) {
  if (confirm("Remover permanentemente este cupom?")) {
    const res = await fetch(`/api/cupons/${id}`, { method: 'DELETE' });
    if (res.ok) {
      carregarCupons();
    } else {
      alert("Erro ao excluir cupom");
    }
  }
}

async function carregarConfiguracoes() {
  try {
    const res = await fetch('/api/configuracoes');
    const data = await res.json();
    document.getElementById('cfg-preco').value = data.preco_replay;
    document.getElementById('cfg-duracao').value = data.duracao_replay_segundos;
  } catch (e) {
    console.error("Erro ao carregar configurações:", e);
  }
}

async function salvarConfiguracoes() {
  const preco = parseFloat(document.getElementById('cfg-preco').value);
  const duracao = parseInt(document.getElementById('cfg-duracao').value);
  const msgEl = document.getElementById('cfg-msg');

  try {
    const res = await fetch('/api/configuracoes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        preco_replay: preco,
        duracao_replay_segundos: duracao
      })
    });
    const d = await res.json();
    
    msgEl.style.display = 'block';
    if (res.ok) {
      msgEl.style.background = '#064e3b';
      msgEl.style.color = '#34d399';
      msgEl.textContent = 'Configurações salvas! Novos replays e cobranças Pix usarão esses valores imediatamente.';
    } else {
      msgEl.style.background = '#7f1d1d';
      msgEl.style.color = '#f87171';
      msgEl.textContent = d.erro || 'Falha ao salvar.';
    }
  } catch (e) {
    msgEl.style.display = 'block';
    msgEl.style.background = '#7f1d1d';
    msgEl.style.color = '#f87171';
    msgEl.textContent = 'Erro ao se comunicar com o servidor.';
  }

  setTimeout(() => { msgEl.style.display = 'none'; }, 4000);
}

async function listarCameras() {
  const select = document.getElementById('camera-select');
  try {
    const res = await fetch('/api/cameras');
    const data = await res.json();
    select.innerHTML = '';
    if (!data || data.length === 0) {
      select.innerHTML = '<option value="0">Nenhuma câmera detectada (Fallback: 0)</option>';
      return;
    }
    data.forEach(cam => {
      const opt = document.createElement('option');
      opt.value = cam.index;
      opt.textContent = `${cam.nome} [Índice ${cam.index}] — ${cam.resolucao}`;
      select.appendChild(opt);
    });
  } catch (e) {
    select.innerHTML = '<option value="0">Erro ao listar dispositivos</option>';
  }
}

async function iniciar(servico) {
  await fetch(`/api/iniciar/${servico}`, { method: 'POST' });
  setTimeout(atualizarStatus, 1000);
}

async function iniciarCamera() {
  const camIdx = document.getElementById('camera-select').value;
  await fetch('/api/iniciar/camera', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ camera_index: camIdx })
  });
  setTimeout(atualizarStatus, 1000);
}

async function parar(servico) {
  await fetch(`/api/parar/${servico}`, { method: 'POST' });
  setTimeout(atualizarStatus, 1000);
}

function carregarTudo() {
  atualizarStatus();
  atualizarMetricas();
  atualizarFinanceiro();
  carregarCupons();
  carregarConfiguracoes();
}

listarCameras();
carregarTudo();

setInterval(atualizarStatus, 3000);
setInterval(atualizarMetricas, 10000);
setInterval(atualizarFinanceiro, 15000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)