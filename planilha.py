PREFIXOS_DE_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def celula_segura(valor):
    if isinstance(valor, str) and valor.startswith(PREFIXOS_DE_FORMULA):
        return "'" + valor
    return valor
