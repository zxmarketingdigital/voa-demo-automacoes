"""Servidor da demo VOA (só stdlib).

Uso:
    python3 server.py                       # sobe o painel em :8792 nos hosts do config
    python3 server.py --exportar-estatico   # grava demo-estatica.json (dias 0..60, sem envio)

O envio real de WhatsApp vai SEMPRE e SÓ para config.envio.destino_unico.
"""

import argparse
import http.client
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import motor

RAIZ = Path(__file__).resolve().parent
ARQ_CONFIG = RAIZ / "config.json"
ARQ_CONFIG_LOCAL = RAIZ / "config.local.json"  # números e hosts reais, fora do git
ARQ_CLIENTES = RAIZ / "clientes.json"
ARQ_MENSAGENS = RAIZ / "mensagens.json"
ARQ_MENSAGENS_FIXTURE = RAIZ / "tests" / "fixtures" / "mensagens.json"
ARQ_PAINEL = RAIZ / "painel.html"
ARQ_ESTATICO = RAIZ / "demo-estatica.json"
ARQ_ENV = Path.home() / ".zxlab-mission-control" / ".env"

EVOLUTION_URL = "http://127.0.0.1:8080"
DIA_MAX = 60
DIAS_ESTATICO = 60  # o painel navega até o dia 60, como /api/estado
MAX_CORPO = 4096
HOSTS_PROIBIDOS = {"", "0.0.0.0", "::", "*"}
CAMPOS_DE_DESTINO = {"destino", "destino_unico", "number", "numero", "número", "telefone", "phone", "jid", "remoteJid"}
RE_KEY_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
RE_NUMERO = re.compile(r"^[0-9]{10,15}$")
STATUS_ORDEM = ("PENDING", "SERVER_ACK", "DELIVERY_ACK", "READ")


def _timeout_env(nome, padrao):
    try:
        valor = float(os.environ.get(nome, ""))
    except ValueError:
        return padrao
    return valor if valor > 0 else padrao


EVOLUTION_TIMEOUT = _timeout_env("ZX_VOA_EVOLUTION_TIMEOUT", 20.0)


# ---------------------------------------------------------------- dados

def _ler_json(caminho):
    with open(caminho, encoding="utf-8") as f:
        return json.load(f)


def carregar_config():
    """config.json (público) com envio/servidor sobrescritos pelo config.local.json, se existir."""
    cfg = _ler_json(ARQ_CONFIG)
    if ARQ_CONFIG_LOCAL.exists():
        local = _ler_json(ARQ_CONFIG_LOCAL)
        for bloco in ("envio", "servidor"):
            if isinstance(local.get(bloco), dict):
                cfg[bloco] = {**cfg.get(bloco, {}), **local[bloco]}
    return cfg


def carregar_clientes():
    return _ler_json(ARQ_CLIENTES)


def carregar_mensagens():
    """Lê mensagens.json (ou a fixture de teste, se a copy ainda não existir).

    Devolve (mensagens, fonte). Falta de chave é erro: melhor não subir do que
    mostrar uma régua com buraco na reunião.
    """
    if ARQ_MENSAGENS.exists():
        mensagens, fonte = _ler_json(ARQ_MENSAGENS), "mensagens.json"
    else:
        mensagens, fonte = _ler_json(ARQ_MENSAGENS_FIXTURE), "fixture de teste"
    for chave in motor.TODAS_AS_CHAVES:
        motor.texto_da_mensagem(mensagens, chave)
    return mensagens, fonte


def data_do_dia(config, dia):
    return date.fromisoformat(config["data_inicio_demo"]) + timedelta(days=dia)


def estado(dia, config, clientes, mensagens):
    """Estado da demo no dia N (N dias depois de data_inicio_demo)."""
    linha = []
    eventos = []
    for n in range(dia + 1):
        d = data_do_dia(config, n)
        do_dia = [{**e, "dia": n, "data": d.isoformat()} for e in motor.eventos_do_dia(d, clientes, config, mensagens)]
        linha.extend(do_dia)
        if n == dia:
            eventos = do_dia
    return {
        "data": data_do_dia(config, dia).isoformat(),
        "dia": dia,
        "clientes": clientes,
        "eventos": eventos,
        "linha_do_tempo": linha,
    }


