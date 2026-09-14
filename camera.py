import os
import sys
import time
import json
import shutil
import subprocess
import threading
from collections import deque
from datetime import datetime, timezone
from queue import Queue

import cv2
from dotenv import load_dotenv
import requests as remote_req
from supabase import create_client

load_dotenv()

# ============================================================
# DIRETÓRIOS E AMBIENTE
# ============================================================
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

GRAVACOES_DIR = os.path.join(BASE_DIR, "Gravações")
REPLAYS_DIR   = os.path.join(BASE_DIR, "Replays")
PREVIEWS_DIR  = os.path.join(BASE_DIR, "Previews")
CONFIG_FILE   = os.path.join(BASE_DIR, "config.json")

os.makedirs(GRAVACOES_DIR, exist_ok=True)
os.makedirs(REPLAYS_DIR, exist_ok=True)
os.makedirs(PREVIEWS_DIR, exist_ok=True)

# Busca FFmpeg local ou nas variáveis de ambiente do sistema
FFMPEG_BIN = os.path.join(BASE_DIR, "ffmpeg.exe")
if not os.path.exists(FFMPEG_BIN):
    FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

LOG_FILE       = os.path.join(BASE_DIR, "log.txt")
ALERTA_FILE    = os.path.join(BASE_DIR, "ALERTA.txt")
BANDWIDTH_FILE = os.path.join(BASE_DIR, "bandwidth.json")

# ============================================================
# PARÂMETROS OPERACIONAIS
# ============================================================
FPS              = int(os.getenv("CAMERA_FPS", "30"))
SEGMENT_DURATION = int(os.getenv("SEGMENT_DURATION_SEC", "3600"))

STORAGE_LIMITE_BYTES   = int(os.getenv("STORAGE_LIMITE_BYTES", str(1 * 1024 * 1024 * 1024)))
BANDWIDTH_LIMITE_BYTES = int(os.getenv("BANDWIDTH_LIMITE_BYTES", str(2 * 1024 * 1024 * 1024)))
ALERTA_PERCENTUAL      = 70.0

CONTINUO_LOCAL_HORAS = 5
REPLAY_LOCAL_HORAS   = 24
PREVIEW_LOCAL_HORAS  = 2
NUVEM_HORAS          = 24

REMOTE_PANEL_URL = os.getenv("REMOTE_PANEL_URL", "")


def obter_duracao_replay():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
                return int(dados.get("duracao_replay_segundos", 20))
        except Exception:
            pass
    return int(os.getenv("REPLAY_DURATION_SEC", "20"))

# ============================================================
# SUPABASE CLIENT
# ============================================================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL ou SUPABASE_KEY não configurados no .env")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# CONTROLE DE EXECUÇÃO E LOGS
# ============================================================
running = True
# Buffer com capacidade de até 300 segundos (5 minutos) para suportar durações configuráveis
MAX_BUFFER_SEGUNDOS = 300
frame_buffer = deque(maxlen=FPS * MAX_BUFFER_SEGUNDOS)
event_queue = Queue()
cap = None


