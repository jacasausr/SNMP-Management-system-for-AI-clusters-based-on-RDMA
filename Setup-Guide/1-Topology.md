# Guía de despliegue: Cluster RoCEv2 con Soft-RoCE sobre QEMU/KVM en WSL2

## Índice

1. Requisitos previos
2. Instalación de QEMU con KVM
3. Preparación de la imagen base
4. Creación de las VMs (workers + switch)
5. Topología de red en WSL
6. Arranque de las VMs
7. Configuración de red persistente en las VMs
8. Configuración de OVS y port mirroring en el switch
9. Configuración de Soft-RoCE en los workers
10. Verificación de la topología
11. Captura y análisis con Wireshark
12. Contadores disponibles para monitorización
13. Scripts de automatización
14. Problemas frecuentes y soluciones

---

## 1. Requisitos previos

### Sistema operativo

- **Windows 11** (obligatorio para virtualización anidada en WSL2).

```powershell
wsl --version
```

Si la versión es anterior a 2.0.0:

```powershell
wsl --update
```

### Hardware

- CPU con soporte de virtualización (Intel VT-x o AMD-V) habilitado en BIOS.
- Mínimo 16 GB de RAM (WSL + 4 VMs de 2 GB cada una).
- 20 GB de disco libre para imágenes de VM.

### Configuración de WSL

Editar `C:\Users\<USUARIO_WINDOWS>\.wslconfig`:

```ini
[wsl2]
nestedVirtualization=true
memory=10GB
processors=4
```

Reiniciar WSL:

```powershell
wsl --shutdown
```

Verificar KVM:

```bash
ls -la /dev/kvm
```

### Identificar el usuario de Windows

```bash
cmd.exe /c 'echo %USERNAME%'
```

En esta guía se usa `<USUARIO_WINDOWS>` como placeholder.

---

## 2. Instalación de QEMU con KVM

```bash
sudo apt update
sudo apt install -y qemu-system-x86 qemu-utils cloud-image-utils \
    bridge-utils iproute2 wget genisoimage
```

Verificar:

```bash
qemu-system-x86_64 -accel help
```

Debe listar `kvm`.

---

## 3. Preparación de la imagen base

### 3.1 Descargar imagen cloud de Ubuntu

```bash
mkdir -p ~/qemu-roce && cd ~/qemu-roce
wget https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img
```

### 3.2 Crear disco de preparación

```bash
qemu-img create -f qcow2 -b jammy-server-cloudimg-amd64.img -F qcow2 prep.qcow2 10G
```

### 3.3 Cloud-init para la preparación

```bash
mkdir -p ~/qemu-roce/cloud-init/prep

cat > ~/qemu-roce/cloud-init/prep/user-data << 'EOF'
#cloud-config
hostname: prep
manage_etc_hosts: true
users:
  - name: user
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: false
    plain_text_passwd: "rdma"
ssh_pwauth: true
EOF

cat > ~/qemu-roce/cloud-init/prep/meta-data << 'EOF'
instance-id: prep
local-hostname: prep
EOF

genisoimage -output ~/qemu-roce/seed-prep.iso -volid cidata -joliet -rock \
    ~/qemu-roce/cloud-init/prep/user-data \
    ~/qemu-roce/cloud-init/prep/meta-data
```

### 3.4 Arrancar con NAT e instalar paquetes

```bash
sudo qemu-system-x86_64 \
    -name prep \
    -machine q35,accel=kvm \
    -cpu host \
    -m 2048 \
    -smp 2 \
    -drive file=$HOME/qemu-roce/prep.qcow2,format=qcow2,if=virtio \
    -drive file=$HOME/qemu-roce/seed-prep.iso,format=raw,if=virtio \
    -netdev user,id=net0 \
    -device virtio-net-pci,netdev=net0 \
    -nographic \
    -serial mon:stdio
```

Login: `user` / `rdma`. Instalar paquetes:

```bash
sudo apt update
sudo apt install -y \
    linux-modules-extra-$(uname -r) \
    rdma-core \
    ibverbs-utils \
    perftest \
    iproute2 \
    ethtool \
    tcpdump \
    snmpd \
    snmp \
    snmp-mibs-downloader

sudo modprobe rdma_rxe
lsmod | grep rdma_rxe

sudo apt clean
sudo poweroff
```

