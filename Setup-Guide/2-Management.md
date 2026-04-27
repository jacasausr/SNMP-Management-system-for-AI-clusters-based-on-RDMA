# Guía de la Componente 1: Monitorización SNMP del Cluster RoCEv2

## Índice

1. Visión general
2. Agentes SNMP
3. Controlador de tráfico
4. Gestor SNMP (monitor)
5. InfluxDB
6. Métricas del sistema
7. Despliegue y operación paso a paso
8. Notas técnicas relevantes

---

## 1. Visión general

Esta componente implementa el pipeline de monitorización del cluster: recolectar contadores de rendimiento de los workers y el switch, calcularles métricas derivadas, y almacenarlas en InfluxDB para su posterior visualización en Grafana.

El flujo de datos es unidireccional:

```
Workers (agente SNMP) ──┐
                        ├──→ Gestor SNMP (WSL) ──→ InfluxDB ──→ Grafana
Switch  (agente SNMP) ──┘
```

El gestor pollea las 4 entidades cada 5 segundos, calcula métricas derivadas (tasas, ratios, asimetrías) y escribe tanto los contadores crudos como las derivadas a InfluxDB. En paralelo, un controlador de tráfico en cada worker genera tráfico RDMA que simula entrenamiento distribuido.

### Estructura del repositorio

```
Proyecto_Gestion/
├── scripts/
│   ├── Network_Topology.sh      # Crea bridges y TAPs en WSL
│   ├── Config_OVS.sh            # Configura OVS dentro del switch
│   ├── deploy_agent.sh          # Despliega roce_agent.py en los 3 workers
│   └── start-traffic.sh         # Lanza/para controladores de tráfico vía SSH
├── src/
│   ├── snmp_manager/            # Gestor SNMP (Python, se ejecuta en WSL)
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── models.py
│   │   ├── poller.py
│   │   ├── calculator.py
│   │   ├── writer.py
│   │   ├── manager.py
│   │   └── trap_receiver.py     # Placeholder para componente 2
│   └── Agents/
│       ├── roce_agent.py        # Agente SNMP para workers
│       └── ovs_agent.py         # Agente SNMP para switch
└── traffic_controller.py        # Simulador de tráfico (se copia a cada worker)
```

---

## 2. Agentes SNMP

### Qué hacen

Cada entidad del cluster (3 workers + 1 switch) ejecuta `snmpd` con una extensión `pass_persist` que expone contadores específicos bajo el subárbol OID `.1.3.6.1.4.1.99999`.

El protocolo `pass_persist` funciona así: `snmpd` lanza el script Python como subproceso persistente. Cuando llega una petición SNMP GET o GETNEXT al subárbol configurado, `snmpd` escribe el comando y el OID por stdin del script. El script lee el contador correspondiente del sistema, y responde por stdout con tres líneas: OID, tipo SNMP y valor. El script queda vivo en un bucle infinito respondiendo peticiones. La comunicación es exclusivamente texto plano por stdin/stdout — todo el transporte UDP, comunidades y protocolo SNMP lo maneja `snmpd`.

La configuración de `snmpd` en cada VM reside en `/etc/snmp/snmpd.conf` y contiene: dirección de escucha (UDP:161), community SNMP (`public`, restringida a la IP del gestor y localhost), y la línea `pass_persist` que registra el script bajo el OID raíz. El script se ubica en `/usr/local/bin/` dentro de cada VM.

### Agente de los workers (`roce_agent.py`)

Expone dos tablas:

La primera tabla (bajo `.1.3.6.1.4.1.99999.1`) contiene los contadores InfiniBand de Soft-RoCE. Todos se leen del directorio `/sys/class/infiniband/rxe0/ports/1/hw_counters/`. Los contadores principales son `sent_pkts` y `rcvd_pkts` (paquetes RDMA transmitidos y recibidos), junto con contadores de errores como `rcvd_seq_err`, `retry_exceeded_err`, `duplicate_request` y otros indicadores de salud del enlace RDMA.

