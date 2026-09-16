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
import numpy as np
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
# PARÂMETROS OPERACIONAIS E CONFIGURAÇÃO
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
# BUFFERS CIRCULARES E CONTROLE DE EXECUÇÃO
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
    if sys.platform.startswith("linux"):
        cap_target = f"/dev/video{idx}"
        backend = cv2.CAP_V4L2
    else:
        cap_target = int(idx)
        backend = cv2.CAP_MSMF if sys.platform == "win32" else cv2.CAP_ANY

    while running:
        cap = cv2.VideoCapture(cap_target, backend)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        cap.set(cv2.CAP_PROP_FPS, FPS)

        if not cap.isOpened():
            print(f"[ERRO] Falha ao conectar {tag} em {cap_target}. Reconectando em 3s...", flush=True)
            time.sleep(3)
            continue

        print(f"[ONLINE] {tag} conectada em {cap_target}!", flush=True)
        while running:
            ret, frame = cap.read()
            if not ret:
                print(f"[ALERTA] Perda de sinal em {tag} ({cap_target}). Tentando reconectar...", flush=True)
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
            FFMPEG_BIN, "-y",
            "-ss", "00:00:00",
            "-i", video_path,
            "-t", str(duracao_preview),
            "-c:v", "libx264",
            "-profile:v", "baseline",
            "-level", "3.0",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-movflags", "+faststart",
            "-an",
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

        # Inserção com a coluna corrigida para 'preview_nome'
        supabase.table("replays").insert({
            "nome": file_name,
            "url": url,
            "preview_url": preview_url,
            "preview_nome": preview_name
        }).execute()

        adicionar_bandwidth(file_path)
        adicionar_bandwidth(preview_path)
        print(f"[CONCLUÍDO] Replay da {prefixo} inserido na tabela com sucesso!\nURL: {url}", flush=True)
        return url
    except Exception as e:
        log_error(f"Erro upload_video ({prefixo}): {e}")
        print(f"[ERRO] Falha ao enviar/inserir no Supabase: {e}", flush=True)
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
                    print(f"\n[GATILHO {cam_tag.upper()}] Extraindo últimos {qtd_corte/FPS:.1f}s...", flush=True)

                    replay = save_replay(frames, cam_tag)
                    if replay:
                        preview = save_preview(replay, cam_tag, prev_sec)
                        if preview:
                            upload_video(replay, preview, cam_tag)
        except Exception as e:
            log_error(f"Erro process_events: {e}")
        time.sleep(0.01)

# ============================================================
# LOOP PRINCIPAL COM MONITORAMENTO VISUAL DUPLO
# ============================================================
def main():
    global running

    config_inicial = carregar_config()
    cam_a_idx = int(os.getenv("CAMERA_A_INDEX", str(config_inicial.get("camera_a_index", 0))))
    cam_d_idx = int(os.getenv("CAMERA_D_INDEX", str(config_inicial.get("camera_d_index", 10))))

    # Inicializa thread consumidora da fila de exportação/upload
    threading.Thread(target=process_events, daemon=True).start()

    # Inicializa threads de gravação contínua nos buffers
    threading.Thread(target=capturar_camera, args=(cam_a_idx, buffer_cam_a, "CÂMERA 1 (A)"), daemon=True).start()
    threading.Thread(target=capturar_camera, args=(cam_d_idx, buffer_cam_d, "CÂMERA 2 (D)"), daemon=True).start()

    print("\n=======================================================")
    print("      KLIP REPLAY - SISTEMA DE CÂMERAS DUPLAS         ")
    print(f"  [A] Salva lance da CÂMERA 1 (/dev/video{cam_a_idx})  ")
    print(f"  [D] Salva lance da CÂMERA 2 (/dev/video{cam_d_idx}) ")
    print("  [Q] Encerra o serviço                                ")
    print("=======================================================\n", flush=True)

    # Cria janela física de monitoramento lado a lado
    nome_janela = "KLIP REPLAY - MONITOR QUADRA (LADO A LADO)"
    cv2.namedWindow(nome_janela, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(nome_janela, 1280, 360)

    largura_sub = 640
    altura_sub = 360

    try:
        while running:
            # Frame da Câmera 1 (A)
            if len(buffer_cam_a) > 0:
                frame_a = cv2.resize(buffer_cam_a[-1], (largura_sub, altura_sub))
                cv2.putText(frame_a, f"CAM 1 [A] - /dev/video{cam_a_idx}", (20, 35),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 230, 118), 2)
            else:
                frame_a = np.zeros((altura_sub, largura_sub, 3), dtype=np.uint8)
                cv2.putText(frame_a, "CAM 1 CONECTANDO...", (40, 180),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

            # Frame da Câmera 2 (D)
            if len(buffer_cam_d) > 0:
                frame_d = cv2.resize(buffer_cam_d[-1], (largura_sub, altura_sub))
                cv2.putText(frame_d, f"CAM 2 [D] - /dev/video{cam_d_idx}", (20, 35),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (56, 189, 248), 2)
            else:
                frame_d = np.zeros((altura_sub, largura_sub, 3), dtype=np.uint8)
                cv2.putText(frame_d, "CAM 2 CONECTANDO...", (40, 180),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

            # Mescla os dois feeds horizontalmente
            tela_dupla = cv2.hconcat([frame_a, frame_d])

            # Linha divisória e relógio no rodapé
            cv2.line(tela_dupla, (largura_sub, 0), (largura_sub, altura_sub), (50, 60, 80), 2)
            relogio = datetime.now().strftime("%H:%M:%S")
            cv2.putText(tela_dupla, relogio, (largura_sub - 45, 345),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

            cv2.imshow(nome_janela, tela_dupla)

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