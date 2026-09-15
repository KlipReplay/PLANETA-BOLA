import os
import sys
import subprocess
import json
import time
from datetime import datetime
import cv2
from flask import Flask, jsonify, request, render_template_string, Response
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
    padrao = {
        "preco_replay": 0.15,
        "duracao_buffer_segundos": 60,
        "tempo_pre_clique_segundos": 20,
        "tempo_pos_clique_segundos": 5,
        "duracao_preview_segundos": 5
    }
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

# ============================================================
# ROTAS DA API
# ============================================================
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
        buffer_total = int(dados.get("duracao_buffer_segundos", 60))
        tempo_pre = int(dados.get("tempo_pre_clique_segundos", 20))
        tempo_pos = int(dados.get("tempo_pos_clique_segundos", 5))
        duracao_prev = int(dados.get("duracao_preview_segundos", 5))

        if preco < 0.10:
            return jsonify({"erro": "O preço mínimo é R$ 0,10"}), 400
        if buffer_total < 30 or buffer_total > 600:
            return jsonify({"erro": "O buffer na RAM/Disco deve ficar entre 30 e 600 segundos"}), 400
        if tempo_pre < 5 or tempo_pre > buffer_total:
            return jsonify({"erro": "O tempo pré-clique não pode exceder o buffer total"}), 400
        if tempo_pos < 0 or tempo_pos > 60:
            return jsonify({"erro": "O tempo pós-clique deve ficar entre 0 e 60 segundos"}), 400
        if duracao_prev < 2 or duracao_prev > 15:
            return jsonify({"erro": "A prévia deve ter entre 2 e 15 segundos"}), 400

        nova_config = {
            "preco_replay": round(preco, 2),
            "duracao_buffer_segundos": buffer_total,
            "tempo_pre_clique_segundos": tempo_pre,
            "tempo_pos_clique_segundos": tempo_pos,
            "duracao_preview_segundos": duracao_prev
        }

        if salvar_config_local(nova_config):
            return jsonify({"mensagem": "Configurações salvas!", "config": nova_config}), 200
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

# ============================================================
# STREAMING DE TESTE DA CÂMERA SOB DEMANDA (SEM JANELA FIXA)
# ============================================================
def gerar_frames_preview(cam_idx):
    backend = cv2.CAP_MSMF if sys.platform == "win32" else cv2.CAP_ANY
    cap = cv2.VideoCapture(int(cam_idx), backend)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    
    try:
        while True:
            sucesso, frame = cap.read()
            if not sucesso:
                break
            # Insere data/hora no frame de teste
            texto = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            cv2.putText(frame, texto, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 230, 118), 2)
            
            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
            time.sleep(0.04) # ~25 FPS
    finally:
        cap.release()

