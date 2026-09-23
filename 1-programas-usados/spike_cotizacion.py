#!/usr/bin/env python3
"""
Generador de carga de MODELO ABIERTO para el ASR "(ASR) Autoescalado agil de
cotizacion ante campanas de socios" (Scalability, Critical):

    8.3 TPS -> 830 TPS (x100) en 60 s de rampa, con respuesta esperada de
    500 ms bajo carga maxima.

Diferencia con load_test.py (modelo cerrado): load_test.py mantiene N usuarios
en bucle y mide la capacidad que el sistema alcanza; este script fija la TASA
DE LLEGADA (TPS objetivo) y la sostiene pase lo que pase, que es la unica
forma de reproducir un pico de campana: el socio no deja de mandar trafico
porque nosotros nos pongamos lentos.

Perfil de carga (tres fases consecutivas):

    |<-- baseline -->|<-- ramp-up -->|<------ steady ------>|
     8.3 TPS            8.3 -> 830     830 TPS sostenidos

Salidas (con --out-prefix P, en este mismo directorio):
    P_raw.csv       una fila por peticion (para reprocesar lo que sea)
    P_seconds.csv   agregado por segundo: TPS real, p50/p95/p99, errores
                    -> es el insumo de la grafica del pico
    P_summary.json  resumen + veredicto del ASR

Uso tipico:
    # Campana C -- el pico x100 del ASR
    python spike_cotizacion.py --base-url http://<DNS_ALB> \\
        --baseline-rate 8.3 --baseline-duration 120 \\
        --rate 830 --ramp-up 60 --duration 300 --socios 10 \\
        --out-prefix C_pico_r1

    # Campana A -- barrido de capacidad fija (sin fase de baseline)
    python spike_cotizacion.py --base-url http://<DNS_ALB> \\
        --rate 400 --ramp-up 10 --duration 60 --socios 1 --out-prefix A_N3_400

Requiere haber corrido seed_load_test.py antes (usa cliente_ids.txt y los
socio_*_id.txt que deja el seeder). Generado con ayuda de un agente de IA;
el prompt queda documentado en README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).parent
TICK_S = 0.005  # resolucion del planificador de llegadas


@dataclass
class Muestra:
    t_rel: float  # segundos desde el inicio de la corrida (inicio de la peticion)
    epoch: float  # time.time() del envio; se convierte a ISO al escribir el CSV
    socio_idx: int
    status_code: int | None
    latency_ms: float
    error: str  # "" si fue exitosa


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def leer_lineas(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]


def percentil(valores: list[float], pct: float) -> float:
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    k = max(0, min(len(ordenados) - 1, int(round(pct / 100 * len(ordenados))) - 1))
    return ordenados[k]


def clasificar_error(status: int | None, exc_name: str) -> str:
    """Los 408 son la violacion del ASR (LatencyBudget(500) los genera), los
    429 son la cuota del socio agotada y los 5xx son fallas reales. Mezclarlos
    en un solo 'error %' hace imposible interpretar la corrida."""
    if exc_name:
        return exc_name
    if status is None:
        return "sin_respuesta"
    if status == 408:
        return "http_408_presupuesto"
    if status == 429:
        return "http_429_cuota"
    if 500 <= status < 600:
        return "http_5xx"
    if 400 <= status < 500:
        return f"http_{status}"
    return ""


def cargar_socios(args: argparse.Namespace) -> list[str]:
    if args.socio_id:
        return [args.socio_id]
    if args.socios_file:
        ids = leer_lineas(Path(args.socios_file))
        if not ids:
            sys.exit(f"[error] {args.socios_file} esta vacio o no existe")
    else:
        # Orden deliberado: 'principal' y 'control' tienen cuota alta; el de
        # 'aislamiento' (limite 50) se deja fuera para no contaminar la
        # corrida con 429 -- ese socio existe para el experimento de bulkhead.
        candidatos = ["socio_ids.txt", "socio_principal_id.txt", "socio_control_id.txt"]
        ids = []
        for nombre in candidatos:
            ids.extend(leer_lineas(HERE / nombre))
        if not ids:
            sys.exit(
                "[error] no encontre ids de socio. Corra seed_load_test.py primero, "
                "o pase --socio-id / --socios-file."
            )
    vistos: list[str] = []
    for s in ids:  # sin duplicados, conservando el orden
        if s not in vistos:
            vistos.append(s)
    if args.socios > len(vistos):
        print(
            f"[aviso] pidio --socios {args.socios} pero solo hay {len(vistos)} ids "
            f"disponibles; se usaran {len(vistos)}. Para la hipotesis H2 (1 socio vs "
            f"10 socios) siembre mas socios y pase --socios-file socio_ids.txt."
        )
    return vistos[: args.socios] if args.socios > 0 else vistos


# --------------------------------------------------------------------------
# Planificacion de llegadas (modelo abierto)
# --------------------------------------------------------------------------
def llegadas_acumuladas(t: float, b: float, d0: float, r: float, ramp: float) -> float:
    """Numero de peticiones que YA deberian haberse enviado en el instante t.
    Integral de la tasa: constante b en [0,d0), rampa lineal b->r en
    [d0,d0+ramp) y constante r a partir de ahi."""
    if t <= 0:
        return 0.0
    if t <= d0:
        return b * t
    acum = b * d0
    if t <= d0 + ramp:
        dt = t - d0
        pendiente = (r - b) / ramp if ramp > 0 else 0.0
        return acum + b * dt + pendiente * dt * dt / 2
    acum += (b + r) / 2 * ramp if ramp > 0 else 0.0
    return acum + r * (t - d0 - ramp)


# --------------------------------------------------------------------------
# Envio
# --------------------------------------------------------------------------
async def enviar(
    client: httpx.AsyncClient,
    cuerpo: bytes,
    cabeceras: dict,
    socio_idx: int,
    t_inicio: float,
    muestras: list[Muestra],
) -> None:
    t_rel = time.monotonic() - t_inicio
    ts = time.time()
    t0 = time.monotonic()
    status: int | None = None
    exc_name = ""
    try:
        resp = await client.post("/cotizaciones", content=cuerpo, headers=cabeceras)
        status = resp.status_code
    except httpx.TimeoutException:
        exc_name = "timeout"
    except httpx.ConnectError:
        exc_name = "connection_error"
    except httpx.HTTPError as exc:
        exc_name = f"http_error:{type(exc).__name__}"
    except RuntimeError:
        exc_name = "cliente_cerrado"
    latencia = (time.monotonic() - t0) * 1000
    muestras.append(
        Muestra(t_rel, ts, socio_idx, status, latencia, clasificar_error(status, exc_name))
    )


async def trabajador(
    cola: asyncio.Queue,
    client: httpx.AsyncClient,
    cuerpos: list[bytes],
    cabeceras: list[dict],
    t_inicio: float,
    muestras: list[Muestra],
) -> None:
    """Worker persistente. Crear una Task por peticion (el diseno anterior)
    costaba tanta CPU que el generador topaba en ~170 TPS por nucleo: cuando
    se atrasaba, acumulaba miles de tasks pendientes y el event loop se iba
    en administrarlas. Con un pool fijo, el costo por peticion es un get() de
    cola y el envio."""
    while True:
        item = await cola.get()
        if item is None:
            cola.task_done()
            return
        i, idx = item
        await enviar(client, cuerpos[i % len(cuerpos)], cabeceras[idx], idx, t_inicio, muestras)
        cola.task_done()


async def correr(args: argparse.Namespace) -> tuple[list[Muestra], float, int]:
    clientes = leer_lineas(Path(args.clientes_file) if Path(args.clientes_file).is_absolute()
                           else HERE / args.clientes_file)
    if not clientes:
        sys.exit(f"[error] no hay clientes en {args.clientes_file}; corra seed_load_test.py")
    socios = cargar_socios(args)

    total_s = args.baseline_duration + args.ramp_up + args.duration
    esperadas = llegadas_acumuladas(
        total_s, args.baseline_rate, args.baseline_duration, args.rate, args.ramp_up
    )
    print(
        f"Perfil: {args.baseline_rate} TPS x {args.baseline_duration}s -> rampa "
        f"{args.ramp_up}s -> {args.rate} TPS x {args.duration}s\n"
        f"Socios: {len(socios)} | clientes: {len(clientes)} | "
        f"peticiones programadas: ~{esperadas:,.0f} | duracion: {total_s:.0f}s"
    )

    # Precalculado una sola vez: serializar el JSON y armar las cabeceras en
    # cada peticion costaba mas CPU que la propia E/S y topaba el generador
    # en ~170 TPS por nucleo (medido con `top` durante una corrida).
    cuerpos = [
        json.dumps({
            "clienteId": c,
            "tipoSeguro": args.tipo_seguro,
            "montoAsegurado": args.monto,
            "moneda": args.moneda,
        }).encode()
        for c in clientes
    ]
    cabeceras = [{"x-socio-id": s, "content-type": "application/json"} for s in socios]

    muestras: list[Muestra] = []
    limits = httpx.Limits(
        max_connections=args.max_inflight, max_keepalive_connections=args.max_inflight
    )
    cola: asyncio.Queue = asyncio.Queue(maxsize=args.max_inflight)
    enviadas = 0
    atraso_max = 0      # cuanto llego a quedarse atras el generador (peticiones)
    descartadas = 0     # peticiones que el generador no pudo ni encolar

    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=args.timeout, limits=limits
    ) as client:
        t_inicio = time.monotonic()
        pool = [
            asyncio.create_task(trabajador(cola, client, cuerpos, cabeceras, t_inicio, muestras))
            for _ in range(args.workers)
        ]
        proximo_reporte = args.report_every
        while True:
            ahora = time.monotonic() - t_inicio
            if ahora >= total_s:
                break

            objetivo = llegadas_acumuladas(
                ahora, args.baseline_rate, args.baseline_duration, args.rate, args.ramp_up
            )
            pendientes = int(objetivo) - enviadas
            atraso_max = max(atraso_max, pendientes)
            # No disparamos una rafaga gigante por un atasco momentaneo: el
            # plan es acumulativo, asi que lo que no sale en este tick sale en
            # el siguiente. `atraso_max` deja constancia de cuanto nos atrasamos.
            pendientes = min(pendientes, args.max_burst)

            if cola.full():
                # El servidor no responde al ritmo al que el plan exige enviar.
                # Seguir encolando tareas solo hace crecer la memoria del
                # generador: se descartan y se reportan. `enviadas` avanza
                # igual, porque el plan es acumulativo: sin esto, el mismo
                # atraso se vuelve a contar en cada tick (5 ms) y el numero
                # de descartadas se dispara a cientos de miles.
                descartadas += max(0, pendientes)
                enviadas += max(0, pendientes)
                pendientes = 0

            for _ in range(max(0, pendientes)):
                try:
                    cola.put_nowait((enviadas, enviadas % len(socios)))
                except asyncio.QueueFull:
                    descartadas += 1
                enviadas += 1

            if ahora >= proximo_reporte:
                ventana = [m for m in muestras if m.t_rel >= ahora - args.report_every]
                lat = [m.latency_ms for m in ventana]
                errs = sum(1 for m in ventana if m.error)
                print(
                    f"  t={ahora:6.1f}s  objetivo={objetivo:8.0f}  enviadas={enviadas:8d}  "
                    f"ventana: {len(ventana)/args.report_every:7.1f} TPS  "
                    f"p95={percentil(lat, 95):7.1f} ms  errores={errs:5d}  "
                    f"en cola={cola.qsize()}"
                )
                proximo_reporte += args.report_every

            await asyncio.sleep(TICK_S)

        print(f"Fin de la inyeccion. Drenando {cola.qsize()} peticiones en cola ...")
        try:
            await asyncio.wait_for(cola.join(), timeout=args.timeout + 10)
        except asyncio.TimeoutError:
            print(f"  quedaron {cola.qsize()} sin enviar; se descartan")
        for _ in pool:
            cola.put_nowait(None)
        await asyncio.gather(*pool, return_exceptions=True)
        pared = time.monotonic() - t_inicio

    return muestras, pared, {"atraso_max": atraso_max, "descartadas": descartadas,
                             "programadas": int(objetivo), "enviadas": enviadas}


# --------------------------------------------------------------------------
# Resultados
# --------------------------------------------------------------------------
def agregar_por_segundo(muestras: list[Muestra]) -> list[dict]:
    por_seg: dict[int, list[Muestra]] = defaultdict(list)
    for m in muestras:
        por_seg[int(m.t_rel)].append(m)
    filas = []
    for seg in sorted(por_seg):
        grupo = por_seg[seg]
        lat = [m.latency_ms for m in grupo]
        conteo: dict[str, int] = defaultdict(int)
        for m in grupo:
            if m.error:
                conteo[m.error] += 1
        filas.append(
            {
                "t_rel_s": seg,
                "peticiones": len(grupo),
                "tps": len(grupo),
                "ok": len(grupo) - sum(conteo.values()),
                "err_408_presupuesto": conteo.get("http_408_presupuesto", 0),
                "err_429_cuota": conteo.get("http_429_cuota", 0),
                "err_5xx": conteo.get("http_5xx", 0),
                "err_conexion": conteo.get("connection_error", 0) + conteo.get("timeout", 0),
                "p50_ms": round(percentil(lat, 50), 2),
                "p95_ms": round(percentil(lat, 95), 2),
                "p99_ms": round(percentil(lat, 99), 2),
            }
        )
    return filas


def calcular_ttr(filas: list[dict], t_pico: float, umbral_ms: float, sostener_s: int) -> float | None:
    """TTR: segundos desde el inicio de la rampa hasta que el p95 por segundo
    vuelve por debajo del umbral y se mantiene `sostener_s` segundos seguidos.
    None = nunca se recupero dentro de la corrida."""
    seguidos = 0
    for fila in filas:
        if fila["t_rel_s"] < t_pico:
            continue
        if fila["p95_ms"] <= umbral_ms and fila["peticiones"] > 0:
            seguidos += 1
            if seguidos >= sostener_s:
                return round(fila["t_rel_s"] - sostener_s + 1 - t_pico, 1)
        else:
            seguidos = 0
    return None


def resumir(
    muestras: list[Muestra], filas: list[dict], args: argparse.Namespace,
    pared: float, gen: dict,
) -> dict:
    t_pico = args.baseline_duration
    t_steady = args.baseline_duration + args.ramp_up
    steady = [m for m in muestras if m.t_rel >= t_steady]
    lat_steady = [m.latency_ms for m in steady]
    lat_todo = [m.latency_ms for m in muestras]

    conteo: dict[str, int] = defaultdict(int)
    for m in steady:
        if m.error:
            conteo[m.error] += 1
    errores_steady = sum(conteo.values())
    # Los 429 son cuota agotada: un defecto del montaje de la prueba, no del
    # sistema bajo prueba. Se reportan aparte para no inflar el error % del ASR.
    err_pct = round(100 * errores_steady / len(steady), 2) if steady else 0.0
    tps_steady = round(len(steady) / args.duration, 2) if args.duration else 0.0
    p95_steady = round(percentil(lat_steady, 95), 2)

    cumple = (
        p95_steady <= args.asr_p95
        and err_pct <= args.asr_error_pct
        and tps_steady >= args.rate * args.asr_tolerancia_tps
    )

    return {
        "configuracion": {
            "base_url": args.base_url,
            "baseline_rate": args.baseline_rate,
            "baseline_duration_s": args.baseline_duration,
            "rate_objetivo": args.rate,
            "ramp_up_s": args.ramp_up,
            "duration_s": args.duration,
            "socios": args.socios,
            "max_inflight": args.max_inflight,
        },
        "totales": {
            "peticiones": len(muestras),
            "duracion_pared_s": round(pared, 2),
            "tps_promedio_global": round(len(muestras) / pared, 2) if pared else 0.0,
            "generador": gen,
            "p50_ms": round(percentil(lat_todo, 50), 2),
            "p95_ms": round(percentil(lat_todo, 95), 2),
            "p99_ms": round(percentil(lat_todo, 99), 2),
            "max_ms": round(max(lat_todo), 2) if lat_todo else 0.0,
            "desv_std_ms": round(statistics.pstdev(lat_todo), 2) if len(lat_todo) > 1 else 0.0,
        },
        "carga_maxima": {  # la ventana que evalua el ASR
            "ventana": f"t >= {t_steady:.0f}s",
            "peticiones": len(steady),
            "tps_alcanzado": tps_steady,
            "p50_ms": round(percentil(lat_steady, 50), 2),
            "p95_ms": p95_steady,
            "p99_ms": round(percentil(lat_steady, 99), 2),
            "error_pct": err_pct,
            "errores_por_tipo": dict(conteo),
        },
        "tiempos_del_pico": {
            "inicio_rampa_s": t_pico,
            "ttr_s": calcular_ttr(filas, t_pico, args.asr_p95, args.ttr_sostener),
            "nota": "TTR = segundos desde el inicio de la rampa hasta que el p95 por "
                    "segundo vuelve bajo el umbral y se sostiene. El TTS (instancia "
                    "nueva healthy) sale de watch_scaling.sh, no de aqui.",
        },
        "asr": {
            "p95_objetivo_ms": args.asr_p95,
            "error_pct_objetivo": args.asr_error_pct,
            "tps_objetivo": args.rate,
            "cumple": cumple,
        },
    }


def escribir_salidas(
    muestras: list[Muestra], filas: list[dict], resumen: dict, prefijo: str
) -> None:
    raw = HERE / f"{prefijo}_raw.csv"
    with raw.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_rel_s", "timestamp_iso", "socio_idx", "status_code", "latency_ms", "error"])
        for m in muestras:
            w.writerow([
                round(m.t_rel, 3),
                datetime.fromtimestamp(m.epoch, timezone.utc).isoformat(),
                m.socio_idx,
                m.status_code or "", round(m.latency_ms, 3), m.error,
            ])

    segundos = HERE / f"{prefijo}_seconds.csv"
    with segundos.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0].keys()) if filas else ["t_rel_s"])
        w.writeheader()
        w.writerows(filas)

    resumen_path = HERE / f"{prefijo}_summary.json"
    resumen_path.write_text(json.dumps(resumen, indent=2, ensure_ascii=False))
    print(f"\nSalidas: {raw.name} | {segundos.name} | {resumen_path.name}")


def imprimir_resumen(resumen: dict, args: argparse.Namespace) -> None:
    cm = resumen["carga_maxima"]
    print("\n" + "=" * 68)
    print("RESUMEN -- ventana de carga maxima (la que evalua el ASR)")
    print("=" * 68)
    print(f"  TPS alcanzado : {cm['tps_alcanzado']}  (objetivo {resumen['asr']['tps_objetivo']})")
    print(f"  p50 / p95 / p99 : {cm['p50_ms']} / {cm['p95_ms']} / {cm['p99_ms']} ms")
    print(f"  Error %       : {cm['error_pct']}")
    if cm["errores_por_tipo"]:
        for tipo, n in sorted(cm["errores_por_tipo"].items(), key=lambda x: -x[1]):
            print(f"      {tipo:26s} {n}")
    ttr = resumen["tiempos_del_pico"]["ttr_s"]
    print(f"  TTR           : {ttr if ttr is not None else 'no se recupero en la corrida'}")
    gen = resumen["totales"]["generador"]
    if gen["descartadas"] or gen["atraso_max"] > args.max_burst_ref:
        print(f"  [aviso] generador: atraso maximo {gen['atraso_max']} peticiones, "
              f"{gen['descartadas']} descartadas (programadas {gen['programadas']}, "
              f"enviadas {gen['enviadas']}). Si esto es grande, el cuello de botella "
              f"puede ser su equipo: reparta la carga entre dos maquinas.")
    print(f"\n  ASR: {'CUMPLE' if resumen['asr']['cumple'] else 'NO CUMPLE'}")
    print("=" * 68)


# --------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description="Generador de modelo abierto (tasa fija) para POST /cotizaciones",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--base-url", default="http://localhost:3000")
    p.add_argument("--rate", type=float, required=True, help="TPS objetivo de la fase sostenida")
    p.add_argument("--ramp-up", type=float, default=60.0, help="Segundos de rampa hasta --rate")
    p.add_argument("--duration", type=float, default=300.0, help="Segundos sosteniendo --rate")
    p.add_argument("--baseline-rate", type=float, default=0.0, help="TPS de operacion normal antes del pico")
    p.add_argument("--baseline-duration", type=float, default=0.0, help="Segundos de la fase de operacion normal")
    p.add_argument("--socios", type=int, default=1, help="Cuantos socios distintos rotar (H2)")
    p.add_argument("--socios-file", help="Archivo con un id de socio por linea")
    p.add_argument("--socio-id", help="Forzar un unico socio por id")
    p.add_argument("--clientes-file", default="cliente_ids.txt")
    p.add_argument("--tipo-seguro", default="viaje")
    p.add_argument("--monto", type=int, default=3_000_000)
    p.add_argument("--moneda", default="COP")
    p.add_argument("--timeout", type=float, default=10.0, help="Timeout por peticion (s)")
    p.add_argument("--workers", type=int, default=400, help="Workers persistentes que envian en paralelo")
    p.add_argument("--max-inflight", type=int, default=3000, help="Tope de peticiones simultaneas del generador")
    p.add_argument("--max-burst", type=int, default=800, help="Tope de peticiones disparadas por tick")
    p.add_argument("--report-every", type=float, default=10.0, help="Segundos entre reportes en pantalla")
    p.add_argument("--asr-p95", type=float, default=500.0, help="Umbral de p95 del ASR (ms)")
    p.add_argument("--asr-error-pct", type=float, default=2.0)
    p.add_argument("--asr-tolerancia-tps", type=float, default=0.95, help="Fraccion del TPS objetivo que se considera sostenido")
    p.add_argument("--ttr-sostener", type=int, default=5, help="Segundos seguidos bajo el umbral para declarar recuperacion")
    p.add_argument("--out-prefix", default="spike")
    args = p.parse_args()
    args.max_burst_ref = args.max_burst  # umbral para avisar de atraso del generador

    muestras, pared, gen = asyncio.run(correr(args))
    if not muestras:
        print("[error] no se registro ninguna peticion")
        return 1
    muestras.sort(key=lambda m: m.t_rel)
    filas = agregar_por_segundo(muestras)
    resumen = resumir(muestras, filas, args, pared, gen)
    escribir_salidas(muestras, filas, resumen, args.out_prefix)
    imprimir_resumen(resumen, args)
    return 0 if resumen["asr"]["cumple"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
