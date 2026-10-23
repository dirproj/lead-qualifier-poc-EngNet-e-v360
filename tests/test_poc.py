"""Testes offline (sem rede, sem chave). Rodar:  python -m unittest discover -s tests -v"""
import json
import tempfile
import types
import unittest
from pathlib import Path

from classificacao import llm, regras
from classificacao.guardrails import validar_resposta
from enriquecimento import contatos, identidade, orquestrador
from enriquecimento.descoberta import bloqueado, extrair_cnpjs
from extracao.validacao import cnpj_valido
from planilha import celula_segura

LEAD = {"titulo_projeto": "Plataforma de inteligência artificial para triagem de exames",
        "tematica": "Saúde / TI", "municipio": "Brasília"}

BOA = json.dumps({"nota_fit": 8, "confianca": "alta", "dados_insuficientes": False,
                  "justificativa": "Projeto de IA para exames.", "servicos_sugeridos": ["dados_ia"],
                  "evidencias": ["inteligência artificial"]})

MEI = "11222333000181"
OUTRO = "11444777000161"
CONTADOR = "77888999000181"


class ClienteGeminiFalso:
    def __init__(self, respostas):
        self.respostas, self.chamadas = list(respostas), 0
        self.models = types.SimpleNamespace(generate_content=self._gerar)

    def _gerar(self, **kw):
        self.chamadas += 1
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return types.SimpleNamespace(text=r)


class TestIA(unittest.TestCase):
    def setUp(self):
        self._sleep = llm.time.sleep
        llm.time.sleep = lambda *_: None

    def tearDown(self):
        llm.time.sleep = self._sleep

    def test_resposta_valida(self):
        r = llm.classificar_lead(LEAD, ClienteGeminiFalso([BOA]), "m")
        self.assertEqual((r["status_ia"], r["nota_fit"], r["faixa_fit"]), ("ok", 8, "alto"))

    def test_limite_temporario_tenta_de_novo(self):
        c = ClienteGeminiFalso([Exception("429 RESOURCE_EXHAUSTED"), BOA])
        self.assertEqual(llm.classificar_lead(LEAD, c, "m")["nota_fit"], 8)
        self.assertEqual(c.chamadas, 2)

    def test_chave_invalida_e_fatal(self):
        with self.assertRaises(llm.ErroFatalIA):
            llm.classificar_lead(LEAD, ClienteGeminiFalso([Exception("401 UNAUTHENTICATED")]), "m")

    def test_json_quebrado_vira_falha_sem_nota(self):
        r = llm.classificar_lead(LEAD, ClienteGeminiFalso(["isto não é json"] * 3), "m")
        self.assertEqual(r["status_ia"], "falha")
        self.assertIsNone(r["nota_fit"])

    def test_sem_chave_marca_o_lead(self):
        chave_original = llm._chave_api
        llm._chave_api = lambda: None
        try:
            out = llm.classificar_leads([dict(LEAD), dict(LEAD, titulo_projeto="Outro")], usar_cache=False, pausa=0)
        finally:
            llm._chave_api = chave_original
        self.assertEqual([l["status_ia"] for l in out], ["sem_chave", "sem_chave"])
        self.assertIn("GEMINI_API_KEY", out[0]["avisos_ia"][0])

    def test_nome_do_proponente_nao_vai_para_a_ia(self):
        self.assertNotIn("proponente", llm.montar_payload({**LEAD, "proponente": "Fulano de Tal"}))


class TestGuardrails(unittest.TestCase):
    def test_tolera_formato_de_modelo_aberto(self):
        d = {"nota": "7", "confiança": "média", "justificativa": "ok", "servicos_sugeridos": ["dados_ia", "xpto"],
             "evidencias": ["inteligencia artificial"]}
        r, problemas = validar_resposta(d, "inteligencia artificial")
        self.assertEqual(problemas, [])
        self.assertEqual((r["nota_fit"], r["confianca"], r["servicos_sugeridos"]), (7, "media", ["dados_ia"]))
        self.assertIn("xpto", " ".join(r["avisos_ia"]))

    def test_evidencia_inventada_rebaixa_a_nota(self):
        d = {"nota_fit": 9, "confianca": "alta", "dados_insuficientes": False, "justificativa": "ok",
             "servicos_sugeridos": [], "evidencias": ["tem investidores e faturamento alto"]}
        r, _ = validar_resposta(d, "plataforma de dados")
        self.assertEqual((r["nota_fit"], r["confianca"], r["status_ia"]), (6, "baixa", "revisar"))

    def test_regras_nunca_passam_de_6_sem_cnae(self):
        r = regras.pontuar({"titulo_projeto": "Plataforma de dados com IoT e nuvem e LGPD", "tematica": "TI"})
        self.assertLessEqual(r["nota_fit"], 6)
        r0 = regras.pontuar({"titulo_projeto": "Cooperativa de apicultura", "tematica": "Agro"})
        self.assertLessEqual(r0["nota_fit"], 2)