@app.route("/video_feed")
def video_feed():
    cam_idx = request.args.get("index", "0")
    return Response(gerar_frames_preview(cam_idx),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


HTML_DASHBOARD = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin - Central de Controle Arena</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lucide@latest"></script>
<style>
  :root {
    --bg-main: #090c14;
    --bg-card: #0f1523;
    --bg-inner: #151d30;
    --border: #1e293f;
    --accent: #00e676;
    --accent-blue: #38bdf8;
    --danger: #ef4444;
    --warning: #facc15;
    --text-primary: #f8fafc;
    --text-secondary: #94a3b8;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Outfit', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--bg-main);
    color: var(--text-primary);
    padding: 30px 20px;
  }
  .font-mono { font-family: 'Space Grotesk', monospace; }
  .container { max-width: 1040px; margin: 0 auto; }

  /* HEADER */
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--border);
    padding-bottom: 20px;
    margin-bottom: 24px;
  }
  .brand { display: flex; align-items: center; gap: 12px; }
  .brand-logo {
    width: 42px;
    height: 42px;
    background: linear-gradient(135deg, #182338, #0f172a);
    border: 1px solid var(--border);
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--accent);
  }
  .brand-title { font-size: 20px; font-weight: 800; letter-spacing: 0.5px; }
  .brand-sub { font-size: 12px; color: var(--text-secondary); }

  /* TABS */
  .tabs { display: flex; gap: 10px; margin-bottom: 24px; }
  .tab-btn {
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text-secondary);
    padding: 10px 18px;
    border-radius: 10px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 8px;
    transition: all .2s;
  }
  .tab-btn:hover { color: #fff; border-color: #334155; }
  .tab-btn.active {
    background: rgba(0, 230, 118, 0.1);
    color: var(--accent);
    border-color: rgba(0, 230, 118, 0.4);
    box-shadow: 0 4px 12px rgba(0, 230, 118, 0.15);
  }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  /* CARDS & GRIDS */
  .section-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-secondary);
    font-weight: 800;
    margin: 24px 0 12px 0;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .grid-metrics {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 14px;
    margin-bottom: 20px;
  }
  .card-metric {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 18px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
  }
  .card-metric h4 {
    font-size: 12px;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 6px;
  }
  .metric-val { font-size: 24px; font-weight: 800; }
  .metric-sub { font-size: 12px; color: #64748b; margin-top: 4px; }

  .split-box {
    margin-top: 12px;
    border-top: 1px solid var(--border);
    padding-top: 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
  }
  .split-row { display: flex; justify-content: space-between; align-items: center; }

  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px;
    margin-bottom: 16px;
  }
  .card-title {
    font-size: 15px;
    font-weight: 700;
    margin-bottom: 16px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  /* BOTÕES E CONTROLES */
  .btn {
    padding: 9px 16px;
    border-radius: 10px;
    border: none;
    font-weight: 700;
    font-size: 12px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    transition: all .2s;
  }
  .btn:hover { opacity: .9; transform: translateY(-1px); }
  .btn-start { background: var(--accent); color: #08090d; }
  .btn-stop { background: var(--danger); color: white; }
  .btn-blue { background: var(--accent-blue); color: #082f49; }
  .btn-outline { background: var(--bg-inner); color: var(--text-secondary); border: 1px solid var(--border); }
  .btn-outline:hover { color: #fff; border-color: #475569; }

  .status-badge {
    font-size: 10px;
    padding: 4px 10px;
    border-radius: 9999px;
    font-weight: 800;
    letter-spacing: 0.5px;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .status-online { background: rgba(0, 230, 118, 0.15); color: var(--accent); border: 1px solid rgba(0, 230, 118, 0.3); }
  .status-offline { background: rgba(239, 68, 68, 0.15); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.3); }
  .dot-pulse { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }

  input, select {
    background: var(--bg-inner);
    border: 1px solid var(--border);
    color: var(--text-primary);
    padding: 10px 14px;
    border-radius: 10px;
    font-size: 13px;
    outline: none;
    transition: border-color .2s;
  }
  input:focus, select:focus { border-color: var(--accent); }
  select { width: 100%; margin-bottom: 14px; }

  /* FORMULÁRIO DE TEMPOS DO REPLAY */
  .config-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 20px;
  }
  .config-item { display: flex; flex-direction: column; gap: 6px; }
  .config-item label { font-size: 12px; font-weight: 600; color: var(--text-secondary); }
  .config-item small { font-size: 11px; color: #64748b; line-height: 1.4; }

  /* TABELA CUPONS */
  .cupom-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 10px; }
  .cupom-table th { padding: 12px; border-bottom: 1px solid var(--border); color: var(--text-secondary); font-weight: 700; text-align: left; }
  .cupom-table td { padding: 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  .tag-code { font-family: 'Space Grotesk', monospace; font-size: 12px; background: var(--bg-inner); border: 1px solid var(--border); color: var(--accent-blue); padding: 4px 8px; border-radius: 6px; font-weight: 700; }

  /* MODAL PREVIEW CÂMERA */
  .modal-overlay {
    display: none;
    position: fixed;
    inset: 0;
    z-index: 999;
    background: rgba(0, 0, 0, 0.88);
    backdrop-filter: blur(8px);
    align-items: center;
    justify-content: center;
    padding: 16px;
  }
  .modal-overlay.open { display: flex; }
  .modal-box {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 20px;
    max-width: 800px;
    width: 100%;
    overflow: hidden;
    position: relative;
    box-shadow: 0 25px 50px rgba(0,0,0,0.8);
  }
  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
  }
  .modal-feed {
    width: 100%;
    aspect-ratio: 16/9;
    background: #000;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .modal-feed img { width: 100%; height: 100%; object-fit: contain; }

  .progress-bg { width: 100%; height: 8px; background: var(--bg-inner); border-radius: 9999px; overflow: hidden; margin-top: 10px; }
  .progress-bar { height: 100%; background: var(--accent-blue); width: 0%; transition: width .3s; }
  .alert-box { padding: 12px 16px; border-radius: 10px; font-size: 12px; margin-top: 14px; display: none; }
</style>
</head>
<body>

<div class="container">
  <!-- HEADER -->
  <div class="header">
    <div class="brand">
      <div class="brand-logo"><i data-lucide="video"></i></div>
      <div>
        <div class="brand-title">KLIP REPLAY <span style="color: var(--accent); font-size: 14px;">ADMIN</span></div>
        <div class="brand-sub">Gerenciador de Processos, Tempos e Faturamento</div>
      </div>
    </div>
    <button class="btn btn-outline" onclick="carregarTudo()">
      <i data-lucide="refresh-cw" id="btn-sync-icon"></i> Sincronizar
    </button>
  </div>

  <!-- TABS -->
  <div class="tabs">
    <button class="tab-btn active" onclick="trocarAba('visao-geral')"><i data-lucide="activity"></i> Operação & Sistema</button>
    <button class="tab-btn" onclick="trocarAba('tempos')"><i data-lucide="clock"></i> Tempos & Replay</button>
    <button class="tab-btn" onclick="trocarAba('cupons')"><i data-lucide="tag"></i> Cupons Promocionais</button>
  </div>

  <!-- ABA 1: OPERAÇÃO E DISPOSITIVOS -->
  <div id="tab-visao-geral" class="tab-content active">
    <div class="section-label"><i data-lucide="dollar-sign"></i> Faturamento & Repasse (70% Sistema / 30% Quadra)</div>
    <div class="grid-metrics">
      <div class="card-metric">
        <div>
          <h4>Hoje</h4>
          <div id="fin-hoje-total" class="metric-val" style="color: var(--accent);">R$ 0,00</div>
          <div id="fin-hoje-qtd" class="metric-sub">0 replays vendidos</div>
        </div>
        <div class="split-box">
          <div class="split-row"><span>70% Sistema:</span><strong id="fin-hoje-70" style="color: var(--accent-blue);">R$ 0,00</strong></div>
          <div class="split-row"><span>30% Quadra:</span><strong id="fin-hoje-30" style="color: var(--warning);">R$ 0,00</strong></div>
        </div>
      </div>

      <div class="card-metric">
        <div>
          <h4>Mês Atual</h4>
          <div id="fin-mes-total" class="metric-val" style="color: var(--accent);">R$ 0,00</div>
          <div id="fin-mes-qtd" class="metric-sub">0 replays vendidos</div>
        </div>
        <div class="split-box">
          <div class="split-row"><span>70% Sistema:</span><strong id="fin-mes-70" style="color: var(--accent-blue);">R$ 0,00</strong></div>
          <div class="split-row"><span>30% Quadra:</span><strong id="fin-mes-30" style="color: var(--warning);">R$ 0,00</strong></div>
        </div>
      </div>

      <div class="card-metric">
        <div>
          <h4>Total Acumulado</h4>
          <div id="fin-geral-total" class="metric-val" style="color: #38bdf8;">R$ 0,00</div>
          <div id="fin-geral-qtd" class="metric-sub">0 vendas aprovadas</div>
        </div>
        <div class="split-box">
          <div class="split-row"><span>70% Sistema:</span><strong id="fin-geral-70" style="color: var(--accent-blue);">R$ 0,00</strong></div>
          <div class="split-row"><span>30% Quadra:</span><strong id="fin-geral-30" style="color: var(--warning);">R$ 0,00</strong></div>
        </div>
      </div>

      <div class="card-metric">
        <div>
          <h4>Supabase Storage</h4>
          <div id="m-supa-tam" class="metric-val" style="color: #cbd5e1;">0 MB</div>
          <div id="m-supa-sub" class="metric-sub">0 arquivos (0%)</div>
        </div>
        <div class="progress-bg"><div id="m-supa-bar" class="progress-bar"></div></div>
      </div>
    </div>

    <div class="section-label"><i data-lucide="cpu"></i> Gerenciamento de Serviços</div>
    <div class="card">
      <div class="card-title">
        <span>Servidor de Pagamento Pix & API (server.py)</span>
        <span id="badge-server" class="status-badge status-offline"><span class="dot-pulse"></span>OFFLINE</span>
      </div>
      <div style="display: flex; gap: 8px;">
        <button class="btn btn-start" onclick="iniciar('server')"><i data-lucide="play"></i> Iniciar Servidor</button>
        <button class="btn btn-stop" onclick="parar('server')"><i data-lucide="square"></i> Parar Servidor</button>
      </div>
    </div>

    <div class="card">
      <div class="card-title">
        <span>Captura da Câmera & Detecção de Lance (camera.py)</span>
        <span id="badge-camera" class="status-badge status-offline"><span class="dot-pulse"></span>OFFLINE</span>
      </div>
      <label style="font-size:12px; color:var(--text-secondary); display:block; margin-bottom:8px;">Dispositivo de Vídeo Conectado:</label>
      <select id="camera-select">
        <option value="0">Detectando câmeras...</option>
      </select>
      
      <div style="display: flex; gap: 8px; flex-wrap: wrap;">
        <button class="btn btn-start" onclick="iniciarCamera()"><i data-lucide="play"></i> Iniciar Gravação Câmera</button>
        <button class="btn btn-stop" onclick="parar('camera')"><i data-lucide="square"></i> Parar Câmera</button>
        <!-- BOTÃO DE VISUALIZAÇÃO SOB DEMANDA (SEM JANELA FIXA) -->
        <button class="btn btn-blue" onclick="abrirPreviewCamera()"><i data-lucide="eye"></i> Visualizar Câmera (Preview)</button>
      </div>
    </div>
  </div>

  <!-- ABA 2: TEMPOS DO REPLAY E PREÇO -->
  <div id="tab-tempos" class="tab-content">
    <div class="card">
      <div class="card-title">Ajustes de Buffer e Duração dos Lances</div>
      
      <div class="config-grid">
        <div class="config-item">
          <label>Buffer Total Mantido na Memória (segundos):</label>
          <input type="number" id="cfg-buffer" min="30" max="300">
          <small>Tamanho da fila circular contínua (ex: 60s). Define o limite máximo que a câmera guarda.</small>
        </div>

        <div class="config-item">
          <label>Tempo Retroativo Pré-Clique (segundos):</label>
          <input type="number" id="cfg-tempo-pre" min="5" max="120">
          <small>Quantos segundos antes de pressionar o botão da quadra devem entrar no replay.</small>
        </div>

        <div class="config-item">
          <label>Tempo de Gravação Pós-Clique (segundos):</label>
          <input type="number" id="cfg-tempo-pos" min="0" max="30">
          <small>Quantos segundos após o acionamento ainda serão gravados (para pegar a comemoração).</small>
        </div>

        <div class="config-item">
          <label>Duração do Preview Web (segundos):</label>
          <input type="number" id="cfg-preview" min="2" max="15">
          <small>Duração do clipe rápido gratuito exibido no feed do site (padrão: 5s).</small>
        </div>

        <div class="config-item">
          <label>Preço Padrão do Replay (R$):</label>
          <input type="number" id="cfg-preco" step="0.01" min="0.10">
          <small>Cobrança padrão na emissão do QR Code Pix (Mínimo: R$ 0,10).</small>
        </div>
      </div>

      <button class="btn btn-start" onclick="salvarConfiguracoes()"><i data-lucide="check"></i> Salvar Parâmetros</button>
      <div id="cfg-msg" class="alert-box"></div>
    </div>
  </div>

  <!-- ABA 3: CUPONS -->
  <div id="tab-cupons" class="tab-content">
    <div class="card">
      <div class="card-title">Criar Novo Cupom Promocional</div>
      <div style="display: flex; gap: 10px; flex-wrap: wrap;">
        <input type="text" id="cupom-nome" placeholder="CÓDIGO (ex: FINAL10)" style="text-transform: uppercase; min-width: 180px;">
        <select id="cupom-tipo" style="width: auto; margin-bottom: 0;">
          <option value="porcentagem">% Porcentagem</option>
          <option value="fixo">R$ Fixo</option>
        </select>
        <input type="number" step="0.01" id="cupom-valor" placeholder="Valor (ex: 20 ou 2.00)" style="width: 160px;">
        <button class="btn btn-start" onclick="adicionarCupom()"><i data-lucide="plus"></i> Cadastrar Cupom</button>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Cupons Cadastrados no Supabase</div>
      <table class="cupom-table">
        <thead>
          <tr>
            <th>Código</th>
            <th>Tipo</th>
            <th>Desconto</th>
            <th>Usos</th>
            <th>Ação</th>
          </tr>
        </thead>
        <tbody id="lista-cupons">
          <tr><td colspan="5" style="color: #64748b;">Carregando cupons...</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<!-- MODAL DE PREVIEW DA CÂMERA -->
<div class="modal-overlay" id="camera-modal">
  <div class="modal-box">
    <div class="modal-header">
      <div style="display: flex; align-items: center; gap: 8px;">
        <i data-lucide="camera" style="color: var(--accent); width: 18px; height: 18px;"></i>
        <strong style="font-size: 14px;">Preview Ao Vivo do Dispositivo</strong>
      </div>
      <button class="btn btn-outline" style="padding: 4px 10px;" onclick="fecharPreviewCamera()">Fechar ✕</button>
    </div>
    <div class="modal-feed">
      <img id="feed-img" src="" alt="Aguardando Feed da Câmera...">
    </div>
    <div style="padding: 12px 20px; font-size: 11px; color: var(--text-secondary); display: flex; justify-content: space-between;">
      <span>💡 Use esta tela para ajustar foco e enquadramento da quadra.</span>
      <span style="color: var(--accent);">Feed temporário sob demanda</span>
    </div>
  </div>
</div>

<script>
function trocarAba(aba) {
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
  
  if (aba === 'visao-geral') {
    document.querySelector("button[onclick*='visao-geral']").classList.add('active');
    document.getElementById('tab-visao-geral').classList.add('active');
  } else if (aba === 'tempos') {
    document.querySelector("button[onclick*='tempos']").classList.add('active');
    document.getElementById('tab-tempos').classList.add('active');
    carregarConfiguracoes();
  } else if (aba === 'cupons') {
    document.querySelector("button[onclick*='cupons']").classList.add('active');
    document.getElementById('tab-cupons').classList.add('active');
    carregarCupons();
  }
}

async function atualizarStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    configurarBadge('server', data.server);
    configurarBadge('camera', data.camera);
  } catch (e) {}
}

function configurarBadge(servico, status) {
  const badge = document.getElementById(`badge-${servico}`);
  badge.innerHTML = `<span class="dot-pulse"></span>${status}`;
  badge.className = `status-badge ${status === 'ONLINE' ? 'status-online' : 'status-offline'}`;
}

async function atualizarMetricas() {
  try {
    const res = await fetch('/api/metricas');
    const d = await res.json();
    document.getElementById('m-supa-tam').textContent = d.supabase.formatado;
    document.getElementById('m-supa-sub').textContent = `${d.supabase.qtd} arquivos (${d.supabase.pct}% de 1GB)`;
    const bar = document.getElementById('m-supa-bar');
    bar.style.width = `${Math.min(d.supabase.pct, 100)}%`;
    bar.style.background = d.supabase.pct > 80 ? 'var(--danger)' : 'var(--accent-blue)';
  } catch (e) {}
}

async function atualizarFinanceiro() {
  try {
    const res = await fetch('/api/financeiro');
    const d = await res.json();

    document.getElementById('fin-hoje-total').textContent = d.hoje.total;
    document.getElementById('fin-hoje-qtd').textContent = `${d.hoje.qtd} replays vendidos`;
    document.getElementById('fin-hoje-70').textContent = d.hoje.p70;
    document.getElementById('fin-hoje-30').textContent = d.hoje.p30;

    document.getElementById('fin-mes-total').textContent = d.mes.total;
    document.getElementById('fin-mes-qtd').textContent = `${d.mes.qtd} replays vendidos`;
    document.getElementById('fin-mes-70').textContent = d.mes.p70;
    document.getElementById('fin-mes-30').textContent = d.mes.p30;

    document.getElementById('fin-geral-total').textContent = d.geral.total;
    document.getElementById('fin-geral-qtd').textContent = `${d.geral.qtd} vendas aprovadas`;
    document.getElementById('fin-geral-70').textContent = d.geral.p70;
    document.getElementById('fin-geral-30').textContent = d.geral.p30;
  } catch (e) {}
}

async function carregarConfiguracoes() {
  try {
    const res = await fetch('/api/configuracoes');
    const d = await res.json();
    document.getElementById('cfg-preco').value = d.preco_replay;
    document.getElementById('cfg-buffer').value = d.duracao_buffer_segundos || 60;
    document.getElementById('cfg-tempo-pre').value = d.tempo_pre_clique_segundos || 20;
    document.getElementById('cfg-tempo-pos').value = d.tempo_pos_clique_segundos || 5;
    document.getElementById('cfg-preview').value = d.duracao_preview_segundos || 5;
  } catch (e) {}
}

async function salvarConfiguracoes() {
  const preco = parseFloat(document.getElementById('cfg-preco').value);
  const buffer = parseInt(document.getElementById('cfg-buffer').value);
  const pre = parseInt(document.getElementById('cfg-tempo-pre').value);
  const pos = parseInt(document.getElementById('cfg-tempo-pos').value);
  const prev = parseInt(document.getElementById('cfg-preview').value);
  const msgEl = document.getElementById('cfg-msg');

  try {
    const res = await fetch('/api/configuracoes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        preco_replay: preco,
        duracao_buffer_segundos: buffer,
        tempo_pre_clique_segundos: pre,
        tempo_pos_clique_segundos: pos,
        duracao_preview_segundos: prev
      })
    });
    const d = await res.json();
    msgEl.style.display = 'block';
    if (res.ok) {
      msgEl.style.background = 'rgba(0, 230, 118, 0.15)';
      msgEl.style.color = 'var(--accent)';
      msgEl.textContent = 'Parâmetros atualizados! camera.py e server.py aplicarão esses valores.';
    } else {
      msgEl.style.background = 'rgba(239, 68, 68, 0.15)';
      msgEl.style.color = 'var(--danger)';
      msgEl.textContent = d.erro || 'Falha ao salvar.';
    }
  } catch (e) {
    msgEl.style.display = 'block';
    msgEl.style.background = 'rgba(239, 68, 68, 0.15)';
    msgEl.style.color = 'var(--danger)';
    msgEl.textContent = 'Erro de comunicação.';
  }
  setTimeout(() => { msgEl.style.display = 'none'; }, 4000);
}

