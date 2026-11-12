"""Testes da demo VOA. A Evolution é SEMPRE mocada: nenhum teste envia nada."""

import http.client
import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import motor  # noqa: E402
import server  # noqa: E402
server.ARQ_CONFIG_LOCAL = Path(__file__).parent / "_sem_config_local.json"  # testes usam só o config público

FIXTURE = RAIZ / "tests" / "fixtures" / "mensagens.json"
SEGREDO = "SEGREDO-EVOLUTION-NAO-VAZAR-123"


def _carregar():
    config = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
    clientes = json.loads((RAIZ / "clientes.json").read_text(encoding="utf-8"))
    mensagens = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return config, clientes, mensagens


class _Resp:
    def __init__(self, dados):
        self._dados = json.dumps(dados).encode("utf-8")

    def read(self):
        return self._dados

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class EvolutionFalsa:
    """Substitui urllib.request.urlopen e registra cada chamada."""

    def __init__(self):
        self.chamadas = []
        self.estado = "open"
        self.owner = "351910000001@s.whatsapp.net"
        self.falha_envio = None
        self.updates = [{"status": "SERVER_ACK"}, {"status": "DELIVERY_ACK"}]

    def __call__(self, req, timeout=None):
        url = req.full_url
        corpo = json.loads(req.data.decode("utf-8")) if req.data else None
        self.chamadas.append({"url": url, "metodo": req.get_method(), "corpo": corpo, "apikey": req.get_header("Apikey")})
        if not url.startswith("http://127.0.0.1:8080/"):
            raise AssertionError(f"chamada fora da Evolution local: {url}")
        if "/instance/connectionState/" in url:
            return _Resp({"instance": {"instanceName": "rafael-business", "state": self.estado}})
        if "/instance/fetchInstances" in url:
            return _Resp([{"name": "rafael-business", "connectionStatus": self.estado, "ownerJid": self.owner, "token": "token-instancia"}])
        if "/message/sendText/" in url:
            if self.falha_envio:
                raise server.urllib.error.HTTPError(url, self.falha_envio, "erro", {}, io.BytesIO(SEGREDO.encode()))
            return _Resp({"key": {"id": "ABC123DEF456", "remoteJid": corpo["number"] + "@s.whatsapp.net", "fromMe": True}, "status": "PENDING"})
        if "/chat/findMessages/" in url:
            return _Resp({"messages": {"total": 1, "records": [{"key": {"id": corpo["where"]["key"]["id"]}, "MessageUpdate": self.updates}]}})
        raise AssertionError(f"rota inesperada: {url}")

    def envios(self):
        return [c for c in self.chamadas if "/message/sendText/" in c["url"]]


# ---------------------------------------------------------------- motor

