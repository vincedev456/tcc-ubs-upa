#!/usr/bin/env python3
"""Prioridade de trafego no laboratorio 1 (sem tunel). Uso: python3 qos.py REPETICOES
Saida: $LABHOME/dados/lab-QOS.csv (nome que analisa-lab.py consome com --pref lab-)."""
import subprocess, re, time, statistics as st, csv, sys, json, os
BASE = os.environ.get("LABHOME", "/tmp/lab"); DADOS = f"{BASE}/dados"
CENARIOS = (("sem carga", "fifo", False, "0xb8"), ("carga, fila unica, sonda EF", "fifo", True, "0xb8"),
            ("carga, FILA-WAN, sonda EF", "wan", True, "0xb8"), ("carga, FILA-WAN, sonda sem marca", "wan", True, "0x00"))
def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True)
def ns(n, c): return sh(f"ip netns exec {n} {c}")
def build():
    for n in ("qa", "qb"): sh(f"ip netns add {n}"); ns(n, "ip link set lo up")
    sh("ip link add qv1 type veth peer name qv2; ip link set qv1 netns qa; ip link set qv2 netns qb")
    ns("qa", "ip addr add 10.5.0.1/24 dev qv1"); ns("qb", "ip addr add 10.5.0.2/24 dev qv2")
    ns("qa", "ip link set qv1 up"); ns("qb", "ip link set qv2 up")
def stop():
    for n in ("qa", "qb"): sh(f"ip netns pids {n} | xargs -r kill -9"); sh(f"ip netns del {n}")
def qdisc(mode):
    ns("qa", "tc qdisc del dev qv1 root")
    if mode == "fifo":   # WAN de 10 Mbit/s, fila unica
        ns("qa", "tc qdisc add dev qv1 root handle 1: htb default 20")
        ns("qa", "tc class add dev qv1 parent 1: classid 1:1 htb rate 10mbit ceil 10mbit")
        ns("qa", "tc class add dev qv1 parent 1:1 classid 1:20 htb rate 10mbit ceil 10mbit")
        ns("qa", "tc qdisc add dev qv1 parent 1:20 handle 20: pfifo limit 500")
    else:              # FILA-WAN: VOZ (EF) prioridade, CLINICO (AF31) 40%, resto
        ns("qa", "tc qdisc add dev qv1 root handle 1: htb default 30")
        ns("qa", "tc class add dev qv1 parent 1: classid 1:1 htb rate 10mbit ceil 10mbit")
        ns("qa", "tc class add dev qv1 parent 1:1 classid 1:10 htb rate 2mbit ceil 10mbit prio 0")
        ns("qa", "tc class add dev qv1 parent 1:1 classid 1:20 htb rate 4mbit ceil 10mbit prio 1")
        ns("qa", "tc class add dev qv1 parent 1:1 classid 1:30 htb rate 4mbit ceil 10mbit prio 2")
        for c in (10, 20, 30): ns("qa", f"tc qdisc add dev qv1 parent 1:{c} handle {c}: pfifo limit 500")
        # DSCP EF = 46 -> TOS 0xb8 ; AF31 = 26 -> TOS 0x68
        ns("qa", "tc filter add dev qv1 parent 1: protocol ip prio 1 u32 match ip tos 0xb8 0xfc flowid 1:10")
        ns("qa", "tc filter add dev qv1 parent 1: protocol ip prio 2 u32 match ip tos 0x68 0xfc flowid 1:20")
def medida(carga, dscp_sonda, dur=15):
    js = f"{BASE}/iperf.json"
    srv = subprocess.Popen(f"exec ip netns exec qb iperf3 -s -1 -p 5201 >/dev/null 2>&1", shell=True); time.sleep(0.5)
    cli = None
    if carga:
        cli = subprocess.Popen(f"exec ip netns exec qa iperf3 -c 10.5.0.2 -t {dur+4} -P 4 -J > {js} 2>&1", shell=True); time.sleep(3)
    r = ns("qa", f"ping -n -i 0.05 -c {int(dur/0.05)} -Q {dscp_sonda} 10.5.0.2")
    rtts = sorted(float(x) for x in re.findall(r"time=([\d.]+) ms", r.stdout))
    m = re.search(r"(\d+) packets transmitted, (\d+) received", r.stdout)
    tx, rx = (int(m[1]), int(m[2])) if m else (int(dur / 0.05), 0)
    bw = None
    if cli:
        cli.wait(timeout=60)
        try: bw = json.load(open(js))["end"]["sum_received"]["bits_per_second"] / 1e6
        except (OSError, ValueError, KeyError): pass
    srv.kill(); srv.wait()
    return dict(n=len(rtts), media=st.mean(rtts) if rtts else None, p95=rtts[int(.95*len(rtts))-1] if rtts else None,
                max=max(rtts) if rtts else None, perda=100*(tx-rx)/tx, mbps=bw)
if __name__ == "__main__":
    rep = int(sys.argv[1]); os.makedirs(DADOS, exist_ok=True); build()
    try:
        with open(f"{DADOS}/lab-QOS.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["cenario", "rep", "sonda_dscp", "media_ms", "p95_ms", "max_ms", "perda_pct", "vazao_mbps"])
            for rp in range(1, rep + 1):
                for cen, mode, carga, dscp in CENARIOS:
                    qdisc(mode); r = medida(carga, dscp, dur=12)
                    w.writerow([cen, rp, dscp, *(None if r[k] is None else round(r[k], 2) for k in ("media", "p95", "max", "perda", "mbps"))]); f.flush()
                    print(cen, rp, r, flush=True)
    finally:
        stop()
