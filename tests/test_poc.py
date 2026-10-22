"""Testes offline (sem rede, sem chave). Rodar:  python -m unittest discover -s tests -v"""
import json
import types
import unittest

import openai

from classificacao import llm, regras
from classificacao.guardrails import validar_resposta, normalizar_dados
from enriquecimento import descoberta as cr
from enriquecimento.contatos import analisar_telefone, classificar, extrair_contatos_html, coletar_contatos
from extracao.validacao import cnpj_valido

LEAD = {"titulo_projeto": "Plataforma de inteligência artificial para triagem de exames",
        "tematica": "Saúde / TI", "municipio": "Brasília"}


# ----------------------------------------------------------- IA (cliente simulado)
class RespostaFalsa:
    def __init__(self, conteudo):
        msg = types.SimpleNamespace(content=conteudo, refusal=None)
        self.choices = [types.SimpleNamespace(message=msg)]


class ClienteFalso:
    """Devolve, em ordem, textos ou exceções."""
    def __init__(self, respostas):
        self.respostas, self.chamadas = list(respostas), 0
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.chamadas += 1
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return RespostaFalsa(r)


def erro_http(status, texto, cls=openai.APIStatusError):
    import httpx
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    resp = httpx.Response(status, request=req, json={"error": {"message": texto}})
    return cls(texto, response=resp, body={"error": {"message": texto}})


BOA = json.dumps({"nota_fit": 8, "confianca": "alta", "dados_insuficientes": False,
                  "justificativa": "Projeto de IA para exames.", "servicos_sugeridos": ["dados_ia"],
                  "evidencias": ["inteligência artificial"]})


class TestIA(unittest.TestCase):
    def setUp(self):
        llm._ultima_chamada = 0
        self._pausa = llm.os.environ.get("LLM_PAUSA")
        llm.os.environ["LLM_PAUSA"] = "0"
        llm.time.sleep = lambda *_: None   # testes rápidos

    def test_tolera_formato_de_modelo_aberto(self):
        # nota como texto, confiança com acento, serviço fora do catálogo, sem 'evidencias'
        d = {"nota": "7", "confiança": "média", "justificativa": "ok", "servicos_sugeridos": ["dados_ia", "xpto"],
             "evidencias": ["inteligencia artificial"]}
        r, problemas = validar_resposta(d, "inteligencia artificial")
        self.assertEqual(problemas, [])
        self.assertEqual(r["nota_fit"], 7)
        self.assertEqual(r["confianca"], "media")
        self.assertEqual(r["servicos_sugeridos"], ["dados_ia"])
        self.assertIn("xpto", " ".join(r["avisos_ia"]))

    def test_json_com_cerca_markdown(self):
        r = llm.classificar_lead(LEAD, ClienteFalso(["```json\n" + BOA + "\n```", "```json\n" + BOA + "\n```"]), "m")
        self.assertEqual(r["status_ia"], "ok")
        self.assertEqual(r["nota_fit"], 8)

    def test_limite_por_minuto_tenta_de_novo(self):
        c = ClienteFalso([erro_http(429, "rate limit reached for requests per minute", openai.RateLimitError), BOA])
        r = llm.classificar_lead(LEAD, c, "m")
        self.assertEqual(r["nota_fit"], 8)
        self.assertEqual(c.chamadas, 2)

    def test_sem_credito_openai_e_fatal(self):
        c = ClienteFalso([erro_http(429, "You exceeded your current quota (insufficient_quota)", openai.RateLimitError)])
        with self.assertRaises(llm.ErroFatalIA):
            llm.classificar_lead(LEAD, c, "m")

    def test_cota_diaria_vira_regras_no_resto_do_lote(self):
        c = ClienteFalso([erro_http(429, "Rate limit reached ... tokens per day (TPD)", openai.RateLimitError)])
        leads = [dict(LEAD), dict(LEAD, titulo_projeto="Outro projeto de software")]
        out = llm.classificar_leads(leads, client=c, provedor="groq", modelo="m", usar_cache=False)
        self.assertEqual([l["status_ia"] for l in out], ["regras", "regras"])
        self.assertTrue(all(l["nota_fit"] is not None for l in out))   # NOTA SEMPRE SAI
        self.assertEqual(c.chamadas, 1)                                 # parou de chamar

    def test_modelo_descontinuado_troca_de_modelo(self):
        c = ClienteFalso([erro_http(400, "The model `velho` has been decommissioned and is no longer supported"), BOA])
        out = llm.classificar_leads([dict(LEAD)], client=c, provedor="groq", usar_cache=False)
        # sem 'modelo' fixo: usa a lista preferida (cliente falso não lista modelos)
        self.assertEqual(out[0]["status_ia"], "ok")

    def test_ia_com_json_quebrado_cai_em_regras(self):
        c = ClienteFalso(["isto não é json"] * 4)
        out = llm.classificar_leads([dict(LEAD)], client=c, provedor="groq", modelo="m", usar_cache=False)
        self.assertEqual(out[0]["status_ia"], "regras")
        self.assertIsNotNone(out[0]["nota_fit"])

    def test_sem_chave_ainda_gera_nota(self):
        antes = llm.os.environ.pop("GROQ_API_KEY", None)
        try:
            out = llm.classificar_leads([dict(LEAD)], provedor="groq", usar_cache=False)
        finally:
            if antes:
                llm.os.environ["GROQ_API_KEY"] = antes
        self.assertEqual(out[0]["status_ia"], "regras")
        self.assertIn("GROQ_API_KEY", out[0]["avisos_ia"][0])

    def test_regras_nunca_passam_de_6_sem_cnae_e_nao_inventam(self):
        r = regras.pontuar({"titulo_projeto": "Plataforma de dados com IoT e nuvem e LGPD", "tematica": "TI"})
        self.assertLessEqual(r["nota_fit"], 6)
        r0 = regras.pontuar({"titulo_projeto": "Cooperativa de apicultura", "tematica": "Agro"})
        self.assertLessEqual(r0["nota_fit"], 2)
        self.assertEqual(r0["origem_nota"], "regras")

    def test_nome_do_proponente_nao_vai_para_a_ia(self):
        self.assertNotIn("proponente", llm.montar_payload({**LEAD, "proponente": "Fulano de Tal"}))


