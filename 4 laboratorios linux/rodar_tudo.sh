#!/bin/bash
# Executa o TCC inteiro de ponta a ponta. Requer: Ubuntu 22.04/24.04 (ou similar) com ROOT REAL, kernel com
# GRE, ESP, netem, htb, u32/gact e veth/bridge (modulos carregaveis), pacote frr 8.4.4, iperf3, iproute2, python3-scipy.
# Uso:  sudo ./rodar_tudo.sh [REPETICOES_QOS]      (de dentro da pasta "4 laboratorios linux")
#       sudo SO_LAB2=1 ./rodar_tudo.sh [N]         (so o laboratorio 2; saida em resultados-lab2-...)
# Saida: ./resultados-AAAA-MM-DD_HHMM/ (CSVs com os nomes que analisa-lab.py espera, logs, relatorio.txt, ambiente.txt)
set -u
[ "$(id -u)" = 0 ] || { echo "ERRO: precisa ser root (sudo)."; exit 1; }
AQUI=$(cd "$(dirname "$0")" && pwd); QOSN=${1:-5}
OUT="$AQUI/resultados-${SO_LAB2:+lab2-}$(date +%F_%H%M)"; mkdir -p "$OUT/logs" "$OUT/dados" "$OUT/dados2"
# Lab 1 roda o FRR como usuario "frr": a pasta de trabalho precisa ser acessivel a ele (nao pode ficar dentro de /home)
export LABHOME=/tmp/tcc-lab1; rm -rf "$LABHOME"; mkdir -p "$LABHOME"; chmod 755 "$LABHOME"
# Lab 2 tambem roda o FRR como "frr": pasta de trabalho fora do /home
L2HOME=/tmp/tcc-lab2; rm -rf "$L2HOME"; mkdir -p "$L2HOME/logs"; chmod 755 "$L2HOME"
{
  echo "== ambiente =="; date -Is; uname -a; . /etc/os-release; echo "$PRETTY_NAME"; nproc; free -m | sed -n 2p
  python3 --version; python3 -c "import scipy;print('scipy',scipy.__version__)"
  dpkg -l frr 2>/dev/null | tail -1; iperf3 --version | head -1; ip -V; tc -V
  [ -f /proc/config.gz ] && zcat /proc/config.gz | grep -E "CONFIG_NET_IPGRE=|CONFIG_INET_ESP=|CONFIG_NET_SCH_NETEM=|CONFIG_NET_SCH_HTB="
} > "$OUT/ambiente.txt" 2>&1
modprobe -a ip_gre esp4 xfrm_user sch_netem sch_htb cls_u32 act_gact veth bridge 2>>"$OUT/ambiente.txt"
# validacao das dependencias (falha cedo, sem relatorio falso)
FALTA=0
for b in /usr/lib/frr/zebra /usr/lib/frr/ospfd /usr/lib/frr/bfdd /usr/bin/vtysh; do [ -x $b ] || { echo "falta $b"; FALTA=1; }; done
for c in ip tc ping iperf3 unshare python3; do command -v $c >/dev/null || { echo "falta $c"; FALTA=1; }; done
python3 -c "import scipy" 2>/dev/null || { echo "falta python3-scipy"; FALTA=1; }
ip netns add _t && ip link add _g type gre local 1.1.1.1 remote 2.2.2.2 2>/dev/null && tc qdisc add dev lo root netem delay 1ms 2>/dev/null; R=$?
ip link del _g 2>/dev/null; tc qdisc del dev lo root 2>/dev/null; ip netns del _t 2>/dev/null
[ $R = 0 ] || { echo "kernel sem GRE e/ou netem"; FALTA=1; }
[ $FALTA = 0 ] || { echo "Dependencias ausentes; abortando."; exit 2; }
J=$(( $(nproc) > 1 ? $(nproc) : 1 ))   # falhas em paralelo (Lab 1 e Lab 2)
if [ -z "${SO_LAB2:-}" ]; then
# teste previo: um laboratorio precisa chegar ao estado estavel (OSPF Full nos dois lados e rota pelo ISP-A)
echo "== Teste previo do Lab 1 =="
( cd "$AQUI/laboratorio 1" && python3 - <<'PY'
import sys; sys.path.insert(0, ".")
import lab
L = lab.Lab("pre", lab.CFGS["C3"]); L.build()
ok = L.steady(90)
if not ok:
    print("FALHA: laboratorio nao estabilizou. nfull UPA/UBS =", L.nfull(L.upa), L.nfull(L.ubs), "via =", L.via())
    print(lab.ns(L.upa, "ps aux | grep -E 'zebra|ospfd' | grep -v grep").stdout)
    print(L.vty(L.upa, "show ip ospf neighbor")); print(lab.ns(L.upa, "ip -br addr").stdout)
L.stop(); sys.exit(0 if ok else 1)
PY
) > "$OUT/logs/teste-previo.log" 2>&1 || { echo "Teste previo FALHOU; veja $OUT/logs/teste-previo.log"; cat "$OUT/logs/teste-previo.log"; exit 3; }
echo "Teste previo OK"
cd "$AQUI/laboratorio 1"
echo "== Lab 1: baterias (C1 a C5) e QoS =="
run1() { python3 bateria.py "$@" > "$OUT/logs/lab1-$1.log" 2>&1; }
# C1 em 4 partes (32 falhas principais, como na Tabela 10); C2..C5: 20 principais + 3 de controle
( run1 C1a 10 2 0 & run1 C1b 10 1 1 & wait; run1 C1c 6 0 0 & run1 C1d 6 0 1 & wait; run1 C2 20 3 0 & run1 C3 20 3 0 & wait; run1 C4 20 3 0 & run1 C5 20 3 0 & wait )
python3 qos.py "$QOSN" > "$OUT/logs/lab1-qos.log" 2>&1
cp "$LABHOME"/dados/*.csv "$OUT/dados/"
fi
echo "== Lab 2: GRE sobre ESP =="
cd "$AQUI/laboratorio 2"; cp lab2.py "$L2HOME/"
export LABHOME="$L2HOME" FRR_LD="" FRR_BIN=/usr/lib/frr FRR_VTYSH=/usr/bin/vtysh FRR_USER=frr
lab2() { unshare -mn python3 "$L2HOME/lab2.py" "$@" >> "$L2HOME/logs/lab2.log" 2>&1; }   # mount ns proprio: protege o /run do host
echo "== Teste previo do Lab 2 =="
unshare -mn python3 - "$L2HOME" <<'PY' > "$OUT/logs/teste-previo-lab2.log" 2>&1 || { echo "Teste previo do Lab 2 FALHOU:"; cat "$OUT/logs/teste-previo-lab2.log"; exit 4; }
import sys; sys.path.insert(0, sys.argv[1])
import lab2
L = lab2.Lab("pre", lab2.CFGS["C3"]); L.build(); ok = L.steady(90)
if not ok:
    print("FALHA: lab2 nao estabilizou. nfull UPA/UBS =", L.nfull(L.upa), L.nfull(L.ubs), "via =", L.via())
    print(lab2.ns(L.upa, "ps aux | grep -E 'zebra|ospfd' | grep -v grep").stdout)
L.stop(); sys.exit(0 if ok else 1)
PY
echo "Teste previo do Lab 2 OK"
trap 'touch "$OUT/.fim"' EXIT
mkdir -p "$OUT/andamento-lab2"
( ( while [ ! -f "$OUT/.fim" ]; do cp -r "$L2HOME/logs" "$L2HOME/dados2" "$OUT/andamento-lab2/" 2>/dev/null; sleep 30; done ) & )
for c in C1 C2 C3 C4 C5; do
  n=20; [ $c = C1 ] && n=16; [ $c = C2 ] && n=14
  for i in $(seq 1 $n); do
    p=A; sd=$([ $((i % 2)) = 1 ] && echo UPA || echo UBS); fb=$([ $c = C1 -o $c = C2 ] && echo 0 || echo 1)
    lab2 one $c $i $p $sd $fb &
    [ $((i % J)) = 0 ] && wait
  done; wait
  for i in 101 102 103; do lab2 one $c $i B $([ $((i % 2)) = 1 ] && echo UPA || echo UBS) $fb & done; wait
done
unshare -mn python3 "$L2HOME/lab2.py" verif > "$OUT/dados2/lab2-verif.json" 2>>"$L2HOME/logs/lab2.log"
unshare -mn python3 "$L2HOME/lab2.py" veriftun > "$OUT/dados2/lab2-verif-tunel.json" 2>>"$L2HOME/logs/lab2.log"
for i in $(seq 1 5); do lab2 qos1 $i; done
for i in 1 2 3 4; do lab2 micro C5 $i 50,100,150,200,250,300,350,400; lab2 micro C4 $i 300,400,500,600,700,800,900,1000; lab2 micro C3 $i 500,1000,1500,2000,2500,3000; done
unshare -mn python3 "$L2HOME/lab2.py" consolida >> "$L2HOME/logs/lab2.log" 2>&1
cp "$L2HOME"/dados2/lab2-*.csv "$OUT/dados2/" 2>/dev/null; cp -r "$L2HOME/logs" "$OUT/lab2-logs"
echo "== Analise =="
cd "$AQUI"
{ python3 -I analisa-testes.py --dados dados
  [ -z "${SO_LAB2:-}" ] && { echo; echo "######## LAB 1 (sem tunel)"; python3 -I analisa-lab.py --dados "$OUT/dados" --pref lab-; }
  echo; echo "######## LAB 2 (tunel GRE sobre ESP)"; python3 -I analisa-lab.py --dados "$OUT/dados2" --pref lab2-
} > "$OUT/relatorio.txt" 2>&1
sha256sum "$OUT"/dados/*.csv "$OUT"/dados2/* > "$OUT/integridade-sha256.txt" 2>/dev/null
echo "Pronto: $OUT/relatorio.txt"
