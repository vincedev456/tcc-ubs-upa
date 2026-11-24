#!/bin/sh
# par.sh CFG FB PATH:SIDE:IDX ...   -> roda as falhas em paralelo (cada uma em seus proprios namespaces) e consolida
#   ex.: sudo ./par.sh C3 1 A:UPA:1 A:UBS:2 B:UPA:3
# Requer root e o pacote frr. Resultado: $LABHOME/dados2/lab2-CFG.csv (parciais em $LABHOME/dados2/parciais/)
[ "$(id -u)" = 0 ] || { echo "precisa ser root (sudo)"; exit 1; }
LABHOME=${LABHOME:-/tmp/tcc-lab2}; export LABHOME
LAB2=${LAB2:-$(dirname "$0")/lab2.py}
cfg=$1; fb=$2; shift 2
mkdir -p "$LABHOME/logs"
for t in "$@"; do
  p=${t%%:*}; r=${t#*:}; sd=${r%%:*}; i=${r#*:}
  python3 "$LAB2" one "$cfg" "$i" "$p" "$sd" "$fb" >> "$LABHOME/logs/$cfg.log" 2>&1 &
done
wait
python3 "$LAB2" consolida
