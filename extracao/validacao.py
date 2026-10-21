"""
Funções puras de validação de CNPJ (sem dependências externas).

Suporta o CNPJ alfanumérico (letras nas 12 primeiras posições), que a
Receita Federal passou a emitir em 2026. Os 2 dígitos verificadores
continuam sendo numéricos. O cálculo é o mesmo módulo 11, usando
(código ASCII do caractere - 48) como valor de cada posição.
"""

import re

_PESOS_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
_PESOS_2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]


def limpar_cnpj(texto: str | None) -> str:
    """Remove pontuação e padroniza em maiúsculas."""
    return re.sub(r"[^0-9A-Za-z]", "", texto or "").upper()


def _digito(base: str, pesos: list[int]) -> str:
    soma = sum((ord(ch) - 48) * p for ch, p in zip(base, pesos))
    resto = soma % 11
    return "0" if resto < 2 else str(11 - resto)


def cnpj_valido(cnpj: str | None) -> bool:
    """Confere tamanho e os dois dígitos verificadores."""
    c = limpar_cnpj(cnpj)
    if len(c) != 14 or not c[12:].isdigit() or c == c[0] * 14:
        return False
    d1 = _digito(c[:12], _PESOS_1)
    d2 = _digito(c[:12] + d1, _PESOS_2)
    return c[12:] == d1 + d2
