"""
GUARDRAILS (anti-alucinação) da etapa 3
=====================================================================
Camadas, de fora para dentro:
  1. Schema JSON estrito (a API só devolve campos/valores permitidos).
  2. Validação em Python: tipos, faixa da nota, serviços do catálogo.
     Falhou -> o llm.py pede nova resposta, apontando o erro.
  3. Ancoragem: cada evidência precisa ser trecho LITERAL dos dados de
     entrada. Evidência inventada é descartada e o lead vai para revisão.
  4. Regras determinísticas: dados insuficientes limitam a nota; nota
     alta sem evidência é rebaixada; termos factuais que não estão nos
     dados (faturamento, investidores...) geram aviso.
  5. A faixa (alto/médio/baixo) é calculada aqui, não pelo modelo.
=====================================================================
"""

from __future__ import annotations

import re
import unicodedata

from classificacao.perfil import CATALOGO_SERVICOS

CONFIANCA = ("alta", "media", "baixa")
MAX_JUSTIFICATIVA = 500
NOTA_MAX_SEM_EVIDENCIA = 6
NOTA_MAX_DADOS_INSUFICIENTES = 4

# Se aparecerem na justificativa sem estar nos dados, é provável invenção.
TERMOS_SUSPEITOS = (
    "faturamento", "receita", "funcionarios", "colaboradores", "investidores",
    "rodada", "serie a", "clientes pagantes", "concorrentes", "valuation",
)

SCHEMA = {
    "type": "object",
    "properties": {
        "nota_fit": {"type": "integer"},
        "confianca": {"type": "string", "enum": list(CONFIANCA)},
        "dados_insuficientes": {"type": "boolean"},
        "justificativa": {"type": "string"},
        "servicos_sugeridos": {
            "type": "array",
            "items": {"type": "string", "enum": list(CATALOGO_SERVICOS)},
        },
        "evidencias": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "nota_fit", "confianca", "dados_insuficientes",
        "justificativa", "servicos_sugeridos", "evidencias",
    ],
    #"additionalProperties": False,
}


def _norm(texto: str) -> str:
    """Minúsculo, sem acento e sem pontuação, para comparar trechos."""
    sem_acento = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", sem_acento.lower()).split())


ALIAS_CAMPOS = {
    "nota": "nota_fit", "score": "nota_fit", "fit": "nota_fit",
    "confiança": "confianca", "confidence": "confianca",
    "evidências": "evidencias", "serviços_sugeridos": "servicos_sugeridos",
    "servicos": "servicos_sugeridos", "justificação": "justificativa",
}


def normalizar_dados(dados):
    """Deixa a resposta do modelo no formato esperado ANTES de validar.
    Modelos abertos (Llama, GPT-OSS) em modo JSON costumam devolver a nota como
    texto ("7"), a confiança com acento ("média") ou omitir listas vazias. Em vez
    de reprovar (e gastar uma chamada), corrigimos o que é inequívoco."""
    if not isinstance(dados, dict):
        return dados
    d = {ALIAS_CAMPOS.get(k, k): v for k, v in dados.items()}
    d["_avisos_normalizacao"] = []

    nota = d.get("nota_fit")
    if isinstance(nota, str):
        achado = re.search(r"\d+(?:[.,]\d+)?", nota)
        nota = float(achado.group().replace(",", ".")) if achado else nota
    if isinstance(nota, float) and not isinstance(nota, bool):
        nota = int(round(nota))
    if nota is not None:
        d["nota_fit"] = nota

    conf = d.get("confianca")
    if isinstance(conf, str):
        c = _norm(conf)
        d["confianca"] = {"alta": "alta", "media": "media", "baixa": "baixa"}.get(c, conf)

    ins = d.get("dados_insuficientes")
    if isinstance(ins, str):
        d["dados_insuficientes"] = _norm(ins) in ("true", "sim", "verdadeiro", "1")
    elif ins is None:
        d["dados_insuficientes"] = False

    serv = d.get("servicos_sugeridos")
    if serv is None:
        serv = []
    if isinstance(serv, str):
        serv = [s.strip() for s in serv.split(",") if s.strip()]
    if isinstance(serv, list):
        validos = [s for s in serv if s in CATALOGO_SERVICOS]
        invalidos = [str(s) for s in serv if s not in CATALOGO_SERVICOS]
        if invalidos:
            d["_avisos_normalizacao"].append(f"serviços fora do catálogo descartados: {invalidos}")
        d["servicos_sugeridos"] = validos

    evid = d.get("evidencias")
    if evid is None:
        d["evidencias"] = []
    elif isinstance(evid, str):
        d["evidencias"] = [evid]
    return d


