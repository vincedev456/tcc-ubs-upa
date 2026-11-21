#!/usr/bin/env python3
"""Laboratorio 2: GRE sobre ESP (IPsec em modo transporte, chaves fixas) + OSPF/BFD + atraso (netem) + QoS.
Roda dentro de 'unshare -rnm'. Topologia: UPA-R e UBS-R, dois ISPs (pontes L2 com atraso), um tunel GRE
protegido por ESP em cada caminho. OSPF e BFD rodam sobre as interfaces de tunel.

Saidas (pasta $LABHOME/dados2, padrao ~/lab/dados2), ja com os nomes que analisa-lab.py consome (--pref lab2-):
  rec      -> lab2-<CFG><sufixo>.csv        (bateria sequencial completa)
  qos      -> lab2-QOS.csv
  one, qos1, micro gravam parciais em dados2/parciais/ (com cabecalho), e
  consolida -> junta as parciais em lab2-C1..C5.csv, lab2-QOS.csv, lab2-MICRO.csv, lab2-MICRO-ARPFIXO.csv

Uso:
  python3 lab2.py rec C3a 20 3 0          python3 lab2.py one C3 1 A UPA 1
  python3 lab2.py qos 5                   python3 lab2.py qos1 1
  python3 lab2.py micro C5 1 50,100,150   python3 lab2.py verif | veriftun
  python3 lab2.py consolida
"""
import subprocess, sys, time, os, re, csv, glob, random, signal, statistics as st, json
H = os.environ.get("LABHOME", os.path.expanduser("~/lab"))
# FRR: por padrao o conjunto em $LABHOME/r (bin + lib); com o pacote frr da distribuicao (como root):
#   FRR_LD="" FRR_BIN=/usr/lib/frr FRR_VTYSH=/usr/bin/vtysh
LD = os.environ.get("FRR_LD", f"{H}/r/lib/ld-linux-x86-64.so.2 --library-path {H}/r/lib"); BIN = os.environ.get("FRR_BIN", f"{H}/r/bin")
VTYSH = os.environ.get("FRR_VTYSH", f"{BIN}/vtysh")
FRR_USER = os.environ.get("FRR_USER", "root")   # com o pacote frr da distribuicao use "frr" (o FRR recusa 'root' fora do grupo frrvty)
RUN = f"{H}/run"
DADOS2 = f"{H}/dados2"; PARC = f"{DADOS2}/parciais"
CAB_FALHA = ["config", "nome", "execucao", "caminho", "lado", "outage_s", "perdidos", "failback_s", "perdidos_fb"]
CAB_QOS = ["cenario", "rep", "sonda_dscp", "media_ms", "p95_ms", "max_ms", "perda_pct", "vazao_mbps"]
CAB_MICRO = ["config", "nome", "lab", "tentativa", "duracao_nominal_ms", "janela_medida_ms", "troca_de_caminho",
             "execucoes_spf", "bfd_ignorar", "proximos_saltos_vistos"]   # bfd_ignorar: eventos de queda do BFD (nao usado na analise)
CENARIOS_QOS = (("sem carga", "fifo", False, "0xb8"), ("carga, fila unica, sonda EF", "fifo", True, "0xb8"),
                ("carga, FILA-WAN, sonda EF", "wan", True, "0xb8"), ("carga, FILA-WAN, sonda sem marca", "wan", True, "0x00"))


def sh(c, **kw): return subprocess.run(c, shell=True, capture_output=True, text=True, **kw)
def ns(n, c): return sh(f"ip netns exec {n} {c}")


def prep():
    if os.path.exists("/run/.prep"): return
    sh("mount -t tmpfs tmpfs /run")
    sh("cp /etc/group /run/g; echo 'frrvty:x:999:root' >> /run/g; echo 'frr:x:998:' >> /run/g; mount --bind /run/g /etc/group")
    sh("cp -r /etc/pam.d /run/pamd; printf 'auth sufficient pam_permit.so\\naccount sufficient pam_permit.so\\n' > /run/pamd/frr; mount --bind /run/pamd /etc/pam.d")
    sh("ip link set lo up"); open("/run/.prep", "w").write("1")