# ----------------------------------------------------------- CNPJ
def brasilapi_falsa(base):
    return lambda cnpj: base.get(cnpj)


MEI = "11222333000181"
assert cnpj_valido(MEI)
OUTRO = "11444777000161"
assert cnpj_valido(OUTRO)


class TestCNPJ(unittest.TestCase):
    def lead(self):
        return {"proponente": "Maria Souza Lima", "titulo_projeto": "App Agro Smart", "municipio": "Brasília"}

    def test_extrai_candidatos_validos(self):
        txt = f"CNPJ 11.222.333/0001-81 e lixo 12345678901234 e {OUTRO}"
        self.assertEqual(cr.candidatos_do_texto(txt), [MEI, OUTRO])

    def test_mei_com_nome_na_razao_e_mesmo_municipio_e_alta(self):
        api = {MEI: {"razao_social": "12345678 MARIA SOUZA LIMA", "municipio": "BRASILIA",
                     "descricao_situacao_cadastral": "ATIVA"}}
        v = cr.verificar(MEI, self.lead(), brasilapi_falsa(api))
        self.assertEqual(v["confianca"], "alta")

    def test_homonimo_de_outra_cidade_fica_media(self):
        api = {MEI: {"razao_social": "12345678 MARIA SOUZA LIMA", "municipio": "MANAUS",
                     "descricao_situacao_cadastral": "ATIVA"}}
        self.assertEqual(cr.verificar(MEI, self.lead(), brasilapi_falsa(api))["confianca"], "media")

    def test_empresa_inativa_nao_e_usada(self):
        api = {MEI: {"razao_social": "12345678 MARIA SOUZA LIMA", "municipio": "BRASILIA",
                     "descricao_situacao_cadastral": "BAIXADA"}}
        self.assertEqual(cr.verificar(MEI, self.lead(), brasilapi_falsa(api))["confianca"], "baixa")

    def test_socio_no_qsa_confirma_ltda(self):
        api = {OUTRO: {"razao_social": "AGRO SMART TECNOLOGIA LTDA", "municipio": "BRASILIA",
                       "descricao_situacao_cadastral": "ATIVA",
                       "qsa": [{"nome_socio": "MARIA SOUZA LIMA"}]}}
        self.assertEqual(cr.verificar(OUTRO, self.lead(), brasilapi_falsa(api))["confianca"], "alta")

    def test_fluxo_completo_com_busca_simulada(self):
        api = {MEI: {"razao_social": "12345678 MARIA SOUZA LIMA", "municipio": "BRASILIA",
                     "descricao_situacao_cadastral": "ATIVA"}}
        buscar = lambda q, **k: [{"href": "https://cnpj.biz/" + MEI, "title": "Maria", "body": ""}]
        baixar = lambda url, **k: (None, "robots")
        r = cr.resolver_cnpj_lead(self.lead(), buscar=buscar, baixar=baixar, consultar=brasilapi_falsa(api))
        self.assertEqual((r["cnpj"], r["cnpj_status"]), (MEI, "encontrado"))

    def test_nada_encontrado_nao_inventa(self):
        r = cr.resolver_cnpj_lead(self.lead(), buscar=lambda q, **k: [], baixar=lambda u, **k: (None, "x"),
                                  consultar=lambda c: None)
        self.assertIsNone(r["cnpj"])
        self.assertEqual(r["cnpj_status"], "nao_encontrado")

    def test_empate_vira_ambiguo(self):
        api = {c: {"razao_social": "11111111 MARIA SOUZA LIMA", "municipio": "BRASILIA",
                   "descricao_situacao_cadastral": "ATIVA"} for c in (MEI, OUTRO)}
        buscar = lambda q, **k: [{"href": f"https://x/{MEI}", "title": OUTRO, "body": ""}]
        r = cr.resolver_cnpj_lead(self.lead(), buscar=buscar, baixar=lambda u, **k: (None, "x"),
                                  consultar=brasilapi_falsa(api))
        self.assertEqual(r["cnpj_status"], "ambiguo")
        self.assertIsNone(r["cnpj"])


