#!/usr/bin/env python3
"""Uma bateria do laboratorio 1.
Uso: python3 bateria.py CONFIG FALHAS_PRINCIPAL FALHAS_CONTROLE [LADO_INICIAL]
     ex.: python3 bateria.py C3 20 3 0   (CONFIG pode ter sufixo: C1a, C1b, ...)
Saida: $LABHOME/dados/lab-CONFIG.csv (nome que analisa-lab.py consome com --pref lab-)."""
import sys, csv, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lab import Lab, CFGS, CABECALHO, DADOS, BASE

k = sys.argv[1]; n = int(sys.argv[2]); nb = int(sys.argv[3]); start = int(sys.argv[4]) if len(sys.argv) > 4 else 0
c = CFGS[k[:2]]; L = Lab(k.lower(), c); os.makedirs(DADOS, exist_ok=True)
plan = [("A", ["UPA", "UBS"][(i + start) % 2]) for i in range(n)] + [("B", ["UPA", "UBS"][i % 2]) for i in range(nb)]
try:
    L.build()
    with open(f"{DADOS}/lab-{k}.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(CABECALHO)
        for i, (p, sd) in enumerate(plan, 1):
            try:
                r = L.run(p, sd)
            except Exception as e:          # uma execucao com erro nao derruba a bateria
                print("erro na execucao", i, repr(e), flush=True); continue
            if r is None: print("falha steady", i, flush=True); continue
            w.writerow([k[:2], c["nome"], i, p, sd, round(r["outage"], 3), r["perdidos"], round(r["failback"], 3), r["perdidos_fb"]]); f.flush()
            print(k, i, p, sd, r, flush=True)
finally:
    L.stop()
open(f"{BASE}/done-{k}", "w").write("ok")