### 3.5 Consolidar imagen base

```bash
cd ~/qemu-roce
qemu-img convert -O qcow2 prep.qcow2 base-roce.qcow2
```

---

## 4. Creación de las VMs (workers + switch)

### 4.1 Topología objetivo

| VM | Rol | IP | MAC |
|---|---|---|---|
| vm1 | Worker 1 (legítimo) | 10.10.0.1 | 52:54:00:00:00:01 |
| vm2 | Worker 2 (legítimo) | 10.10.0.2 | 52:54:00:00:00:02 |
| vm3 | Worker 3 (hackeado) | 10.10.0.3 | 52:54:00:00:00:03 |
| switch | Switch OVS + Snort | 10.10.0.10 | 52:54:00:00:00:11/12/13/10 |
| Host WSL | Gestión (SNMP/InfluxDB/Grafana) | 10.10.0.254 | — |

### 4.2 Crear discos delta

```bash
cd ~/qemu-roce
qemu-img create -f qcow2 -b base-roce.qcow2 -F qcow2 vm1.qcow2 10G
qemu-img create -f qcow2 -b base-roce.qcow2 -F qcow2 vm2.qcow2 10G
qemu-img create -f qcow2 -b base-roce.qcow2 -F qcow2 vm3.qcow2 10G
qemu-img create -f qcow2 -b base-roce.qcow2 -F qcow2 switch.qcow2 10G
```

**Importante**: no volver a ejecutar `qemu-img create` sobre un disco que ya tiene cambios. Esto destruye el delta y se pierden los datos.

### 4.3 Cloud-init para cada VM

```bash
for VM in vm1 vm2 vm3 switch; do
    mkdir -p ~/qemu-roce/cloud-init/$VM

    cat > ~/qemu-roce/cloud-init/$VM/user-data << EOF
#cloud-config
hostname: $VM
manage_etc_hosts: true
users:
  - name: user
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: false
    plain_text_passwd: "rdma"
ssh_pwauth: true
EOF

    cat > ~/qemu-roce/cloud-init/$VM/meta-data << EOF
instance-id: $VM
local-hostname: $VM
EOF

    genisoimage -output ~/qemu-roce/seed-$VM.iso -volid cidata -joliet -rock \
        ~/qemu-roce/cloud-init/$VM/user-data \
        ~/qemu-roce/cloud-init/$VM/meta-data
done
```

### 4.4 Instalar OVS y Snort en el switch

Arrancar el switch con NAT:

```bash
sudo qemu-system-x86_64 \
    -name switch-prep \
    -machine q35,accel=kvm \
    -cpu host \
    -m 2048 \
    -smp 2 \
    -drive file=$HOME/qemu-roce/switch.qcow2,format=qcow2,if=virtio \
    -drive file=$HOME/qemu-roce/seed-switch.iso,format=raw,if=virtio \
    -netdev user,id=net0 \
    -device virtio-net-pci,netdev=net0 \
    -nographic \
    -serial mon:stdio
```

Login `user`/`rdma`, instalar y verificar:

```bash
sudo apt update
sudo apt install -y openvswitch-switch snort3 python3-pip

# Verificar
dpkg -l | grep openvswitch
which ovs-vsctl
sudo systemctl status openvswitch-switch
which snort

sudo apt clean
sudo poweroff
```

---

## 5. Topología de red en WSL

La topología usa bridges punto a punto como "cables" L2 entre cada worker y el switch. Un bridge de gestión conecta el host WSL al switch.

```
Host WSL (10.10.0.254)
  │
  br-mgmt ── tap-sm ──────────────────┐
                                       │
  br-link1 ── tap-w1 ── tap-s1 ──┐    │
  br-link2 ── tap-w2 ── tap-s2 ──┼────┼── Switch VM (OVS br0)
  br-link3 ── tap-w3 ── tap-s3 ──┘    │
        │           │          │       │
     Worker1     Worker2    Worker3    │
                                       │
                               mirror0 → Snort
```

### 5.1 Crear la topología