La segunda tabla (bajo `.1.3.6.1.4.1.99999.2`) contiene los contadores ECN del stack IP, leídos de `/proc/net/netstat`. Expone `InCEPkts` (paquetes marcados con Congestion Experienced), `InECT0Pkts`, `InECT1Pkts` e `InNoECTPkts`.

**Nota sobre contadores**: Soft-RoCE (rdma_rxe) solo expone `hw_counters/`, no el directorio `counters/` que sí existe en hardware InfiniBand real (ej. Mellanox ConnectX). Esto implica que no hay contadores de bytes a nivel InfiniBand — solo de paquetes. El throughput se expresa en paquetes por segundo, no en Mbps. Es una limitación inherente a la implementación software.

### Agente del switch (`ovs_agent.py`)

Expone contadores OVS por puerto bajo `.1.3.6.1.4.1.99999.3`, con esquema `.3.METRICA.PUERTO`. Los contadores se obtienen parseando la salida de `ovs-ofctl dump-ports br0` e incluyen bytes TX/RX, paquetes TX/RX, drops TX/RX y errores RX por cada puerto.

Un detalle de implementación: `snmpd` ejecuta el script como el usuario `Debian-snmp`, que no tiene permisos para ejecutar `ovs-ofctl`. Se resuelve con una regla sudoers específica que permite a ese usuario ejecutar exclusivamente `/usr/bin/ovs-ofctl` sin contraseña. Esta regla se configura una vez en el switch con:

```
echo 'Debian-snmp ALL=(root) NOPASSWD: /usr/bin/ovs-ofctl' | sudo tee /etc/sudoers.d/snmpd-ovs
```

---

## 3. Controlador de tráfico

### Qué simula

El controlador (`traffic_controller.py`) simula el patrón de tráfico de un cluster de entrenamiento IA distribuido. Cada iteración de entrenamiento tiene dos fases: una fase de computación (forward + backward pass) donde no hay tráfico de red, seguida de una fase de comunicación (all-reduce) donde los workers intercambian gradientes.

### Ring all-reduce

La comunicación entre workers sigue el patrón ring all-reduce: cada worker envía datos a su vecino siguiente y recibe del anterior formando un anillo (W1→W2→W3→W1). Cada fase de comunicación ejecuta dos ráfagas RDMA (reduce-scatter y all-gather), que es lo que haría NCCL en un entrenamiento real con 3 GPUs.

El tráfico se genera con `ib_write_bw` del paquete `perftest`. Cada worker mantiene un servidor `ib_write_bw` escuchando permanentemente en un puerto dedicado para recibir datos de su vecino anterior, y actúa como cliente cuando necesita enviar al vecino siguiente. Como `ib_write_bw` no soporta modo servidor persistente (no tiene flag `--run_infinitely`), el servidor se relanza automáticamente en un hilo tras cada conexión completada.

### Varianza y realismo

La fase de computación tiene duración aleatoria (entre 1 y 5 segundos) para simular la variabilidad natural entre iteraciones. Cada worker tiene varianza independiente, lo que provoca desincronización gradual entre ellos — exactamente lo que ocurre en un cluster real donde la velocidad de cada GPU varía ligeramente.

El Worker 3 (hackeado) añade un delay extra de ~0.5 segundos por iteración, simulando la degradación causada por la criptominería que consume parte de sus recursos. Esto lo convierte en el straggler más frecuente del cluster, detectable en las métricas de asimetría.

### Nota sobre perftest

En la versión de `perftest` instalada, el flag `-g` significa multicast group, no GID index. El GID index se especifica con `-x`. Esto difiere de `ibv_rc_pingpong` donde `-g` sí es GID index. Además, los flags `-q` (queue depth) y `--report_gbits` no son reconocidos en esta versión. Los comandos de `ib_write_bw` usan exclusivamente `-d rxe0 -x 1 -p <puerto> -s 65536 -D 2`.

---

## 4. Gestor SNMP (monitor)

### Arquitectura

El gestor es un paquete Python (`src/snmp_manager/`) con separación estricta de responsabilidades en 7 módulos. El diseño sigue el principio de que cada módulo es testeable de forma independiente: se puede verificar el cálculo de métricas sin VMs corriendo, o la generación de puntos InfluxDB sin conexión a la base de datos.