class TestMotor(unittest.TestCase):
    def setUp(self):
        self.config, self.clientes, self.mensagens = _carregar()
        self.inicio = date.fromisoformat(self.config["data_inicio_demo"])

    def _eventos_0_a_30(self):
        todos = []
        for n in range(31):
            todos += motor.eventos_do_dia(self.inicio + timedelta(days=n), self.clientes, self.config, self.mensagens)
        return todos

    def test_todas_as_10_chaves_disparam_entre_dia_0_e_30(self):
        chaves = {e["chave"] for e in self._eventos_0_a_30()}
        self.assertEqual(len(motor.TODAS_AS_CHAVES), 10)
        self.assertEqual(chaves, set(motor.TODAS_AS_CHAVES))

    def test_oito_clientes_ficticios_sem_telefone(self):
        self.assertEqual(len(self.clientes), 8)
        for c in self.clientes:
            for campo in c:
                self.assertNotIn(campo.lower(), {"telefone", "phone", "whatsapp", "numero", "number"})
            self.assertNotIn(self.config["envio"]["destino_unico"], json.dumps(c))

    def test_formato_do_evento_e_texto_renderizado(self):
        for e in self._eventos_0_a_30():
            self.assertEqual(set(e), {"cliente_id", "regua", "chave", "texto"})
            self.assertIn(e["regua"], {"teste", "filtro", "cobranca"})
            self.assertTrue(e["chave"].startswith(e["regua"]))
            self.assertNotRegex(e["texto"], r"\{(nome|produto|data|horario[123])\}")

    def test_funcao_pura(self):
        d = self.inicio
        copia = json.loads(json.dumps(self.clientes))
        a = motor.eventos_do_dia(d, self.clientes, self.config, self.mensagens)
        b = motor.eventos_do_dia(d, self.clientes, self.config, self.mensagens)
        self.assertEqual(a, b)
        self.assertEqual(copia, self.clientes)

    def test_regra_do_filtro(self):
        c4 = next(c for c in self.clientes if c["id"] == "c4")
        troca = motor.data_da_troca(c4, self.config)
        self.assertEqual(troca, date(2026, 11, 20))
        esperado = {
            troca - timedelta(days=7): "filtro_aviso",
            troca - timedelta(days=6): "filtro_confirmacao",
            troca + timedelta(days=1): "filtro_upsell_shower",
        }
        for d, chave in esperado.items():
            chaves = [e["chave"] for e in motor.eventos_do_dia(d, [c4], self.config, self.mensagens)]
            self.assertEqual(chaves, [chave])

    def test_cobranca_e_teste_nos_dias_do_config(self):
        c6 = next(c for c in self.clientes if c["id"] == "c6")
        falha = date.fromisoformat(c6["pagamento_falhou_em"])
        for n, chave in zip([0, 3, 7], ["cobranca_d0", "cobranca_d3", "cobranca_d7"]):
            evs = motor.eventos_do_dia(falha + timedelta(days=n), [c6], self.config, self.mensagens)
            self.assertIn(chave, [e["chave"] for e in evs])
        c1 = next(c for c in self.clientes if c["id"] == "c1")
        ini = date.fromisoformat(c1["inicio_teste"])
        for n in [0, 7, 21, 28]:
            evs = motor.eventos_do_dia(ini + timedelta(days=n), [c1], self.config, self.mensagens)
            self.assertEqual([e["chave"] for e in evs], [f"teste_d{n}"])

    def test_somar_meses_fim_de_mes(self):
        self.assertEqual(motor.somar_meses(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(motor.somar_meses(date(2024, 2, 29), 12), date(2025, 2, 28))
        self.assertEqual(motor.somar_meses(date(2025, 11, 20), 12), date(2026, 11, 20))

    def test_mensagem_ausente_e_erro(self):
        msgs = dict(self.mensagens)
        del msgs["teste_d0"]
        with self.assertRaises(KeyError):
            motor.eventos_do_dia(self.inicio, self.clientes, self.config, msgs)

    @unittest.skipUnless((RAIZ / "mensagens.json").exists(), "copy real ainda não existe")
    def test_copy_real_sem_chave_sobrando_e_contrato_dos_placeholders(self):
        reais = json.loads((RAIZ / "mensagens.json").read_text(encoding="utf-8"))
        todos = []
        for n in range(31):
            todos += motor.eventos_do_dia(self.inicio + timedelta(days=n), self.clientes, self.config, reais)
        self.assertEqual({e["chave"] for e in todos}, set(motor.TODAS_AS_CHAVES))
        for e in todos:
            self.assertNotIn("{", e["texto"], e["chave"])
            self.assertNotIn("}", e["texto"], e["chave"])
        # {data} do teste = início + 28 (DD/MM); {nome} = primeiro nome
        c1 = next(c for c in self.clientes if c["id"] == "c1")
        fim = date.fromisoformat(c1["inicio_teste"]) + timedelta(days=28)
        ev = motor.eventos_do_dia(date.fromisoformat(c1["inicio_teste"]), [c1], self.config,
                                  {"teste_d0": "{nome}|{data}"} | {k: "x" for k in motor.TODAS_AS_CHAVES if k != "teste_d0"})
        self.assertEqual(ev[0]["texto"], f"Ana|{fim.strftime('%d/%m')}")
        # filtro_confirmacao usa o mesmo {horario1} proposto no aviso
        c4 = next(c for c in self.clientes if c["id"] == "c4")
        troca = motor.data_da_troca(c4, self.config)
        self.assertEqual(motor.horarios_da_visita(troca)[0], "sexta, 20/11, 10h00")

    def test_placeholder_desconhecido_fica_intacto(self):
        self.assertEqual(motor.renderizar("{nome} {x}", {"nome": "Ana"}), "Ana {x}")


# ---------------------------------------------------------------- HTTP

class TestServidor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            # a fixture faz o papel de mensagens.json (textos de teste, previsíveis)
            mock.patch.object(server, "ARQ_MENSAGENS", FIXTURE),
            mock.patch.object(server, "ler_chave_evolution", return_value=SEGREDO),
        ]
        for p in cls._patches:
            p.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.porta = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in cls._patches:
            p.stop()

    def setUp(self):
        self.evo = EvolutionFalsa()
        self.p = mock.patch.object(server.urllib.request, "urlopen", self.evo)
        self.p.start()
        self.config, self.clientes, self.mensagens = _carregar()
        self.stderr = io.StringIO()
        self.p_err = mock.patch.object(sys, "stderr", self.stderr)
        self.p_err.start()

    def tearDown(self):
        self.p.stop()
        self.p_err.stop()

    def _req(self, metodo, caminho, corpo=None):
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None
        headers = {"Content-Type": "application/json"} if dados is not None else {}
        con.request(metodo, caminho, body=dados, headers=headers)
        r = con.getresponse()
        bruto = r.read().decode("utf-8")
        con.close()
        return r.status, bruto

    def _json(self, metodo, caminho, corpo=None):
        status, bruto = self._req(metodo, caminho, corpo)
        return status, json.loads(bruto)

    # -- estado

    def test_estado_dia_0(self):
        st, d = self._json("GET", "/api/estado?dia=0")
        self.assertEqual(st, 200)
        self.assertEqual(d["data"], "2026-11-12")
        self.assertEqual(d["dia"], 0)
        self.assertEqual(len(d["clientes"]), 8)
        self.assertTrue(d["eventos"])
        self.assertEqual(d["linha_do_tempo"], d["eventos"])
        for e in d["eventos"]:
            self.assertEqual(e["dia"], 0)

    def test_estado_linha_do_tempo_cobre_as_10_chaves_no_dia_30(self):
        st, d = self._json("GET", "/api/estado?dia=30")
        self.assertEqual(st, 200)
        self.assertEqual({e["chave"] for e in d["linha_do_tempo"]}, set(motor.TODAS_AS_CHAVES))
        self.assertTrue(all(0 <= e["dia"] <= 30 for e in d["linha_do_tempo"]))

    def test_estado_dia_invalido(self):
        for q in ("dia=61", "dia=-1", "dia=abc", "dia=1.5"):
            st, _ = self._json("GET", f"/api/estado?{q}")
            self.assertEqual(st, 400, q)

    def test_estado_nao_expoe_numeros(self):
        _, bruto = self._req("GET", "/api/estado?dia=30")
        self.assertNotIn(self.config["envio"]["destino_unico"], bruto)
        self.assertNotIn(self.config["envio"]["owner_esperado"], bruto)
        self.assertEqual(self.evo.chamadas, [])

    # -- enviar

    def _evento(self, dia=0):
        d = date.fromisoformat(self.config["data_inicio_demo"]) + timedelta(days=dia)
        return motor.eventos_do_dia(d, self.clientes, self.config, self.mensagens)[0]

    def test_enviar_ok_vai_so_para_destino_unico_com_texto_do_motor(self):
        ev = self._evento(0)
        st, bruto = self._req("POST", "/api/enviar", {"cliente_id": ev["cliente_id"], "chave": ev["chave"], "dia": 0, "texto": "TEXTO FORJADO PELA PÁGINA"})
        self.assertEqual(st, 200, bruto)
        d = json.loads(bruto)
        self.assertEqual(d, {"key_id": "ABC123DEF456", "status": "PENDING"})
        envios = self.evo.envios()
        self.assertEqual(len(envios), 1)
        self.assertEqual(envios[0]["corpo"], {"number": "351910000002", "text": ev["texto"]})
        self.assertNotIn("FORJADO", envios[0]["corpo"]["text"])
        self.assertTrue(envios[0]["url"].endswith("/message/sendText/rafael-business"))
        self.assertEqual(envios[0]["apikey"], SEGREDO)
        self.assertNotIn(SEGREDO, bruto)
        self.assertNotIn(SEGREDO, self.stderr.getvalue())
        # checagem de instância antes do envio
        urls = [c["url"] for c in self.evo.chamadas]
        self.assertIn("connectionState", urls[0])
        self.assertIn("fetchInstances", urls[1])
        self.assertIn("sendText", urls[2])

    def test_enviar_recusa_destino_no_pedido(self):
        ev = self._evento(0)
        for campo in ("destino", "number", "numero", "telefone"):
            st, d = self._json("POST", "/api/enviar", {"cliente_id": ev["cliente_id"], "chave": ev["chave"], "dia": 0, campo: "351912345678"})
            self.assertEqual(st, 400, campo)
        self.assertEqual(self.evo.chamadas, [])

    def test_enviar_recusa_evento_inexistente(self):
        casos = [
            {"cliente_id": "c99", "chave": "teste_d0", "dia": 0},
            {"cliente_id": "c1", "chave": "teste_d7", "dia": 0},
            {"cliente_id": "c1", "chave": "chave_inventada", "dia": 0},
            {"cliente_id": "c1", "chave": "teste_d0", "dia": 61},
            {"cliente_id": "c1", "chave": "teste_d0", "dia": True},
            {"cliente_id": "c1", "chave": "teste_d0"},
            {"chave": "teste_d0", "dia": 0},
        ]
        for corpo in casos:
            st, _ = self._json("POST", "/api/enviar", corpo)
            self.assertEqual(st, 400, corpo)
        self.assertEqual(self.evo.chamadas, [])

    def test_enviar_corpo_invalido(self):
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        con.request("POST", "/api/enviar", body=b"nao-json", headers={"Content-Type": "application/json"})
        self.assertEqual(con.getresponse().status, 400)
        con.close()
        st, _ = self._json("POST", "/api/enviar", ["lista"])
        self.assertEqual(st, 400)
        self.assertEqual(self.evo.chamadas, [])

    def test_enviar_503_com_instancia_fechada(self):
        self.evo.estado = "close"
        ev = self._evento(0)
        st, d = self._json("POST", "/api/enviar", {"cliente_id": ev["cliente_id"], "chave": ev["chave"], "dia": 0})
        self.assertEqual(st, 503)
        self.assertIn("não está conectada", d["erro"])
        self.assertEqual(self.evo.envios(), [])

    def test_enviar_503_com_numero_pareado_errado(self):
        self.evo.owner = "554396379767@s.whatsapp.net"
        ev = self._evento(0)
        st, d = self._json("POST", "/api/enviar", {"cliente_id": ev["cliente_id"], "chave": ev["chave"], "dia": 0})
        self.assertEqual(st, 503)
        self.assertIn("pareada noutro número", d["erro"])
        self.assertNotRegex(d["erro"], r"[0-9]{3,}")
        self.assertEqual(self.evo.envios(), [])

    def test_enviar_503_sem_owner(self):
        self.evo.owner = None
        ev = self._evento(0)
        st, _ = self._json("POST", "/api/enviar", {"cliente_id": ev["cliente_id"], "chave": ev["chave"], "dia": 0})
        self.assertEqual(st, 503)
        self.assertEqual(self.evo.envios(), [])

    def test_enviar_erro_da_evolution_nao_vaza_chave(self):
        ev = self._evento(0)
        corpo = {"cliente_id": ev["cliente_id"], "chave": ev["chave"], "dia": 0}
        # 4xx: certamente não saiu → 502
        self.evo.falha_envio = 400
        st, bruto = self._req("POST", "/api/enviar", corpo)
        self.assertEqual(st, 502)
        self.assertNotIn(SEGREDO, bruto)
        # 5xx: pode ter saído → 504 indeterminado, nunca "falhou"
        self.evo.falha_envio = 500
        st, bruto = self._req("POST", "/api/enviar", corpo)
        self.assertEqual(st, 504)
        self.assertTrue(json.loads(bruto)["indeterminado"])
        self.assertNotIn(SEGREDO, bruto)
        self.assertNotIn(SEGREDO, self.stderr.getvalue())

    def test_enviar_resposta_interrompida_e_indeterminado(self):
        ev = self._evento(0)
        original = self.evo.__call__

        def corta(req, timeout=None):
            if "/message/sendText/" in req.full_url:
                raise http.client.IncompleteRead(b"{")
            return original(req, timeout)

        with mock.patch.object(server.urllib.request, "urlopen", corta):
            st, d = self._json("POST", "/api/enviar", {"cliente_id": ev["cliente_id"], "chave": ev["chave"], "dia": 0})
        self.assertEqual(st, 504)
        self.assertTrue(d["indeterminado"])

    def test_enviar_status_error_na_resposta_e_502(self):
        ev = self._evento(0)
        original = self.evo.__call__

        def erro(req, timeout=None):
            if "/message/sendText/" in req.full_url:
                return _Resp({"key": {"id": "ERR1"}, "status": "ERROR"})
            return original(req, timeout)

        with mock.patch.object(server.urllib.request, "urlopen", erro):
            st, d = self._json("POST", "/api/enviar", {"cliente_id": ev["cliente_id"], "chave": ev["chave"], "dia": 0})
        self.assertEqual(st, 502)
        self.assertEqual(d["status"], "ERROR")

    def test_enviar_desligado_sem_copy_real(self):
        ev = self._evento(0)
        with mock.patch.object(server, "ARQ_MENSAGENS", RAIZ / "tests" / "fixtures" / "nao-existe.json"):
            st, d = self._json("POST", "/api/enviar", {"cliente_id": ev["cliente_id"], "chave": ev["chave"], "dia": 0})
        self.assertEqual(st, 503)
        self.assertEqual(self.evo.chamadas, [])

    def test_enviar_timeout_e_indeterminado(self):
        ev = self._evento(0)
        original = self.evo.__call__

        def lento(req, timeout=None):
            if "/message/sendText/" in req.full_url:
                self.evo.chamadas.append({"url": req.full_url})
                raise TimeoutError("timed out")
            return original(req, timeout)

        with mock.patch.object(server.urllib.request, "urlopen", lento):
            st, d = self._json("POST", "/api/enviar", {"cliente_id": ev["cliente_id"], "chave": ev["chave"], "dia": 0})
        self.assertEqual(st, 504)
        self.assertTrue(d["indeterminado"])

    def test_enviar_conexao_recusada_e_502(self):
        ev = self._evento(0)
        original = self.evo.__call__

        def recusa(req, timeout=None):
            if "/message/sendText/" in req.full_url:
                raise server.urllib.error.URLError(ConnectionRefusedError(61, "refused"))
            return original(req, timeout)

        with mock.patch.object(server.urllib.request, "urlopen", recusa):
            st, _ = self._json("POST", "/api/enviar", {"cliente_id": ev["cliente_id"], "chave": ev["chave"], "dia": 0})
        self.assertEqual(st, 502)

    def test_post_exige_json_e_host_permitido(self):
        ev = self._evento(0)
        dados = json.dumps({"cliente_id": ev["cliente_id"], "chave": ev["chave"], "dia": 0}).encode()
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        con.request("POST", "/api/enviar", body=dados, headers={"Content-Type": "text/plain"})
        self.assertEqual(con.getresponse().status, 415)
        con.close()
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        con.putrequest("POST", "/api/enviar", skip_host=True)
        con.putheader("Host", f"evil.example:{self.porta}")
        con.putheader("Content-Type", "application/json")
        con.putheader("Content-Length", str(len(dados)))
        con.endheaders(dados)
        self.assertEqual(con.getresponse().status, 403)
        con.close()
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        con.putrequest("GET", "/api/estado?dia=0", skip_host=True)
        con.putheader("Host", f"evil.example:{self.porta}")
        con.endheaders()
        self.assertEqual(con.getresponse().status, 403)
        con.close()
        self.assertEqual(self.evo.chamadas, [])

    def test_enviar_503_com_destino_invalido_no_config(self):
        ev = self._evento(0)
        for ruim in ("abc", "351910000002\n", "+351910000002", "35191234567890123"):
            cfg = dict(self.config, envio=dict(self.config["envio"], destino_unico=ruim))
            with mock.patch.object(server, "carregar_config", return_value=cfg):
                st, _ = self._json("POST", "/api/enviar", {"cliente_id": ev["cliente_id"], "chave": ev["chave"], "dia": 0})
            self.assertEqual(st, 503, repr(ruim))
        self.assertEqual(self.evo.chamadas, [])

    # -- status

    def test_status_desconhecido_sem_consultar(self):
        st, d = self._json("GET", "/api/status?key_id=NAOENVIADO1")
        self.assertEqual((st, d), (200, {"status": "desconhecido"}))
        self.assertEqual(self.evo.chamadas, [])
        st, _ = self._json("GET", "/api/status?key_id=../../x")
        self.assertEqual(st, 400)

    def test_status_apos_envio(self):
        ev = self._evento(0)
        st, d = self._json("POST", "/api/enviar", {"cliente_id": ev["cliente_id"], "chave": ev["chave"], "dia": 0})
        self.assertEqual(st, 200)
        st, s = self._json("GET", f"/api/status?key_id={d['key_id']}")
        self.assertEqual((st, s), (200, {"status": "DELIVERY_ACK"}))
        busca = [c for c in self.evo.chamadas if "findMessages" in c["url"]][-1]
        self.assertEqual(busca["corpo"], {"where": {"key": {"id": "ABC123DEF456"}}})
        self.evo.updates = [{"status": "ERROR"}]
        _, s = self._json("GET", f"/api/status?key_id={d['key_id']}")
        self.assertEqual(s, {"status": "ERROR"})
        self.evo.updates = [{"status": "SERVER_ACK"}, {"status": "ERROR"}]
        _, s = self._json("GET", f"/api/status?key_id={d['key_id']}")
        self.assertEqual(s, {"status": "ERROR"})
        self.evo.updates = []
        _, s = self._json("GET", f"/api/status?key_id={d['key_id']}")
        self.assertEqual(s, {"status": "desconhecido"})

    # -- outros

    def test_rota_inexistente_e_arquivos_fora_da_lista(self):
        for caminho in ("/config.json", "/../config.json", "/server.py", "/api/nada"):
            st, _ = self._req("GET", caminho)
            self.assertEqual(st, 404, caminho)

    def test_log_sem_query(self):
        self._req("GET", "/api/status?key_id=SEGREDOQUERY")
        self.assertNotIn("SEGREDOQUERY", self.stderr.getvalue())


class TestExportacao(unittest.TestCase):
    def test_exportar_estatico(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(server, "ARQ_MENSAGENS", Path(tmp) / "nao-existe.json"), \
                mock.patch.object(server.urllib.request, "urlopen", side_effect=AssertionError("sem rede")):
            saida = Path(tmp) / "demo-estatica.json"
            server.exportar_estatico(saida)
            bruto = saida.read_text(encoding="utf-8")
        d = json.loads(bruto)
        cfg = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(list(d), [str(n) for n in range(61)])
        self.assertEqual([d[str(n)]["dia"] for n in range(61)], list(range(61)))
        self.assertEqual({e["chave"] for e in d["30"]["linha_do_tempo"]}, set(motor.TODAS_AS_CHAVES))
        self.assertEqual(set(d["0"]), {"data", "dia", "clientes", "eventos", "linha_do_tempo"})
        for proibido in (cfg["envio"]["destino_unico"], cfg["envio"]["owner_esperado"], "rafael-business", "envio"):
            self.assertNotIn(proibido, bruto)


class TestChaveEServir(unittest.TestCase):
    def test_ler_chave(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text('OUTRA=1\nexport EVOLUTION_API_KEY="abc123"\n', encoding="utf-8")
            self.assertEqual(server.ler_chave_evolution(env), "abc123")
            env.write_text("OUTRA=1\n", encoding="utf-8")
            with self.assertRaises(server.EvolutionErro) as cm:
                server.ler_chave_evolution(env)
            self.assertNotIn("abc123", str(cm.exception))
            with self.assertRaises(server.EvolutionErro):
                server.ler_chave_evolution(Path(tmp) / "nada.env")

    def test_servir_recusa_host_curinga(self):
        cfg = {"servidor": {"porta": 8792, "hosts": ["0.0.0.0"]}}
        with mock.patch.object(server, "carregar_config", return_value=cfg), \
                mock.patch.object(server, "carregar_mensagens", return_value=({}, "mensagens.json")), \
                mock.patch.object(server, "ThreadingHTTPServer") as srv:
            with self.assertRaises(SystemExit):
                server.servir()
            srv.assert_not_called()

    def test_config_real_escuta_so_nos_hosts_e_porta_do_contrato(self):
        cfg = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["servidor"], {"porta": 8792, "hosts": ["127.0.0.1"]})
        self.assertEqual(cfg["envio"]["destino_unico"], "351910000002")


if __name__ == "__main__":
    unittest.main()
