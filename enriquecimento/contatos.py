"""
CONTATO: telefone e e-mail só entram na planilha se forem CLARAMENTE
da pessoa, da startup ou do laboratório dela.
=====================================================================
Regra do projeto: um contato só vai para a coluna principal
(`telefone` / `email`) quando TODAS as condições valem:
  1. está completo (sem dígitos ocultos, DDD válido, 8 ou 9 dígitos);
  2. não é de contabilidade, coworking, recepção ou condomínio;
  3. não se repete em CNPJs diferentes (sinal de contador/escritório);
  4. o CNPJ foi vinculado ao proponente/startup (ver identidade.py);
  5. há corroboração: e-mail no domínio da própria startup/pessoa, ou o
     CNPJ é de MEI/empresário individual com o NOME do proponente.

Qualquer outro caso NÃO vai para a coluna principal. O motivo fica
explícito em `telefone_status` / `telefone_obs` (e o número, quando
existe, fica só em `telefone_candidato`, rotulado).
=====================================================================
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from enriquecimento.identidade import nome_da_startup, normalizar_nome, _tokens

DDDS_VALIDOS = {
    11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 24, 27, 28, 31, 32, 33, 34, 35, 37, 38,
    41, 42, 43, 44, 45, 46, 47, 48, 49, 51, 53, 54, 55, 61, 62, 63, 64, 65, 66, 67, 68, 69,
    71, 73, 74, 75, 77, 79, 81, 82, 83, 84, 85, 86, 87, 88, 89, 91, 92, 93, 94, 95, 96, 97, 98, 99,
}
PALAVRAS_CONTABILIDADE = (
    "contab", "contador", "contabil", "assessoria", "fiscal", "bpo", "tributar",
    "legaliza", "despachante", "escritorio", "abertura",
)
PALAVRAS_COWORKING = (
    "cowork", "recepcao", "portaria", "condominio", "predial", "administradora",
    "businesscenter", "centroempresarial", "virtualoffice", "salacomercial",
)
PROVEDORES_GRATIS = {
    "gmail", "hotmail", "outlook", "yahoo", "live", "icloud", "bol", "uol",
    "terra", "ig", "globo", "protonmail", "msn", "me",
}
PREFIXOS_DE_CELULAR_SEM_NONO_DIGITO = "6789"
AJUSTE_NONO_DIGITO = "nono dígito acrescentado (o cadastro guarda o celular no formato antigo)"
_RE_MASCARA = re.compile(r"[*•#_·●]|[xX]{2,}")
_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# prioridade ao escolher entre vários números/e-mails do mesmo cadastro
_PRIORIDADE = {"confirmado": 0, "nao_confirmado": 1, "compartilhado": 2, "contabilidade": 3,
               "coworking": 3, "mascarado": 4, "incompleto": 5, "invalido": 5}


# ---------------------------------------------------------------
# Telefone
# ---------------------------------------------------------------
def analisar_telefone(bruto: str | None) -> dict:
    """Valida a FORMA do número. status: completo | mascarado | incompleto | invalido."""
    texto = str(bruto or "").strip()
    if _RE_MASCARA.search(texto):
        return {"status": "mascarado", "digitos": "", "tipo": None, "formatado": texto,
                "motivo": "dígitos ocultos"}
    d = re.sub(r"\D", "", texto)
    if len(d) in (11, 12) and d.startswith("0"):
        d = d[1:]
    if len(d) in (12, 13) and d.startswith("55"):
        d = d[2:]
    if len(d) in (8, 9):
        return {"status": "incompleto", "digitos": d, "tipo": None, "formatado": texto,
                "motivo": "sem DDD"}
    if len(d) not in (10, 11):
        return {"status": "incompleto", "digitos": d, "tipo": None, "formatado": texto,
                "motivo": "quantidade de dígitos incompleta"}
    ddd, numero = int(d[:2]), d[2:]
    if ddd not in DDDS_VALIDOS:
        return {"status": "invalido", "digitos": d, "tipo": None, "formatado": texto,
                "motivo": f"DDD {ddd} inexistente"}
    if len(set(numero)) == 1 or numero in ("12345678", "123456789", "987654321", "87654321"):
        return {"status": "invalido", "digitos": d, "tipo": None, "formatado": texto,
                "motivo": "número fictício (dígitos repetidos ou sequência)"}
    if len(numero) == 9 and numero[0] == "9":
        tipo, fmt = "celular", f"({d[:2]}) {numero[:5]}-{numero[5:]}"
    elif len(numero) == 8 and numero[0] in "2345":
        tipo, fmt = "fixo", f"({d[:2]}) {numero[:4]}-{numero[4:]}"
    elif len(numero) == 8 and numero[0] in PREFIXOS_DE_CELULAR_SEM_NONO_DIGITO:
        return {"status": "completo", "digitos": d, "tipo": "celular",
                "formatado": f"({d[:2]}) 9{numero[:4]}-{numero[4:]}", "motivo": AJUSTE_NONO_DIGITO}
    else:
        return {"status": "incompleto", "digitos": d, "tipo": None, "formatado": texto,
                "motivo": "formato fora do padrão (celular sem o 9 ou prefixo inválido)"}
    return {"status": "completo", "digitos": d, "tipo": tipo, "formatado": fmt, "motivo": ""}


# ---------------------------------------------------------------
# E-mail
# ---------------------------------------------------------------
def analisar_email(bruto: str | None) -> dict:
    texto = str(bruto or "").strip().lower()
    if _RE_MASCARA.search(texto):
        return {"status": "mascarado", "email": texto, "motivo": "caracteres ocultos"}
    if not _RE_EMAIL.match(texto):
        return {"status": "invalido", "email": texto, "motivo": "formato inválido"}
    return {"status": "completo", "email": texto, "motivo": ""}


def _letras(texto: str) -> str:
    return re.sub(r"[^a-z]", "", normalizar_nome(texto).lower())


def sinal_de_terceiro(email: str | None) -> tuple[str, str] | None:
    """('contabilidade'|'coworking', termo) se o e-mail parece de terceiro."""
    if not email:
        return None
    alvo = _letras(email)
    for termo in PALAVRAS_CONTABILIDADE:
        if termo in alvo:
            return "contabilidade", termo
    for termo in PALAVRAS_COWORKING:
        if termo in alvo:
            return "coworking", termo
    return None


def dominio_proprio(email: str, nomes: list[str | None]) -> bool:
    """O domínio do e-mail (fora provedores grátis) lembra a startup/empresa?"""
    label = _letras(email.split("@")[1].split(".")[0])
    if len(label) < 4 or label in PROVEDORES_GRATIS:
        return False
    for n in nomes:
        alvo = _letras(n or "")
        if len(alvo) >= 4 and (label in alvo or alvo in label):
            return True
    return False


def email_com_nome_da_pessoa(email: str, proponente: str | None) -> bool:
    """Parte local do e-mail contém 2+ nomes do proponente (ex.: joao.silva@...)."""
    local = _letras(email.split("@")[0])
    toks = [t.lower() for t in _tokens(proponente) if len(t) >= 3]
    return sum(1 for t in toks if t in local) >= 2


# ---------------------------------------------------------------
# Compartilhamento entre CNPJs (sinal de contador/escritório)
# ---------------------------------------------------------------
def impressao(valor: str) -> str:
    return hashlib.sha256(valor.encode("utf-8")).hexdigest()


def impressao_telefone(bruto: str) -> str | None:
    digitos = analisar_telefone(bruto)["digitos"]
    return impressao(digitos) if digitos else None


def impressao_email(bruto: str) -> str:
    return impressao(str(bruto).strip().lower())


def contatos_compartilhados(registros: list[dict]) -> tuple[set[str], set[str]]:
    """Impressões de telefones/e-mails que aparecem em 2+ CNPJs-raiz diferentes."""
    tel: dict[str, set[str]] = defaultdict(set)
    mail: dict[str, set[str]] = defaultdict(set)
    for r in registros:
        raiz = (r.get("cnpj") or "")[:8]
        impressoes_tel = [impressao_telefone(t) for t in r.get("telefones") or []]
        for i in impressoes_tel + list(r.get("telefones_impressao") or []):
            if i:
                tel[i].add(raiz)
        impressoes_mail = [impressao_email(r["email"])] if r.get("email") else []
        for i in impressoes_mail + list(r.get("email_impressao") or []):
            mail[i].add(raiz)
    return ({k for k, v in tel.items() if len(v) >= 2},
            {k for k, v in mail.items() if len(v) >= 2})


def minimizar_contatos(registro: dict) -> dict:
    if registro.get("contatos_minimizados"):
        return registro
    telefones = [i for i in (impressao_telefone(t) for t in registro.get("telefones") or []) if i]
    email = [impressao_email(registro["email"])] if registro.get("email") else []
    return {**registro, "telefones": [], "email": None, "telefones_impressao": telefones,
            "email_impressao": email, "contatos_minimizados": True}


# ---------------------------------------------------------------
# Decisão final
# ---------------------------------------------------------------
def _decidir(analise: dict, terceiro, compartilhado: bool, vinculo_ok: bool,
             corrobora: bool, titular_exato: bool) -> tuple[str, str, str | None]:
    """Devolve (status, observação, confiança 'alta'|'media'|None)."""
    if analise["status"] == "mascarado":
        return "mascarado", "dígitos ocultos: não usar", None
    if analise["status"] in ("incompleto", "invalido"):
        return analise["status"], f"número {analise['status']}: {analise.get('motivo', '')}", None
    if compartilhado:
        return "compartilhado", "mesmo contato em mais de um CNPJ (provável contabilidade/coworking)", None
    if terceiro:
        tipo, termo = terceiro
        rotulo = "contabilidade" if tipo == "contabilidade" else "coworking/recepção/condomínio"
        return tipo, f"provável {rotulo} (termo '{termo}' no e-mail do cadastro)", None
    if not vinculo_ok:
        return "nao_confirmado", "CNPJ não vinculado com segurança ao proponente/startup", None
    if corrobora:
        return "confirmado", "completo; e-mail do domínio/nome da própria startup ou pessoa corrobora", "alta"
    if titular_exato:
        return ("confirmado",
                "completo; cadastro do MEI/empresário individual no nome do proponente "
                "(pode ter sido preenchido pelo contador: confirme na 1ª ligação)", "media")
    return ("nao_confirmado",
            "completo e sem sinais de terceiro, mas não dá para afirmar que é da pessoa/startup "
            "(vem do cadastro do CNPJ e pode ser do contador)", None)


def avaliar_contato(registro: dict, identidade: dict, lead: dict,
                    tel_compartilhados: set[str], email_compartilhados: set[str],
                    vinculo_ok: bool = True) -> dict:
    """Aplica a regra de contato a um CNPJ já vinculado ao lead."""
    email_raw = registro.get("email")
    em = analisar_email(email_raw) if email_raw else None
    terceiro = sinal_de_terceiro(em["email"]) if em and em["status"] == "completo" else None

    nomes = [nome_da_startup(lead.get("titulo_projeto")), registro.get("nome_fantasia"),
             registro.get("razao_social")]
    corrobora = bool(em and em["status"] == "completo" and (
        dominio_proprio(em["email"], nomes) or email_com_nome_da_pessoa(em["email"], lead.get("proponente"))))
    titular_exato = bool(identidade.get("pessoa") == "exato" and registro.get("titular_unico"))

    # ----- telefone -----
    opcoes = []
    for bruto in registro.get("telefones") or []:
        a = analisar_telefone(bruto)
        status, obs, conf = _decidir(
            a, terceiro, bool(a["digitos"]) and impressao(a["digitos"]) in tel_compartilhados,
            vinculo_ok, corrobora, titular_exato)
        if a["motivo"] == AJUSTE_NONO_DIGITO:
            obs = f"{obs}; {AJUSTE_NONO_DIGITO}"
        opcoes.append((_PRIORIDADE[status], status, obs, conf, a))
    if opcoes:
        _, status, obs, conf, a = min(opcoes, key=lambda o: o[0])
        telefone = {
            "telefone": a["formatado"] if status == "confirmado" else None,
            "telefone_tipo": a["tipo"] if status == "confirmado" else None,
            "telefone_confianca": conf,
            "telefone_status": status,
            "telefone_obs": obs,
            "telefone_candidato": None if status == "confirmado" else (a["formatado"] or None),
        }
    else:
        telefone = {"telefone": None, "telefone_tipo": None, "telefone_confianca": None,
                    "telefone_status": "sem_dado", "telefone_obs": "cadastro do CNPJ sem telefone",
                    "telefone_candidato": None}

    # ----- e-mail -----
    if em:
        if em["status"] != "completo":
            st, ob, cf = em["status"], f"e-mail {em['status']}: {em['motivo']}", None
        else:
            st, ob, cf = _decidir(
                {"status": "completo"}, terceiro, impressao_email(em["email"]) in email_compartilhados,
                vinculo_ok, corrobora, titular_exato)
            ob = ob.replace("número", "e-mail")
        email = {
            "email": em["email"] if st == "confirmado" else None,
            "email_confianca": cf, "email_status": st, "email_obs": ob,
            "email_candidato": None if st == "confirmado" else em["email"],
        }
    else:
        email = {"email": None, "email_confianca": None, "email_status": "sem_dado",
                 "email_obs": "cadastro do CNPJ sem e-mail", "email_candidato": None}
    return {**telefone, **email}