#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Analise dos testes de recuperacao (T4) do laboratorio UPA-UBS no Packet Tracer.

Entradas (CSV, na mesma pasta ou indicada por --dados):
  execucoes.csv  - pacotes enviados e perdidos em cada execucao (estatisticas do ping)
  pares-full.csv - instantes em que os dois roteadores registraram LOADING -> FULL
                   no mesmo tunel; servem para estimar a diferenca entre os relogios
  deteccao.csv   - instante da queda no roteador proximo (Interface down) e da
                   expiracao do dead timer no roteador remoto (Dead timer expired)

Saidas: diferenca entre relogios, tempo de deteccao no roteador remoto por
execucao, resumo por condicao (media, desvio-padrao, minimo, maximo) e o teste
da hipotese H3 exatamente como na secao 4.2 do TCC (criterio formulado durante o trabalho):
  (a) todos os tempos em [D-H, D], com tolerancia de 1 s pela resolucao das marcas de tempo, e
  (b) intervalo de confianca de 95% da media (t de Student) contendo a media prevista D-H/2.
H3 so pode ser "nao rejeitada": compatibilidade, nao prova. Verificacoes complementares do
Quadro 28 (fora do criterio de H3): teste t, Kolmogorov-Smirnov, bootstrap do desvio-padrao,
Mann-Whitney e equivalencia (TOST, margens de 3 s e 2 s).