async function carregarCupons() {
  const tbody = document.getElementById('lista-cupons');
  try {
    const res = await fetch('/api/cupons');
    const cupons = await res.json();
    tbody.innerHTML = '';
    if (!cupons || cupons.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="color: #64748b; padding: 14px 10px;">Nenhum cupom ativo.</td></tr>';
      return;
    }
    cupons.forEach(c => {
      const desc = c.tipo === 'porcentagem' ? `${c.valor}% OFF` : `R$ ${parseFloat(c.valor).toFixed(2).replace(".", ",")} OFF`;
      tbody.innerHTML += `
        <tr>
          <td><span class="tag-code">${c.codigo}</span></td>
          <td style="text-transform: capitalize; color: #cbd5e1;">${c.tipo}</td>
          <td style="color: var(--accent); font-weight: 700;">${desc}</td>
          <td style="color: #94a3b8;">${c.usos || 0}x</td>
          <td><button class="btn btn-stop" style="padding: 4px 8px; font-size: 11px;" onclick="removerCupom('${c.id}')">Excluir</button></td>
        </tr>
      `;
    });
  } catch (e) {}
}

async function adicionarCupom() {
  const codigo = document.getElementById('cupom-nome').value.trim().toUpperCase();
  const tipo = document.getElementById('cupom-tipo').value;
  const valor = document.getElementById('cupom-valor').value;

  if (!codigo || !valor) return alert("Informe código e valor.");

  const res = await fetch('/api/cupons', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ codigo, tipo, valor })
  });
  if (res.ok) {
    document.getElementById('cupom-nome').value = '';
    document.getElementById('cupom-valor').value = '';
    carregarCupons();
  } else {
    const d = await res.json();
    alert(d.erro || "Falha ao criar cupom");
  }
}

