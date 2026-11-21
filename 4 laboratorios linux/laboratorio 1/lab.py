#!/usr/bin/env python3
"""Laboratorio Linux (namespaces + FRR) para medir o tempo de recuperacao.
Topologia: UPA-R e UBS-R ligados por dois enlaces (ISP-A e ISP-B). Cada ISP e uma ponte (L2)
transparente, de modo que a queda de uma porta do ISP e percebida na hora pelo roteador proximo
e so pelo temporizador (ou BFD) no roteador remoto, como no Packet Tracer.

Pasta de trabalho: $LABHOME (padrao /tmp/lab). Os CSV vao para $LABHOME/dados com os nomes que
analisa-lab.py espera (lab-C*.csv, lab-QOS.csv)."""
import subprocess, time, os, re, random, signal
FRR = os.environ.get("FRR", "/usr/lib/frr")   # pacote frr da distribuicao
BASE = os.environ.get("LABHOME", "/tmp/lab")
DADOS = f"{BASE}/dados"
CABECALHO = ["config", "nome", "execucao", "caminho", "lado", "outage_s", "perdidos", "failback_s", "perdidos_fb"]


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


def ns(n, cmd): return sh(f"ip netns exec {n} {cmd}")



CFGS = {
 "C1": dict(nome="OSPF 10/40", hello=10, dead=40, bfd=None),
 "C2": dict(nome="OSPF 5/20", hello=5, dead=20, bfd=None),
 "C3": dict(nome="OSPF 1/3", hello=1, dead=3, bfd=None),
 "C4": dict(nome="BFD 300 ms x 3", hello=10, dead=40, bfd=(300, 3)),
 "C5": dict(nome="BFD 100 ms x 3", hello=10, dead=40, bfd=(100, 3)),
}