### Data Transfer Objects (`models.py`)

Todas las estructuras de datos que fluyen entre capas son dataclasses inmutables (`frozen=True`). Esto garantiza que un dato, una vez construido, no es modificado por ninguna capa — se crea uno nuevo. Los DTOs cubren 5 niveles: lectura cruda de worker, lectura cruda de puerto OVS, métricas derivadas de worker, métricas derivadas de puerto OVS, y métricas globales del cluster.

Existe además un `PollCycle` mutable que agrupa los resultados de un ciclo de polling completo. No es frozen porque se construye progresivamente durante el poll.

### Polling SNMP (`poller.py`)

Usa la API asyncio nativa de pysnmp 7.x (`pysnmp.hlapi.v3arch.asyncio`). Las peticiones a los 4 agentes se lanzan en paralelo con `asyncio.gather`, lo que reduce la latencia del ciclo. Un timestamp único capturado al inicio del ciclo se comparte entre todas las lecturas para garantizar coherencia temporal.

Si un agente no responde (timeout de 2 segundos), el poller devuelve `None` para esa entidad sin bloquear al resto. Esto es necesario para el caso en que el Worker 3 sea desconectado vía OVS — el gestor debe seguir funcionando con los workers restantes.

### Cálculo de métricas (`calculator.py`)

Módulo sin I/O, exclusivamente lógica de cálculo. Mantiene el estado del poll anterior para calcular deltas entre ciclos (necesarios para tasas). El primer ciclo solo almacena valores base y no produce derivadas.

El cálculo de `rdma_vs_ovs_ratio` requiere cruzar datos de dos fuentes distintas (contadores RDMA del worker y contadores de tráfico del puerto OVS correspondiente). Este cruce se hace en la capa del calculador: recibe ambos DTOs como parámetros y devuelve el ratio. Ninguna capa inferior sabe que el cruce existe.

Protege contra counter wraps (contadores que vuelven a 0 tras llegar al máximo) tratando deltas negativos como 0.

### Escritura a InfluxDB (`writer.py`)

Implementa el patrón Repository: abstrae el acceso a InfluxDB detrás de una interfaz que habla exclusivamente en objetos del dominio (DTOs). Cada método acepta un DTO y lo transforma internamente en line protocol de InfluxDB. Al arrancar, crea la database y la retention policy si no existen.

Registra explícitamente cuando un worker es inalcanzable, escribiendo un punto con `unreachable=True` para que Grafana pueda mostrar huecos o alertas visuales.

### Orquestador (`manager.py`)

Coordina el ciclo completo en un bucle asyncio: poll → calculate → write, cada 5 segundos. Si el ciclo tarda más que el intervalo (por timeouts SNMP), loguea un warning y continúa inmediatamente sin acumular retraso.

El indexado de puertos OVS por worker se hace una vez por ciclo para evitar búsquedas repetidas durante el cálculo de métricas cruzadas.

### Entry point (`main.py`)

Construye los 4 componentes, conecta a InfluxDB, y ejecuta el manager. Maneja señales SIGINT/SIGTERM para shutdown limpio (cierra la conexión a InfluxDB). Soporta `--debug` para logging verbose.

### Configuración (`config.py`)

Centraliza toda la configuración: IPs de la topología, mapeo worker↔puerto OVS, definición de OIDs, parámetros SNMP (community, timeout, retries), intervalo de polling, y conexión a InfluxDB.

---

## 5. InfluxDB

### Instalación

InfluxDB 1.x desde los repositorios de Ubuntu. El cliente CLI es un paquete separado. Para instalar ambos en WSL:

```
sudo apt install -y influxdb influxdb-client
sudo systemctl enable influxdb
sudo systemctl start influxdb
```

Verificar que el servidor responde:

```
influx -execute "SHOW DATABASES"
```

La database `roce_cluster` y su retention policy se crean automáticamente al arrancar el gestor por primera vez.

### Esquema de datos

La database `roce_cluster` contiene 5 measurements:

