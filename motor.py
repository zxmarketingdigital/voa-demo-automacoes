"""Regras das três réguas da demo VOA (mês grátis, filtro, cobrança).

`eventos_do_dia` é função pura: recebe a data, os clientes, o config e as
mensagens já carregados e devolve os eventos daquele dia. Nada de I/O aqui.
"""

import re
from datetime import date, timedelta

CHAVES_TESTE = ("teste_d0", "teste_d7", "teste_d21", "teste_d28")
CHAVES_FILTRO = ("filtro_aviso", "filtro_confirmacao", "filtro_upsell_shower")
CHAVES_COBRANCA = ("cobranca_d0", "cobranca_d3", "cobranca_d7")
TODAS_AS_CHAVES = CHAVES_TESTE + CHAVES_FILTRO + CHAVES_COBRANCA

DIAS_SEMANA = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")

_PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")


def _data(valor):
    if valor is None or valor == "":
        return None
    if isinstance(valor, date):
        return valor
    return date.fromisoformat(str(valor))


def somar_meses(d, meses):
    """Soma meses mantendo o dia; se o mês não tiver o dia, usa o último."""
    total = d.month - 1 + meses
    ano = d.year + total // 12
    mes = total % 12 + 1
    for dia in (d.day, 30, 29, 28):
        try:
            return date(ano, mes, min(d.day, dia))
        except ValueError:
            continue
    raise ValueError("data inválida")  # inalcançável


def fmt_data(d):
    """DD/MM, como a copy (mensagens.json → _placeholders) espera."""
    return d.strftime("%d/%m")


def _fmt_horario(d, hora):
    return f"{DIAS_SEMANA[d.weekday()]}, {d.strftime('%d/%m')}, {hora}"


def horarios_da_visita(troca):
    """Três opções de horário para a troca do filtro (texto em pt-PT)."""
    return (
        _fmt_horario(troca, "10h00"),
        _fmt_horario(troca, "14h30"),
        _fmt_horario(troca + timedelta(days=1), "11h00"),
    )


def texto_da_mensagem(mensagens, chave):
    valor = mensagens.get(chave)
    if isinstance(valor, dict):
        valor = valor.get("texto")
    if not isinstance(valor, str) or not valor.strip():
        raise KeyError(f"mensagem ausente ou vazia: {chave}")
    return valor


def renderizar(modelo, variaveis):
    """Troca só os placeholders conhecidos; o resto do texto fica intacto."""
    return _PLACEHOLDER.sub(
        lambda m: str(variaveis[m.group(1)]) if m.group(1) in variaveis else m.group(0),
        modelo,
    )


def data_da_troca(cliente, config):
    base = _data(cliente.get("ultima_troca_filtro")) or _data(cliente.get("data_instalacao"))
    if base is None:
        return None
    return somar_meses(base, int(config["filtro"]["meses_troca"]))


def _candidatos(cliente, config):
    """Gera (data, regua, chave, variaveis_extra) de todas as mensagens do cliente."""
    nome = cliente.get("nome", "")
    produto = cliente.get("produto", "")
    # {nome} é o primeiro nome (contrato da copy); o completo fica em {nome_completo}.
    base = {"nome": nome.split(" ")[0] if nome else "", "nome_completo": nome, "produto": produto}

    inicio = _data(cliente.get("inicio_teste"))
    if cliente.get("tipo") == "teste" and inicio is not None:
        dias = [int(x) for x in config["teste"]["dias"]]
        fim = inicio + timedelta(days=max(dias))
        for chave, n in zip(CHAVES_TESTE, dias):
            yield inicio + timedelta(days=n), "teste", chave, {**base, "data": fmt_data(fim)}

    troca = data_da_troca(cliente, config)
    if troca is not None:
        h1, h2, h3 = horarios_da_visita(troca)
        variaveis = {**base, "data": fmt_data(troca), "horario1": h1, "horario2": h2, "horario3": h3}
        aviso = troca - timedelta(days=int(config["filtro"]["aviso_dias_antes"]))
        yield aviso, "filtro", "filtro_aviso", variaveis
        yield aviso + timedelta(days=1), "filtro", "filtro_confirmacao", variaveis
        yield troca + timedelta(days=1), "filtro", "filtro_upsell_shower", variaveis

    falha = _data(cliente.get("pagamento_falhou_em"))
    if falha is not None:
        dias = [int(x) for x in config["cobranca"]["dias_apos_falha"]]
        for chave, n in zip(CHAVES_COBRANCA, dias):
            yield falha + timedelta(days=n), "cobranca", chave, {**base, "data": fmt_data(falha)}


def eventos_do_dia(data, clientes, config, mensagens):
    """Eventos de mensagem que disparam em `data`.

    Retorna lista de {"cliente_id", "regua", "chave", "texto"}, na ordem dos
    clientes e, dentro de cada cliente, teste → filtro → cobrança.
    """
    data = _data(data)
    eventos = []
    for cliente in clientes:
        for quando, regua, chave, variaveis in _candidatos(cliente, config):
            if quando == data:
                eventos.append({
                    "cliente_id": cliente["id"],
                    "regua": regua,
                    "chave": chave,
                    "texto": renderizar(texto_da_mensagem(mensagens, chave), variaveis),
                })
    return eventos
