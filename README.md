# Prueba del ASR de escalabilidad — Solventa

Este repositorio contiene **todo lo que hicimos para probar si el sistema aguanta el pico de una
campaña de socios**: los programas que usamos, los datos que obtuvimos y el análisis.

**Resultado corto: no aguanta.** El objetivo era atender **830 cotizaciones por segundo**
respondiendo en menos de medio segundo. El sistema llegó a **488 por segundo** y rechazó una de
cada tres peticiones.

---

## 1. Qué queríamos probar

El escenario dice que **un banco aliado lanza una promoción** y su tráfico se multiplica por cien:
pasa de 8 cotizaciones por segundo a 830, en menos de un minuto. La idea que queríamos comprobar
era simple:

> Si la infraestructura **agrega servidores automáticamente** cuando sube la demanda, debería poder
> atender ese pico sin que los clientes noten la diferencia.

La prueba se hizo sobre `POST /cotizaciones`, que es el camino completo: verifica el permiso del
cliente, calcula su perfil de riesgo, aplica las reglas de precio y guarda la cotización.

**Cómo sabemos si pasa o falla:** el sistema corta cualquier cotización que tarde más de medio
segundo y responde con un error. Entonces, si el sistema no alcanza, **lo vemos como porcentaje de
peticiones rechazadas**, no como respuestas lentas.

---

## 2. Cómo montamos la prueba

### Las máquinas

| Para qué | Cuántas | Qué son |
|---|---|---|
| Base de datos | 1 | PostgreSQL, disco de 20 GB |
| Aplicación | 1 a 4 | La aplicación de Solventa, una por zona |
| Repartidor de tráfico | 1 | Balanceador que distribuye entre los servidores |
| Generadores de carga | 4 | Máquinas que simulan a los socios mandando cotizaciones |

Todas en Amazon (`us-east-1`), del mismo tamaño (`t3.medium`).


### La carga de datos (antes de medir)

Una cotización no se puede crear de la nada: necesita un cliente con permiso vigente y un socio con
cuota disponible. Por eso, **antes de empezar, sembramos datos de prueba** con el programa
`seed_load_test.py`, que los crea **usando la aplicación real** (no metiendo filas a mano en la
base), para que pasen por las mismas reglas de negocio que vamos a probar:

- **200 clientes**, cada uno con su consentimiento vigente — sin eso, el sistema rechaza la
  cotización por falta de permiso.
- **20 pólizas** de muestra.
- **13 socios** con cuota alta (50 millones de solicitudes), para que la prueba no se detenga porque
  un socio se quedó sin cupo.

Durante la prueba, cada cotización se manda a nombre de un cliente y un socio de esa lista, rotando
entre ellos.

**Y algo importante:** antes de **cada** corrida borramos las cotizaciones que dejó la anterior. 
---

## 3. Las etapas de la prueba

La prueba se diseñó como un **descarte por etapas**: cada una elimina un sospechoso, para que la
conclusión no dependa de una sola medición.

### Etapa A — ¿Cuánto aguanta según cuántos servidores tenga?

| Parámetro | Valor |
|---|---|
| Servidores | 1, luego 2, luego 4 |
| Carga probada | 100, 200, 300, 400, 600, 800 y 1000 por segundo, según el caso |
| Duración | 10 segundos de arranque + 60 segundos sosteniendo la carga |
| Socios | 2 |
| Repeticiones | 3 de cada una |

**Para qué:** es la etapa que decide todo. Si al agregar servidores la capacidad sube, entonces el
crecimiento automático sirve y solo hay que hacerlo rápido. Si no sube, el problema está en otro
lado.

### Etapa B — ¿El límite por socio frena el sistema?

| Parámetro | Valor |
|---|---|
| Servidores | 4 |
| Carga | 600 por segundo |
| Socios | 1 en unas corridas, 10 en otras |
| Repeticiones | 3 de cada una |

**Para qué:** el sistema lleva un contador de cuántas solicitudes usó cada socio, y ese contador
vive en **un solo renglón** de la base. Queríamos ver si todas las peticiones de un mismo socio
tienen que hacer fila para actualizarlo.

### Etapa C — El pico real del escenario

| Parámetro | Valor |
|---|---|
| Servidores | 4 |
| Perfil | 2 minutos a 8 por segundo → sube a 832 en 1 minuto → 5 minutos sosteniendo 832 |
| Socios | 2 |
| Repeticiones | 5 |

**Para qué:** las otras etapas usan carga constante. Esta reproduce el escenario tal como está
escrito: operación normal, subidón repentino y aguante. **Es la prueba del ASR propiamente dicha.**

### Etapa D — ¿Mejora si la base no espera al disco?

| Parámetro | Valor |
|---|---|
| Cambio | La base confirma las operaciones sin esperar a escribirlas en disco |
| Todo lo demás | Igual que la etapa C |
| Repeticiones | 2 |

**Para qué:** habíamos visto que las peticiones esperaban por la escritura a disco. Esta etapa
convierte esa sospecha en una prueba: si al quitar la espera el sistema mejora, era eso.

### Etapa E — ¿Y si no existiera el límite por socio?

| Parámetro | Valor |
|---|---|
| Socios | 10 en lugar de 2 |
| Todo lo demás | Igual que la etapa C |
| Repeticiones | 2 |

**Para qué:** repartir el tráfico entre 10 socios es como abrir 10 puertas en vez de una. Permite
medir cuánto pesaba ese freno, sin cambiar el programa.