**`roce_worker_raw`**: contadores crudos por worker. Tags: `worker`. Fields: todos los contadores de `hw_counters` y ECN tal cual llegan del agente SNMP.

**`roce_worker`**: métricas derivadas por worker. Tags: `worker`. Fields: `sent_pps`, `rcvd_pps`, `error_rate`, `retransmission_ratio`, `ecn_ratio`, `rdma_vs_ovs_ratio`.

**`ovs_port_raw`**: contadores crudos por puerto OVS. Tags: `port`, `connected_to`. Fields: bytes, paquetes, drops, errores TX/RX.

**`ovs_port`**: métricas derivadas por puerto. Tags: `port`, `connected_to`. Fields: `port_throughput_mbps`, `port_drop_rate`.

**`cluster`**: métricas globales. Fields: `asymmetry_index`, `max_min_spread`, `straggler_id`, `mean_pkt_rate`.

Retention policy `one_day`: los datos se eliminan automáticamente tras 24 horas.

### Consultas útiles

Últimas métricas derivadas de los workers:

```
influx -database roce_cluster -execute "SELECT * FROM roce_worker ORDER BY time DESC LIMIT 5"
```

Estado del cluster:

```
influx -database roce_cluster -execute "SELECT * FROM cluster ORDER BY time DESC LIMIT 5"
```

Contadores crudos OVS:

```
influx -database roce_cluster -execute "SELECT * FROM ovs_port_raw ORDER BY time DESC LIMIT 5"
```

Borrar todos los datos para empezar de cero (sin eliminar la database):

```
influx -database roce_cluster -execute "DROP SERIES FROM /.*/"
```

---

## 6. Métricas del sistema

### Por worker

**`sent_pps` / `rcvd_pps`**: tasa de paquetes RDMA por segundo. Es la métrica principal de actividad y la base para detectar asimetrías. Se calcula como delta de `sent_pkts` entre polls dividido por el intervalo.

**`error_rate`**: tasa de errores RDMA por segundo. Suma de deltas de `rcvd_seq_err`, `retry_exceeded_err`, `rcvd_rnr_err`, `send_rnr_err`, `completer_retry_err`, `send_err` y `retry_rnr_exceeded_err`. Un pico indica problemas en el enlace.

**`retransmission_ratio`**: proporción de reintentos agotados sobre el total de paquetes enviados. Un 0.01% es normal; un 1% indica enlace degradado.

**`ecn_ratio`**: proporción de paquetes marcados con Congestion Experienced sobre el total (CE + NoECT). Indica nivel de congestión en la red.

**`rdma_vs_ovs_ratio`**: ratio entre paquetes vistos por OVS en el puerto del worker y paquetes RDMA reportados por el worker. En condiciones normales es cercano a 1.0 (todo el tráfico es RDMA). Si un worker genera tráfico no-RDMA (C2, minería), el ratio baja porque OVS ve más paquetes de los que InfiniBand reporta. Se calcula para todos los workers, y la detección de anomalías sale de comparar los ratios entre sí.

### Por puerto OVS

**`port_throughput_mbps`**: caudal total del puerto en Mbps. A diferencia de los workers (solo paquetes), OVS sí tiene contadores de bytes.

**`port_drop_rate`**: tasa de paquetes descartados por segundo. Correlaciona con `retry_exceeded_err` en el worker conectado a ese puerto.

### Cluster-wide

**`asymmetry_index`**: coeficiente de variación del `sent_pps` entre workers (desviación estándar / media). Cercano a 0 = cluster equilibrado. Cercano a 1 = un worker es significativamente más lento. Esta es la métrica más valiosa para gestión de clusters de entrenamiento IA, donde el worker más lento frena a todos los demás.

**`max_min_spread`**: diferencia absoluta entre el worker más rápido y el más lento en paquetes por segundo.

**`straggler_id`**: identificador del worker con menor `sent_pps` en cada ciclo. En el sistema actual, vm3 debería ser el straggler más frecuente por la degradación simulada del malware.

**`mean_pkt_rate`**: media de `sent_pps` de todos los workers. Indica la actividad general del cluster.

---

## 7. Despliegue y operación paso a paso