```bash
#!/bin/bash
set -e

# Limpiar topología anterior
for i in 0 1 2 3; do sudo ip link del tap$i 2>/dev/null || true; done
for br in br-roce br-link1 br-link2 br-link3 br-mgmt; do
    sudo ip link del $br 2>/dev/null || true
done
for tap in tap-w1 tap-w2 tap-w3 tap-s1 tap-s2 tap-s3 tap-sm; do
    sudo ip link del $tap 2>/dev/null || true
done

# Bridges punto a punto (worker ↔ switch)
for i in 1 2 3; do
    sudo ip link add br-link$i type bridge
    sudo ip link set br-link$i up
    sudo ip tuntap add dev tap-w$i mode tap
    sudo ip link set tap-w$i master br-link$i
    sudo ip link set tap-w$i up
    sudo ip tuntap add dev tap-s$i mode tap
    sudo ip link set tap-s$i master br-link$i
    sudo ip link set tap-s$i up
done

# Bridge de gestión (host ↔ switch)
sudo ip link add br-mgmt type bridge
sudo ip link set br-mgmt up
sudo ip addr add 10.10.0.254/24 dev br-mgmt
sudo ip tuntap add dev tap-sm mode tap
sudo ip link set tap-sm master br-mgmt
sudo ip link set tap-sm up

# iptables y forwarding
sudo sysctl -w net.bridge.bridge-nf-call-iptables=0 > /dev/null
sudo sysctl -w net.bridge.bridge-nf-call-ip6tables=0 > /dev/null
sudo iptables -I FORWARD -j ACCEPT
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null

echo "=== Topología lista ==="
for br in br-link1 br-link2 br-link3 br-mgmt; do
    echo "--- $br ---"
    bridge link show master $br
done
echo ""
echo "Lanzar las VMs:"
echo "  ./start-vm.sh 1|2|3"
echo "  ./start-switch.sh"
```

Esta configuración no sobrevive a un reinicio de WSL. Ejecutar `start-topology.sh` (sección 13) antes de arrancar las VMs.

---

## 6. Arranque de las VMs

Cada VM se arranca en una terminal WSL distinta.

### Workers

```bash
# Worker 1
sudo qemu-system-x86_64 -name vm1 -machine q35,accel=kvm -cpu host -m 2048 -smp 2 \
    -drive file=$HOME/qemu-roce/vm1.qcow2,format=qcow2,if=virtio \
    -drive file=$HOME/qemu-roce/seed-vm1.iso,format=raw,if=virtio \
    -netdev tap,id=net0,ifname=tap-w1,script=no,downscript=no \
    -device virtio-net-pci,netdev=net0,mac=52:54:00:00:00:01 \
    -nographic -serial mon:stdio

# Worker 2
sudo qemu-system-x86_64 -name vm2 -machine q35,accel=kvm -cpu host -m 2048 -smp 2 \
    -drive file=$HOME/qemu-roce/vm2.qcow2,format=qcow2,if=virtio \
    -drive file=$HOME/qemu-roce/seed-vm2.iso,format=raw,if=virtio \
    -netdev tap,id=net0,ifname=tap-w2,script=no,downscript=no \
    -device virtio-net-pci,netdev=net0,mac=52:54:00:00:00:02 \
    -nographic -serial mon:stdio

# Worker 3
sudo qemu-system-x86_64 -name vm3 -machine q35,accel=kvm -cpu host -m 2048 -smp 2 \
    -drive file=$HOME/qemu-roce/vm3.qcow2,format=qcow2,if=virtio \
    -drive file=$HOME/qemu-roce/seed-vm3.iso,format=raw,if=virtio \
    -netdev tap,id=net0,ifname=tap-w3,script=no,downscript=no \
    -device virtio-net-pci,netdev=net0,mac=52:54:00:00:00:03 \
    -nographic -serial mon:stdio
```

### Switch (4 NICs: 3 data + 1 gestión)

