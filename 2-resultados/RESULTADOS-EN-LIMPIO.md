# Resultados en limpio

Promedio de las repeticiones de cada corrida. **Cumple** significa que respondió en menos de
medio segundo y falló menos del 2 % de las veces.


## Etapa A · cuántos servidores

| Servidores | Carga pedida | Socios | Veces | Atendió | Tiempo típico | 95 de cada 100 | Falladas | ¿Cumple? |
|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| 1 | 100/s | 2 | 3 | 96/s | 23 ms | 27 ms | 0.0 % | sí |
| 1 | 200/s | 2 | 3 | 200/s | 25 ms | 30 ms | 0.0 % | sí |
| 1 | 300/s | 2 | 3 | 296/s | 839 ms | 1.4 s | 76.3 % | **no** |
| 1 | 400/s | 2 | 3 | 400/s | 4.3 s | 7.7 s | 100.0 % | **no** |
| 2 | 200/s | 2 | 3 | 200/s | 30 ms | 35 ms | 0.0 % | sí |
| 2 | 400/s | 2 | 3 | 400/s | 30 ms | 38 ms | 0.0 % | sí |
| 2 | 600/s | 2 | 3 | 599/s | 969 ms | 8.9 s | 63.1 % | **no** |
| 2 | 800/s | 2 | 3 | 785/s | 3.3 s | 13.2 s | 69.7 % | **no** |
| 4 | 400/s | 2 | 3 | 400/s | 29 ms | 54 ms | 0.0 % | sí |
| 4 | 600/s | 2 | 3 | 598/s | 1.1 s | 9.6 s | 55.8 % | **no** |
| 4 | 800/s | 2 | 3 | 789/s | 2.9 s | 9.3 s | 84.4 % | **no** |
| 4 | 1000/s | 2 | 3 | 914/s | 3.7 s | 12.2 s | 57.5 % | **no** |

## Etapa B · límite por socio

| Servidores | Carga pedida | Socios | Veces | Atendió | Tiempo típico | 95 de cada 100 | Falladas | ¿Cumple? |
|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| 4 | 600/s | 1 | 3 | 595/s | 4.3 s | 11.3 s | 89.9 % | **no** |
| 4 | 600/s | 10 | 3 | 600/s | 38 ms | 2.7 s | 0.5 % | **no** |

## Etapa C · el pico del escenario

| Servidores | Carga pedida | Socios | Veces | Atendió | Tiempo típico | 95 de cada 100 | Falladas | ¿Cumple? |
|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| 4 | 832/s | 2 | 5 | 488/s | 4.3 s | 14.0 s | 33.1 % | **no** |

## Etapa D · sin esperar al disco

| Servidores | Carga pedida | Socios | Veces | Atendió | Tiempo típico | 95 de cada 100 | Falladas | ¿Cumple? |
|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| 4 | 832/s | 2 | 2 | 587/s | 2.8 s | 12.6 s | 6.3 % | **no** |

## Etapa E · repartido en 10 socios

| Servidores | Carga pedida | Socios | Veces | Atendió | Tiempo típico | 95 de cada 100 | Falladas | ¿Cumple? |
|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| 4 | 832/s | 10 | 2 | 603/s | 2.6 s | 12.4 s | 11.8 % | **no** |

## Etapa F · las dos cosas

| Servidores | Carga pedida | Socios | Veces | Atendió | Tiempo típico | 95 de cada 100 | Falladas | ¿Cumple? |
|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| 4 | 832/s | 10 | 2 | 608/s | 2.9 s | 13.0 s | 7.2 % | **no** |