### Prerrequisitos

La topología debe estar funcionando (ver `README-RDMA-Virtualizacion_2.md`). InfluxDB debe estar corriendo en WSL. Las dependencias Python del gestor deben estar instaladas:

```
pip install pysnmp influxdb --break-system-packages
```

### Paso 1 — Topología de red en WSL

Desde el directorio `scripts/`:

```
sudo ./Network_Topology.sh
```

Verificar que los bridges y TAPs se crearon:

```
bridge link show
```

### Paso 2 — Arrancar las VMs

En 4 terminales separadas, desde el directorio raíz del proyecto (los scripts `start-vm.sh` y `start-switch.sh` están documentados en el README de topología):

```
# Terminal 1
sudo qemu-system-x86_64 -name vm1 -machine q35,accel=kvm -cpu host -m 2048 -smp 2 \
    -drive file=$HOME/qemu-roce/vm1.qcow2,format=qcow2,if=virtio \
    -drive file=$HOME/qemu-roce/seed-vm1.iso,format=raw,if=virtio \
    -netdev tap,id=net0,ifname=tap-w1,script=no,downscript=no \
    -device virtio-net-pci,netdev=net0,mac=52:54:00:00:00:01 \
    -nographic -serial mon:stdio

# Terminal 2 — ídem para vm2 (tap-w2, mac :02)
# Terminal 3 — ídem para vm3 (tap-w3, mac :03)
# Terminal 4 — switch con 4 NICs (ver README de topología sección 6)
```

Login en cada VM: `user` / `rdma`.

### Paso 3 — Verificar Soft-RoCE en los workers

Desde WSL, comprobar que `rxe0` existe en los 3 workers:

```
for ip in 10.10.0.1 10.10.0.2 10.10.0.3; do
    echo "--- $ip ---"
    ssh user@$ip "rdma link"
done
```

Si `rxe0` no aparece en alguno, recrearlo:

```
ssh user@10.10.0.X "sudo modprobe rdma_rxe && sudo rdma link add rxe0 type rxe netdev enp0s2"
```

### Paso 4 — Verificar agentes SNMP

Desde WSL, comprobar que los agentes responden en los workers:

```
snmpwalk -v2c -c public 10.10.0.1 .1.3.6.1.4.1.99999
```

Debe devolver los OIDs de las dos tablas. Si devuelve error o no devuelve nada, verificar que `snmpd` está corriendo y que el fichero `/usr/local/bin/roce_agent.py` existe dentro de la VM:

```
ssh user@10.10.0.1 "sudo systemctl status snmpd"
ssh user@10.10.0.1 "ls -la /usr/local/bin/roce_agent.py"
```

Para el switch:

```
snmpwalk -v2c -c public 10.10.0.10 .1.3.6.1.4.1.99999.3
```

### Paso 5 — Desplegar agentes (si se han modificado)

Desde el directorio `Agents/`:

```
cd src/Agents/
../../scripts/deploy_agent.sh
```

El script copia `roce_agent.py` a los 3 workers vía SCP, lo coloca en `/usr/local/bin/`, reinicia `snmpd`, y ejecuta un `snmpwalk` de verificación automáticamente.

Para el agente del switch, el despliegue es manual:

```
scp ovs_agent.py user@10.10.0.10:/tmp/
ssh user@10.10.0.10 "sudo cp /tmp/ovs_agent.py /usr/local/bin/ovs_agent.py && sudo chmod +x /usr/local/bin/ovs_agent.py && sudo systemctl restart snmpd"
```

### Paso 6 — Lanzar controladores de tráfico

Primero copiar el controlador a las VMs si no se ha hecho:

```
for ip in 10.10.0.1 10.10.0.2 10.10.0.3; do
    scp traffic_controller.py user@$ip:/home/user/
done
```

Lanzar desde el directorio `scripts/`:

```
./start-traffic.sh
```

Verificar que generan tráfico consultando los logs:

```
ssh user@10.10.0.1 "tail -5 /tmp/traffic_w1.log"
ssh user@10.10.0.2 "tail -5 /tmp/traffic_w2.log"
ssh user@10.10.0.3 "tail -5 /tmp/traffic_w3.log"
```