```bash
sudo qemu-system-x86_64 -name switch -machine q35,accel=kvm -cpu host -m 2048 -smp 2 \
    -drive file=$HOME/qemu-roce/switch.qcow2,format=qcow2,if=virtio \
    -drive file=$HOME/qemu-roce/seed-switch.iso,format=raw,if=virtio \
    -netdev tap,id=sw1,ifname=tap-s1,script=no,downscript=no \
    -device virtio-net-pci,netdev=sw1,mac=52:54:00:00:00:11 \
    -netdev tap,id=sw2,ifname=tap-s2,script=no,downscript=no \
    -device virtio-net-pci,netdev=sw2,mac=52:54:00:00:00:12 \
    -netdev tap,id=sw3,ifname=tap-s3,script=no,downscript=no \
    -device virtio-net-pci,netdev=sw3,mac=52:54:00:00:00:13 \
    -netdev tap,id=mgmt,ifname=tap-sm,script=no,downscript=no \
    -device virtio-net-pci,netdev=mgmt,mac=52:54:00:00:00:10 \
    -nographic -serial mon:stdio
```

### Credenciales y gestión

- Login: `user` / `rdma`
- Apagar: `sudo poweroff`
- Monitor QEMU: `Ctrl+A` seguido de `C`, escribir `quit` para forzar apagado.
- SSH desde WSL: `ssh user@10.10.0.X` (contraseña: `rdma`)

---

## 7. Configuración de red persistente en las VMs

Cloud-init genera un fichero netplan con match por MAC que interfiere con las MACs asignadas por QEMU. Hay que desactivarlo y crear un netplan estático en cada VM.

### 7.1 En cada worker

Ejecutar dentro de la VM (ajustar la IP según el worker):

```bash
# Desactivar cloud-init network
sudo tee /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg << 'EOF'
network: {config: disabled}
EOF

# Eliminar netplan de cloud-init
sudo rm -f /etc/netplan/50-cloud-init.yaml

# Crear netplan estático (ajustar IP: .1 / .2 / .3)
sudo tee /etc/netplan/01-static.yaml << 'EOF'
network:
  version: 2
  ethernets:
    enp0s2:
      dhcp4: false
      addresses:
        - 10.10.0.1/24
EOF

sudo chmod 600 /etc/netplan/01-static.yaml
sudo netplan apply
```

IPs por worker:
- Worker 1: `10.10.0.1/24`
- Worker 2: `10.10.0.2/24`
- Worker 3: `10.10.0.3/24`

### 7.2 En el switch

```bash
# Desactivar cloud-init network
sudo tee /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg << 'EOF'
network: {config: disabled}
EOF

sudo rm -f /etc/netplan/50-cloud-init.yaml

# Netplan: levantar interfaces sin IP (OVS las gestiona)
sudo tee /etc/netplan/01-switch.yaml << 'EOF'
network:
  version: 2
  ethernets:
    enp0s2:
      dhcp4: false
    enp0s3:
      dhcp4: false
    enp0s4:
      dhcp4: false
    enp0s5:
      dhcp4: false
EOF

sudo chmod 600 /etc/netplan/01-switch.yaml
sudo netplan apply
```

---

## 8. Configuración de OVS y port mirroring en el switch

### 8.1 Mapeo de interfaces del switch

Las interfaces se asignan según el orden de los `-device` en el comando QEMU:

| Interfaz | TAP en WSL | Conecta a |
|---|---|---|
| enp0s2 | tap-s1 | Worker 1 |
| enp0s3 | tap-s2 | Worker 2 |
| enp0s4 | tap-s3 | Worker 3 |
| enp0s5 | tap-sm | Host WSL (gestión) |

### 8.2 Configuración inicial de OVS

Ejecutar dentro del switch:

```bash
# Arrancar OVS
sudo systemctl start openvswitch-switch

# Crear bridge OVS
sudo ovs-vsctl add-br br0

# Añadir puertos
sudo ovs-vsctl add-port br0 enp0s2   # worker 1
sudo ovs-vsctl add-port br0 enp0s3   # worker 2
sudo ovs-vsctl add-port br0 enp0s4   # worker 3
sudo ovs-vsctl add-port br0 enp0s5   # host management

# Levantar interfaces
for iface in enp0s2 enp0s3 enp0s4 enp0s5; do
    sudo ip link set $iface up
done

# IP del switch
sudo ip addr add 10.10.0.10/24 dev br0
sudo ip link set br0 up

# Verificar
sudo ovs-vsctl show
```

