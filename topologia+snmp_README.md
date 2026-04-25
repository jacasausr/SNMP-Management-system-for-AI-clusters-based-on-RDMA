# 🚀 GUÍA COMPLETA DE DESPLIEGUE: CLUSTER RoCEv2 Y TELEMETRÍA

> **Nota importante:** Sigue este orden de terminales en tu entorno WSL. Sitúate siempre en el directorio base `~/qemu-roce` antes de ejecutar los comandos.

---

## FASE 1: Preparación del Host (WSL)

### 1.1 Crear el Script de Red

Abre la **Terminal 1**, ejecuta `nano start-topology.sh` y pega el siguiente código:

```bash
#!/bin/bash
set -e

echo "=== Creando bridge y TAPs ==="
sudo ip link add br-roce type bridge 2>/dev/null || true
sudo ip link set br-roce up

for i in 0 1 2 10; do
    sudo ip tuntap add dev tap$i mode tap 2>/dev/null || true
    sudo ip link set tap$i master br-roce
    sudo ip link set tap$i up
done

sudo ip addr add 10.10.0.254/24 dev br-roce 2>/dev/null || true

echo "=== Configurando iptables ==="
sudo sysctl -w net.bridge.bridge-nf-call-iptables=0 > /dev/null
sudo sysctl -w net.bridge.bridge-nf-call-ip6tables=0 > /dev/null
sudo iptables -I FORWARD -i br-roce -o br-roce -j ACCEPT 2>/dev/null || true
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null

echo "=== Topología lista ==="
bridge link show
```

Cierra y guarda (Ctrl+O, Enter, Ctrl+X). Dale permisos de ejecución:
```bash
chmod +x start-topology.sh
```

### 1.2 Crear el Script para VM3

En la misma **Terminal 1**, ejecuta `nano start-mv3.sh` y pega este código:

```bash
#!/bin/bash
BASE_DIR="$HOME/qemu-roce"
VM_NAME="vm3"
TAP_IF="tap2"
MAC_ADDR="52:54:00:00:00:03"

sudo ip tuntap add dev $TAP_IF mode tap 2>/dev/null || true
sudo ip link set $TAP_IF master br-roce
sudo ip link set $TAP_IF up

mkdir -p "$BASE_DIR/cloud-init/$VM_NAME"

cat <<EOF > "$BASE_DIR/cloud-init/$VM_NAME/user-data"
#cloud-config
hostname: $VM_NAME
manage_etc_hosts: true
users:
  - name: user
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: false
    plain_text_passwd: "rdma"
ssh_pwauth: true
EOF

cat <<EOF > "$BASE_DIR/cloud-init/$VM_NAME/meta-data"
instance-id: $VM_NAME
local-hostname: $VM_NAME
EOF

genisoimage -quiet -output "$BASE_DIR/seed-$VM_NAME.iso" -volid cidata -joliet -rock \
   "$BASE_DIR/cloud-init/$VM_NAME/user-data" \
   "$BASE_DIR/cloud-init/$VM_NAME/meta-data"

if [ ! -f "$BASE_DIR/$VM_NAME.qcow2" ]; then
    qemu-img create -f qcow2 -b "$BASE_DIR/base-roce.qcow2" -F qcow2 "$BASE_DIR/$VM_NAME.qcow2"
fi

sudo qemu-system-x86_64 -name $VM_NAME -machine q35,accel=kvm -cpu host -m 2048 -smp 2 \
    -drive file="$BASE_DIR/$VM_NAME.qcow2",format=qcow2,if=virtio \
    -drive file="$BASE_DIR/seed-$VM_NAME.iso",format=raw,if=virtio \
    -netdev tap,id=net0,ifname=$TAP_IF,script=no,downscript=no \
    -device virtio-net-pci,netdev=net0,mac=$MAC_ADDR -nographic -serial mon:stdio
```

Cierra y guarda. Dale permisos de ejecución:
```bash
chmod +x start-mv3.sh
```

---

## FASE 2: Arranque de la Topología

Abre **5 pestañas** de terminal y ejecuta un comando en cada una para levantar la topología:

* **Terminal 1:** `sudo ./start-topology.sh`
* **Terminal 2 (VM1):** `./start-vm.sh 1`
* **Terminal 3 (VM2):** `./start-vm.sh 2`
* **Terminal 4 (VM3):** `./start-mv3.sh`
* **Terminal 5 (Switch):** `./start-switch.sh`

---

## FASE 3: Activación de Red (Consola QEMU)

Loguéate en las ventanas negras de QEMU utilizando las siguientes credenciales:
* **Usuario:** `user`
* **Contraseña:** `rdma`

### 3.1 Configuración de Workers (VM1, VM2, VM3)

En las **tres máquinas**, ejecuta `nano setup-roce.sh` y pega este bloque:

```bash
#!/bin/bash
sudo apt update && sudo apt install snmpd snmp -y

sudo bash -c 'cat > /etc/snmp/snmpd.conf' << 'EOF'
agentAddress udp:161
rocommunity public 10.10.0.254
rocommunity public localhost
pass_persist .1.3.6.1.4.1.99999 /usr/local/bin/roce_agent.py
EOF

sudo bash -c 'cat > /usr/local/bin/roce_agent.py' << 'EOF'
#!/usr/bin/env python3
import sys, os

BASE_OID, IFACE = ".1.3.6.1.4.1.99999", "enp0s2"

def read_val(f):
    try:
        with open(f"/sys/class/net/{IFACE}/statistics/{f}", "r") as f_obj:
            return f_obj.read().strip()
    except:
        return "0"

def get_metrics():
    m = {f"{BASE_OID}.1.{i}": "0" for i in range(1,10)}
    m[f"{BASE_OID}.1.1"], m[f"{BASE_OID}.1.2"] = read_val("tx_bytes"), read_val("rx_bytes")
    m[f"{BASE_OID}.1.3"], m[f"{BASE_OID}.1.4"] = read_val("tx_packets"), read_val("rx_packets")
    try:
        with open("/proc/net/netstat", "r") as f:
            lines = f.readlines()
            for i, line in enumerate(lines):
                if line.startswith("IpExt:"):
                    k = line.split()
                    v = lines[i+1].split()
                    m[f"{BASE_OID}.2.1"] = v[k.index("InCEPkts")]
                    m[f"{BASE_OID}.2.4"] = v[k.index("InNoECTPkts")]
    except:
        pass
    return m

def main():
    sys.stdout = os.fdopen(sys.stdout.fileno(), "w", 1)
    while True:
        l = sys.stdin.readline()
        if not l or "PING" in l:
            if l: print("PONG")
            continue
        oid = sys.stdin.readline().strip()
        metrics = get_metrics()
        if "getnext" in l:
            for k in sorted(metrics.keys()):
                if k > oid:
                    print(k); print("counter64"); print(metrics[k])
                    break
            else:
                print("NONE")
        else:
            if oid in metrics:
                print(oid); print("counter64"); print(metrics[oid])
            else:
                print("NONE")

if __name__ == "__main__":
    main()
EOF

sudo chmod +x /usr/local/bin/roce_agent.py
sudo systemctl restart snmpd
```

Hazlo ejecutable y lánzalo:
```bash
chmod +x setup-roce.sh && ./setup-roce.sh
```

### 3.2 Activación del Switch (Terminal 5)

En la consola del switch, ejecuta lo siguiente para habilitar la IP de gestión:

```bash
sudo ip addr add 10.10.0.10/24 dev enp0s2 && sudo ip link set enp0s2 up
```

---

## FASE 4: Gestión y Telemetría (SSH)

Abre **4 pestañas nuevas** en tu entorno WSL (Terminales 6 a 9) y conéctate:

* **T6:** `ssh user@10.10.0.1`
* **T7:** `ssh user@10.10.0.2`
* **T8:** `ssh user@10.10.0.3`
* **T9:** `ssh user@10.10.0.10`

### 4.1 Configuración Final del Switch (Terminal 9)

Dentro de la sesión SSH del Switch (`10.10.0.10`), configura el agente OVS instalando dependencias y editando archivos:

```bash
sudo apt update && sudo apt install openvswitch-switch snmpd -y
sudo ovs-vsctl add-br br-roce
```

Edita la configuración de SNMP: `sudo nano /etc/snmp/snmpd.conf`. Borra todo y pega:
```text
agentAddress udp:161
rocommunity public 10.10.0.254
rocommunity public localhost
pass_persist .1.3.6.1.4.1.99999.3 /usr/local/bin/ovs_agent.py
```

Crea el agente OVS: `sudo nano /usr/local/bin/ovs_agent.py` y pega el script en Python:
```python
#!/usr/bin/env python3
import sys, os, subprocess, re

BASE_OID = ".1.3.6.1.4.1.99999.3"

def get_metrics():
    m = {f"{BASE_OID}.{i}": "0" for i in range(1, 8)}
    try:
        res = subprocess.check_output(["sudo", "ovs-ofctl", "dump-ports", "br-roce"]).decode()
        m[f"{BASE_OID}.1"] = str(sum(map(int, re.findall(r"rx.*?bytes[:=](\d+)", res, re.I|re.S))))
        m[f"{BASE_OID}.2"] = str(sum(map(int, re.findall(r"tx.*?bytes[:=](\d+)", res, re.I|re.S))))
        m[f"{BASE_OID}.3"] = str(sum(map(int, re.findall(r"rx.*?pkts[:=](\d+)", res, re.I|re.S))))
        m[f"{BASE_OID}.4"] = str(sum(map(int, re.findall(r"tx.*?pkts[:=](\d+)", res, re.I|re.S))))
    except:
        pass
    return m

def main():
    sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 1)
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        if "PING" in line:
            print("PONG")
            continue
            
        oid_req = sys.stdin.readline().strip()
        metrics = get_metrics()
        
        if "getnext" in line:
            for k in sorted(metrics.keys()):
                if k > oid_req:
                    print(k); print("counter64"); print(metrics[k])
                    break
            else:
                print("NONE")
        else:
            if oid_req in metrics:
                print(oid_req); print("counter64"); print(metrics[oid_req])
            else:
                print("NONE")

if __name__ == "__main__":
    main()
```

Por último, dale permisos y reinicia el servicio:
```bash
sudo chmod +x /usr/local/bin/ovs_agent.py && sudo systemctl restart snmpd
```

---

## FASE 5: Verificación

**1. Generar tráfico (T6):**
Ejecuta esto desde la Terminal 6 (VM1), asegurándote de que VM2 está a la escucha:
```bash
ibv_rc_pingpong -d rxe0 -g 1 10.10.0.2
```

**2. Telemetría desde Host (T1):**
Prueba que los agentes SNMP devuelven los contadores consultando la MIB:
```bash
snmpwalk -v2c -c public 10.10.0.1 .1.3.6.1.4.1.99999
snmpwalk -v2c -c public 10.10.0.10 .1.3.6.1.4.1.99999.3
```