### Etapa F — ¿Alcanza con arreglar las dos cosas?

| Parámetro | Valor |
|---|---|
| Cambios | Los dos anteriores al tiempo: 10 socios y sin esperar al disco |
| Repeticiones | 2 |

**Para qué:** si arreglando los dos frenos conocidos el sistema sigue sin llegar a 830, es que hay
un tercer problema más de fondo.

### Además: cuánto tarda en aparecer un servidor nuevo

Aparte de las corridas, cronometramos el ciclo completo de encender un servidor: desde que se pide
hasta que empieza a atender clientes de verdad.

**En total: 53 corridas, 2 horas y 41 minutos de carga efectiva.**

---

## 4. Lo que encontramos

### Agregar servidores no agrega capacidad

| Servidores | Cuánto aguanta cumpliendo la meta |
|---|---|
| 1 | 200 por segundo |
| 2 | 400 por segundo |
| **4** | **400 por segundo** |

De 1 a 2 servidores la capacidad se duplicó. **De 2 a 4 no cambió nada.** Y mientras el sistema se
ahogaba, los servidores estaban al **30 % de uso**: no les faltaba potencia, estaban esperando.

### Un solo socio no puede crecer

A la misma carga de 600 por segundo, con la misma infraestructura:

| | Tiempo de respuesta | Peticiones falladas |
|---|---|---|
| Todo el tráfico de **1 socio** | 4,2 segundos | 90 % |
| Repartido entre **10 socios** | 0,038 segundos | 0,5 % |

La diferencia es enorme, y la causa es el contador de cuota: todas las cotizaciones del mismo socio
tienen que actualizar el mismo renglón de la base, una por una.

**Esto es lo más grave para el escenario**, porque el escenario dice que **un socio** lanza la
promoción. Las 830 cotizaciones por segundo vienen todas de él.

### El servidor nuevo llega tarde

**81 segundos** desde que se pide un servidor hasta que atiende clientes (18 segundos en encender,
63 en instalarse y quedar listo). El escenario da **60 segundos** para absorber todo el pico. Y eso
sin contar el tiempo que tarda Amazon en darse cuenta de que el tráfico subió, que es de uno a dos
minutos más.

### Ni arreglando los dos frenos se llega

| Qué probamos | Cotizaciones por segundo | Falladas |
|---|---|---|
| Como está hoy | 488 | 33 % |
| Sin esperar al disco | 587 | 6 % |
| Repartido en 10 socios | 603 | 12 % |
| **Las dos cosas juntas** | **608** | **7 %** |

Los arreglos bajan mucho los errores, pero el techo se queda cerca de **600 por segundo**. Falta
bastante para 830.

---

## 5. Qué significa todo esto

**El cuello de botella es la base de datos, no los servidores.** Todos los servidores comparten una
sola base, y ahí es donde se forma la fila. Por eso agregar servidores no ayuda: es como contratar
más meseros cuando el problema es que hay una sola cocina.

Las peticiones esperan por dos cosas: que el dato se escriba en disco, y por ese renglón único del
contador de cuota.

Pero al quitar esos dos frenos el sistema solo llegó a 608 por segundo. Lo que queda es el problema
de fondo: **cada cotización escribe seis veces en la base**, y eso no se arregla cambiando
configuración.

### Qué recomendamos, en orden

1. **Que la cotización no espere a que se guarde todo.** Guardar lo mínimo, responderle al cliente,
   y hacer el resto en segundo plano. Es lo único que rompe el techo de 608.
2. **Sacar el contador de cuota de ese renglón único**, llevándolo a un sistema en memoria o
   repartiéndolo en varios renglones. Sin esto, ningún socio pasa de unas 300 por segundo.
3. **Tener los servidores listos de antemano** en vez de crearlos cuando llega el pico.
4. **No esperar al disco solo donde se puede:** sirve para cotizaciones, donde perder una ante una
   caída es tolerable; **no sirve para cobros**, donde no se puede perder ni una transacción.

Una aclaración: **poner una segunda base de datos para lectura no ayudaría**, porque el pico es
todo escritura.

---

## 6. Qué hay en cada carpeta

| Carpeta | Qué contiene |
|---|---|
| `1-programas-usados/` | `spike_cotizacion.py` genera la carga · `orquestador.sh` ejecuta las 53 corridas aplicando siempre el mismo procedimiento · `seed_load_test.py` siembra los datos de prueba |
| `2-resultados/` | `tabla_resultados.csv` con las 53 corridas y sus números · `resumenes-por-corrida/` con el detalle de cada proceso · `medicion-segundo-a-segundo/` con cómo evolucionó cada prueba en el tiempo |


### Cómo leer `tabla_resultados.csv`

Una fila por corrida:

| Columna | Qué significa |
|---|---|
| `etiqueta` | Nombre de la corrida (etapa, servidores, carga, repetición) |
| `instancias` | Cuántos servidores de aplicación estaban activos |
| `tps_objetivo` | Cuántas cotizaciones por segundo le pedimos |
| `socios` | Entre cuántos socios se repartió |
| `tps_total` | Cuántas alcanzó a atender de verdad |
| `p50_max_ms` | La mitad de las peticiones tardó menos que esto |
| `p95_max_ms` | 95 de cada 100 tardaron menos que esto |
| `error_pct` | Porcentaje de peticiones falladas |
| `t_inicio` / `t_fin` | Cuándo corrió (sirve para cruzar con las métricas de Amazon) |
