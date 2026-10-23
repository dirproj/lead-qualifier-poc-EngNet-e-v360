import os
import requests
from dotenv import load_dotenv

load_dotenv()

chave = os.getenv("CASADOSDADOS_API_KEY")
print(f"Chave carregada: {'SIM' if chave else 'NAO'} (primeiros 10 chars: {chave[:10] if chave else 'vazio'})")

url = "https://api.casadosdados.com.br/v5/public/cnpj/pesquisa"
payload = {
    "busca_textual": [{
        "texto": ["Erick Muzart Fonseca dos Santos"],
        "tipo_busca": "exata",
        "razao_social": True,
        "nome_fantasia": False,
        "nome_socio": True
    }],
    "situacao_cadastral": ["ATIVA"],
    "uf": ["DF"],
    "limite": 20,
    "pagina": 1
}
headers = {
    "Content-Type": "application/json",
    "User-Agent": "LeadQualifierPoC/1.0",
    "api-key": chave
}

print(f"Enviando POST para {url}")
try:
    r = requests.post(url, json=payload, headers=headers, timeout=20)
    print(f"Status: {r.status_code}")
    print(f"Resposta (primeiros 500 chars): {r.text[:500]}")
except Exception as e:
    print(f"Erro: {e}")
    