CFGS = {
 "C1": dict(nome="OSPF 10/40", hello=10, dead=40, bfd=None),
 "C2": dict(nome="OSPF 5/20", hello=5, dead=20, bfd=None),
 "C3": dict(nome="OSPF 1/3", hello=1, dead=3, bfd=None),
 "C4": dict(nome="BFD 300 ms x 3", hello=10, dead=40, bfd=(300, 3)),
 "C5": dict(nome="BFD 100 ms x 3", hello=10, dead=40, bfd=(100, 3)),
}
KEYS = {"A": ("0x"+"11"*32, "0x"+"22"*20, "0x"+"33"*32, "0x"+"44"*20), "B": ("0x"+"55"*32, "0x"+"66"*20, "0x"+"77"*32, "0x"+"88"*20)}
DELAY = {"A": 8, "B": 12}   # atraso de ida em cada ISP, ms
ENDERECOS = {"A": ("172.16.0.1", "172.16.0.2"), "B": ("172.16.0.5", "172.16.0.6")}


class Lab:
    def __init__(s, k, cfg, tunnel=True, esp=True, tun_mtu=1400, esp_mode="transport"):
        s.k = k; s.cfg = cfg; s.dir = f"{RUN}/{k}"; os.makedirs(s.dir, exist_ok=True)
        s.upa = f"upa{k}"; s.ubs = f"ubs{k}"; s.isp = {"A": f"ispa{k}", "B": f"ispb{k}"}
        s.tunnel = tunnel; s.esp = esp; s.tun_mtu = tun_mtu; s.esp_mode = esp_mode

    def build(s):
        prep()
        for n in [s.upa, s.ubs, *s.isp.values()]:
            sh(f"ip netns add {n}"); ns(n, "ip link set lo up")
        s.ifs = {s.upa: {}, s.ubs: {}}
        for i, (p, (a, b)) in enumerate(ENDERECOS.items()):
            l = p.lower(); isp = s.isp[p]
            u1, i1, i2, b2 = f"u{s.k}{l}", f"i{s.k}{l}1", f"i{s.k}{l}2", f"b{s.k}{l}"
            sh(f"ip link add {u1} type veth peer name {i1}; ip link add {i2} type veth peer name {b2}")
            sh(f"ip link set {u1} netns {s.upa}; ip link set {i1} netns {isp}; ip link set {i2} netns {isp}; ip link set {b2} netns {s.ubs}")
            ns(s.upa, f"ip addr add {a}/30 dev {u1}"); ns(s.ubs, f"ip addr add {b}/30 dev {b2}")
            ns(isp, "ip link add br0 type bridge"); ns(isp, "sh -c 'echo 0 > /sys/class/net/br0/bridge/stp_state'")
            for x in (i1, i2):
                ns(isp, f"ip link set {x} master br0"); ns(isp, f"ip link set {x} up")
                ns(isp, f"tc qdisc add dev {x} root netem delay {DELAY[p]}ms")
            ns(isp, "ip link set br0 up"); ns(s.upa, f"ip link set {u1} up"); ns(s.ubs, f"ip link set {b2} up")
            s.ifs[s.upa][p] = u1; s.ifs[s.ubs][p] = b2
            if s.tunnel:
                t = f"tun{l}"; ta, tb = (f"10.255.{i+1}.1", f"10.255.{i+1}.2")
                ns(s.upa, f"ip link add {t} type gre local {a} remote {b} ttl 64 tos inherit"); ns(s.ubs, f"ip link add {t} type gre local {b} remote {a} ttl 64 tos inherit")
                ns(s.upa, f"ip addr add {ta}/30 dev {t}"); ns(s.ubs, f"ip addr add {tb}/30 dev {t}")
                for r in (s.upa, s.ubs): ns(r, f"ip link set {t} mtu {s.tun_mtu} up")
                if s.esp:
                    k1, a1, k2, a2 = KEYS[p]; sp = 0x1000 + 0x10*(i+1)
                    # sentido UPA->UBS: spi sp+1, chave k1/a1; sentido UBS->UPA: spi sp+2, chave k2/a2 (iguais nas duas pontas)
                    for r in (s.upa, s.ubs):
                        ns(r, f"ip xfrm state add src {a} dst {b} proto esp spi {sp+1} reqid {i+1} mode {s.esp_mode} enc 'cbc(aes)' {k1} auth 'hmac(sha1)' {a1}")
                        ns(r, f"ip xfrm state add src {b} dst {a} proto esp spi {sp+2} reqid {i+1} mode {s.esp_mode} enc 'cbc(aes)' {k2} auth 'hmac(sha1)' {a2}")
                    ns(s.upa, f"ip xfrm policy add src {a}/32 dst {b}/32 proto gre dir out tmpl src {a} dst {b} proto esp reqid {i+1} mode {s.esp_mode}")
                    ns(s.upa, f"ip xfrm policy add src {b}/32 dst {a}/32 proto gre dir in tmpl src {b} dst {a} proto esp reqid {i+1} mode {s.esp_mode}")
                    ns(s.ubs, f"ip xfrm policy add src {b}/32 dst {a}/32 proto gre dir out tmpl src {b} dst {a} proto esp reqid {i+1} mode {s.esp_mode}")
                    ns(s.ubs, f"ip xfrm policy add src {a}/32 dst {b}/32 proto gre dir in tmpl src {a} dst {b} proto esp reqid {i+1} mode {s.esp_mode}")
                s.ifs[s.upa][p] = t; s.ifs[s.ubs][p] = t
        ns(s.upa, "ip addr add 10.1.10.1/32 dev lo"); ns(s.ubs, "ip addr add 10.2.10.1/32 dev lo")
        if os.environ.get("ARP_FIXO"):
            def mac(r, dev): return json.loads(ns(r, f"ip -j link show {dev}").stdout)[0]["address"]
            for p, (a, b) in ENDERECOS.items():
                l = p.lower(); u, bb = f"u{s.k}{l}", f"b{s.k}{l}"
                ns(s.upa, f"ip neigh replace {b} lladdr {mac(s.ubs, bb)} dev {u} nud permanent")
                ns(s.ubs, f"ip neigh replace {a} lladdr {mac(s.upa, u)} dev {bb} nud permanent")
        for r, rid, lo in ((s.upa, "1.1.1.1", "10.1.10.1/32"), (s.ubs, "2.2.2.2", "10.2.10.1/32")):
            d = f"{s.dir}/{r}"; os.makedirs(d, exist_ok=True); c = s.cfg
            open(f"{d}/zebra.conf", "w").write(f"hostname {r}\n")
            if c["bfd"]:
                tx, m = c["bfd"]; open(f"{d}/bfdd.conf", "w").write(f"hostname {r}\nbfd\n profile P\n  detect-multiplier {m}\n  receive-interval {tx}\n  transmit-interval {tx}\n")
            o = f"hostname {r}\n"
            for p, cost in (("A", 10), ("B", 100)):
                o += f"interface {s.ifs[r][p]}\n ip ospf network point-to-point\n ip ospf hello-interval {c['hello']}\n ip ospf dead-interval {c['dead']}\n ip ospf cost {cost}\n"
                if c["bfd"]: o += " ip ospf bfd\n ip ospf bfd profile P\n"
            nets = ["10.255.1.0/30", "10.255.2.0/30"] if s.tunnel else ["172.16.0.0/30", "172.16.0.4/30"]
            o += f"router ospf\n ospf router-id {rid}\n" + "".join(f" network {n} area 0\n" for n in nets) + f" network {lo} area 0\n"
            open(f"{d}/ospfd.conf", "w").write(o)
            if FRR_USER != "root": sh(f"chown -R {FRR_USER}:{FRR_USER} {d}; chmod 777 {d}")
            for x in ["zebra", "ospfd"] + (["bfdd"] if c["bfd"] else []):
                ns(r, f"{LD} {BIN}/{x} -d -f {d}/{x}.conf -i {d}/{x}.pid -z {d}/z.api --vty_socket {d} -A 127.0.0.1 -u {FRR_USER} -g {FRR_USER}")

    def vty(s, r, cmd): return ns(r, f"{LD} {VTYSH} --vty_socket {s.dir}/{r} -c '{cmd}'").stdout
    def nfull(s, r): return s.vty(r, "show ip ospf neighbor").count("Full")

    def via(s):
        o = ns(s.ubs, "ip route get 10.1.10.1 from 10.2.10.1").stdout
        m = re.search(r"via (\S+)", o); return m.group(1) if m else None

    def steady(s, timeout=150):
        want = "10.255.1.1" if s.tunnel else "172.16.0.1"
        t = time.time()
        while time.time() - t < timeout:
            if s.nfull(s.upa) == 2 and s.nfull(s.ubs) == 2 and s.via() == want:
                time.sleep(2); return True
            time.sleep(0.5)
        return False

    def fail(s, path, side, up=False):
        l = path.lower(); isp = s.isp[path]; x = f"i{s.k}{l}{'1' if side == 'UPA' else '2'}"
        ns(isp, f"ip link set {x} {'up' if up else 'down'}")

    def run(s, path, side, tmax=150, fb=True):
        if not s.steady(): return None
        out = f"{s.dir}/ping.txt"
        # a sonda sai do lado OPOSTO ao da falha: um ping que perde o proprio link local trava ~3 s sozinho (artefato de medida)
        sn, sa, da = (s.ubs, "10.2.10.1", "10.1.10.1") if side == "UPA" else (s.upa, "10.1.10.1", "10.2.10.1")
        pr = subprocess.Popen(f"exec ip netns exec {sn} ping -D -n -i 0.02 -I {sa} {da} > {out}", shell=True, preexec_fn=os.setsid)
        try:
            time.sleep(3 + random.uniform(0, 2*max(s.cfg['hello'], 1))); tf = time.time(); s.fail(path, side)

            def last_ts():
                try:
                    for ln in reversed(open(out).read().strip().splitlines()):
                        m = re.match(r"\[(\d+\.\d+)\] \d+ bytes from", ln)
                        if m: return float(m.group(1))
                except OSError:
                    pass
                return 0
            t0 = time.time()
            while time.time() - t0 < tmax:
                time.sleep(0.5)
                if last_ts() > tf + 1 and time.time() - tf > 2: break
            time.sleep(3); tr = time.time(); s.fail(path, side, up=True)
            if fb:
                s.steady(timeout=150); time.sleep(3)
        finally:
            try: os.killpg(pr.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            pr.wait()
        r = s.analisa(out, tf, tr)
        if not fb: r['failback'] = ''; r['perdidos_fb'] = ''
        return r

    def analisa(s, out, tf, tr):
        ts = []
        for ln in open(out):
            m = re.match(r"\[(\d+\.\d+)\] \d+ bytes from .*icmp_seq=(\d+)", ln)
            if m: ts.append((float(m.group(1)), int(m.group(2))))

        def gap(a, b):
            best = 0; miss = 0
            for (x, sx), (y, sy) in zip(ts, ts[1:]):
                if y >= a and x <= b:
                    best = max(best, y - x); miss += sy - sx - 1
            return max(best - 0.02, 0), miss
        g1, m1 = gap(tf - 0.05, tr); g2, m2 = gap(tr - 0.05, tr + 120)
        return dict(outage=g1, perdidos=m1, failback=g2, perdidos_fb=m2)

    def stop(s):
        for n in [s.upa, s.ubs, *s.isp.values()]:
            sh(f"ip netns pids {n} | xargs -r kill -9"); sh(f"ip netns del {n}")


def linha_falha(ck, nome, idx, p, sd, r):
    fb = r["failback"] if r["failback"] == '' else round(r["failback"], 3)
    return [ck, nome, idx, p, sd, round(r["outage"], 3), r["perdidos"], fb, r["perdidos_fb"]]


def novo_csv(caminho, cabecalho):
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    f = open(caminho, "w", newline=""); w = csv.writer(f); w.writerow(cabecalho); f.flush()
    return f, w


# ---------- Baterias de falha ----------
def rec(k, n, nb, start=0):
    c = CFGS[k[:2]]; L = Lab(k.lower(), c)
    plan = [("A", ["UPA", "UBS"][(i+start) % 2]) for i in range(n)] + [("B", ["UPA", "UBS"][i % 2]) for i in range(nb)]
    f, w = novo_csv(f"{DADOS2}/lab2-{k}.csv", CAB_FALHA)
    try:
        L.build()
        for i, (p, sd) in enumerate(plan, 1):
            try:
                r = L.run(p, sd)
            except Exception as e:      # uma execucao com erro nao derruba a bateria
                print("erro na execucao", i, repr(e), flush=True); continue
            if r is None: print("falha steady", i, flush=True); continue
            w.writerow(linha_falha(k[:2], c["nome"], i, p, sd, r)); f.flush()
            print(k, i, p, sd, r, flush=True)
    finally:
        f.close(); L.stop()
    open(f"{DADOS2}/done-{k}", "w").write("ok")


def one(ck, idx, p, sd, fb):
    c = CFGS[ck]; L = Lab(f"{ck.lower()}x{idx}", c)
    try:
        L.build(); r = L.run(p, sd, fb=fb)
    finally:
        L.stop()
    if r is None: print("falha steady", ck, idx, flush=True); return
    f, w = novo_csv(f"{PARC}/lab2-{ck}-{idx}.csv", CAB_FALHA)
    w.writerow(linha_falha(ck, c["nome"], idx, p, sd, r)); f.close()
    print(ck, idx, p, sd, r, flush=True)


# ---------- Verificacoes extras: cifragem, MTU, DSCP, QoS ----------
def contadores(L, p="A"):
    """Instala filtros que contam, na ponte do ISP, os pacotes por protocolo e por TOS (cabecalho externo)."""
    isp = L.isp[p]; x = f"i{L.k}{p.lower()}1"
    ns(isp, f"tc qdisc add dev {x} ingress")
    regras = [("ef", "protocol ip prio 1 u32 match ip protocol 50 0xff match ip tos 0xb8 0xfc"),
              ("af31", "protocol ip prio 2 u32 match ip protocol 50 0xff match ip tos 0x68 0xfc"),
              ("esp", "protocol ip prio 3 u32 match ip protocol 50 0xff"), ("gre", "protocol ip prio 4 u32 match ip protocol 47 0xff"),
              ("icmp_claro", "protocol ip prio 5 u32 match ip protocol 1 0xff")]
    for nome, r in regras: ns(isp, f"tc filter add dev {x} parent ffff: {r} action gact ok")
    return x


def ler(L, x, p="A"):
    o = ns(L.isp[p], f"tc -s filter show dev {x} parent ffff:").stdout
    res = {}; prios = ["ef", "af31", "esp", "gre", "icmp_claro"]
    blocos = re.split(r"filter protocol ip pref (\d+)", o)
    for i in range(1, len(blocos), 2):
        pr = int(blocos[i]); m = re.search(r"Sent (\d+) bytes (\d+) pkt", blocos[i+1])
        if m and 1 <= pr <= 5: res[prios[pr-1]] = (int(m[2]), int(m[1]))
    t = [res.get(k, (0, 0)) for k in ('esp', 'ef', 'af31')]; res['esp_total'] = (sum(x[0] for x in t), sum(x[1] for x in t))
    return res


def verif(rep=1):
    """Cifragem (so ESP na rede), MTU (maior ping sem fragmentar), sobrecarga medida, DSCP pelo tunel."""
    L = Lab("v", CFGS["C3"])
    try:
        L.build(); assert L.steady(90)
        x = contadores(L); out = {}
        base = ler(L, x)
        ns(L.upa, "ping -c 30 -i 0.1 -I 10.1.10.1 10.2.10.1 >/dev/null")
        d = ler(L, x); out["cifragem"] = {k: d.get(k, (0, 0))[0]-base.get(k, (0, 0))[0] for k in ("gre", "esp_total", "icmp_claro")}
        # sobrecarga: um ping de 1000 bytes; bytes na rede por pacote ESP
        q0 = ler(L, x); time.sleep(4); q1 = ler(L, x); t0 = time.time()
        b0 = ler(L, x); ns(L.upa, "ping -c 40 -i 0.1 -s 1000 -I 10.1.10.1 10.2.10.1 >/dev/null"); b1 = ler(L, x); dt = time.time()-t0
        bg = (q1["esp_total"][1]-q0["esp_total"][1])/4.0   # bytes/s de controle (hellos) sem ping
        by = b1["esp_total"][1]-b0["esp_total"][1]-bg*dt; pk = 40
        out["sobrecarga"] = dict(carga_interna_ip=1028, bytes_ip_externo_por_pacote=round(by/pk, 1), sobrecarga_bytes=round(by/pk-1028, 1))
        # MTU: maior payload com DF que passa pelo tunel
        for sz in (1372, 1373, 1380, 1400, 1420, 1472):
            r = ns(L.upa, f"ping -c2 -W1 -M do -s {sz} -I 10.1.10.1 10.2.10.1").stdout
            rx = re.search(r"(\d+) received", r); out.setdefault("mtu", {})[sz] = int(rx[1]) if rx else 0
        # DSCP
        d0 = ler(L, x)
        ns(L.upa, "ping -c 20 -i 0.1 -Q 0xb8 -I 10.1.10.1 10.2.10.1 >/dev/null"); d1 = ler(L, x)
        ns(L.upa, "ping -c 20 -i 0.1 -Q 0x68 -I 10.1.10.1 10.2.10.1 >/dev/null"); d2 = ler(L, x)
        g = lambda a, b, k: b.get(k, (0, 0))[0]-a.get(k, (0, 0))[0]
        out["dscp"] = dict(enviados_EF=20, esp_com_EF=g(d0, d1, "ef"), enviados_AF31=20, esp_com_AF31=g(d1, d2, "af31"))
        out["rtt_ms"] = ns(L.upa, "ping -c 20 -i 0.1 -I 10.1.10.1 10.2.10.1").stdout.splitlines()[-1]
    finally:
        L.stop()
    return out


def qdisc(L, mode):
    wan = f"u{L.k}a"   # shaping na saida da WAN da UPA (underlay), casando o TOS externo
    ns(L.upa, f"tc qdisc del dev {wan} root")
    if mode == "fifo":
        ns(L.upa, f"tc qdisc add dev {wan} root handle 1: htb default 20")
        ns(L.upa, f"tc class add dev {wan} parent 1: classid 1:1 htb rate 10mbit ceil 10mbit")
        ns(L.upa, f"tc class add dev {wan} parent 1:1 classid 1:20 htb rate 10mbit ceil 10mbit")
        ns(L.upa, f"tc qdisc add dev {wan} parent 1:20 handle 20: pfifo limit 500")
    else:
        ns(L.upa, f"tc qdisc add dev {wan} root handle 1: htb default 30")
        ns(L.upa, f"tc class add dev {wan} parent 1: classid 1:1 htb rate 10mbit ceil 10mbit")
        for cid, rate, pr in ((10, 2, 0), (20, 4, 1), (30, 4, 2)):
            ns(L.upa, f"tc class add dev {wan} parent 1:1 classid 1:{cid} htb rate {rate}mbit ceil 10mbit prio {pr}")
            ns(L.upa, f"tc qdisc add dev {wan} parent 1:{cid} handle {cid}: pfifo limit 500")
        ns(L.upa, f"tc filter add dev {wan} parent 1: protocol ip prio 1 u32 match ip tos 0xb8 0xfc flowid 1:10")
        ns(L.upa, f"tc filter add dev {wan} parent 1: protocol ip prio 2 u32 match ip tos 0x68 0xfc flowid 1:20")


FLOOD = '''
import socket,threading,sys,time
mode,addr,src,dur=sys.argv[1],sys.argv[2],sys.argv[3],float(sys.argv[4])
if mode=="srv":
    s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind((addr,5001)); s.listen(8); s.settimeout(dur+8); tot=[0]
    def h(c):
        while True:
            d=c.recv(65536)
            if not d: break
            tot[0]+=len(d)
    t0=time.time(); ths=[]
    try:
        while time.time()-t0<dur+6:
            c,_=s.accept(); th=threading.Thread(target=h,args=(c,),daemon=True); th.start(); ths.append(th)
    except OSError: pass
    time.sleep(1); print(tot[0]*8/dur/1e6)
else:
    def f():
        c=socket.socket(); c.bind((src,0)); c.connect((addr,5001)); b=b"x"*65536; t0=time.time()
        while time.time()-t0<dur: c.send(b)
        c.close()
    ths=[threading.Thread(target=f) for _ in range(4)]
    [t.start() for t in ths]; [t.join() for t in ths]
'''


def medida(L, carga, dscp, dur=10):
    open(f"{H}/flood.py", "w").write(FLOOD)
    srv = cli = None; saida = f"{H}/srv_{L.k}.out"
    if carga:
        srv = subprocess.Popen(f"exec ip netns exec {L.ubs} python3 {H}/flood.py srv 10.2.10.1 x {dur+3} > {saida}", shell=True); time.sleep(0.7)
        cli = subprocess.Popen(f"exec ip netns exec {L.upa} python3 {H}/flood.py cli 10.2.10.1 10.1.10.1 {dur+3}", shell=True); time.sleep(3)
    r = ns(L.upa, f"ping -n -i 0.05 -c {int(dur/0.05)} -Q {dscp} -I 10.1.10.1 10.2.10.1")
    rtts = sorted(float(x) for x in re.findall(r"time=([\d.]+) ms", r.stdout))
    m = re.search(r"(\d+) packets transmitted, (\d+) received", r.stdout)
    tx, rx = (int(m[1]), int(m[2])) if m else (int(dur/0.05), 0)
    bw = None
    if cli:
        cli.wait(timeout=60); srv.wait(timeout=60)
        try: bw = float(open(saida).read().strip())   # o servidor ja divide pelos dur+3 s de carga
        except (OSError, ValueError): pass
    return dict(media=st.mean(rtts) if rtts else None, p95=rtts[int(.95*len(rtts))-1] if rtts else None,
                max=max(rtts) if rtts else None, perda=100*(tx-rx)/tx, mbps=bw)


def rodada_qos(L, rp, w, f):
    for cen, mode, carga, dscp in CENARIOS_QOS:
        qdisc(L, mode); r = medida(L, carga, dscp)
        w.writerow([cen, rp, dscp, *(None if r[k] is None else round(r[k], 2) for k in ("media", "p95", "max", "perda", "mbps"))]); f.flush()
        print(cen, rp, r, flush=True)


def qos(rep):
    L = Lab("q", CFGS["C3"]); f, w = novo_csv(f"{DADOS2}/lab2-QOS.csv", CAB_QOS)
    try:
        L.build(); assert L.steady(90)
        for rp in range(1, rep+1): rodada_qos(L, rp, w, f)
    finally:
        f.close(); L.stop()


def qos1(rp):
    L = Lab(f"q{rp}", CFGS["C3"]); f, w = novo_csv(f"{PARC}/lab2-QOS-{rp}.csv", CAB_QOS)
    try:
        L.build(); assert L.steady(90)
        rodada_qos(L, rp, w, f)
    finally:
        f.close(); L.stop()


# ---------- Modo tunel do ESP: sobrecarga e MTU ----------
def verif_tunel():
    L = Lab("w", CFGS["C3"], esp_mode="tunnel")
    try:
        L.build(); assert L.steady(90)
        x = contadores(L); out = {"modo": "tunnel"}
        q0 = ler(L, x); time.sleep(4); q1 = ler(L, x); bg = (q1["esp_total"][1]-q0["esp_total"][1])/4.0
        for carga in (1000, 1372):
            t0 = time.time(); b0 = ler(L, x)
            ns(L.upa, f"ping -c 40 -i 0.1 -s {carga} -I 10.1.10.1 10.2.10.1 >/dev/null"); b1 = ler(L, x); dt = time.time()-t0
            by = b1["esp_total"][1]-b0["esp_total"][1]-bg*dt
            out[f"interno_{carga+28}"] = dict(bytes_externo=round(by/40, 1), sobrecarga=round(by/40-(carga+28), 1))
        for sz in (1372, 1373):
            r = ns(L.upa, f"ping -c2 -W1 -M do -s {sz} -I 10.1.10.1 10.2.10.1").stdout
            rx = re.search(r"(\d+) received", r); out.setdefault("mtu", {})[sz] = int(rx[1]) if rx else 0
    finally:
        L.stop()
    return out


# ---------- Microinterrupcoes: o mecanismo declara queda falsa? ----------
CFGS_MICRO = {"C3": dict(nome="OSPF 1/3", hello=1, dead=3, bfd=None),
              "C4": dict(nome="BFD 300 ms x 3", hello=1, dead=40, bfd=(300, 3)),
              "C5": dict(nome="BFD 100 ms x 3", hello=1, dead=40, bfd=(100, 3))}


def contadores_rot(L, r):
    o = L.vty(r, "show ip ospf"); m = re.search(r"SPF algorithm executed (\d+) times", o); spf = int(m[1]) if m else -1
    b = L.vty(r, "show bfd peers counters"); bd = sum(int(x) for x in re.findall(r"Session down events: (\d+)", b))
    return spf, bd


def micro(ck, idx, durs, tmax=100):
    c = CFGS_MICRO[ck]; L = Lab(f"m{ck[1]}{idx}", c)
    sufixo = "-ARPFIXO" if os.environ.get("ARP_FIXO") else ""
    f, w = novo_csv(f"{PARC}/lab2-MICRO{sufixo}-{ck}-{idx}.csv", CAB_MICRO)
    try:
        L.build(); t_ini = time.time()
        if not L.steady(60): print("falha steady"); return
        isp = L.isp["A"]; p1, p2 = f"i{L.k}a1", f"i{L.k}a2"
        on = f"{L.dir}/on.tc"; off = f"{L.dir}/off.tc"
        open(on, "w").write(f"qdisc change dev {p1} root netem delay 8ms loss 100%\nqdisc change dev {p2} root netem delay 8ms loss 100%\n")
        open(off, "w").write(f"qdisc change dev {p1} root netem delay 8ms\nqdisc change dev {p2} root netem delay 8ms\n")
        rnd = random.Random(int(idx)*7919)
        for k, d in enumerate(durs):
            if time.time()-t_ini > tmax: break
            if not L.steady(30): continue
            time.sleep(rnd.uniform(0.5, 1.5))
            a_ubs = contadores_rot(L, L.ubs); a_upa = contadores_rot(L, L.upa)
            t0 = time.time(); ns(isp, f"tc -b {on}"); t1 = time.time()
            while time.time() < t1+d/1000.0: time.sleep(0.002)
            t2 = time.time(); ns(isp, f"tc -b {off}"); t3 = time.time()
            janela = ((t2+t3)/2-(t0+t1)/2)*1000
            caiu = False; vias = set(); tfim = time.time()+1.2
            while time.time() < tfim:
                v = L.via(); vias.add(str(v))
                if v is not None and v != "10.255.1.1": caiu = True
                time.sleep(0.02)
            time.sleep(0.3)
            b_ubs = contadores_rot(L, L.ubs); b_upa = contadores_rot(L, L.upa)
            spf = (b_ubs[0]-a_ubs[0])+(b_upa[0]-a_upa[0]); bfd = (b_ubs[1]-a_ubs[1])+(b_upa[1]-a_upa[1])
            w.writerow([ck, c["nome"], idx, k, d, round(janela, 1), int(caiu), spf, bfd, "|".join(sorted(vias))]); f.flush()
    finally:
        f.close(); L.stop()


# ---------- Consolidacao das parciais nos nomes que analisa-lab.py espera ----------
def consolida(pasta=DADOS2):
    parc = os.path.join(pasta, "parciais")
    if not os.path.isdir(parc): print("nenhuma parcial em", parc); return {}
    grupos = {}
    for fn in sorted(glob.glob(os.path.join(parc, "lab2-*.csv"))):
        # lab2-C3-7.csv, lab2-QOS-2.csv, lab2-MICRO-C5-1.csv, lab2-MICRO-ARPFIXO-C3-2.csv
        m = re.match(r"lab2-(MICRO-ARPFIXO|MICRO|QOS|C[1-5])(?:-(C[1-5]))?-(\d+)\.csv$", os.path.basename(fn))
        if m: grupos.setdefault(m[1], []).append(((m[2] or "", int(m[3])), fn))
    feitos = {}
    for alvo, arqs in grupos.items():
        cab = CAB_MICRO if alvo.startswith("MICRO") else CAB_QOS if alvo == "QOS" else CAB_FALHA
        linhas = []
        for idx, fn in sorted(arqs):
            with open(fn, newline="", encoding="utf-8") as fh:
                rd = csv.reader(fh); h = next(rd, None)
                if h != cab: print("cabecalho inesperado, ignorado:", fn); continue
                linhas += list(rd)
        saida = os.path.join(pasta, f"lab2-{alvo}.csv")
        with open(saida, "w", newline="") as fo:
            w = csv.writer(fo); w.writerow(cab); w.writerows(linhas)
        feitos[saida] = len(linhas); print(f"{saida}: {len(linhas)} linhas de {len(arqs)} parciais")
    return feitos


def main(argv):
    if len(argv) < 2: print(__doc__); return 2
    mode, a = argv[1], argv[2:]
    if mode == "rec": rec(a[0], int(a[1]), int(a[2]), int(a[3]) if len(a) > 3 else 0)
    elif mode == "one": one(a[0], a[1], a[2], a[3], a[4] == "1")
    elif mode == "verif": print(json.dumps(verif(), indent=1))
    elif mode == "veriftun": print(json.dumps(verif_tunel()))
    elif mode == "qos": qos(int(a[0]))
    elif mode == "qos1": qos1(int(a[0]))
    elif mode == "micro": micro(a[0], a[1], [int(x) for x in a[2].split(",")])
    elif mode == "consolida": consolida(a[0] if a else DADOS2)
    else: print(__doc__); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