Debe mostrar iteraciones de compute y all-reduce sin errores constantes. Errores esporádicos de all-gather son normales (desincronización entre servidor y cliente).

### Paso 7 — Arrancar el gestor SNMP

Desde el directorio del gestor:

```
cd src/snmp_manager/
python3 main.py
```

O en modo debug para ver detalle por worker:

```
python3 main.py --debug
```

El primer ciclo muestra "almacenando base, sin derivadas" para cada worker (necesita dos polls para calcular deltas). A partir del segundo ciclo aparecen las métricas del cluster:

```
Ciclo 2 — workers: 3/3, puertos OVS: 4
  Cluster: asymmetry=0.4783, spread=79414 pps, straggler=vm2, mean=83024 pps
```

### Paso 8 — Verificar datos en InfluxDB

Con el gestor corriendo, en otra terminal:

```
influx -database roce_cluster -execute "SELECT * FROM roce_worker ORDER BY time DESC LIMIT 3"
influx -database roce_cluster -execute "SELECT * FROM cluster ORDER BY time DESC LIMIT 3"
influx -database roce_cluster -execute "SELECT * FROM ovs_port ORDER BY time DESC LIMIT 3"
```

Los campos `sent_pps`, `rcvd_pps` deben tener valores del orden de decenas de miles. El `rdma_vs_ovs_ratio` debe estar cercano a 1.0. El `asymmetry_index` varía entre ciclos.

### Parada completa

```
# 1. Parar gestor SNMP (Ctrl+C en su terminal)

# 2. Parar controladores de tráfico
./scripts/start-traffic.sh stop

# 3. Apagar VMs (en cada terminal QEMU)
sudo poweroff

# o forzar desde WSL con el monitor QEMU: Ctrl+A, C, escribir "quit"
```

---

## 8. Notas técnicas relevantes

### pysnmp 7.x

La versión 7 de pysnmp cambió la API respecto a versiones anteriores. Los imports son de `pysnmp.hlapi.v3arch.asyncio`. Todas las funciones usan snake_case (`get_cmd`, no `getCmd`). `UdpTransportTarget` se crea con una factory async (`await UdpTransportTarget.create(...)`). Las llamadas son nativamente asyncio, sin necesidad de thread pool.

### Persistencia tras reinicio de VMs

Las VMs pierden la configuración de Soft-RoCE (`rxe0`) al reiniciar si no se habilitó el servicio systemd `setup-roce.service` (documentado en el README de topología, sección 9). Sin `rxe0` activo, los contadores de `hw_counters` no existen y el agente devuelve todo a 0. Si los valores llegan a 0 con tráfico activo, verificar `rdma link` dentro de la VM.

La topología de red en WSL (bridges y TAPs) tampoco sobrevive a un reinicio de WSL. Ejecutar `Network_Topology.sh` antes de arrancar las VMs.

### Congestión simulada (pendiente de activación)

Se puede simular congestión ECN y pérdida de paquetes en el switch con `tc netem`. En el switch, vía SSH:

ECN al 1% en todos los puertos data:

```
for iface in enp0s2 enp0s3 enp0s4; do
    sudo tc qdisc add dev $iface root netem ecn 1%
done
```

Drops al 0.1% adicionales en el puerto del Worker 3:

```
sudo tc qdisc replace dev enp0s4 root netem loss 0.1% ecn 1%
```

Esto haría que `ecn_ratio` suba en todos los workers y que `retry_exceeded_err` suba en el Worker 3. No se ha activado aún para no interferir con la verificación del pipeline base.

Para revertir:

```
for iface in enp0s2 enp0s3 enp0s4; do
    sudo tc qdisc del dev $iface root 2>/dev/null
done
```

### SSH sin contraseña

Para que los scripts (`deploy_agent.sh`, `start-traffic.sh`) funcionen sin pedir contraseña repetidamente:

```
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
for ip in 10.10.0.1 10.10.0.2 10.10.0.3 10.10.0.10; do
    ssh-copy-id user@$ip
done
```