### 8.3 Port mirroring para Snort

```bash
# Puerto interno donde Snort escuchará
sudo ovs-vsctl add-port br0 mirror0 -- set interface mirror0 type=internal
sudo ip link set mirror0 up

# Mirror: todo el tráfico de/hacia Worker 3 → mirror0
sudo ovs-vsctl -- set bridge br0 mirrors=@m \
    -- --id=@src get port enp0s4 \
    -- --id=@dst get port mirror0 \
    -- --id=@m create mirror name=snort-mirror \
       select-src-port=@src select-dst-port=@src output-port=@dst

# Verificar
sudo ovs-vsctl list mirror
```

### 8.4 Persistencia de OVS

OVS almacena su configuración (bridge, puertos, mirrors) en `/etc/openvswitch/conf.db`, que sobrevive reboots. Solo hay que hacer persistente el levantamiento de interfaces y la IP.

```bash
sudo tee /usr/local/bin/setup-ovs.sh << 'SCRIPT'
#!/bin/bash
until ovs-vsctl show > /dev/null 2>&1; do sleep 1; done

for iface in enp0s2 enp0s3 enp0s4 enp0s5; do
    ip link set $iface up
done

ip addr add 10.10.0.10/24 dev br0 2>/dev/null || true
ip link set br0 up
ip link set mirror0 up
SCRIPT

sudo chmod +x /usr/local/bin/setup-ovs.sh

sudo tee /etc/systemd/system/setup-ovs.service << 'EOF'
[Unit]
Description=Setup OVS interfaces and IP
After=openvswitch-switch.service
Requires=openvswitch-switch.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/setup-ovs.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable openvswitch-switch
sudo systemctl enable setup-ovs.service
```

### 8.5 Desconexión programática de un worker

Para desconectar el Worker 3 (puerto `enp0s4` en OVS):

```bash
sudo ovs-ofctl mod-port br0 enp0s4 down
```

Para reconectarlo:

```bash
sudo ovs-ofctl mod-port br0 enp0s4 up
```

---

## 9. Configuración de Soft-RoCE en los workers

Ejecutar en cada worker (vm1, vm2, vm3):

```bash
sudo modprobe rdma_rxe
sudo rdma link add rxe0 type rxe netdev enp0s2
sudo ethtool -K enp0s2 tx off rx off tso off gso off gro off 2>/dev/null || true

# Verificar
rdma link
ibv_devices
ibv_devinfo -d rxe0
```

Verificar GID índice 1 (IPv4-mapped):

```bash
cat /sys/class/infiniband/rxe0/ports/1/gids/1
```

Debe contener la IP del worker en formato IPv4-mapped (ej: `0000:0000:0000:0000:0000:ffff:0a0a:0001` para 10.10.0.1).

### Persistencia de Soft-RoCE

Para que `rxe0` se cree automáticamente al arrancar:

```bash
sudo tee /usr/local/bin/setup-roce.sh << 'SCRIPT'
#!/bin/bash
modprobe rdma_rxe
IFACE=$(ip -o link show | awk -F': ' '!/lo/{print $2; exit}')
rdma link add rxe0 type rxe netdev "$IFACE" 2>/dev/null || true
ethtool -K "$IFACE" tx off rx off tso off gso off gro off 2>/dev/null || true
SCRIPT

sudo chmod +x /usr/local/bin/setup-roce.sh

sudo tee /etc/systemd/system/setup-roce.service << 'EOF'
[Unit]
Description=Setup Soft-RoCE
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/setup-roce.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable setup-roce.service
```

---

## 10. Verificación de la topología

### 10.1 Conectividad IP

Desde cualquier worker:

```bash
ping -c 2 10.10.0.10    # switch
ping -c 2 10.10.0.254   # host WSL
ping -c 2 10.10.0.1     # worker 1
ping -c 2 10.10.0.2     # worker 2
ping -c 2 10.10.0.3     # worker 3
```

### 10.2 OVS

Desde el switch:

```bash
sudo ovs-vsctl show
sudo ovs-ofctl dump-ports br0
sudo ovs-vsctl list mirror
```

