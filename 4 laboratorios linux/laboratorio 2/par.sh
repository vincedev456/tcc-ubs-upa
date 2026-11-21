#!/bin/sh
# par.sh CFG FB PATH:SIDE:IDX ...   -> roda em paralelo, um unshare por falha, e consolida no fim
#   ex.: ./par.sh C3 1 A:UPA:1 A:UBS:2 B:UPA:3
# Resultado: $LABHOME/dados2/lab2-CFG.csv (parciais em $LABHOME/dados2/parciais/)
LABHOME=${LABHOME:-$HOME/lab}; export LABHOME
LAB2=${LAB2:-$LABHOME/lab2.py}
cfg=$1; fb=$2; shift 2
mkdir -p "$LABHOME/logs"
for t in "$@"; do
  p=${t%%:*}; r=${t#*:}; sd=${r%%:*}; i=${r#*:}
  unshare -rnm python3 "$LAB2" one "$cfg" "$i" "$p" "$sd" "$fb" >> "$LABHOME/logs/$cfg.log" 2>&1 &
done
wait
python3 "$LAB2" consolida