Uso: python3 analisa-testes.py [--dados pasta] [--hello 10] [--dead 40]
"""
import argparse
import csv
import math
import os
import statistics as st


def seg(hms):
    h, m, s = (int(x) for x in hms.split(":"))
    return 3600 * h + 60 * m + s


def ler(pasta, nome):
    with open(os.path.join(pasta, nome), encoding="utf-8") as f:
        return list(csv.DictReader(f))


# Quantis t(0,975; gl) para o caso de o scipy nao estar instalado (gl > 30: aproximacao normal)
T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
        20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048,
        29: 2.045, 30: 2.042}
TOLERANCIA_H3 = 1.0   # s, resolucao das marcas de tempo (secao 4.2)


def t975(gl):
    try:
        from scipy import stats as sp
        return sp.t.ppf(0.975, gl)
    except ImportError:
        return T975.get(gl, 1.960)


def resumo(valores):
    n = len(valores)
    media = st.mean(valores)
    dp = st.stdev(valores) if n > 1 else 0.0
    return n, media, dp, min(valores), max(valores)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dados", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados"))
    ap.add_argument("--hello", type=float, default=10.0)
    ap.add_argument("--dead", type=float, default=40.0)
    a = ap.parse_args()

    # 1. Diferenca entre os relogios (UPA-R menos UBS-R)
    difs = [seg(p["UPA-R"]) - seg(p["UBS-R"]) for p in ler(a.dados, "pares-full.csv")]
    offset = st.mean(difs)
    print(f"Diferenca entre relogios UPA-R - UBS-R: {offset:.1f} s "
          f"(n = {len(difs)}, min {min(difs)} s, max {max(difs)} s)")

    # 2. Tempo de deteccao no roteador remoto, no relogio do UPA-R
    def no_relogio_upa(roteador, hms):
        return seg(hms) + (offset if roteador == "UBS-R" else 0)

    tempos, por_lado, por_op = [], {}, {}
    print("\nExecucao  Operadora  Lado  Deteccao no remoto (s)")
    for r in ler(a.dados, "deteccao.csv"):
        t = no_relogio_upa(r["roteador_remoto"], r["evento_remoto"]) - \
            no_relogio_upa(r["roteador_proximo"], r["evento_proximo"])
        tempos.append(t)
        por_lado.setdefault(r["lado"], []).append(t)
        por_op.setdefault(r["operadora"], []).append(t)
        print(f"{r['execucao']:>8}  {r['operadora']:>9}  {r['lado']:>4}  {t:6.0f}")

    n, m, dp, mn, mx = resumo(tempos)
    ep = dp / math.sqrt(n)
    prev_media = a.dead - a.hello / 2
    print(f"\nTodas: n={n} media={m:.1f} s dp={dp:.2f} s min={mn:.0f} max={mx:.0f} ep={ep:.2f} s")
    for k, v in sorted(por_lado.items()):
        print("Lado %s: n=%d media=%.1f dp=%.2f min=%.0f max=%.0f" % ((k,) + resumo(v)))
    for k, v in sorted(por_op.items()):
        print("Operadora %s: n=%d media=%.1f dp=%.2f min=%.0f max=%.0f" % ((k,) + resumo(v)))
    lo_faixa, hi_faixa = a.dead - a.hello, a.dead
    dentro = sum(1 for t in tempos if lo_faixa - TOLERANCIA_H3 <= t <= hi_faixa + TOLERANCIA_H3)
    print(f"\nModelo: faixa [{lo_faixa:.0f}; {hi_faixa:.0f}] s, media {prev_media:.1f} s, "
          f"dp uniforme {a.hello / math.sqrt(12):.2f} s")
    tcrit = t975(n - 1)
    lo_ic, hi_ic = m - tcrit * ep, m + tcrit * ep
    crit_a = dentro == n
    crit_b = lo_ic <= prev_media <= hi_ic
    print(f"H3 (secao 4.2), criterio (a): execucoes na faixa com tolerancia de {TOLERANCIA_H3:.0f} s: "
          f"{dentro} de {n} -> {'atendido' if crit_a else 'NAO atendido'}")
    print(f"H3 (secao 4.2), criterio (b): IC 95% da media {lo_ic:.1f} a {hi_ic:.1f} s; contem {prev_media:.1f} s: "
          f"{'sim' if crit_b else 'nao'} -> {'atendido' if crit_b else 'NAO atendido'}")
    print(f"|media medida - media prevista| = {abs(m - prev_media):.2f} s (informativo)")
    print(f"H3 {'nao rejeitada (dados compativeis com o modelo)' if crit_a and crit_b else 'rejeitada'}")

    # 3. Verificacoes estatisticas (requer scipy)
    try:
        from scipy import stats as sp
    except ImportError:
        sp = None
        print("\nscipy nao instalado: verificacoes estatisticas omitidas (pip install scipy)")
    if sp:
        print()
        tt = sp.ttest_1samp(tempos, prev_media)
        print(f"Teste t contra {prev_media:.1f} s: t = {tt.statistic:.2f}; p = {tt.pvalue:.2f}")
        # Kolmogorov-Smirnov contra a uniforme. Como as marcas de tempo tem resolucao de 1 s e ha empates, o
        # valor-p e obtido por simulacao do proprio modelo: falha em fase uniforme e marcas truncadas ao segundo.
        import random
        rnd = random.Random(1)
        lo = a.dead - a.hello

        def d_ks(x):
            x = sorted(x); k = len(x)
            F = lambda v: min(max((v - lo) / a.hello, 0.0), 1.0)
            return max(max((i + 1) / k - F(v), F(v) - i / k) for i, v in enumerate(x))

        def amostra():
            out = []
            for _ in range(n):
                t0 = rnd.uniform(0, 1000)
                out.append(int(t0 + rnd.uniform(lo, a.dead)) - int(t0))
            return out
        d0 = d_ks(tempos)
        sims = [amostra() for _ in range(10000)]
        p_ks = sum(d_ks(x) >= d0 for x in sims) / len(sims)
        print(f"Kolmogorov-Smirnov contra U({lo:.0f}; {a.dead:.0f}): D = {d0:.2f}; p por simulacao = {p_ks:.2f}")
        # Dispersao: intervalo de 95% do desvio-padrao por reamostragem (bootstrap), comparado ao previsto
        bs = sorted(st.stdev(rnd.choices(tempos, k=n)) for _ in range(10000))
        print(f"Desvio-padrao: {dp:.2f} s; IC 95% por bootstrap {bs[250]:.1f} a {bs[9750]:.1f} s; "
              f"previsto {a.hello / math.sqrt(12):.1f} s")
        for nome, grupos_mw in (("lado", por_lado), ("operadora", por_op)):
            g = list(grupos_mw.values())
            print(f"Mann-Whitney por {nome}: p = {sp.mannwhitneyu(g[0], g[1]).pvalue:.2f}")
        # Equivalencia (TOST; SCHUIRMANN, 1987; LAKENS, 2017): H0 = |media - D-H/2| >= margem
        for margem in (3.0, 2.0):
            p1 = 1 - sp.t.cdf((m - (prev_media - margem)) / ep, n - 1)
            p2 = sp.t.cdf((m - (prev_media + margem)) / ep, n - 1)
            p = max(p1, p2)
            print(f"TOST com margem de {margem:.0f} s: p = {p:.3f} -> "
                  f"{'equivalencia demonstrada' if p < 0.05 else 'equivalencia nao demonstrada'}")

    # 4. Pacotes perdidos por condicao
    print("\nPerdas por condicao (pacotes)")
    grupos = {}
    for r in ler(a.dados, "execucoes.csv"):
        grupos.setdefault((r["operadora"], r["lado"]), []).append(int(r["perdidos"]))
    for (op, lado), v in sorted(grupos.items()):
        n, m, dp, mn, mx = resumo(v)
        print(f"Operadora {op}, lado {lado}: n={n} media={m:.1f} dp={dp:.2f} min={mn} max={mx}")


if __name__ == "__main__":
    main()