### 10.3 RDMA (ping-pong entre workers)

En Worker 2 (servidor):

```bash
ibv_rc_pingpong -d rxe0 -g 1
```

En Worker 1 (cliente):

```bash
ibv_rc_pingpong -d rxe0 -g 1 10.10.0.2
```

Resultado esperado:

```
8192000 bytes in 0.xx seconds = xxxx Mbit/sec
1000 iters in 0.xx seconds = xx.xx usec/iter
```

### 10.4 Port mirroring

En el switch, verificar que mirror0 ve tráfico del Worker 3:

```bash
sudo tcpdump -i mirror0 -c 10
```

Mientras tanto, desde Worker 3:

```bash
ping -c 5 10.10.0.1
```

Los paquetes deben aparecer en la captura de `mirror0`.

---

## 11. Captura y análisis con Wireshark

### 11.1 Capturar tráfico

Desde el switch (todo el tráfico OVS):

```bash
sudo tcpdump -i br0 -w /tmp/roce.pcap
```

O desde WSL (solo tráfico de gestión):

```bash
sudo tcpdump -i br-mgmt -w /tmp/roce.pcap
```

### 11.2 Copiar a Windows

```bash
cp /tmp/roce.pcap /mnt/c/Users/<USUARIO_WINDOWS>/Desktop/roce.pcap
```

### 11.3 Filtros útiles en Wireshark

| Filtro | Qué muestra |
|---|---|
| `udp.port == 4791` | Todo el tráfico RoCEv2 |
| `infiniband` | Paquetes con cabeceras InfiniBand |
| `infiniband.bth.opcode` | Filtrar por tipo de operación |
| `ip.addr == 10.10.0.3` | Tráfico del Worker 3 |

Si Wireshark no activa el disector InfiniBand: click derecho sobre paquete UDP:4791 → `Decode As...` → seleccionar `InfiniBand`.

---

## 12. Contadores disponibles para monitorización

### 12.1 Contadores estándar del puerto (workers)

Ubicación: `/sys/class/infiniband/rxe0/ports/1/counters/`

```bash
for f in /sys/class/infiniband/rxe0/ports/1/counters/*; do
    echo "$(basename $f): $(cat $f)"
done
```

| Contador | Qué mide |
|---|---|
| `port_xmit_data` | Datos transmitidos (unidades de 4 bytes) |
| `port_rcv_data` | Datos recibidos (unidades de 4 bytes) |
| `port_xmit_packets` | Paquetes transmitidos |
| `port_rcv_packets` | Paquetes recibidos |
| `port_xmit_discards` | Paquetes descartados al transmitir |
| `port_rcv_errors` | Errores en recepción |
| `unicast_xmit_packets` | Tráfico unicast TX |
| `unicast_rcv_packets` | Tráfico unicast RX |
| `multicast_xmit_packets` | Tráfico multicast TX |
| `multicast_rcv_packets` | Tráfico multicast RX |

### 12.2 Contadores específicos de Soft-RoCE (workers)

Ubicación: `/sys/class/infiniband/rxe0/ports/1/hw_counters/`

```bash
for f in /sys/class/infiniband/rxe0/ports/1/hw_counters/*; do
    echo "$(basename $f): $(cat $f)"
done
```

| Contador | Qué mide |
|---|---|
| `sent_pkts` | Paquetes procesados por el driver (TX) |
| `rcvd_pkts` | Paquetes procesados por el driver (RX) |
| `send_err` | Errores en la ruta de envío |
| `rcvd_seq_err` | Errores de secuencia en recepción |
| `out_of_seq_request` | Requests fuera de secuencia |
| `duplicate_request` | Requests duplicadas |
| `retry_exceeded_err` | Reintentos agotados |
| `retry_rnr_exceeded_err` | Reintentos RNR agotados |
| `rcvd_rnr_err` | Recepciones con Receiver Not Ready |
| `send_rnr_err` | Envíos con Receiver Not Ready |
| `ack_deferred` | ACKs diferidos |
| `rdma_sends` | Operaciones RDMA SEND |
| `rdma_recvs` | Operaciones RDMA RECV |
| `link_downed` | Veces que el link cayó |