class TestCNPJ(unittest.TestCase):
    def test_valida_digitos(self):
        self.assertTrue(cnpj_valido("11.222.333/0001-81"))
        self.assertFalse(cnpj_valido("11.222.333/0001-82"))
        self.assertFalse(cnpj_valido("00000000000000"))

    def test_extrai_so_candidatos_validos(self):
        self.assertEqual(extrair_cnpjs(f"CNPJ 11.222.333/0001-81 lixo 12345678901234 e {OUTRO}"), [MEI, OUTRO])

    def test_detecta_bloqueio_sem_contornar(self):
        self.assertTrue(bloqueado(200, "<title>Just a moment...</title>"))
        self.assertTrue(bloqueado(429, ""))
        self.assertFalse(bloqueado(200, "<html>ok</html>"))


class TestIdentidade(unittest.TestCase):
    BASE = {"situacao_cadastral": "ATIVA", "uf": "DF", "razao_social": "AGROSMART TECNOLOGIA LTDA"}
    LEAD = {"proponente": "Maria Souza Lima", "titulo_projeto": "AgroSmart: sensores no campo"}

    def test_socio_exato_e_startup_e_alta(self):
        r = identidade.avaliar(self.LEAD, {**self.BASE, "socios": ["MARIA SOUZA LIMA"]})
        self.assertEqual((r["confianca"], r["alerta"]), ("alta", None))

    def test_empresa_inativa_ou_de_outra_uf_e_rejeitada(self):
        inativa = identidade.avaliar(self.LEAD, {**self.BASE, "socios": ["MARIA SOUZA LIMA"], "situacao_cadastral": "BAIXADA"})
        outra_uf = identidade.avaliar(self.LEAD, {**self.BASE, "socios": ["MARIA SOUZA LIMA"], "uf": "SP"})
        self.assertIsNone(inativa["confianca"])
        self.assertIsNone(outra_uf["confianca"])

    def test_bate_parcial_com_dois_nomes_gera_alerta(self):
        lead = {"proponente": "Maria Souza", "titulo_projeto": "AgroSmart: sensores"}
        r = identidade.avaliar(lead, {**self.BASE, "socios": ["MARIA SOUZA LIMA OLIVEIRA"]})
        self.assertEqual(r["confianca"], "media")
        self.assertIn("homônimo", r["alerta"])


class TestContatos(unittest.TestCase):
    def test_forma_do_telefone(self):
        self.assertEqual(contatos.analisar_telefone("(61) 99812-3456")["status"], "completo")
        self.assertEqual(contatos.analisar_telefone("+55 61 3333-4444")["status"], "completo")
        self.assertEqual(contatos.analisar_telefone("(61) 9****-4321")["status"], "mascarado")
        self.assertEqual(contatos.analisar_telefone("99812-3456")["status"], "incompleto")
        self.assertEqual(contatos.analisar_telefone("(00) 99812-3456")["status"], "invalido")

    def avaliar(self, registro, compartilhados=(set(), set())):
        ident = {"pessoa": "exato"}
        lead = {"proponente": "Maria Souza Lima", "titulo_projeto": "AgroSmart: sensores"}
        return contatos.avaliar_contato(registro, ident, lead, *compartilhados)

    def test_email_do_dominio_da_startup_confirma(self):
        r = self.avaliar({"razao_social": "AGROSMART LTDA", "telefones": ["61 99812-3456"],
                          "email": "contato@agrosmart.com.br"})
        self.assertEqual((r["telefone"], r["email"]), ("(61) 99812-3456", "contato@agrosmart.com.br"))

    def test_email_de_contabilidade_bloqueia_o_contato(self):
        r = self.avaliar({"razao_social": "AGROSMART LTDA", "telefones": ["61 99812-3456"],
                          "email": "fiscal@contabilxyz.com.br"})
        self.assertIsNone(r["telefone"])
        self.assertEqual(r["telefone_status"], "contabilidade")

    def test_telefone_repetido_em_outro_cnpj_e_compartilhado(self):
        registros = [{"cnpj": MEI, "telefones": ["61 3333-4444"]},
                     contatos.minimizar_contatos({"cnpj": CONTADOR, "telefones": ["61 3333-4444"]})]
        compartilhados = contatos.contatos_compartilhados(registros)
        r = self.avaliar({"razao_social": "AGROSMART LTDA", "telefones": ["61 3333-4444"],
                          "email": "contato@agrosmart.com.br"}, compartilhados)
        self.assertEqual(r["telefone_status"], "compartilhado")

    def test_minimizar_tira_o_contato_e_guarda_a_impressao(self):
        r = contatos.minimizar_contatos({"cnpj": OUTRO, "telefones": ["61 3333-4444"], "email": "x@y.com"})
        self.assertEqual((r["telefones"], r["email"]), ([], None))
        self.assertEqual(len(r["telefones_impressao"]), 1)
        self.assertNotIn("3333", json.dumps(r))


