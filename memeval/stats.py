"""Статистика для парного сравнения «до / после» на одном и том же наборе вопросов.

Парный тест (тест Макнемара) нужен потому, что оба варианта отвечают на ОДНИ И ТЕ ЖЕ вопросы:
важны только вопросы, где варианты разошлись. b — починилось, c — сломалось.
p-value считается точно (биномиальное распределение), приближение хи-квадрат не используется.
"""
import math


def wilson(successes, n, z=1.96):
    """Доверительный интервал 95% для доли (метод Вилсона) — честнее нормального при малых n."""
    if not n:
        return (0.0, 0.0)
    p = successes / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar(pairs):
    """pairs — список (correct_a, correct_b) по одним и тем же вопросам.

    Возвращает b (было неверно → стало верно), c (было верно → стало неверно),
    точное двустороннее p-value и разницу в точности.
    """
    both = sum(1 for a, b in pairs if a and b)
    neither = sum(1 for a, b in pairs if not a and not b)
    b = sum(1 for a, bb in pairs if not a and bb)
    c = sum(1 for a, bb in pairs if a and not bb)
    n = b + c
    if n == 0:
        p = 1.0
    else:
        k = min(b, c)
        tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
        p = min(1.0, 2 * tail)
    acc_a = sum(1 for a, _ in pairs if a) / len(pairs) if pairs else 0
    acc_b = sum(1 for _, bb in pairs if bb) / len(pairs) if pairs else 0
    return {"n": len(pairs), "both": both, "neither": neither, "fixed": b, "broken": c,
            "p_value": p, "acc_a": acc_a, "acc_b": acc_b, "delta": acc_b - acc_a,
            "significant": p < 0.05}


def required_discordant(delta_pairs=0.0):
    """Сколько расхождений нужно, чтобы односторонний перевес стал значимым (p < 0.05, все в одну сторону).

    Полезно для планирования размера набора: при b=5, c=0 точное p = 2/2^5 = 0.0625 — ещё не значимо,
    при b=6, c=0 p = 0.031 — значимо.
    """
    n = 1
    while 2 / (2 ** n) >= 0.05:
        n += 1
    return n