async function removerCupom(id) {
  if (confirm("Excluir este cupom?")) {
    await fetch(`/api/cupons/${id}`, { method: 'DELETE' });
    carregarCupons();
  }
}

async function listarCameras() {
  const select = document.getElementById('camera-select');
  try {
    const res = await fetch('/api/cameras');
    const data = await res.json();
    select.innerHTML = '';
    if (!data || data.length === 0) {
      select.innerHTML = '<option value="0">Dispositivo Padrão (Índice 0)</option>';
      return;
    }
    data.forEach(cam => {
      const opt = document.createElement('option');
      opt.value = cam.index;
      opt.textContent = `${cam.nome} [Índice ${cam.index}] — ${cam.resolucao}`;
      select.appendChild(opt);
    });
  } catch (e) {
    select.innerHTML = '<option value="0">Índice 0</option>';
  }
}

function abrirPreviewCamera() {
  const camIdx = document.getElementById('camera-select').value;
  const feedImg = document.getElementById('feed-img');
  feedImg.src = `/video_feed?index=${camIdx}&t=${Date.now()}`;
  document.getElementById('camera-modal').classList.add('open');
}

function fecharPreviewCamera() {
  document.getElementById('feed-img').src = '';
  document.getElementById('camera-modal').classList.remove('open');
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
  const icon = document.getElementById('btn-sync-icon');
  if (icon) icon.classList.add('lucide-spin');
  atualizarStatus();
  atualizarMetricas();
  atualizarFinanceiro();
  carregarConfiguracoes();
  carregarCupons();
  setTimeout(() => { if (icon) icon.classList.remove('lucide-spin'); }, 600);
}

listarCameras();
carregarTudo();
setInterval(atualizarStatus, 3000);
setInterval(atualizarFinanceiro, 15000);
setTimeout(() => { if (window.lucide) lucide.createIcons(); }, 100);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)