### 12.3 Contadores ECN (workers)

Ubicación: `/proc/net/netstat`, línea `IpExt`.

```bash
awk '/IpExt:/ && !/^IpExt: [A-Z]/ {print "InCEPkts:", $18, "InECT0Pkts:", $17, "InECT1Pkts:", $16, "InNoECTPkts:", $15}' /proc/net/netstat
```

| Campo | Qué mide |
|---|---|
| `InCEPkts` | Paquetes marcados con CE (Congestion Experienced) |
| `InECT0Pkts` | Paquetes con bit ECT(0) |
| `InECT1Pkts` | Paquetes con bit ECT(1) |
| `InNoECTPkts` | Paquetes sin bits ECT |

### 12.4 Contadores OVS por puerto (switch)

```bash
sudo ovs-ofctl dump-ports br0
```

Muestra bytes TX/RX, paquetes TX/RX, drops y errores por puerto.

### 12.5 Formato JSON para automatización (workers)

```bash
rdma statistic show link rxe0 -j -p
```

### 12.6 Contadores no disponibles en Soft-RoCE

| Métrica | Motivo |
|---|---|
| PFC por prioridad (802.1Qbb) | Requiere hardware DCB real |
| CNP (Congestion Notification Packets) | Soft-RoCE no implementa DCQCN |

---

## 13. Scripts de automatización

### start-topology.sh

Crea la topología de red en WSL. Ejecutar antes de arrancar las VMs, después de cada reinicio de WSL.

```bash
#!/bin/bash
set -e

echo "=== Limpiando topología anterior ==="
for i in 0 1 2 3; do sudo ip link del tap$i 2>/dev/null || true; done
for br in br-roce br-link1 br-link2 br-link3 br-mgmt; do
    sudo ip link del $br 2>/dev/null || true
done
for tap in tap-w1 tap-w2 tap-w3 tap-s1 tap-s2 tap-s3 tap-sm; do
    sudo ip link del $tap 2>/dev/null || true
done

echo "=== Creando bridges punto a punto ==="
for i in 1 2 3; do
    sudo ip link add br-link$i type bridge
    sudo ip link set br-link$i up
    sudo ip tuntap add dev tap-w$i mode tap
    sudo ip link set tap-w$i master br-link$i
    sudo ip link set tap-w$i up
    sudo ip tuntap add dev tap-s$i mode tap
    sudo ip link set tap-s$i master br-link$i
    sudo ip link set tap-s$i up
done

echo "=== Creando bridge de gestión ==="
sudo ip link add br-mgmt type bridge
sudo ip link set br-mgmt up
sudo ip addr add 10.10.0.254/24 dev br-mgmt
sudo ip tuntap add dev tap-sm mode tap
sudo ip link set tap-sm master br-mgmt
sudo ip link set tap-sm up

echo "=== Configurando iptables ==="
sudo sysctl -w net.bridge.bridge-nf-call-iptables=0 > /dev/null
sudo sysctl -w net.bridge.bridge-nf-call-ip6tables=0 > /dev/null
sudo iptables -I FORWARD -j ACCEPT
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null

echo "=== Topología lista ==="
for br in br-link1 br-link2 br-link3 br-mgmt; do
    echo "--- $br ---"
    bridge link show master $br
done
echo ""
echo "Lanzar las VMs:"
echo "  ./start-vm.sh 1|2|3"
echo "  ./start-switch.sh"
```

### start-vm.sh

```bash
#!/bin/bash
VM_ID=${1:?Uso: ./start-vm.sh <1|2|3>}
MAC="52:54:00:00:00:0${VM_ID}"
DIR="$HOME/qemu-roce"

sudo qemu-system-x86_64 \
    -name "vm${VM_ID}" \
    -machine q35,accel=kvm \
    -cpu host \
    -m 2048 \
    -smp 2 \
    -drive "file=${DIR}/vm${VM_ID}.qcow2,format=qcow2,if=virtio" \
    -drive "file=${DIR}/seed-vm${VM_ID}.iso,format=raw,if=virtio" \
    -netdev "tap,id=net0,ifname=tap-w${VM_ID},script=no,downscript=no" \
    -device "virtio-net-pci,netdev=net0,mac=${MAC}" \
    -nographic \
    -serial mon:stdio
```

