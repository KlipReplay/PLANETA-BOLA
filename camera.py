import os
import sys
import time
import json
import shutil
import subprocess
import threading
from collections import deque
from datetime import datetime
from queue import Queue

import cv2
from dotenv import load_dotenv
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
BANDWIDTH_FILE = os.path.join(BASE_DIR, "bandwidth.json")
LOG_FILE       = os.path.join(BASE_DIR, "log.txt")

os.makedirs(GRAVACOES_DIR, exist_ok=True)
os.makedirs(REPLAYS_DIR, exist_ok=True)
os.makedirs(PREVIEWS_DIR, exist_ok=True)

if sys.platform == "win32":
    FFMPEG_BIN = os.path.join(BASE_DIR, "ffmpeg.exe")
    if not os.path.exists(FFMPEG_BIN):
        FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"
else:
    FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

# ============================================================
# PARÂMETROS OPERACIONAIS
# ============================================================
FPS = int(os.getenv("CAMERA_FPS", "30"))

def carregar_config():
    padrao = {
        "preco_replay": 0.15,
        "duracao_buffer_segundos": 60,
        "tempo_pre_clique_segundos": 40,
        "tempo_pos_clique_segundos": 0,
        "duracao_preview_segundos": 5,
        "camera_a_index": 0,
        "camera_d_index": 10
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                dados = json.load(f)
                padrao.update(dados)
        except Exception:
            pass
    return padrao

cfg = carregar_config()

# ============================================================
# SUPABASE CLIENT
# ============================================================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL ou SUPABASE_KEY não configurados no .env")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# BUFFERS CIRCULARES E FILA DE PROCESSAMENTO
# ============================================================
running = True
MAX_BUFFER_SEGUNDOS = int(cfg.get("duracao_buffer_segundos", 60))
MAX_FRAMES = FPS * MAX_BUFFER_SEGUNDOS

buffer_cam_a = deque(maxlen=MAX_FRAMES)
buffer_cam_d = deque(maxlen=MAX_FRAMES)
event_queue = Queue()


def log_error(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def adicionar_bandwidth(file_path):
    try:
        if os.path.exists(file_path):
            tamanho = os.path.getsize(file_path)
            usado = 0
            if os.path.exists(BANDWIDTH_FILE):
                with open(BANDWIDTH_FILE, "r", encoding="utf-8") as f:
                    usado = json.load(f).get("usado_bytes", 0)
            with open(BANDWIDTH_FILE, "w", encoding="utf-8") as f:
                json.dump({"usado_bytes": usado + tamanho}, f)
    except Exception:
        pass

# ============================================================
# THREADS DE CAPTURA INDEPENDENTES
# ============================================================
def capturar_camera(idx, buffer_destino, tag):
    backend = cv2.CAP_V4L2 if sys.platform.startswith("linux") else (cv2.CAP_MSMF if sys.platform == "win32" else cv2.CAP_ANY)
    while running:
        cap = cv2.VideoCapture(int(idx), backend)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        cap.set(cv2.CAP_PROP_FPS, FPS)

        if not cap.isOpened():
            print(f"[ERRO] Falha ao conectar {tag} no índice {idx}. Reconectando em 3s...", flush=True)
            time.sleep(3)
            continue

        print(f"[ONLINE] {tag} ativa no dispositivo /dev/video{idx}!", flush=True)
        while running:
            ret, frame = cap.read()
            if not ret:
                print(f"[ALERTA] Perda de sinal em {tag} (/dev/video{idx}). Tentando reconectar...", flush=True)
                break
            buffer_destino.append(frame)

        cap.release()
        time.sleep(2)

# ============================================================
# PROCESSAMENTO DE VÍDEO E UPLOAD (SUPABASE)
# ============================================================
def save_replay(frames, prefixo):
    try:
        h, w, _ = frames[0].shape
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        temp = os.path.join(REPLAYS_DIR, f"temp_{prefixo}_{int(time.time())}.avi")
        out = cv2.VideoWriter(temp, cv2.VideoWriter_fourcc(*"XVID"), FPS, (w, h))

        for f in frames:
            out.write(f)
        out.release()

        final = os.path.join(REPLAYS_DIR, f"replay_{prefixo}_{timestamp}.mp4")
        subprocess.run([
            FFMPEG_BIN, "-y", "-i", temp,
            "-vcodec", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-crf", "23", "-preset", "fast",
            final
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        if os.path.exists(temp):
            os.remove(temp)
        return final
    except Exception as e:
        log_error(f"Erro save_replay ({prefixo}): {e}")
        return None


def save_preview(video_path, prefixo, duracao_preview):
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        preview_path = os.path.join(PREVIEWS_DIR, f"preview_{prefixo}_{timestamp}.mp4")
        subprocess.run([
            FFMPEG_BIN, "-y", "-i", video_path, "-t", str(duracao_preview),
            "-vcodec", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-crf", "28", "-preset", "fast", "-an",
            preview_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        if os.path.exists(preview_path):
            return preview_path
        return None
    except Exception as e:
        log_error(f"Erro save_preview ({prefixo}): {e}")
        return None


def upload_video(file_path, preview_path, prefixo):
    try:
        print(f"[SUPABASE] Enviando {prefixo} para o Storage...", flush=True)
        tag_tempo = datetime.now().strftime("%Hh%Mmin%Sseg")
        file_name = f"replay_{prefixo}_{tag_tempo}.mp4"
        preview_name = f"preview_{prefixo}_{tag_tempo}.mp4"

        with open(file_path, "rb") as f:
            supabase.storage.from_("replays").upload(file_name, f, {"content-type": "video/mp4"})

        with open(preview_path, "rb") as f:
            supabase.storage.from_("replays").upload(preview_name, f, {"content-type": "video/mp4"})

        url = supabase.storage.from_("replays").get_public_url(file_name)
        preview_url = supabase.storage.from_("replays").get_public_url(preview_name)

        supabase.table("replays").insert({
            "nome": file_name,
            "url": url,
            "preview_url": preview_url,
            "preview_nom": preview_name
        }).execute()

        adicionar_bandwidth(file_path)
        adicionar_bandwidth(preview_path)
        print(f"[CONCLUÍDO] Replay da {prefixo} sincronizado na nuvem!\nURL: {url}", flush=True)
        return url
    except Exception as e:
        log_error(f"Erro upload_video ({prefixo}): {e}")
        print(f"[ERRO] Falha ao enviar para o Supabase: {e}", flush=True)
        return None


def process_events():
    while running:
        try:
            if not event_queue.empty():
                evento = event_queue.get()
                cam_tag = evento["cam"]  # "cam1" ou "cam2"
                buf = buffer_cam_a if cam_tag == "cam1" else buffer_cam_d

                config_atual = carregar_config()
                pre_sec = int(config_atual.get("tempo_pre_clique_segundos", 40))
                prev_sec = int(config_atual.get("duracao_preview_segundos", 5))
                frames_necessarios = FPS * pre_sec

                if len(buf) > 0:
                    qtd_corte = min(len(buf), frames_necessarios)
                    frames = list(buf)[-qtd_corte:]
                    print(f"\n[GATILHO {cam_tag.upper()}] Cortando {qtd_corte/FPS:.1f}s retroativos...", flush=True)

                    replay = save_replay(frames, cam_tag)
                    if replay:
                        preview = save_preview(replay, cam_tag, prev_sec)
                        if preview:
                            upload_video(replay, preview, cam_tag)
        except Exception as e:
            log_error(f"Erro process_events: {e}")
        time.sleep(0.01)

# ============================================================
# INICIALIZAÇÃO
# ============================================================
def main():
    global running

    config_inicial = carregar_config()
    cam_a_idx = int(os.getenv("CAMERA_A_INDEX", str(config_inicial.get("camera_a_index", 0))))
    cam_d_idx = int(os.getenv("CAMERA_D_INDEX", str(config_inicial.get("camera_d_index", 10))))

    # Thread da fila de corte e envio
    threading.Thread(target=process_events, daemon=True).start()

    # Threads independentes de gravação circular
    threading.Thread(target=capturar_camera, args=(cam_a_idx, buffer_cam_a, "CÂMERA 1 (A)"), daemon=True).start()
    threading.Thread(target=capturar_camera, args=(cam_d_idx, buffer_cam_d, "CÂMERA 2 (D)"), daemon=True).start()

    print("\n=======================================================")
    print("      KLIP REPLAY - SISTEMA DE CÂMERAS DUPLAS         ")
    print(f"  [A] Salva lance da CÂMERA 1 (/dev/video{cam_a_idx})  ")
    print(f"  [D] Salva lance da CÂMERA 2 (/dev/video{cam_d_idx}) ")
    print("  [Q] Encerra o serviço                                ")
    print("=======================================================\n", flush=True)

    # Mini-janela oculta/mínima para escutar as teclas sem ocupar tela
    cv2.namedWindow("KLIP_CONTROLE", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("KLIP_CONTROLE", 300, 80)

    try:
        while running:
            key = cv2.waitKey(30) & 0xFF
            if key in (ord("a"), ord("A")):
                event_queue.put({"cam": "cam1"})
            elif key in (ord("d"), ord("D")):
                event_queue.put({"cam": "cam2"})
            elif key in (ord("q"), ord("Q")):
                break
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()