# ----------------------------------------------------------- Contatos (a regra do usuário)
LEAD_C = {"proponente": "Maria Souza Lima", "cnpj": MEI, "municipio": "Brasília",
          "site_proprio": "https://agrosmart.com.br"}


class TestContatos(unittest.TestCase):
    def test_telefone_completo_e_incompleto(self):
        self.assertEqual(analisar_telefone("(61) 98765-4321")["estado"], "completo")
        self.assertEqual(analisar_telefone("+55 61 3333-4444")["estado"], "completo")
        self.assertEqual(analisar_telefone("(61) 9****-4321")["estado"], "mascarado")
        self.assertEqual(analisar_telefone("(61) 98765-XXXX")["estado"], "mascarado")
        self.assertEqual(analisar_telefone("98765-4321")["estado"], "incompleto")      # sem DDD
        self.assertEqual(analisar_telefone("(00) 98765-4321")["estado"], "incompleto")  # DDD inválido
        self.assertEqual(analisar_telefone("0800 123 4567")["estado"], "central")

    def test_mascarado_nunca_e_confirmado(self):
        r = classificar("telefone", "(61) 9****-4321", ["site_proprio"], "", LEAD_C)
        self.assertEqual(r["classe"], "descartado")
        self.assertIn("escondidos", r["motivo"])

    def test_contabilidade_coworking_recepcao_sao_descartados(self):
        r = classificar("telefone", "(61) 98765-4321", ["site_proprio"], "Fale com nossa contabilidade", LEAD_C)
        self.assertEqual((r["classe"], "contabilidade" in r["motivo"]), ("descartado", True))
        r = classificar("email", "contato@contabilxyz.com.br", ["receita"], "", LEAD_C)
        self.assertEqual(r["classe"], "descartado")
        r = classificar("telefone", "(61) 98765-4321", ["site_proprio"], "Recepção do prédio", LEAD_C)
        self.assertIn("recepção", r["motivo"])
        lead = {**LEAD_C, "complemento_cnpj": "Sala 4 coworking Hub Central"}
        r = classificar("telefone", "(61) 3333-4444", ["receita"], "", lead)
        self.assertIn("coworking", r["motivo"])

    def test_so_receita_e_duvidoso_nao_confirmado(self):
        r = classificar("telefone", "(61) 98765-4321", ["receita", "agregador:cnpj.biz"], "", LEAD_C)
        self.assertEqual(r["classe"], "duvidoso")

    def test_site_proprio_confirma(self):
        r = classificar("telefone", "(61) 98765-4321", ["site_proprio"], "WhatsApp comercial", LEAD_C)
        self.assertEqual(r["classe"], "confirmado")

    def test_email_com_nome_ou_dominio_proprio(self):
        self.assertEqual(classificar("email", "maria.souza@gmail.com", ["receita"], "", LEAD_C)["classe"], "confirmado")
        self.assertEqual(classificar("email", "contato@agrosmart.com.br", ["receita"], "", LEAD_C)["classe"], "confirmado")
        self.assertEqual(classificar("email", "fulano@gmail.com", ["receita"], "", LEAD_C)["classe"], "duvidoso")

    def test_extrai_de_pagina(self):
        html = '<a href="tel:+5561987654321">Ligar</a> <p>Contato: (61) 9****-1111 ou a@b.com</p>'
        achados = extrair_contatos_html(html)
        tipos = {(a["tipo"], a["valor"]) for a in achados}
        self.assertIn(("telefone", "+5561987654321"), tipos)
        self.assertIn(("email", "a@b.com"), tipos)
        self.assertTrue(any("9****" in a["valor"] for a in achados))

    def test_coleta_completa_so_confirma_o_que_tem_prova(self):
        lead = {**LEAD_C, "ddd_telefone_1": "6133334444", "email_receita": "contato@contabilxyz.com.br"}
        html = '<a href="tel:+5561987654321">WhatsApp</a> (61) 9****-0000'
        baixar = lambda url, **k: (html, "ok") if "agrosmart" in url and url.rstrip("/").endswith(".br") else (None, "erro")
        r = coletar_contatos(lead, usar_web=True, buscar=lambda q, **k: [], baixar=baixar)
        self.assertEqual(r["telefone_confirmado"], "(61) 98765-4321")
        self.assertIsNone(r["email_confirmado"])
        classes = {c["valor"]: c["classe"] for c in r["contatos"] if c["tipo"] == "telefone"}
        self.assertEqual(classes["(61) 3333-4444"], "duvidoso")      # só Receita
        self.assertEqual(classes["(61) 9****-0000"], "descartado")   # mascarado


if __name__ == "__main__":
    unittest.main()
