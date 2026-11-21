#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Analise dos resultados do laboratorio Linux (FRR em namespaces de rede).

Entradas (CSV na pasta indicada):
  lab-C1.csv ... lab-C5.csv  - uma linha por falha: config, nome, execucao, caminho (A ou B),
                               lado (UPA ou UBS), outage_s, perdidos, failback_s, perdidos_fb
  lab-QOS.csv                - prioridade de trafego: cenario, rep, media/p95/max do RTT, perda, vazao
  lab2-C1.csv ... lab2-QOS.csv - mesmos formatos, do laboratorio 2 (tunel GRE sobre ESP, com atraso);
                               failback_s fica vazio nas baterias em que o retorno nao foi medido
  lab2-MICRO.csv, lab2-MICRO-ARPFIXO.csv - interrupcoes curtas (sem e com vizinhos ARP fixos)
Os geradores (bateria.py, qos.py, lab2.py rec/qos/consolida) ja gravam com esses nomes.
Ao fim, imprime o veredito de H6 e H7 pelos criterios da secao 4.2.

Uso: python3 analisa-lab.py [--dados pasta] [--meta 5] [--pref lab- | lab2-]
"""
import argparse, csv, glob, math, os, statistics as st


T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
        20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048,
        29: 2.045, 30: 2.042}


def t975(gl):
    try:
        from scipy import stats as sp
        return sp.t.ppf(0.975, gl)
    except ImportError:
        return T975.get(gl, 1.960)


def ler(pasta, padrao):
    linhas = []
    for f in sorted(glob.glob(os.path.join(pasta, padrao))):
        with open(f, encoding="utf-8") as fh:
            linhas += list(csv.DictReader(fh))
    return linhas


def resumo(v):
    n = len(v)
    m = st.mean(v)
    dp = st.stdev(v) if n > 1 else 0.0
    return dict(n=n, media=m, dp=dp, minimo=min(v), maximo=max(v), ep=dp / math.sqrt(n) if n > 1 else 0.0)


# Faixa prevista pelo modelo: T entre (dead - hello) e dead; para o BFD, entre (m-1) x intervalo e m x intervalo.
# Tolerancia: 1 s nos niveis com marcas de tempo de 1 s (10/40 e 5/20) e 0,25 s nos demais, que cobre o
# calculo do SPF e a instalacao da rota (a sonda tem resolucao de 20 ms).
PREVISTO = {"C1": (30.0, 40.0, 1.0), "C2": (15.0, 20.0, 1.0), "C3": (2.0, 3.0, 0.25),
            "C4": (0.6, 0.9, 0.25), "C5": (0.2, 0.3, 0.25)}


def recuperacao(pasta, pref="lab-"):
    """Tempo de interrupcao por configuracao, nas falhas do caminho principal (ISP-A)."""
    saida = {}
    for k in ("C1", "C2", "C3", "C4", "C5"):
        r = ler(pasta, f"{pref}{k}*.csv")
        ativo = [x for x in r if x["caminho"] == "A"]
        ctrl = [x for x in r if x["caminho"] == "B"]
        if not ativo:
            continue
        v = [float(x["outage_s"]) for x in ativo]
        d = resumo(v)
        d["nome"] = ativo[0]["nome"]
        d["valores"] = v
        d["por_lado"] = {s: resumo([float(x["outage_s"]) for x in ativo if x["lado"] == s]) for s in ("UPA", "UBS")
                         if any(x["lado"] == s for x in ativo)}
        lo, hi, tol = PREVISTO[k]
        d["faixa"] = (lo, hi)
        d["dentro"] = sum(1 for x in v if lo - tol <= x <= hi + tol)
        d["na_faixa"] = sum(1 for x in v if lo <= x <= hi)          # sem tolerancia
        d["abaixo"] = sum(1 for x in v if x < lo)                  # refutaria o modelo: interrupcao menor que a deteccao minima
        d["acima"] = sum(1 for x in v if x > hi)
        d["acima_tol"] = sum(1 for x in v if x > hi + tol)
        d["excesso_max"] = max(0.0, max(v) - hi)
        d["meta"] = sum(1 for x in v if x <= 5.0)
        if len(v) > 1:
            t = t975(len(v) - 1)
            d["ic"] = (d["media"] - t * d["ep"], d["media"] + t * d["ep"])
        else:
            d["ic"] = (d["media"], d["media"])
        d["p_lado"] = None
        if len(d["por_lado"]) == 2:
            try:
                from scipy import stats as sp
                d["p_lado"] = sp.mannwhitneyu([float(x["outage_s"]) for x in ativo if x["lado"] == "UPA"],
                                              [float(x["outage_s"]) for x in ativo if x["lado"] == "UBS"]).pvalue
            except ImportError:
                pass
        fb = [float(x["failback_s"]) for x in ativo if x["failback_s"] not in ("", "None")]
        d["failback_max"] = max(fb) if fb else None
        d["failback_n"] = len(fb)
        d["controle_n"] = len(ctrl)
        d["controle_max"] = max((float(x["outage_s"]) for x in ctrl), default=0.0)
        saida[k] = d
    return saida


def qos(pasta, pref="lab-"):
    r = ler(pasta, f"{pref}QOS.csv")
    cen = {}
    for x in r:
        cen.setdefault(x["cenario"], []).append(x)
    out = {}
    for c, xs in cen.items():
        g = lambda k: [float(x[k]) for x in xs if x[k] not in ("", "None")]
        if not g("media_ms"):
            continue
        out[c] = dict(n=len(xs), media=st.mean(g("media_ms")), p95=st.mean(g("p95_ms")), p95_max=max(g("p95_ms")),
                      maximo=max(g("max_ms")), perda=st.mean(g("perda_pct")),
                      vazao=(st.mean(g("vazao_mbps")) if g("vazao_mbps") else None))
    return out


# Microinterrupcoes: para cada mecanismo, quantas interrupcoes curtas do caminho principal provocaram troca de caminho.
# Previsao do modelo: com pacotes periodicos de intervalo i e multiplicador m, a queda e declarada se a janela d
# for maior que m*i - t, com t uniforme entre 0 e i; probabilidade = (d - (m-1)*i)/i, limitada entre 0 e 1.
PARAM_MICRO = {"C3": (1000.0, 3), "C4": (300.0, 3), "C5": (100.0, 3)}


def micro(pasta, pref="lab2-", arquivo="MICRO.csv"):
    r = ler(pasta, f"{pref}{arquivo}")
    out = {}
    for x in r:
        k = x["config"]; d = int(x["duracao_nominal_ms"])
        e = out.setdefault(k, {"nome": x["nome"], "d": {}, "janela_excesso": []})
        n, c = e["d"].get(d, (0, 0))
        e["d"][d] = (n + 1, c + int(x["troca_de_caminho"]))
        e["janela_excesso"].append(float(x["janela_medida_ms"]) - d)
        if "execucoes_spf" in x:
            e.setdefault("troca_sem_spf", 0)
            e["troca_sem_spf"] += int(x["troca_de_caminho"] == "1" and int(x["execucoes_spf"]) == 0)
    for k, e in out.items():
        i, m = PARAM_MICRO[k]
        e["prev"] = {d: min(max((d - (m - 1) * i) / i, 0.0), 1.0) for d in e["d"]}
        e["excesso_medio"] = st.mean(e["janela_excesso"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dados", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados"))
    ap.add_argument("--meta", type=float, default=5.0)
    ap.add_argument("--pref", default="lab-", help="lab- (laboratorio 1, sem tunel) ou lab2- (laboratorio 2, com tunel)")
    a = ap.parse_args()
    print("Interrupcao por configuracao (falhas do caminho principal), prefixo", a.pref)
    rec = recuperacao(a.dados, a.pref)
    for k, d in rec.items():
        atende = sum(1 for x in d["valores"] if x <= a.meta)
        fb = "nao medido" if d["failback_max"] is None else f"{d['failback_max']:.2f} s"
        print(f"{k} {d['nome']}: n={d['n']} media={d['media']:.2f} s dp={d['dp']:.2f} "
              f"min={d['minimo']:.2f} max={d['maximo']:.2f} IC95={d['ic'][0]:.2f} a {d['ic'][1]:.2f} | "
              f"faixa prevista {d['faixa'][0]:g} a {d['faixa'][1]:g} s: {d['na_faixa']} de {d['n']} sem tolerancia, {d['dentro']} com tolerancia, {d['abaixo']} abaixo do limite inferior | "
              f"dentro da meta de {a.meta:.0f} s: {atende} de {d['n']} | "
              f"retorno ao caminho principal, interrupcao maxima {fb} | "
              f"controle (falha no caminho de reserva): {d['controle_n']} execucoes, max {d['controle_max']:.3f} s"
              + (f" | Mann-Whitney por lado p={d['p_lado']:.2f}" if d.get('p_lado') is not None else ""))
        for s, x in d["por_lado"].items():
            print(f"    lado {s}: n={x['n']} media={x['media']:.2f} dp={x['dp']:.2f}")
    if a.pref == "lab2-":
        for arq, titulo in (("MICRO.csv", ""), ("MICRO-ARPFIXO.csv", ", vizinhos ARP fixos")):
            res = micro(a.dados, a.pref, arq)
            if not res:
                continue
            print(f"\nMicrointerrupcoes no caminho principal{titulo}: trocas de caminho / tentativas (previsto pelo modelo)")
            for k, e in sorted(res.items()):
                print(k, e["nome"], " ".join(f"{d} ms: {c}/{n} ({e['prev'][d]:.2f})" for d, (n, c) in sorted(e["d"].items())),
                      f"| janela real {e['excesso_medio']:.1f} ms acima da nominal")
    print("\nPrioridade de trafego (WAN de 10 Mbit/s)")
    q = qos(a.dados, a.pref)
    for c, x in q.items():
        print(f"{c}: n={x['n']} RTT medio {x['media']:.2f} ms, p95 {x['p95']:.1f} ms, max {x['maximo']:.1f} ms, "
              f"perda {x['perda']:.1f}%, vazao {x['vazao'] if x['vazao'] is None else round(x['vazao'],2)} Mbit/s")

    # Vereditos pelos criterios da secao 4.2
    print("\nHipoteses (criterios da secao 4.2)")
    rapidos = [v for k in ("C3", "C4", "C5") if k in rec for v in rec[k]["valores"]]
    lentos = [v for k in ("C1", "C2") if k in rec for v in rec[k]["valores"]]
    if rapidos or lentos:
        ok_r = sum(1 for v in rapidos if v <= a.meta); ok_l = sum(1 for v in lentos if v <= a.meta)
        h6 = rapidos and lentos and ok_r == len(rapidos) and ok_l == 0
        print(f"H6: {ok_r} de {len(rapidos)} falhas com 1 s / 3 s e BFD em ate {a.meta:g} s; "
              f"{ok_l} de {len(lentos)} com 10 s / 40 s e 5 s / 20 s -> "
              f"{'aceita' if h6 else 'nao aceita' if rapidos and lentos else 'incompleta (faltam configuracoes)'}")
    ef = q.get("carga, FILA-WAN, sonda EF")
    if ef:
        print(f"H7: maior p95 da sonda EF com FILA-WAN sob carga = {ef['p95_max']:.2f} ms em {ef['n']} repeticoes "
              f"(limite 150 ms) -> {'aceita' if ef['p95_max'] < 150 else 'rejeitada'}")


if __name__ == "__main__":
    main()
