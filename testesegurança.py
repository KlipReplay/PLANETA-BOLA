import concurrent.futures
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

BACKEND_URL = "http://127.0.0.1:5000"

# Puxa automaticamente a URL correta configurada no seu .env
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
if not SUPABASE_URL:
    SUPABASE_URL = "https://epvwisdjzgudiuopuqlb.supabase.co"

SUPABASE_STORAGE_BASE = f"{SUPABASE_URL}/storage/v1/object/public/replays"

def testar_tentativa_sem_token():
    try:
        url = f"{BACKEND_URL}/download/"
        res = requests.get(url, timeout=5)
        print(f"[*] Acesso sem token -> HTTP {res.status_code} (Esperado: 404)")
    except Exception as e:
        print(f"[!] Falha ao conectar ao backend: {e}")

def testar_tentativa_token_invalido():
    tokens_teste = [
        "token_falso_123",
        "admin",
        "../../etc/passwd",
        "00000000000000000000000000000000"
    ]
    for tk in tokens_teste:
        try:
            res = requests.get(f"{BACKEND_URL}/download/{tk}", timeout=5)
            status = res.status_code
            protegido = status in (403, 404)
            resultado = "PROTEGIDO (Bloqueado)" if protegido else "VULNERÁVEL (Acesso liberado)"
            print(f"[*] Token '{tk}' -> HTTP {status} | {resultado}")
        except Exception as e:
            print(f"[!] Erro ao testar token '{tk}': {e}")

def testar_exposicao_storage_direto(nome_arquivo_exemplo="replay_19h14min15seg.mp4"):
    url_direta = f"{SUPABASE_STORAGE_BASE}/{nome_arquivo_exemplo}"
    print(f"\n[*] Testando acesso direto no Storage: {url_direta}")
    try:
        res = requests.get(url_direta, headers={"Range": "bytes=0-100"}, timeout=6)
        print(f"[*] Resposta do Storage: HTTP {res.status_code}")
        if res.status_code in (200, 206):
            print("[!] ALERTA: O vídeo completo respondeu via link direto público do Storage.")
        elif res.status_code in (400, 403, 404):
            print("[+] PROTEGIDO: O Storage não permitiu o download direto sem credencial.")
    except requests.exceptions.ConnectionError:
        print("[!] Não foi possível resolver o domínio do Supabase. Verifique a SUPABASE_URL no seu .env.")
    except Exception as e:
        print(f"[!] Erro ao consultar o Storage: {e}")

def testar_rajada_concorrente(num_requisicoes=20):
    print(f"\n[*] Disparando {num_requisicoes} requisições simultâneas contra o backend...")
    url = f"{BACKEND_URL}/download/tentativa_bruteforce"

    def disparar(_):
        try:
            inicio = time.time()
            res = requests.get(url, timeout=5)
            return res.status_code, time.time() - inicio
        except requests.RequestException:
            return "FALHA_CONEXAO", 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        resultados = list(executor.map(disparar, range(num_requisicoes)))

    contagem_status = {}
    for status, _ in resultados:
        contagem_status[status] = contagem_status.get(status, 0) + 1

    print(f"[*] Resumo da rajada: {contagem_status}")
    if 429 in contagem_status:
        print("[+] Rate limiting atuou bloqueando o excesso (HTTP 429 Too Many Requests).")
    else:
        print("[-] Nenhuma limitação de taxa ativa: o servidor respondeu com 403/404 para todas as chamadas.")

if __name__ == "__main__":
    print("=== INICIANDO TESTE DE SEGURANÇA E ACESSO ===")
    testar_tentativa_sem_token()
    testar_tentativa_token_invalido()
    testar_exposicao_storage_direto()
    testar_rajada_concorrente()
    print("\n=== AUDITORIA CONCLUÍDA ===")