def exportar_estatico(saida=ARQ_ESTATICO, dias=DIAS_ESTATICO):
    """Grava {"0": estado_dia_0, ..., "N": estado_dia_N} (formato pedido pelo painel).

    Sem nada de envio: o estado só tem clientes e eventos, nunca o bloco envio.
    """
    config, clientes = carregar_config(), carregar_clientes()
    mensagens, fonte = carregar_mensagens()
    dados = {str(n): estado(n, config, clientes, mensagens) for n in range(dias + 1)}
    tmp = Path(str(saida) + ".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(saida)
    return fonte


# ---------------------------------------------------------------- Evolution

class EvolutionErro(Exception):
    """Falha ao falar com a Evolution. A mensagem nunca carrega a chave.

    `codigo` = status HTTP quando houve resposta; `recusada` = conexão recusada
    (a Evolution nem recebeu o pedido).
    """

    def __init__(self, mensagem, codigo=None, recusada=False):
        super().__init__(mensagem)
        self.codigo = codigo
        self.recusada = recusada


class EnvioIndeterminado(Exception):
    """O envio pode ter saído (timeout, 5xx, resposta ilegível): não reenviar às cegas."""


def ler_chave_evolution(caminho=None):
    caminho = Path(caminho) if caminho else ARQ_ENV
    try:
        linhas = caminho.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise EvolutionErro("arquivo .env da Evolution não encontrado")
    for linha in linhas:
        linha = linha.strip()
        if linha.startswith("export "):
            linha = linha[len("export "):].strip()
        if linha.startswith("EVOLUTION_API_KEY="):
            valor = linha.split("=", 1)[1].strip().strip('"').strip("'")
            if valor:
                return valor
    raise EvolutionErro("EVOLUTION_API_KEY ausente no .env")


def evolution(metodo, caminho, corpo=None):
    chave = ler_chave_evolution()
    dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None
    req = urllib.request.Request(
        EVOLUTION_URL + caminho,
        data=dados,
        method=metodo,
        headers={"apikey": chave, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=EVOLUTION_TIMEOUT) as resp:
            bruto = resp.read()
    except urllib.error.HTTPError as e:
        raise EvolutionErro(f"Evolution respondeu HTTP {e.code}", codigo=e.code) from None
    except (urllib.error.URLError, OSError) as e:
        motivo = getattr(e, "reason", e)
        recusada = isinstance(motivo, ConnectionRefusedError) or isinstance(e, ConnectionRefusedError)
        raise EvolutionErro(f"Evolution inacessível ({type(motivo).__name__})", recusada=recusada) from None
    except http.client.HTTPException as e:
        # BadStatusLine, IncompleteRead…: o pedido pode ter sido processado.
        raise EvolutionErro(f"resposta da Evolution interrompida ({type(e).__name__})") from None
    try:
        return json.loads(bruto.decode("utf-8")) if bruto else {}
    except ValueError:
        raise EvolutionErro("Evolution devolveu resposta que não é JSON") from None


def _usuario_do_jid(jid):
    return str(jid or "").split("@", 1)[0].split(":", 1)[0]


def checar_instancia(envio):
    """(True, "") se a instância está aberta e pareada no número esperado."""
    instancia = urllib.parse.quote(envio["instancia"], safe="")
    resp = evolution("GET", f"/instance/connectionState/{instancia}")
    estado_conexao = ((resp or {}).get("instance") or {}).get("state")
    if estado_conexao != "open":
        return False, f"instância {envio['instancia']} não está conectada (estado: {estado_conexao or 'desconhecido'})"
    lista = evolution("GET", f"/instance/fetchInstances?instanceName={instancia}")
    if isinstance(lista, dict):
        lista = [lista]
    owners = [_usuario_do_jid(i.get("ownerJid")) for i in (lista or []) if isinstance(i, dict) and i.get("name", envio["instancia"]) == envio["instancia"]]
    if not owners or owners[0] == "":
        return False, f"instância {envio['instancia']} sem número pareado"
    if owners[0] != str(envio["owner_esperado"]):
        return False, f"instância {envio['instancia']} pareada noutro número, diferente do esperado"
    return True, ""


def normalizar_status(valor):
    valor = str(valor or "").upper()
    if valor == "PLAYED":
        valor = "READ"
    if valor in STATUS_ORDEM or valor == "ERROR":
        return valor
    return "desconhecido"


def enviar_texto(envio, texto):
    """Um único envio, sempre para envio.destino_unico. Devolve (key_id, status)."""
    destino = str(envio["destino_unico"])
    if not RE_NUMERO.fullmatch(destino):
        raise ValueError("destino_unico inválido no config")
    instancia = urllib.parse.quote(envio["instancia"], safe="")
    try:
        resp = evolution("POST", f"/message/sendText/{instancia}", {"number": destino, "text": texto})
    except EvolutionErro as e:
        # 4xx ou conexão recusada: a mensagem certamente não saiu.
        if e.recusada or (e.codigo is not None and 400 <= e.codigo < 500):
            raise
        raise EnvioIndeterminado(str(e)) from None
    key_id = ((resp or {}).get("key") or {}).get("id") if isinstance(resp, dict) else None
    if not key_id:
        raise EnvioIndeterminado("Evolution não devolveu o id da mensagem")
    return str(key_id), normalizar_status(resp.get("status"))


def consultar_status(envio, key_id):
    instancia = urllib.parse.quote(envio["instancia"], safe="")
    resp = evolution("POST", f"/chat/findMessages/{instancia}", {"where": {"key": {"id": key_id}}})
    if isinstance(resp, dict):
        msgs = resp.get("messages", resp)
        registros = msgs.get("records", []) if isinstance(msgs, dict) else msgs
    else:
        registros = resp
    vistos = []
    for reg in registros or []:
        if not isinstance(reg, dict):
            continue
        if ((reg.get("key") or {}).get("id")) not in (None, key_id):
            continue
        for upd in reg.get("MessageUpdate") or []:
            if isinstance(upd, dict):
                vistos.append(normalizar_status(upd.get("status")))
        if reg.get("status"):
            vistos.append(normalizar_status(reg.get("status")))
    if "ERROR" in vistos:
        return "ERROR"
    validos = [s for s in vistos if s in STATUS_ORDEM]
    if validos:
        return max(validos, key=STATUS_ORDEM.index)
    return "desconhecido"


# ---------------------------------------------------------------- HTTP

class Pedido400(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "VOA-demo"
    sys_version = ""
    enviados = set()
    enviados_lock = threading.Lock()

    # Log sem query string nem corpo: nada de key_id, texto ou chave no log.
    def log_message(self, formato, *args):
        sys.stderr.write(f"[voa] {self.command} {urllib.parse.urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}\n")

    def _responder(self, codigo, corpo, tipo="application/json; charset=utf-8"):
        dados = corpo if isinstance(corpo, bytes) else json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(dados)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(dados)

    def _arquivo(self, caminho, tipo):
        if not caminho.exists():
            return self._responder(404, {"erro": f"{caminho.name} ainda não existe"})
        return self._responder(200, caminho.read_bytes(), tipo)

    def _host_permitido(self):
        """Só aceita Host dos endereços do config (mitiga DNS rebinding e CSRF)."""
        porta = self.server.server_address[1]
        try:
            hosts = list(carregar_config()["servidor"]["hosts"])
        except Exception:  # noqa: BLE001
            hosts = []
        permitidos = {f"{h}:{porta}" for h in hosts + ["localhost"]}
        return (self.headers.get("Host") or "").strip().lower() in permitidos

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if not self._host_permitido():
            return self._responder(403, {"erro": "host não permitido"})
        try:
            if url.path in ("/", "/index.html", "/painel.html"):
                return self._arquivo(ARQ_PAINEL, "text/html; charset=utf-8")
            if url.path == "/demo-estatica.json":
                return self._arquivo(ARQ_ESTATICO, "application/json; charset=utf-8")
            if url.path == "/api/estado":
                return self._estado(urllib.parse.parse_qs(url.query))
            if url.path == "/api/status":
                return self._status(urllib.parse.parse_qs(url.query))
            return self._responder(404, {"erro": "rota inexistente"})
        except Pedido400 as e:
            return self._responder(400, {"erro": str(e)})
        except EvolutionErro as e:
            return self._responder(502, {"erro": str(e)})
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[voa] erro interno em GET {url.path}: {type(e).__name__}\n")
            return self._responder(500, {"erro": "erro interno"})

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        if not self._host_permitido():
            return self._responder(403, {"erro": "host não permitido"})
        tipo = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if tipo != "application/json":
            return self._responder(415, {"erro": "Content-Type deve ser application/json"})
        try:
            if url.path == "/api/enviar":
                return self._enviar()
            return self._responder(404, {"erro": "rota inexistente"})
        except Pedido400 as e:
            return self._responder(400, {"erro": str(e)})
        except EnvioIndeterminado:
            return self._responder(504, {
                "erro": "não deu para confirmar se a mensagem saiu: confira o WhatsApp antes de reenviar",
                "indeterminado": True,
            })
        except EvolutionErro as e:
            return self._responder(502, {"erro": str(e)})
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[voa] erro interno em POST {url.path}: {type(e).__name__}\n")
            return self._responder(500, {"erro": "erro interno"})

    # -- rotas

    @staticmethod
    def _dia(valor):
        if isinstance(valor, bool):
            raise Pedido400("dia inválido")
        if isinstance(valor, str) and re.fullmatch(r"[0-9]{1,3}", valor.strip()):
            valor = int(valor.strip())
        if not isinstance(valor, int) or not 0 <= valor <= DIA_MAX:
            raise Pedido400(f"dia deve ser um inteiro entre 0 e {DIA_MAX}")
        return valor

    def _estado(self, qs):
        dia = self._dia((qs.get("dia") or ["0"])[0])
        mensagens, _ = carregar_mensagens()
        return self._responder(200, estado(dia, carregar_config(), carregar_clientes(), mensagens))

    def _ler_corpo(self):
        try:
            tamanho = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise Pedido400("Content-Length inválido")
        if tamanho <= 0 or tamanho > MAX_CORPO:
            raise Pedido400("corpo ausente ou grande demais")
        try:
            corpo = json.loads(self.rfile.read(tamanho).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise Pedido400("corpo não é JSON válido")
        if not isinstance(corpo, dict):
            raise Pedido400("corpo deve ser um objeto JSON")
        return corpo

    def _enviar(self):
        corpo = self._ler_corpo()
        proibidos = sorted(CAMPOS_DE_DESTINO & set(corpo))
        if proibidos:
            raise Pedido400("o destino não é aceito no pedido: o envio vai só para o número de demonstração")
        cliente_id, chave = corpo.get("cliente_id"), corpo.get("chave")
        if not isinstance(cliente_id, str) or not isinstance(chave, str):
            raise Pedido400("cliente_id e chave são obrigatórios")
        dia = self._dia(corpo.get("dia"))

        config, clientes = carregar_config(), carregar_clientes()
        mensagens, fonte = carregar_mensagens()
        if fonte != "mensagens.json":
            return self._responder(503, {"erro": "mensagens.json (copy aprovada) não existe: envio desligado"})
        # O texto vem SEMPRE do motor; qualquer "texto" no corpo é ignorado.
        eventos = motor.eventos_do_dia(data_do_dia(config, dia), clientes, config, mensagens)
        evento = next((e for e in eventos if e["cliente_id"] == cliente_id and e["chave"] == chave), None)
        if evento is None:
            raise Pedido400("evento inexistente nesse dia")

        envio = config.get("envio") or {}
        if not all(envio.get(k) for k in ("instancia", "owner_esperado", "destino_unico")) or not RE_NUMERO.fullmatch(str(envio["destino_unico"])):
            return self._responder(503, {"erro": "bloco envio do config incompleto ou destino inválido"})
        ok, motivo = checar_instancia(envio)
        if not ok:
            return self._responder(503, {"erro": motivo})
        key_id, status = enviar_texto(envio, evento["texto"])
        with self.enviados_lock:
            self.enviados.add(key_id)
        if status == "ERROR":
            return self._responder(502, {"erro": "a Evolution recusou a mensagem", "key_id": key_id, "status": status})
        return self._responder(200, {"key_id": key_id, "status": status})

    def _status(self, qs):
        key_id = (qs.get("key_id") or [""])[0]
        if not RE_KEY_ID.match(key_id):
            raise Pedido400("key_id inválido")
        with self.enviados_lock:
            conhecido = key_id in self.enviados
        if not conhecido:
            return self._responder(200, {"status": "desconhecido"})
        envio = carregar_config()["envio"]
        return self._responder(200, {"status": consultar_status(envio, key_id)})


# ---------------------------------------------------------------- main

def servir():
    config = carregar_config()
    _, fonte = carregar_mensagens()
    if fonte != "mensagens.json":
        sys.stderr.write("[voa] AVISO: mensagens.json não existe; usando a fixture de teste\n")
    porta = int(config["servidor"]["porta"])
    hosts = list(config["servidor"]["hosts"])
    if not hosts or any(str(h).strip() in HOSTS_PROIBIDOS for h in hosts):
        sys.exit("[voa] hosts do config precisam ser endereços explícitos (nada de 0.0.0.0)")
    servidores = []
    for host in hosts:
        try:
            servidores.append(ThreadingHTTPServer((host, porta), Handler))
            sys.stderr.write(f"[voa] painel em http://{host}:{porta}/\n")
        except OSError as e:
            sys.stderr.write(f"[voa] AVISO: não consegui escutar em {host}:{porta} ({e.strerror})\n")
    if not servidores:
        sys.exit("[voa] nenhum host disponível")
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servidores]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        pass
    finally:
        for s in servidores:
            s.shutdown()
            s.server_close()


def main(argv=None):
    p = argparse.ArgumentParser(description="Servidor da demo VOA")
    p.add_argument("--exportar-estatico", action="store_true", help="grava demo-estatica.json (dias 0..60) e sai")
    args = p.parse_args(argv)
    if args.exportar_estatico:
        fonte = exportar_estatico()
        print(f"demo-estatica.json gravado (dias 0..{DIAS_ESTATICO}, mensagens: {fonte})")
        return
    servir()


if __name__ == "__main__":
    main()