class BuscadorFalso:
    slugs: dict = {}

    def __init__(self, resultados):
        self.resultados = resultados

    def buscar(self, termo, tipo, titulo=None):
        return self.resultados.get(termo, []), ["HTTP 200"]

    def fechar(self):
        pass


class TestOrquestrador(unittest.TestCase):
    CADASTRO = {
        MEI: {"cnpj": MEI, "razao_social": "AGROSMART TECNOLOGIA LTDA", "socios": ["MARIA SOUZA LIMA"],
              "situacao_cadastral": "ATIVA", "uf": "DF", "telefones": ["61 99812-3456"],
              "email": "contato@agrosmart.com.br"},
        OUTRO: {"cnpj": OUTRO, "razao_social": "PADARIA DA MARIA LTDA", "socios": ["MARIA SOUZA"],
                "situacao_cadastral": "ATIVA", "uf": "SP", "telefones": ["11 98888-7766"],
                "email": "maria.padaria@gmail.com"},
    }
    LEADS = [{"proponente": "Maria Souza Lima", "titulo_projeto": "AgroSmart: sensores"},
             {"proponente": "Pessoa Que Pediu Para Sair", "titulo_projeto": "Outra: coisa"}]

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp()) / "cache_cnpj.json"
        self._padrao = orquestrador.Cache.__init__.__defaults__
        orquestrador.Cache.__init__.__defaults__ = (True, self.cache)

    def tearDown(self):
        orquestrador.Cache.__init__.__defaults__ = self._padrao

    def rodar(self, nao_contatar=(set(), set())):
        buscador = BuscadorFalso({"AGROSMART": [OUTRO, MEI]})
        consulta = lambda c: (dict(self.CADASTRO[c]), "ok") if c in self.CADASTRO else (None, "nao_encontrado")
        return orquestrador.enriquecer_leads(self.LEADS, mapa_cnpj={}, buscador=buscador, consulta=consulta,
                                             pausa=0, nao_contatar=nao_contatar)

    def test_vincula_o_cnpj_certo_e_confirma_o_contato(self):
        lead = self.rodar()[0]
        self.assertEqual((lead["cnpj"], lead["status_enriquecimento"]), (MEI, "ok"))
        self.assertEqual(lead["telefone"], "(61) 99812-3456")

    def test_cache_nao_guarda_contato_do_homonimo(self):
        self.rodar()
        salvo = json.loads(self.cache.read_text(encoding="utf-8"))["registro"]
        self.assertEqual(salvo[MEI]["telefones"], ["61 99812-3456"])
        self.assertEqual((salvo[OUTRO]["telefones"], salvo[OUTRO]["email"]), ([], None))
        self.assertNotIn("98888", self.cache.read_text(encoding="utf-8"))

    def test_lista_de_nao_contatar_tira_o_lead(self):
        saida = self.rodar(({orquestrador._chave("Pessoa Que Pediu Para Sair")}, set()))
        self.assertEqual([l["proponente"] for l in saida], ["Maria Souza Lima"])
        saida = self.rodar((set(), {MEI}))
        self.assertNotIn(MEI, [l.get("cnpj") for l in saida])


class TestPlanilha(unittest.TestCase):
    def test_celula_que_vira_formula_recebe_apostrofo(self):
        self.assertEqual(celula_segura('=HYPERLINK("http://x")'), '\'=HYPERLINK("http://x")')
        self.assertEqual(celula_segura("+5561"), "'+5561")
        self.assertEqual(celula_segura("Agrosmart"), "Agrosmart")
        self.assertEqual(celula_segura(7), 7)


if __name__ == "__main__":
    unittest.main()