### start-switch.sh

```bash
#!/bin/bash
DIR="$HOME/qemu-roce"

sudo qemu-system-x86_64 \
    -name switch \
    -machine q35,accel=kvm \
    -cpu host \
    -m 2048 \
    -smp 2 \
    -drive "file=${DIR}/switch.qcow2,format=qcow2,if=virtio" \
    -drive "file=${DIR}/seed-switch.iso,format=raw,if=virtio" \
    -netdev tap,id=sw1,ifname=tap-s1,script=no,downscript=no \
    -device virtio-net-pci,netdev=sw1,mac=52:54:00:00:00:11 \
    -netdev tap,id=sw2,ifname=tap-s2,script=no,downscript=no \
    -device virtio-net-pci,netdev=sw2,mac=52:54:00:00:00:12 \
    -netdev tap,id=sw3,ifname=tap-s3,script=no,downscript=no \
    -device virtio-net-pci,netdev=sw3,mac=52:54:00:00:00:13 \
    -netdev tap,id=mgmt,ifname=tap-sm,script=no,downscript=no \
    -device virtio-net-pci,netdev=mgmt,mac=52:54:00:00:00:10 \
    -nographic \
    -serial mon:stdio
```

### cleanup.sh

```bash
#!/bin/bash
echo "=== Limpiando topología ==="
for tap in tap-w1 tap-w2 tap-w3 tap-s1 tap-s2 tap-s3 tap-sm; do
    sudo ip link del $tap 2>/dev/null || true
done
for br in br-link1 br-link2 br-link3 br-mgmt; do
    sudo ip link del $br 2>/dev/null || true
done
echo "Topología eliminada."
```

Hacer ejecutables:

```bash
chmod +x start-topology.sh start-vm.sh start-switch.sh cleanup.sh
```

---

## 14. Problemas frecuentes y soluciones

### "Could not access KVM kernel module: No such file or directory"

Verificar `nestedVirtualization=true` en `.wslconfig` y reiniciar WSL con `wsl --shutdown`.

Alternativa sin KVM: cambiar `-machine q35,accel=kvm` por `-machine q35,accel=tcg` y `-cpu host` por `-cpu max`.

### Las VMs no se ven entre sí

1. Verificar TAPs en los bridges: `bridge link show`.
2. Verificar OVS en el switch: `sudo ovs-vsctl show`.
3. Verificar IPs dentro de las VMs: `ip addr show`.
4. Verificar iptables: `sudo iptables -L FORWARD -v -n`.

### `modprobe rdma_rxe` falla dentro de la VM

```bash
sudo apt install -y linux-modules-extra-$(uname -r)
```

Para instalar paquetes la VM necesita internet. Arrancarla temporalmente con `-netdev user,id=net0` y `-device virtio-net-pci,netdev=net0`.

### OVS/Snort desaparecen del switch tras reinicio

Verificar que no se recreó `switch.qcow2` con `qemu-img create` (destruye el delta):

```bash
qemu-img info ~/qemu-roce/switch.qcow2
```

Si los paquetes se perdieron, reinstalar arrancando con NAT (sección 4.4).

### Netplan falla con "Cannot find unique matching interface"

El fichero `50-cloud-init.yaml` hace match por MAC antigua:

```bash
sudo tee /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg << 'EOF'
network: {config: disabled}
EOF
sudo rm -f /etc/netplan/50-cloud-init.yaml
sudo netplan apply
```

### `ibv_rc_pingpong` da "Failed status Work Request Flushed Error"

Usar `-g 1` y no `-g 0`. Con `-g 0` el driver usa IPv6 link-local sin routing configurado.

### La topología de red se pierde al reiniciar WSL

Ejecutar `start-topology.sh` después de cada reinicio de WSL, antes de arrancar las VMs.

### "could not configure /dev/net/tun: Device or resource busy"

El TAP ya está en uso por otra VM. Cada VM necesita su propio TAP. Verificar que no hay otra instancia de QEMU usando el mismo TAP.
