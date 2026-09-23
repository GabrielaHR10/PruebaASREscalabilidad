#!/bin/bash
# Orquestador del experimento E02 — autoescalado de cotizacion.
#
# Aplica en CADA corrida el protocolo que faltaba en la corrida exploratoria:
#   1. TRUNCATE + VACUUM de las tablas que llena la carga (si no, los indices
#      crecen y las corridas dejan de ser comparables: llegamos a 1,1 M de
#      cotizaciones y el error % se disparo de 35% a 65% sin tocar nada).
#   2. Reset del contador de cuota del socio (si no, aparecen 429 falsos).
#   3. Chequeo de CPUCreditBalance (las t3.medium son burstables: con los
#      creditos agotados la CPU se estrangula y la corrida no sirve).
#   4. Carga repartida entre los 4 generadores (2 procesos cada uno), porque
#      un proceso de Python topa en ~250 TPS por nucleo.
#   5. Descarga inmediata de los resultados y registro en tabla_resultados.csv
#      con las marcas de tiempo, para cruzarlas despues con las metricas del ALB.
#
# Uso:  ./orquestador.sh <fase>     fases: prep | A | B | C | D | todo

set -u
REPO="/Users/hernandez/Documents/sexto semestre/Arquisoft/ISIS2212_202620_S3_Grupo6"
cd "$REPO" || exit 1
source env_solventa.sh
export AWS_REGION=us-east-1
PEM="$HOME/.ssh/labsuser.pem"
SSHO="-o StrictHostKeyChecking=no -o ConnectTimeout=10"
RES="load-tests/resultados-E02/aws"
TABLA="$RES/tabla_resultados.csv"
mkdir -p "$RES"
[ -f "$TABLA" ] || echo "etiqueta,fase,instancias,tps_objetivo,socios,rep,t_inicio,t_fin,tps_total,p50_max_ms,p95_max_ms,error_pct" > "$TABLA"

log() { echo "[$(date +%H:%M:%S)] $*"; }

# ---------------------------------------------------------------- utilidades
sql() { ssh $SSHO -i "$PEM" ubuntu@"$DB_PUB" "sudo docker exec solventa-postgres psql -U postgres -d seguros $*" 2>/dev/null; }

limpiar_bd() {
  sql -c "'TRUNCATE cotizacion.primas, cotizacion.coberturas, cotizacion.cotizaciones, perfilamiento.scores_credito, perfilamiento.perfiles_riesgo, outbox.eventos CASCADE;'" >/dev/null
  sql -c "'UPDATE distribucion.cuotas_trafico SET \"solicitudesConsumidas\" = 0;'" >/dev/null
  sql -c "'VACUUM ANALYZE;'" >/dev/null
}

esperar_creditos() {   # no arranca una corrida con la instancia estrangulada
  # Si la instancia esta en modo `unlimited` nunca se estrangula (paga el
  # excedente), y CloudWatch deja de publicar un balance util: la verificacion
  # no aplica. Solo tiene sentido en modo `standard`.
  local modo
  modo=$(aws ec2 describe-instance-credit-specifications --instance-ids "$DB_ID" \
         --query 'InstanceCreditSpecifications[0].CpuCredits' --output text 2>/dev/null)
  [ "$modo" = "unlimited" ] && return 0
  for i in $(seq 1 20); do
    local c
    c=$(aws cloudwatch get-metric-statistics --namespace AWS/EC2 --metric-name CPUCreditBalance \
        --dimensions Name=InstanceId,Value="$DB_ID" --start-time "$(date -u -v-15M +%Y-%m-%dT%H:%M:%SZ)" \
        --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --period 300 --statistics Minimum \
        --query 'sort_by(Datapoints,&Timestamp)[-1].Minimum' --output text 2>/dev/null)
    c=${c%%.*}; case "$c" in ""|None|null) c=100;; esac
    if [ "$c" -ge 30 ] 2>/dev/null; then return 0; fi
    log "  creditos de la BD bajos ($c): esperando 3 min a que se recarguen"
    sleep 180
  done
}

instancias_activas() {   # $1 = cuantas instancias de app deben quedar en el ALB
  local n=$1 todas=("$APP1" "$APP2" "$APP_B" "$APP_C") i
  for i in "${!todas[@]}"; do
    if [ "$i" -lt "$n" ]; then
      aws elbv2 register-targets --target-group-arn "$TG" --targets Id="${todas[$i]}" >/dev/null 2>&1
    else
      aws elbv2 deregister-targets --target-group-arn "$TG" --targets Id="${todas[$i]}" >/dev/null 2>&1
    fi
  done
  for i in $(seq 0 $((n-1))); do
    aws elbv2 wait target-in-service --target-group-arn "$TG" --targets Id="${todas[$i]}" 2>/dev/null
  done
  log "  ALB con $n instancia(s) activa(s)"
}