def log_error(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def carregar_bandwidth():
    try:
        if os.path.exists(BANDWIDTH_FILE):
            with open(BANDWIDTH_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("usado_bytes", 0)
    except Exception as e:
        log_error(f"Erro ao carregar bandwidth: {e}")
    return 0


def salvar_bandwidth(bytes_usados):
    try:
        with open(BANDWIDTH_FILE, "w", encoding="utf-8") as f:
            json.dump({"usado_bytes": bytes_usados}, f)
    except Exception as e:
        log_error(f"Erro ao salvar bandwidth: {e}")


def adicionar_bandwidth(file_path):
    try:
        if os.path.exists(file_path):
            tamanho = os.path.getsize(file_path)
            atual = carregar_bandwidth()
            salvar_bandwidth(atual + tamanho)
    except Exception as e:
        log_error(f"Erro ao atualizar bandwidth: {e}")


def formatar_tamanho(bytes_val):
    if bytes_val >= 1024 * 1024 * 1024:
        return f"{bytes_val / (1024**3):.2f}GB"
    elif bytes_val >= 1024 * 1024:
        return f"{bytes_val / (1024**2):.1f}MB"
    return f"{bytes_val / 1024:.1f}KB"

# ============================================================
# MONITORAMENTO E ALERTAS
# ============================================================
def verificar_alertas():
    try:
        storage_usado = 0
        try:
            arquivos = supabase.storage.from_("replays").list()
            for arq in arquivos:
                storage_usado += arq.get("metadata", {}).get("size", 0)
        except Exception as e:
            log_error(f"Erro storage Supabase: {e}")

        bandwidth_usado = carregar_bandwidth()
        storage_pct = (storage_usado / STORAGE_LIMITE_BYTES) * 100
        bandwidth_pct = (bandwidth_usado / BANDWIDTH_LIMITE_BYTES) * 100

        if storage_pct >= ALERTA_PERCENTUAL or bandwidth_pct >= ALERTA_PERCENTUAL:
            conteudo = (
                f"=== ALERTA DO SISTEMA ===\n"
                f"Data: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
                f"STORAGE:   {storage_pct:.1f}% ({formatar_tamanho(storage_usado)})\n"
                f"BANDWIDTH: {bandwidth_pct:.1f}% ({formatar_tamanho(bandwidth_usado)})\n"
            )
            with open(ALERTA_FILE, "w", encoding="utf-8") as f:
                f.write(conteudo)
        elif os.path.exists(ALERTA_FILE):
            os.remove(ALERTA_FILE)

        return storage_pct, bandwidth_pct
    except Exception as e:
        log_error(f"Erro verificar_alertas: {e}")
        return 0.0, 0.0


def monitorar():
    global running, cap
    while running:
        try:
            storage_pct, bandwidth_pct = verificar_alertas()

            if REMOTE_PANEL_URL:
                status_cam = "ONLINE" if (cap is not None and cap.isOpened()) else "OFFLINE"
                ultimo_erro = ""
                if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 0:
                    with open(LOG_FILE, "r", encoding="utf-8") as f:
                        linhas = f.readlines()
                        if linhas:
                            ultimo_erro = linhas[-1].strip()[:255]

                payload = {
                    "storage_pct": round(storage_pct, 1),
                    "bandwidth_pct": round(bandwidth_pct, 1),
                    "status_camera": status_cam,
                    "ultimo_erro": ultimo_erro
                }
                try:
                    remote_req.post(REMOTE_PANEL_URL, json=payload, timeout=10)
                except Exception:
                    pass
        except Exception as e:
            log_error(f"Erro monitorar: {e}")

        time.sleep(300)

# ============================================================
# ROTINAS DE LIMPEZA
# ============================================================
def deletar_arquivos_locais(pasta, horas):
    try:
        agora = time.time()
        limite_seg = horas * 3600
        for arq in os.listdir(pasta):
            caminho = os.path.join(pasta, arq)
            if os.path.isfile(caminho) and (agora - os.path.getmtime(caminho)) > limite_seg:
                try:
                    os.remove(caminho)
                except Exception as e:
                    log_error(f"Erro ao remover {arq}: {e}")
    except Exception as e:
        log_error(f"Erro ao varrer {pasta}: {e}")


def limpar_supabase():
    try:
        limite_ts = datetime.now(timezone.utc).timestamp() - (NUVEM_HORAS * 3600)
        resultado = supabase.table("replays").select("id, nome, preview_nome, criado_em").execute()

        for item in resultado.data or []:
            criado_em = item.get("criado_em")
            if not criado_em:
                continue

            criado_ts = datetime.fromisoformat(criado_em.replace("Z", "+00:00")).timestamp()
            if criado_ts < limite_ts:
                remocoes = [n for n in [item.get("nome"), item.get("preview_nome")] if n]
                if remocoes:
                    try:
                        supabase.storage.from_("replays").remove(remocoes)
                    except Exception as e:
                        log_error(f"Erro storage remove {remocoes}: {e}")

                supabase.table("replays").delete().eq("id", item["id"]).execute()
    except Exception as e:
        log_error(f"Erro limpar_supabase: {e}")


def rotina_limpeza():
    global running
    while running:
        deletar_arquivos_locais(GRAVACOES_DIR, CONTINUO_LOCAL_HORAS)
        deletar_arquivos_locais(REPLAYS_DIR, REPLAY_LOCAL_HORAS)
        deletar_arquivos_locais(PREVIEWS_DIR, PREVIEW_LOCAL_HORAS)
        limpar_supabase()
        time.sleep(300)

# ============================================================
# PROCESSAMENTO DE VÍDEO (FFMPEG / OPENCV)
# ============================================================
def save_replay(frames):
    try:
        print(f"[PROCESSANDO] Gravando arquivo temporário do replay ({len(frames)} frames)...", flush=True)
        h, w, _ = frames[0].shape
        temp = os.path.join(REPLAYS_DIR, f"temp_{int(time.time())}.avi")
        out = cv2.VideoWriter(temp, cv2.VideoWriter_fourcc(*"XVID"), FPS, (w, h))

        for f in frames:
            out.write(f)
        out.release()

        final = os.path.join(REPLAYS_DIR, f"replay_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4")
        print("[FFMPEG] Convertendo replay para H.264 MP4...", flush=True)
        subprocess.run([
            FFMPEG_BIN, "-y", "-i", temp,
            "-vcodec", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-crf", "23", "-preset", "fast",
            final
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        if os.path.exists(temp):
            os.remove(temp)
        print(f"[SUCESSO] Replay local salvo: {final}", flush=True)
        return final
    except Exception as e:
        log_error(f"Erro save_replay: {e}")
        print(f"[ERRO] Falha ao salvar replay: {e}", flush=True)
        return None


def save_preview(video_path):
    try:
        print("[FFMPEG] Gerando preview de 5 segundos...", flush=True)
        preview_path = os.path.join(PREVIEWS_DIR, f"preview_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4")
        subprocess.run([
            FFMPEG_BIN, "-y", "-i", video_path, "-t", "5",
            "-vcodec", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-crf", "28", "-preset", "fast", "-an",
            preview_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        if os.path.exists(preview_path):
            print(f"[SUCESSO] Preview gerado: {preview_path}", flush=True)
            return preview_path
        return None
    except Exception as e:
        log_error(f"Erro save_preview: {e}")
        print(f"[ERRO] Falha ao gerar preview: {e}", flush=True)
        return None


def upload_video(file_path, preview_path):
    try:
        print("[SUPABASE] Iniciando upload para o Storage...", flush=True)
        tag_tempo = datetime.now().strftime("%Hh%Mmin%Sseg")
        file_name = f"replay_{tag_tempo}.mp4"
        preview_name = f"preview_{tag_tempo}.mp4"

        with open(file_path, "rb") as f:
            supabase.storage.from_("replays").upload(file_name, f, {"content-type": "video/mp4"})

        with open(preview_path, "rb") as f:
            supabase.storage.from_("replays").upload(preview_name, f, {"content-type": "video/mp4"})

        url = supabase.storage.from_("replays").get_public_url(file_name)
        preview_url = supabase.storage.from_("replays").get_public_url(preview_name)

        print("[SUPABASE] Registrando metadados na tabela 'replays'...", flush=True)
        supabase.table("replays").insert({
            "nome": file_name,
            "url": url,
            "preview_url": preview_url,
            "preview_nome": preview_name
        }).execute()

        adicionar_bandwidth(file_path)
        adicionar_bandwidth(preview_path)
        print(f"[CONCLUÍDO] Vídeo e preview disponíveis na nuvem!\nURL: {url}", flush=True)
        return url
    except Exception as e:
        log_error(f"Erro upload_video: {e}")
        print(f"[ERRO] Falha no upload ao Supabase: {e}", flush=True)
        return None


def process_events():
    global running
    while running:
        try:
            if not event_queue.empty():
                event = event_queue.get()
                if event == "SAVE" and len(frame_buffer) > 0:
                    duracao = obter_duracao_replay()
                    frames_necessarios = FPS * duracao
                    total_disponivel = len(frame_buffer)
                    
                    # Corta exatamente a quantidade de frames da configuração atual
                    qtd_corte = min(total_disponivel, frames_necessarios)
                    frames = list(frame_buffer)[-qtd_corte:]
                    
                    print(f"\n[EVENTO] Tecla [S] acionada! Extraindo {qtd_corte} quadros ({duracao}s configurados)...", flush=True)
                    replay = save_replay(frames)
                    if replay:
                        preview = save_preview(replay)
                        if preview:
                            upload_video(replay, preview)
        except Exception as e:
            log_error(f"Erro process_events: {e}")
            print(f"[ERRO] Falha na fila de eventos: {e}", flush=True)
        time.sleep(0.01)

# ============================================================
# LOOP PRINCIPAL DE CAPTURA
# ============================================================
def inicializar_camera(index=0):
    backend = cv2.CAP_MSMF if sys.platform == "win32" else cv2.CAP_ANY
    while running:
        cap_dev = cv2.VideoCapture(index, backend)
        if cap_dev.isOpened():
            print(f"[OK] Câmera conectada com sucesso no índice {index}", flush=True)
            return cap_dev
        log_error(f"Falha ao abrir sinal da câmera no índice {index}. Tentando novamente em 5s...")
        time.sleep(5)
    return None


def main():
    global running, cap

    for func in [process_events, monitorar, rotina_limpeza]:
        threading.Thread(target=func, daemon=True).start()

    camera_idx = int(os.getenv("CAMERA_INDEX", "0"))
    cap = inicializar_camera(camera_idx)
    if not cap:
        return

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = cv2.VideoWriter_fourcc(*"XVID")

    def novo_arquivo():
        return os.path.join(GRAVACOES_DIR, f"gravacao_{datetime.now().strftime('%Y%m%d_%H%M%S')}.avi")

    current_file = novo_arquivo()
    out = cv2.VideoWriter(current_file, fourcc, FPS, (width, height))
    start_time = time.time()

    print(f"[CÂMERA ONLINE] Gravando no dispositivo {camera_idx}: {current_file}", flush=True)
    print("Comandos: [S] Salvar replay | [Q] Sair", flush=True)

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                log_error("Sinal de vídeo interrompido. Reiniciando captura...")
                print("[AVISO] Sinal de vídeo interrompido. Tentando reconectar...", flush=True)
                out.release()
                cap.release()
                cap = inicializar_camera(camera_idx)
                if not cap:
                    break
                current_file = novo_arquivo()
                out = cv2.VideoWriter(current_file, fourcc, FPS, (width, height))
                start_time = time.time()
                continue

            frame_buffer.append(frame)
            out.write(frame)

            if time.time() - start_time > SEGMENT_DURATION:
                out.release()
                current_file = novo_arquivo()
                out = cv2.VideoWriter(current_file, fourcc, FPS, (width, height))
                start_time = time.time()

            cv2.imshow("Preview - Sistema Replay", frame)
            key = cv2.waitKey(1) & 0xFF

            if key == ord("s"):
                event_queue.put("SAVE")
            elif key == ord("q"):
                break
    except Exception as e:
        log_error(f"Erro loop principal: {e}")
    finally:
        running = False
        if cap and cap.isOpened():
            cap.release()
        if out:
            out.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()