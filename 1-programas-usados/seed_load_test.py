#!/usr/bin/env python3
"""
Seed de datos para las pruebas de carga de Solventa (JMeter + load_test.py).

A diferencia del seeder de Cheapest (un provider de NestJS que inserta
directo en la base), este script siembra a traves de la API real -- porque
en Solventa cotizar/suscribir dispara reglas de negocio reales (Strategy de
rating, decision de suscripcion, transaccion cross-schema, eventos de
dominio) que no tendria sentido replicar con INSERTs manuales.

Crea:
- 3 socios de distribucion con su CuotaTrafico:
    - "principal": limite alto, se usa en los escenarios de latencia/
      escalabilidad (no se debe agotar durante la prueba).
    - "aislamiento": limite bajo (50), pensado para agotarse a proposito en
      el escenario de aislamiento de carga por socio (ASR de bulkhead).
    - "control": limite alto, tambien para el escenario de aislamiento -- su
      trafico debe mantenerse sano mientras "aislamiento" se satura.
- N clientes (UUID deterministico) con un Consentimiento vigente para
  perfilamiento_riesgo (requisito de negocio antes de poder cotizar).
- M polizas de muestra (POST /cotizaciones + POST /suscripciones) para
  alimentar el escenario de lectura (GET /polizas/:id).

Escribe en este mismo directorio: socio_principal_id.txt,
socio_aislamiento_id.txt, socio_control_id.txt, cliente_ids.txt,
poliza_ids.txt -- los demas scripts (load_test.py, load_test.jmx) los leen.

Uso:
    python seed_load_test.py --base-url http://localhost:3000 --clientes 30 --polizas 15
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # namespace fijo, ids reproducibles
HERE = Path(__file__).parent


def det_uuid(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, name))


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def crear_socio(client: httpx.Client, nombre: str, limite: int) -> str:
    now = datetime.now(timezone.utc)
    resp = client.post(
        "/socios",
        json={
            "nombre": nombre,
            "tipo": "banco",
            "nit": f"900{abs(hash(nombre)) % 1_000_000:06d}-1",
            "emailContacto": f"{nombre.lower().replace(' ', '-')}@loadtest.solventa.co",
            "telefonoContacto": "3000000000",
            "estado": "activo",
            "fechaRegistro": iso(now),
        },
    )
    resp.raise_for_status()
    socio_id = resp.json()["idSocio"]

    resp = client.post(
        "/cuotas-trafico",
        json={
            "limiteSolicitudes": limite,
            "periodo": "dia",
            "solicitudesConsumidas": 0,
            "fechaInicio": iso(now),
            "fechaFin": iso(now + timedelta(days=1)),
            "estado": "activa",
            "socioId": socio_id,
        },
    )
    resp.raise_for_status()
    return socio_id


def otorgar_consentimiento(client: httpx.Client, cliente_id: str) -> None:
    resp = client.post(
        "/consentimientos",
        json={
            "clienteId": cliente_id,
            "proposito": "perfilamiento_riesgo",
            "alcanceDatos": "ingresos,endeudamiento,comportamiento_pago",
            "versionPolitica": "load-test-v1",
        },
    )
    resp.raise_for_status()


def emitir_poliza_muestra(client: httpx.Client, cliente_id: str, socio_id: str) -> str:
    resp = client.post(
        "/cotizaciones",
        headers={"x-socio-id": socio_id},
        json={
            "clienteId": cliente_id,
            "tipoSeguro": "viaje",
            "montoAsegurado": 3_000_000,
            "moneda": "COP",
        },
    )
    resp.raise_for_status()
    cotizacion_id = resp.json()["idCotizacion"]

    resp = client.post(
        "/suscripciones",
        json={"cotizacionId": cotizacion_id, "clienteId": cliente_id},
    )
    resp.raise_for_status()
    body = resp.json()
    poliza = body.get("poliza")
    if not poliza:
        raise RuntimeError(f"Suscripcion no genero poliza (resultado: {body.get('suscripcion', {}).get('resultado')})")
    return poliza["idPoliza"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:3000")
    parser.add_argument("--clientes", type=int, default=30, help="Numero de clientes con consentimiento vigente")
    parser.add_argument("--polizas", type=int, default=15, help="Numero de polizas de muestra para el GET")
    parser.add_argument("--limite-principal", type=int, default=1_000_000)
    parser.add_argument("--limite-aislamiento", type=int, default=50)
    parser.add_argument("--limite-control", type=int, default=1_000_000)
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
        print(f"[1/4] Creando socios (principal, aislamiento, control) en {args.base_url} ...")
        socio_principal = crear_socio(client, "Socio Load Test Principal", args.limite_principal)
        socio_aislamiento = crear_socio(client, "Socio Load Test Aislamiento", args.limite_aislamiento)
        socio_control = crear_socio(client, "Socio Load Test Control", args.limite_control)
        print(f"      principal={socio_principal}")
        print(f"      aislamiento={socio_aislamiento}")
        print(f"      control={socio_control}")

        print(f"[2/4] Otorgando consentimiento a {args.clientes} clientes ...")
        clientes = [det_uuid(f"solventa-load-cliente-{i}") for i in range(args.clientes)]
        for i, cliente_id in enumerate(clientes, start=1):
            otorgar_consentimiento(client, cliente_id)
            if i % 10 == 0 or i == len(clientes):
                print(f"      {i}/{len(clientes)}")

        print(f"[3/4] Emitiendo {args.polizas} polizas de muestra (para el GET) ...")
        polizas: list[str] = []
        for i in range(args.polizas):
            cliente_id = clientes[i % len(clientes)]
            try:
                poliza_id = emitir_poliza_muestra(client, cliente_id, socio_principal)
                polizas.append(poliza_id)
            except httpx.HTTPStatusError as exc:
                print(f"      [aviso] poliza {i + 1} fallo: {exc.response.status_code} {exc.response.text[:200]}")
                continue
            if (i + 1) % 5 == 0 or i + 1 == args.polizas:
                print(f"      {i + 1}/{args.polizas}")

        print("[4/4] Escribiendo archivos de salida ...")
        (HERE / "socio_principal_id.txt").write_text(socio_principal + "\n")
        (HERE / "socio_aislamiento_id.txt").write_text(socio_aislamiento + "\n")
        (HERE / "socio_control_id.txt").write_text(socio_control + "\n")
        (HERE / "cliente_ids.txt").write_text("\n".join(clientes) + "\n")
        (HERE / "poliza_ids.txt").write_text("\n".join(polizas) + "\n")

    print("\nListo. Resumen:")
    print(f"  socios:   principal={socio_principal} | aislamiento={socio_aislamiento} | control={socio_control}")
    print(f"  clientes: {len(clientes)} con consentimiento vigente (cliente_ids.txt)")
    print(f"  polizas:  {len(polizas)} emitidas (poliza_ids.txt)")
    if len(polizas) < args.polizas:
        print(f"  [aviso] solo se emitieron {len(polizas)}/{args.polizas} polizas, revise los avisos arriba")
    return 0


if __name__ == "__main__":
    sys.exit(main())