# correr <etiqueta> <fase> <instancias> <tps_total> <socios> <rep> <perfil>
#   perfil: "plano" (rampa 10s + 60s) o "pico" (120s base + rampa 60s + 300s)
correr() {
  local etq=$1 fase=$2 inst=$3 tps=$4 soc=$5 rep=$6 perfil=$7
  local por=$((tps/8)) args ini fin
  if [ "$perfil" = "pico" ]; then
    args="--baseline-rate $(echo "scale=2; $tps/800" | bc) --baseline-duration 120 --rate $por --ramp-up 60 --duration 300"
  else
    args="--rate $por --ramp-up 10 --duration 60"
  fi
  limpiar_bd
  esperar_creditos
  ini=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  log "  corriendo $etq (${tps} TPS, ${inst} inst, ${soc} socios, rep $rep)"
  for H in $GEN_IPS; do
    for i in 1 2; do
      ssh $SSHO -i "$PEM" ubuntu@"$H" "cd load-tests && ~/venv/bin/python spike_cotizacion.py \
        --base-url http://$ALB_DNS $args --socios $soc --workers 250 \
        --out-prefix resultados-E02/${etq}_${H//./_}_$i > /tmp/${etq}_$i.log 2>&1" &
    done
  done
  wait
  fin=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  # recolectar: sumar TPS, tomar el peor p50/p95 y el error % promedio
  local tmp; tmp=$(mktemp)
  for H in $GEN_IPS; do
    scp -q $SSHO -i "$PEM" "ubuntu@$H:/home/ubuntu/load-tests/resultados-E02/${etq}_*_summary.json" "$RES/" 2>/dev/null
  done
  python3 - "$RES" "$etq" > "$tmp" <<'PY'
import json, glob, sys
res, etq = sys.argv[1], sys.argv[2]
tps=0; p50=0; p95=0; err=[]
for f in glob.glob(f"{res}/{etq}_*_summary.json"):
    d=json.load(open(f))["carga_maxima"]
    tps+=d["tps_alcanzado"]; p50=max(p50,d["p50_ms"]); p95=max(p95,d["p95_ms"]); err.append(d["error_pct"])
print(f"{tps:.1f},{p50:.0f},{p95:.0f},{(sum(err)/len(err) if err else 0):.2f}")
PY
  echo "$etq,$fase,$inst,$tps,$soc,$rep,$ini,$fin,$(cat "$tmp")" >> "$TABLA"
  log "  -> $(cat "$tmp")  (tps_total,p50,p95,error%)"
  rm -f "$tmp"
  sleep 20   # enfriamiento entre corridas
}

# ---------------------------------------------------------------- fases
fase_prep() {
  log "FASE 0 — preparacion"
  for id in $APP1 $APP2 $APP_B $APP_C; do
    ip=$(aws ec2 describe-instances --instance-ids "$id" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
    ssh $SSHO -i "$PEM" ubuntu@"$ip" 'sudo docker restart solventa-api >/dev/null' 2>/dev/null && log "  contenedor reiniciado en $ip"
  done
  limpiar_bd; log "  BD limpia"
  sleep 30
  instancias_activas 4
  curl -s -m 10 "http://$ALB_DNS/admision/health" && echo
  log "  preparacion lista"
}

fase_A() {
  log "FASE A — capacidad segun numero de instancias"
  for n in 1 2 4; do
    instancias_activas $n
    case $n in
      1) niveles="100 200 300 400";;
      2) niveles="200 400 600 800";;
      4) niveles="400 600 800 1000";;
    esac
    for tps in $niveles; do
      for rep in 1 2 3; do
        correr "A_N${n}_${tps}_r${rep}" A "$n" "$tps" 2 "$rep" plano
      done
    done
  done
}

fase_B() {
  log "FASE B — aislamiento por socio (1 vs 10)"
  instancias_activas 4
  for soc in 1 10; do
    for rep in 1 2 3; do
      correr "B_${soc}socios_r${rep}" B 4 600 "$soc" "$rep" plano
    done
  done
}

fase_C() {
  log "FASE C — el pico del ASR (8.3 -> 830 TPS, rampa 60 s)"
  instancias_activas 4
  for rep in 1 2 3; do
    correr "C_pico_r${rep}" C 4 832 2 "$rep" pico
  done
}

fase_D() {
  log "FASE D — mitigaciones"
  instancias_activas 4
  sql -c "'ALTER SYSTEM SET synchronous_commit = off;'" -c "'SELECT pg_reload_conf();'" >/dev/null
  log "  synchronous_commit = off"
  for rep in 1 2; do correr "D_synoff_r${rep}" D 4 832 2 "$rep" pico; done
  sql -c "'ALTER SYSTEM SET synchronous_commit = on;'" -c "'SELECT pg_reload_conf();'" >/dev/null
  log "  synchronous_commit = on (revertido)"
}

fase_E() {
  log "FASE E — contrafactual: el pico del ASR repartido entre 10 socios"
  instancias_activas 4
  for rep in 1 2; do
    correr "E_pico10_r${rep}" E 4 832 10 "$rep" pico
  done
}

fase_F() {
  log "FASE F — las dos mitigaciones juntas: 10 socios + synchronous_commit=off"
  instancias_activas 4
  sql -c "'ALTER SYSTEM SET synchronous_commit = off;'" -c "'SELECT pg_reload_conf();'" >/dev/null
  log "  synchronous_commit = off"
  for rep in 1 2; do correr "F_ambas_r${rep}" F 4 832 10 "$rep" pico; done
  sql -c "'ALTER SYSTEM SET synchronous_commit = on;'" -c "'SELECT pg_reload_conf();'" >/dev/null
  log "  synchronous_commit = on (revertido)"
}

fase_A2() {   # lo que falto de la fase A: N=4 a 800 (reps 2 y 3) y 1000 TPS
  log "FASE A2 — completar N=4 a 800 y 1000 TPS"
  instancias_activas 4
  for rep in 2 3; do correr "A_N4_800_r${rep}" A 4 800 2 "$rep" plano; done
  for rep in 1 2 3; do correr "A_N4_1000_r${rep}" A 4 1000 2 "$rep" plano; done
}

case "${1:-todo}" in
  prep) fase_prep;;
  A) fase_A;;
  B) fase_B;;
  C) fase_C;;
  D) fase_D;;
  E) fase_E;;
  F) fase_F;;
  A2) fase_A2;;
  todo) fase_prep; fase_A; fase_B; fase_C; fase_D;;
  *) echo "uso: $0 [prep|A|B|C|D|todo]"; exit 1;;
esac
log "FIN"
