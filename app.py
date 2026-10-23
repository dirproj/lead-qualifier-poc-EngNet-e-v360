"""
ETAPA 4: PAINEL (mini-CRM em Streamlit)
=====================================================================
Mostra os leads extraídos, enriquecidos e classificados pela IA numa
tabela interativa, com filtros, detalhe de cada lead e um funil
comercial simples (etapa + anotações do SDR).

COMO RODAR (a partir da RAIZ do projeto):
    pip install -r requirements.txt     # precisa de streamlit >= 1.37
    streamlit run app.py             # no Windows, se não achar o comando:
    python -m streamlit run app.py
    # abre em http://localhost:8501 (Ctrl+C no terminal encerra o servidor)

A busca de leads é feita pelo botão "Buscar leads" no topo da página: ele
roda o mesmo `pipeline.py` que antes era chamado no terminal (teste rápido
com --max-ia, ou rodada completa) e mostra o andamento ao vivo. A tabela
atualiza sozinha quando a coleta termina.

O painel LÊ data/leads_processados.json (gerado pelo pipeline). O estado
comercial (etapa e anotações) fica em data/crm_estado.json e NÃO é apagado
por novas coletas.
=====================================================================
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from classificacao.perfil import CATALOGO_SERVICOS

RAIZ = Path(__file__).resolve().parent
ARQ_LEADS = RAIZ / "data" / "leads_processados.json"
ARQ_CRM = RAIZ / "data" / "crm_estado.json"
ARQ_PIPELINE = RAIZ / "pipeline.py"

# A página de cada estado segue o padrão https://programacentelha.com.br/<uf>/
URL_CENTELHA = "https://programacentelha.com.br/{uf}/"
ESTADOS = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia",
    "CE": "Ceará", "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás",
    "MA": "Maranhão", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul",
    "MG": "Minas Gerais", "PA": "Pará", "PB": "Paraíba", "PR": "Paraná",
    "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro",
    "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rondônia",
    "RR": "Roraima", "SC": "Santa Catarina", "SP": "São Paulo", "SE": "Sergipe",
    "TO": "Tocantins",
}
MODO_TESTE = "Teste rápido"
MODO_COMPLETO = "Rodada completa"

ETAPAS_CRM = ["Novo", "Contatado", "Em conversa", "Reunião marcada", "Descartado"]
FAIXAS = ["alto", "medio", "baixo", "sem nota"]
ICONE_FAIXA = {"alto": "🟢", "medio": "🟡", "baixo": "🔴", "sem nota": "⚪"}
ROTULO_STATUS_IA = {
    "ok": " ok", "revisar": " revisar", "falha": " falha",
    "sem_chave": " sem chave", "nao_processado": "⏸ não processado",
}
ROTULO_ENRIQ = {
    "ok": " CNPJ vinculado", "sem_cnpj": "— sem CNPJ", "cnpj_invalido": " CNPJ inválido",
    "nao_encontrado_ou_api_fora": " cadastro indisponível", "ambiguo": " ambíguo",
    "nao_verificado": " não verificado",
}
ROTULO_CONTATO = {
    "confirmado": " confirmado", "nao_confirmado": " não confirmado",
    "compartilhado": " compartilhado", "contabilidade": " contabilidade",
    "coworking": " coworking/recepção", "mascarado": " dígitos ocultos",
    "incompleto": " incompleto", "invalido": " inválido", "sem_dado": "— sem dado",
}

st.set_page_config(page_title="Pré-SDR | Mini-CRM", page_icon=" ", layout="wide")


# ---------------------------------------------------------------
# Dados
# ---------------------------------------------------------------
def lead_id(lead: dict) -> str:
    base = f"{lead.get('titulo_projeto', '')}|{lead.get('proponente', '')}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


@st.cache_data(show_spinner=False)
def _ler_leads(caminho: str, mtime: float) -> list[dict]:
    """`mtime` só serve para invalidar o cache quando o arquivo muda."""
    return json.loads(Path(caminho).read_text(encoding="utf-8"))


def carregar_leads() -> list[dict] | None:
    """Devolve os leads, ou None se ainda não há dados legíveis (a página segue
    sendo exibida, para o botão de busca continuar disponível)."""
    if not ARQ_LEADS.exists():
        return None
    try:
        return _ler_leads(str(ARQ_LEADS), ARQ_LEADS.stat().st_mtime)
    except (json.JSONDecodeError, OSError) as e:
        st.error(
            f"Não consegui ler {ARQ_LEADS.name}: {e}. Se uma busca estiver em andamento, "
            "o arquivo pode estar sendo gravado agora; aguarde e recarregue. "
            "Caso contrário, rode a busca novamente."
        )
        return None


def carregar_crm() -> dict:
    try:
        return json.loads(ARQ_CRM.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def salvar_crm(estado: dict) -> bool:
    try:
        ARQ_CRM.parent.mkdir(parents=True, exist_ok=True)
        ARQ_CRM.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError as e:
        st.error(f"Não consegui salvar o estado comercial: {e}")
        return False


def faixa_de(lead: dict) -> str:
    return lead.get("faixa_fit") or "sem nota"


def formatar_cnpj(cnpj: str | None) -> str:
    c = cnpj or ""
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if len(c) == 14 else (c or "—")


def nomes_servicos(chaves: list | None) -> str:
    return ", ".join(CATALOGO_SERVICOS.get(c, c) for c in (chaves or []))


# ---------------------------------------------------------------
# Busca de leads (roda o pipeline.py sem precisar do terminal)
# ---------------------------------------------------------------
class ExecucaoPipeline:
    """Roda `pipeline.py` em um subprocesso e guarda o log.

    Existe UMA instância por servidor (ver `execucao_pipeline`), então:
    - recarregar a página não perde o andamento;
    - dois cliques (ou duas abas) nunca disparam duas coletas ao mesmo tempo,
      o que evitaria gasto duplicado de créditos da IA.
    """

    MAX_LINHAS = 500

    def __init__(self) -> None:
        self._trava = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.linhas: deque[str] = deque(maxlen=self.MAX_LINHAS)
        self.inicio: float | None = None
        self.fim: float | None = None
        self.cancelado = False
        self.descricao = ""

    @property
    def rodando(self) -> bool:
        # `fim` só é preenchido DEPOIS de o log inteiro ter sido lido.
        return self.proc is not None and self.fim is None

    @property
    def codigo(self) -> int | None:
        return self.proc.returncode if self.proc is not None and self.fim is not None else None

    @property
    def duracao(self) -> float:
        if self.inicio is None:
            return 0.0
        return (self.fim or time.time()) - self.inicio

    def iniciar(self, url: str, max_ia: int | None) -> str | None:
        """Dispara a coleta. Devolve uma mensagem de erro, ou None se deu certo."""
        with self._trava:
            if self.rodando:
                return "Já existe uma busca em andamento."
            # Lista de argumentos (sem shell): a URL nunca é interpretada como comando.
            cmd = [sys.executable, "-u", str(ARQ_PIPELINE), "--pagina", url]
            if max_ia:
                cmd += ["--max-ia", str(max_ia)]
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
            self.linhas.clear()
            self.cancelado = False
            self.descricao = f"{url}" + (f" · teste com até {max_ia} chamadas à IA" if max_ia else " · rodada completa")
            try:
                self.proc = subprocess.Popen(
                    cmd, cwd=RAIZ, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
            except OSError as e:
                self.proc = None
                return f"Não consegui iniciar o pipeline: {e}"
            self.inicio, self.fim = time.time(), None
            threading.Thread(target=self._ler_saida, args=(self.proc,), daemon=True).start()
            return None

    def _ler_saida(self, proc: subprocess.Popen) -> None:
        try:
            for linha in proc.stdout:  # type: ignore[union-attr]
                self.linhas.append(linha.rstrip())
        finally:
            proc.wait()
            self.fim = time.time()

    def cancelar(self) -> None:
        if self.rodando and self.proc is not None:
            self.cancelado = True
            self.proc.terminate()


@st.cache_resource
def execucao_pipeline() -> ExecucaoPipeline:
    return ExecucaoPipeline()


def chave_gemini_configurada() -> bool:
    """True se GEMINI_API_KEY existe no ambiente ou no .env (nunca exibe o valor)."""
    if os.environ.get("GEMINI_API_KEY"):
        return True
    try:
        for linha in (RAIZ / ".env").read_text(encoding="utf-8").splitlines():
            nome, _, valor = linha.partition("=")
            if nome.strip().removeprefix("export ").strip() == "GEMINI_API_KEY" and valor.strip().strip("'\""):
                return True
    except OSError:
        pass
    return False


def _fmt_duracao(segundos: float) -> str:
    m, s = divmod(int(segundos), 60)
    return f"{m}min {s:02d}s" if m else f"{s}s"


def _acompanhar() -> None:
    """Mostra o log ao vivo. Roda como fragmento: atualiza sozinho a cada 1,5 s
    sem recarregar a página inteira (precisa de streamlit >= 1.37)."""
    exe = execucao_pipeline()
    if not exe.rodando:  # terminou entre dois ciclos: redesenha a página inteira
        st.rerun()
        return
    st.info(f"⏳ Buscando leads… {_fmt_duracao(exe.duracao)} · {exe.descricao}\n\n"
            "Pode continuar usando o painel; a tabela atualiza quando terminar.")
    st.code("\n".join(list(exe.linhas)[-25:]) or "Iniciando…", language=None)
    if not hasattr(st, "fragment"):  # Streamlit antigo: sem atualização automática
        st.button("Atualizar andamento", key="btn_atualizar_andamento")
    if st.button("⏹ Cancelar busca", key="btn_cancelar_busca"):
        exe.cancelar()


# Com st.fragment(run_every=...) o log se atualiza sozinho; em versões antigas
# do Streamlit a função roda normalmente, com um botão manual de atualização.
acompanhar_execucao = st.fragment(run_every=1.5)(_acompanhar) if hasattr(st, "fragment") else _acompanhar


def _resumo_ultima_execucao(exe: ExecucaoPipeline) -> None:
    if exe.inicio is None or exe.rodando:
        return
    if exe.cancelado:
        st.warning(f"Busca cancelada após {_fmt_duracao(exe.duracao)}. Os dados anteriores foram mantidos "
                   "(ou parciais, se o pipeline já tinha gravado algo).")
    elif exe.codigo == 0:
        st.success(f"✅ Busca concluída em {_fmt_duracao(exe.duracao)} · {exe.descricao}")
    else:
        st.error(f"❌ A busca terminou com erro (código {exe.codigo}). Veja o log abaixo.")
    with st.expander("Ver log da última busca", expanded=exe.codigo not in (0, None) and not exe.cancelado):
        st.code("\n".join(exe.linhas) or "(sem saída)", language=None)


def painel_coleta() -> None:
    exe = execucao_pipeline()
    with st.container(border=True):
        st.markdown("#### 🔎 Buscar leads")

        if not ARQ_PIPELINE.exists():
            st.error(f"Não encontrei `{ARQ_PIPELINE.name}` na pasta do projeto ({RAIZ}).")
            return

        if exe.rodando:
            acompanhar_execucao()
            return

        c1, c2 = st.columns([2, 3])
        uf = c1.selectbox(
            "Estado", list(ESTADOS), index=list(ESTADOS).index("DF"),
            format_func=lambda s: f"{s} · {ESTADOS[s]}", key="coleta_uf",
        )
        modo = c2.radio(
            "Tipo de busca", [MODO_TESTE, MODO_COMPLETO], horizontal=True, key="coleta_modo",
            help="Teste rápido: faz tudo, mas só chama a IA para poucos leads (barato). "
                 "Rodada completa: classifica todos os leads com a IA.",
        )

        max_ia: int | None = None
        liberado = True
        if modo == MODO_TESTE:
            max_ia = int(st.number_input(
                "Máximo de chamadas à IA", min_value=1, max_value=50, value=3, step=1, key="coleta_max_ia",
                help="Os demais leads aparecem como 'não processado', sem nota de fit.",
            ))
        else:
            liberado = st.checkbox(
                "Entendo que a rodada completa usa créditos da API do Gemini", key="coleta_confirma",
            )

        if not chave_gemini_configurada():
            st.warning("Não encontrei GEMINI_API_KEY no `.env`: a busca roda, mas os leads ficam "
                       "sem classificação da IA (status “sem chave”).")

        if st.button("▶ Buscar leads", type="primary", disabled=not liberado, key="btn_buscar_leads"):
            erro = exe.iniciar(URL_CENTELHA.format(uf=uf.lower()), max_ia)
            if erro:
                st.error(erro)
            else:
                st.rerun()

        _resumo_ultima_execucao(exe)


# ---------------------------------------------------------------
# Filtros (barra lateral)
# ---------------------------------------------------------------
def barra_lateral(leads: list[dict]) -> dict:
    sb = st.sidebar
    sb.header("Filtros")

    filtros = {
        "faixas": sb.multiselect("Faixa de fit", FAIXAS, default=FAIXAS),
        "nota_min": sb.slider("Nota mínima", 0, 10, 0),
        "confianca": sb.multiselect(
            "Confiança da IA", ["alta", "media", "baixa"], default=["alta", "media", "baixa"],
            help="Leads sem nota (sem IA) só aparecem se a nota mínima for 0.",
        ),
        "status_ia": sb.multiselect(
            "Status da IA", list(ROTULO_STATUS_IA),
            format_func=ROTULO_STATUS_IA.get, default=list(ROTULO_STATUS_IA),
            help="'revisar' = a IA respondeu, mas um guardrail apontou algo para conferir.",
        ),
        "tematicas": sb.multiselect(
            "Temática", sorted({l.get("tematica") for l in leads if l.get("tematica")}),
        ),
        "so_cnpj": sb.checkbox("Só com CNPJ vinculado"),
        "so_telefone": sb.checkbox("Só com telefone confirmado"),
        "busca": sb.text_input("Buscar no projeto", placeholder="ex.: saúde, IoT, agro"),
    }

    sb.divider()
    filtros["mostrar_proponente"] = sb.checkbox(
        "Mostrar nome do proponente", value=False,
        help="Pessoas físicas. Deixe desligado em apresentações.",
    )
    if sb.button(" Recarregar dados"):
        st.cache_data.clear()
        st.rerun()

    modelo = next((l.get("modelo_ia") for l in leads if l.get("modelo_ia")), None)
    gerado = datetime.fromtimestamp(ARQ_LEADS.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
    sb.caption(f"Dados gerados em {gerado}" + (f" · modelo: {modelo}" if modelo else ""))
    return filtros


def estado_da_lead(lead: dict) -> str:
    """Identifica a UF da lead a partir dos campos disponíveis no JSON."""
    chaves_estado = ("uf", "estado_uf", "sigla_uf", "estado", "unidade_federativa")
    for chave in chaves_estado:
        valor = lead.get(chave)
        if not valor:
            continue
        texto = str(valor).strip()
        sigla = texto.upper()
        if sigla in ESTADOS:
            return sigla
        for uf, nome in ESTADOS.items():
            if texto.casefold() == nome.casefold():
                return uf

    # Algumas fontes guardam apenas a URL da página de origem.
    chaves_url = ("url_origem", "pagina_origem", "url_fonte", "url", "fonte", "link")
    for chave in chaves_url:
        valor = lead.get(chave)
        if not valor:
            continue
        url = str(valor).lower().replace("https://", "http://")
        for uf in ESTADOS:
            if f"programacentelha.com.br/{uf.lower()}/" in url:
                return uf
    return "Não identificado"


def aplicar_filtros(leads: list[dict], f: dict) -> list[dict]:
    busca = f["busca"].strip().lower()
    saida = []
    for l in leads:
        nota = l.get("nota_fit")
        if faixa_de(l) not in f["faixas"]:
            continue
        if f["nota_min"] > 0 and (nota is None or nota < f["nota_min"]):
            continue
        if nota is not None and l.get("confianca") not in f["confianca"]:
            continue
        if (l.get("status_ia") or "nao_processado") not in f["status_ia"]:
            continue
        if f["tematicas"] and l.get("tematica") not in f["tematicas"]:
            continue
        if f["so_cnpj"] and l.get("status_enriquecimento") != "ok":
            continue
        if f["so_telefone"] and not l.get("telefone"):
            continue
        if busca:
            texto = " ".join(str(l.get(c, "")) for c in (
                "titulo_projeto", "tematica", "municipio", "justificativa", "razao_social")).lower()
            if busca not in texto:
                continue
        saida.append(l)
    return sorted(saida, key=lambda l: (l.get("nota_fit") is None, -(l.get("nota_fit") or 0)))


# ---------------------------------------------------------------
# Visão geral
# ---------------------------------------------------------------
def mostrar_kpis(todos: list[dict], filtrados: list[dict]) -> None:
    notas = [l["nota_fit"] for l in todos if l.get("nota_fit") is not None]
    c = st.columns(6)
    c[0].metric("Leads extraídos", len(todos))
    c[1].metric("Exibindo", len(filtrados))
    c[2].metric("Alto fit (7+)", sum(1 for l in todos if faixa_de(l) == "alto"))
    c[3].metric("Para revisar", sum(1 for l in todos if l.get("status_ia") == "revisar"),
                help="Respostas da IA que um guardrail pediu para conferir.")
    c[4].metric("Com CNPJ", sum(1 for l in todos if l.get("status_enriquecimento") == "ok"),
                help="CNPJ encontrado automaticamente (ou informado) e conferido no cadastro oficial.")
    c[5].metric("Telefone confirmado", sum(1 for l in todos if l.get("telefone")),
                help="Só conta número completo e claramente da pessoa/startup (não contador, coworking etc.).")
    if notas:
        st.caption(f"Nota média de fit: {sum(notas) / len(notas):.1f} · {len(notas)} leads classificados")


def montar_tabela(leads: list[dict], crm: dict, mostrar_proponente: bool) -> pd.DataFrame:
    linhas = []
    for l in leads:
        faixa = faixa_de(l)
        linha = {
            "Fit": f"{ICONE_FAIXA[faixa]} {faixa}",
            "Nota": l.get("nota_fit"),
            "Confiança": l.get("confianca") or "—",
            "Projeto": l.get("titulo_projeto"),
        }
        if mostrar_proponente:
            linha["Proponente"] = l.get("proponente")
        linha.update({
            "Temática": l.get("tematica"),
            "Município": l.get("municipio"),
            "Serviços sugeridos": nomes_servicos(l.get("servicos_sugeridos")),
            "IA": ROTULO_STATUS_IA.get(l.get("status_ia"), "—"),
            "CNPJ nº": formatar_cnpj(l.get("cnpj")) if l.get("cnpj") else "—",
            "Telefone": l.get("telefone") or "—",
            "E-mail": l.get("email") or "—",
            "Contato": ROTULO_CONTATO.get(l.get("telefone_status"), "—"),
            "Etapa": crm.get(lead_id(l), {}).get("etapa", "Novo"),
        })
        linhas.append(linha)
    return pd.DataFrame(linhas)


def exportar_csv(leads: list[dict], crm: dict, mostrar_proponente: bool) -> bytes:
    linhas = []
    for l in leads:
        linha = {
            "nota_fit": l.get("nota_fit"), "faixa": faixa_de(l), "confianca": l.get("confianca"),
            "projeto": l.get("titulo_projeto"),
        }
        if mostrar_proponente:
            linha["proponente"] = l.get("proponente")
        linha.update({
            "tematica": l.get("tematica"), "municipio": l.get("municipio"),
            "servicos_sugeridos": nomes_servicos(l.get("servicos_sugeridos")),
            "justificativa": l.get("justificativa"), "status_ia": l.get("status_ia"),
            "cnpj": l.get("cnpj"), "razao_social": l.get("razao_social"),
            "cnpj_origem": l.get("cnpj_origem"), "cnpj_confianca": l.get("cnpj_confianca"),
            "cnae": l.get("cnae_descricao"), "porte": l.get("porte"),
            "telefone": l.get("telefone"), "telefone_status": l.get("telefone_status"),
            "telefone_obs": l.get("telefone_obs"),
            "email": l.get("email"), "email_status": l.get("email_status"),
            "etapa": crm.get(lead_id(l), {}).get("etapa", "Novo"),
            "anotacoes": crm.get(lead_id(l), {}).get("notas", ""),
        })
        linhas.append(linha)
    # utf-8-sig + ';' para abrir direto no Excel em português
    return pd.DataFrame(linhas).to_csv(index=False, sep=";").encode("utf-8-sig")


# ---------------------------------------------------------------
# Detalhe do lead + funil comercial
# ---------------------------------------------------------------
def painel_detalhe(lead: dict, crm: dict, mostrar_proponente: bool) -> None:
    lid = lead_id(lead)
    faixa = faixa_de(lead)
    nota = lead.get("nota_fit")

    st.subheader(lead.get("titulo_projeto") or "(sem título)")
    st.caption(
        f"{lead.get('tematica', '—')} · {lead.get('municipio', '—')}"
        + (f" · Proponente: {lead.get('proponente')}" if mostrar_proponente else "")
    )

    esq, dir_ = st.columns([3, 2])
    with esq:
        m1, m2, m3 = st.columns(3)
        m1.metric("Nota de fit", "—" if nota is None else f"{nota}/10")
        m2.metric("Faixa", f"{ICONE_FAIXA[faixa]} {faixa}")
        m3.metric("Confiança", lead.get("confianca") or "—")

        if lead.get("justificativa"):
            st.markdown("**Por que essa nota**")
            st.write(lead["justificativa"])
        elif lead.get("status_ia") in ("falha", "sem_chave", "nao_processado"):
            st.info(f"Sem avaliação da IA: {'; '.join(lead.get('avisos_ia') or ['não processado'])}")

        if lead.get("evidencias"):
            st.markdown("**Trechos dos dados que sustentam a nota**")
            for e in lead["evidencias"]:
                st.markdown(f"> “{e}”")
        if lead.get("servicos_sugeridos"):
            st.markdown("**Serviços sugeridos:** " + nomes_servicos(lead["servicos_sugeridos"]))
        for aviso in lead.get("avisos_ia") or []:
            if lead.get("status_ia") in ("ok", "revisar"):
                st.warning(f"Guardrail: {aviso}")

    with dir_:
        st.markdown("**Empresa (cadastro da Receita)**")
        if lead.get("status_enriquecimento") == "ok":
            st.write(f"**Razão social:** {lead.get('razao_social') or '—'}")
            st.write(f"**CNPJ:** {formatar_cnpj(lead.get('cnpj'))}")
            st.write(f"**CNAE:** {lead.get('cnae_descricao') or '—'}")
            st.write(f"**Porte:** {lead.get('porte') or '—'} · **Abertura:** {lead.get('data_abertura') or '—'}")
            origem = {"automatico": "busca automática", "manual": "informado pela equipe"}.get(lead.get("cnpj_origem"), "—")
            st.caption(f"Como foi encontrado: {origem} · confiança {lead.get('cnpj_confianca') or '—'}. "
                       f"{lead.get('cnpj_evidencia') or ''}")
        else:
            st.info(f"CNPJ não vinculado ({ROTULO_ENRIQ.get(lead.get('status_enriquecimento'), '—')}). "
                    f"{lead.get('cnpj_evidencia') or ''}")
            for cand in lead.get("cnpj_candidatos") or []:
                st.caption(f"Candidato (NÃO confirmado): {formatar_cnpj(cand.get('cnpj'))} · "
                           f"{cand.get('razao_social')} · {cand.get('motivo')}")

        st.markdown("**Contato**")
        if lead.get("status_enriquecimento") == "ok":
            if lead.get("telefone"):
                st.success(f"📞 {lead['telefone']} ({lead.get('telefone_tipo')}) · confiança {lead.get('telefone_confianca')}")
            else:
                st.warning(f"Telefone não usado: {ROTULO_CONTATO.get(lead.get('telefone_status'), '—')}")
            st.caption(lead.get("telefone_obs") or "")
            if lead.get("telefone_candidato"):
                st.caption(f"Número no cadastro, NÃO confirmado: {lead['telefone_candidato']}")
            if lead.get("email"):
                st.success(f"✉️ {lead['email']} · confiança {lead.get('email_confianca')}")
            else:
                st.warning(f"E-mail não usado: {ROTULO_CONTATO.get(lead.get('email_status'), '—')}")
            st.caption(lead.get("email_obs") or "")
            if lead.get("email_candidato"):
                st.caption(f"E-mail no cadastro, NÃO confirmado: {lead['email_candidato']}")
        else:
            st.caption("Sem CNPJ vinculado, não há contato a confirmar.")

        st.markdown("**Funil comercial**")
        atual = crm.get(lid, {})
        with st.form(f"form_{lid}"):
            etapa = st.selectbox(
                "Etapa", ETAPAS_CRM,
                index=ETAPAS_CRM.index(atual["etapa"]) if atual.get("etapa") in ETAPAS_CRM else 0,
            )
            notas = st.text_area("Anotações do SDR", value=atual.get("notas", ""), height=110)
            if st.form_submit_button("Salvar"):
                crm[lid] = {"etapa": etapa, "notas": notas, "atualizado": datetime.now().isoformat(timespec="seconds")}
                if salvar_crm(crm):
                    st.session_state["_salvo"] = True
                    st.rerun()


# ---------------------------------------------------------------
# Página
# ---------------------------------------------------------------
def main() -> None:
    st.title("🎯 Pré-SDR · Mini-CRM de Leads")
    st.caption("Projetos aprovados em editais de inovação, qualificados por IA com travas anti-alucinação.")
    if st.session_state.pop("_salvo", False):
        st.toast("Estado comercial salvo")

    painel_coleta()

    todos = carregar_leads()
    if todos is None:
        if not execucao_pipeline().rodando:
            st.info("Ainda não há dados. Escolha o estado e clique em **Buscar leads** acima.")
        return
    filtros = barra_lateral(todos)

    # Filtro adicional para navegar pelas leads mapeadas em cada estado.
    estados_disponiveis = sorted({estado_da_lead(l) for l in todos}, key=lambda uf: (uf == "Não identificado", uf))
    opcoes_estado = ["Todos os estados", *estados_disponiveis]
    estado_selecionado = st.selectbox(
        "Estado das leads",
        opcoes_estado,
        key="filtro_estado_leads",
        help="Filtra a tabela para mostrar apenas leads associadas ao estado selecionado.",
    )

    leads_por_estado = todos if estado_selecionado == "Todos os estados" else [
        l for l in todos if estado_da_lead(l) == estado_selecionado
    ]
    filtrados = aplicar_filtros(leads_por_estado, filtros)
    crm = carregar_crm()

    mostrar_kpis(todos, filtrados)
    if estados_disponiveis == ["Não identificado"]:
        st.caption("Não encontrei a UF nos campos das leads. Para filtrar por estado, o JSON precisa registrar a UF ou a URL de origem de cada lead.")

    contagem = pd.Series([faixa_de(l) for l in filtrados]).value_counts().reindex(FAIXAS, fill_value=0)
    with st.expander("Distribuição por faixa de fit", expanded=False):
        st.bar_chart(contagem)

    if not filtrados:
        st.info("Nenhum lead com esses filtros.")
        return

    df = montar_tabela(filtrados, crm, filtros["mostrar_proponente"])
    st.markdown("##### Leads (clique em uma linha para abrir o detalhe)")
    evento = st.dataframe(
        df, hide_index=True, key="tabela_leads",
        on_select="rerun", selection_mode="single-row",
        column_config={
            "Nota": st.column_config.ProgressColumn("Nota", min_value=0, max_value=10, format="%d"),
            "Projeto": st.column_config.TextColumn(width="large"),
            "Serviços sugeridos": st.column_config.TextColumn(width="medium"),
        },
    )
    st.download_button(
        "⬇ Baixar leads filtrados (CSV)",
        data=exportar_csv(filtrados, crm, filtros["mostrar_proponente"]),
        file_name="leads_filtrados.csv", mime="text/csv",
    )

    linhas = evento.selection.rows
    st.divider()
    if linhas and linhas[0] < len(filtrados):
        painel_detalhe(filtrados[linhas[0]], crm, filtros["mostrar_proponente"])
    else:
        st.caption("Selecione um lead na tabela para ver a justificativa da IA, os dados cadastrais e registrar o contato.")


main()