def faixa_da_nota(nota: int) -> str:
    return "alto" if nota >= 7 else "medio" if nota >= 4 else "baixo"


def resultado_falha(motivo: str, status: str = "falha") -> dict:
    return {
        "nota_fit": None, "faixa_fit": None, "confianca": None,
        "dados_insuficientes": None, "justificativa": "",
        "servicos_sugeridos": [], "evidencias": [],
        "status_ia": status, "avisos_ia": [motivo],
    }


def validar_resposta(dados, texto_entrada: str) -> tuple[dict | None, list[str]]:
    """Devolve (resultado, problemas).
    problemas != [] -> resposta rejeitada (o chamador tenta de novo).
    resultado traz `status_ia` ('ok' ou 'revisar') e `avisos_ia`."""
    if not isinstance(dados, dict):
        return None, ["a resposta não é um objeto JSON"]
    dados = normalizar_dados(dados)
    avisos_norm = dados.pop("_avisos_normalizacao", [])
    faltando = [c for c in SCHEMA["required"] if c not in dados]
    if faltando:
        return None, [f"campos ausentes: {faltando}"]

    problemas: list[str] = []
    nota = dados["nota_fit"]
    if isinstance(nota, bool) or not isinstance(nota, int) or not 0 <= nota <= 10:
        problemas.append("nota_fit deve ser um inteiro de 0 a 10")
    if dados["confianca"] not in CONFIANCA:
        problemas.append(f"confianca deve ser uma de {CONFIANCA}")
    if not isinstance(dados["dados_insuficientes"], bool):
        problemas.append("dados_insuficientes deve ser booleano")

    just = dados["justificativa"]
    if not isinstance(just, str) or not just.strip():
        problemas.append("justificativa vazia")
    elif len(just) > MAX_JUSTIFICATIVA:
        problemas.append(f"justificativa acima de {MAX_JUSTIFICATIVA} caracteres")

    servicos = dados["servicos_sugeridos"]
    if not isinstance(servicos, list) or any(s not in CATALOGO_SERVICOS for s in servicos):
        problemas.append("servicos_sugeridos deve conter só chaves do catálogo")

    evid = dados["evidencias"]
    if not isinstance(evid, list) or any(not isinstance(e, str) for e in evid):
        problemas.append("evidencias deve ser uma lista de textos")

    if problemas:
        return None, problemas

    # ---- A partir daqui a resposta é aceita; ajustes viram avisos ----
    avisos: list[str] = list(avisos_norm)
    confianca = dados["confianca"]
    insuficiente = dados["dados_insuficientes"]

    if insuficiente:
        if nota > NOTA_MAX_DADOS_INSUFICIENTES:
            avisos.append(f"nota reduzida de {nota} para {NOTA_MAX_DADOS_INSUFICIENTES} (dados insuficientes)")
            nota = NOTA_MAX_DADOS_INSUFICIENTES
        confianca = "baixa"

    entrada = _norm(texto_entrada)
    validas: list[str] = []
    for e in evid[:3]:
        n = _norm(e)
        if n and n in entrada:
            validas.append(e.strip())
        else:
            avisos.append(f"evidência não encontrada nos dados: '{e[:40]}'")

    if not validas and not insuficiente:
        avisos.append("sem evidência literal nos dados")
        confianca = "baixa"
        if nota > NOTA_MAX_SEM_EVIDENCIA:
            avisos.append(f"nota reduzida de {nota} para {NOTA_MAX_SEM_EVIDENCIA} (sem evidência)")
            nota = NOTA_MAX_SEM_EVIDENCIA

    just_n = f" {_norm(just)} "
    for termo in TERMOS_SUSPEITOS:
        if f" {termo} " in just_n and f" {termo} " not in f" {entrada} ":
            avisos.append(f"termo '{termo}' na justificativa não consta nos dados")

    return {
        "nota_fit": nota,
        "faixa_fit": faixa_da_nota(nota),
        "confianca": confianca,
        "dados_insuficientes": insuficiente,
        "justificativa": just.strip(),
        "servicos_sugeridos": servicos,
        "evidencias": validas,
        "status_ia": "ok" if not avisos else "revisar",
        "origem_nota": "ia",
        "avisos_ia": avisos,
    }, []