class Lab:
    def __init__(s, k, cfg, base=f"{BASE}/run"):
        s.k = k; s.cfg = cfg; s.dir = f"{base}/{k}"; os.makedirs(s.dir, exist_ok=True)
        s.upa = f"upa{k}"; s.ubs = f"ubs{k}"; s.isp = {"A": f"ispa{k}", "B": f"ispb{k}"}

    def build(s):
        for n in [s.upa, s.ubs, *s.isp.values()]:
            sh(f"ip netns add {n}"); ns(n, "ip link set lo up")
        for p, (a, b) in {"A": ("172.16.0.1", "172.16.0.2"), "B": ("172.16.0.5", "172.16.0.6")}.items():
            l = p.lower(); i = s.isp[p]
            u1, i1, i2, b2 = f"u{s.k}{l}", f"i{s.k}{l}1", f"i{s.k}{l}2", f"b{s.k}{l}"
            sh(f"ip link add {u1} type veth peer name {i1}; ip link add {i2} type veth peer name {b2}")
            sh(f"ip link set {u1} netns {s.upa}; ip link set {i1} netns {i}; ip link set {i2} netns {i}; ip link set {b2} netns {s.ubs}")
            ns(s.upa, f"ip addr add {a}/30 dev {u1}"); ns(s.ubs, f"ip addr add {b}/30 dev {b2}")
            ns(i, "ip link add br0 type bridge")
            ns(i, "sh -c 'echo 0 > /sys/class/net/br0/bridge/stp_state' 2>/dev/null")
            for x in (i1, i2): ns(i, f"ip link set {x} master br0"); ns(i, f"ip link set {x} up")
            ns(i, "ip link set br0 up"); ns(s.upa, f"ip link set {u1} up"); ns(s.ubs, f"ip link set {b2} up")
        ns(s.upa, "ip addr add 10.1.10.1/32 dev lo"); ns(s.ubs, "ip addr add 10.2.10.1/32 dev lo")
        s.ifs = {s.upa: {"A": f"u{s.k}a", "B": f"u{s.k}b"}, s.ubs: {"A": f"b{s.k}a", "B": f"b{s.k}b"}}
        for r, rid, lo in ((s.upa, "1.1.1.1", "10.1.10.1/32"), (s.ubs, "2.2.2.2", "10.2.10.1/32")):
            d = f"{s.dir}/{r}"; os.makedirs(d, exist_ok=True)
            c = s.cfg
            open(f"{d}/zebra.conf", "w").write(f"hostname {r}\n")
            if c["bfd"]:
                tx, m = c["bfd"]
                open(f"{d}/bfdd.conf", "w").write(f"hostname {r}\nbfd\n profile P\n  detect-multiplier {m}\n"
                                                  f"  receive-interval {tx}\n  transmit-interval {tx}\n")
            o = f"hostname {r}\n"
            for p, cost in (("A", 10), ("B", 100)):
                o += (f"interface {s.ifs[r][p]}\n ip ospf network point-to-point\n ip ospf hello-interval {c['hello']}\n"
                      f" ip ospf dead-interval {c['dead']}\n ip ospf cost {cost}\n")
                if c["bfd"]: o += " ip ospf bfd\n ip ospf bfd profile P\n"
            o += f"router ospf\n ospf router-id {rid}\n network 172.16.0.0/30 area 0\n network 172.16.0.4/30 area 0\n network {lo} area 0\n"
            open(f"{d}/ospfd.conf", "w").write(o)
            sh(f"chown -R frr:frr {d}; chmod 777 {d}")
            for x in ["zebra", "ospfd"] + (["bfdd"] if c["bfd"] else []):
                ns(r, f"{FRR}/{x} -d -f {d}/{x}.conf -i {d}/{x}.pid -z {d}/z.api --vty_socket {d} -A 127.0.0.1 -u frr -g frr")

    def vty(s, r, cmd): return ns(r, f"vtysh --vty_socket {s.dir}/{r} -c '{cmd}'").stdout
    def nfull(s, r): return s.vty(r, "show ip ospf neighbor").count("Full")

    def via(s):
        o = ns(s.ubs, "ip route get 10.1.10.1 from 10.2.10.1").stdout
        m = re.search(r"via (\S+)", o); return m.group(1) if m else None

    def steady(s, timeout=120):
        t = time.time()
        while time.time() - t < timeout:
            if s.nfull(s.upa) == 2 and s.nfull(s.ubs) == 2 and s.via() == "172.16.0.1":
                time.sleep(2); return True
            time.sleep(0.5)
        return False

    def fail(s, path, side, up=False):
        l = path.lower(); i = s.isp[path]; x = f"i{s.k}{l}{'1' if side == 'UPA' else '2'}"
        ns(i, f"ip link set {x} {'up' if up else 'down'}")

    def run(s, path, side, tmax=150):
        if not s.steady(): return None
        out = f"{s.dir}/ping.txt"
        pr = subprocess.Popen(f"exec ip netns exec {s.ubs} ping -D -n -i 0.02 -I 10.2.10.1 10.1.10.1 > {out}",
                              shell=True, preexec_fn=os.setsid)
        try:
            time.sleep(3 + random.uniform(0, 2 * max(s.cfg['hello'], 1))); tf = time.time(); s.fail(path, side)

            # espera recuperar: nova resposta apos tf+1 s e pelo menos 2 s desde a falha
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
            s.steady(timeout=150); time.sleep(3)
        finally:
            try: os.killpg(pr.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            pr.wait()
        return s.analisa(out, tf, tr)

    def analisa(s, out, tf, tr):
        ts = []
        for ln in open(out):
            m = re.match(r"\[(\d+\.\d+)\] \d+ bytes from .*icmp_seq=(\d+)", ln)
            if m: ts.append((float(m.group(1)), int(m.group(2))))

        def gap(t_a, t_b):
            best = 0; miss = 0
            for (x, sx), (y, sy) in zip(ts, ts[1:]):
                if y >= t_a and x <= t_b:
                    best = max(best, y - x)
                    miss += sy - sx - 1
            return max(best - 0.02, 0), miss
        g1, m1 = gap(tf - 0.05, tr)
        g2, m2 = gap(tr - 0.05, tr + 120)
        return dict(outage=g1, perdidos=m1, failback=g2, perdidos_fb=m2)

    def stop(s):
        for n in [s.upa, s.ubs, *s.isp.values()]:
            sh(f"ip netns pids {n} | xargs -r kill -9"); sh(f"ip netns del {n}")
