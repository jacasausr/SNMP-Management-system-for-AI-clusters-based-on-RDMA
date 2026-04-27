#!/bin/bash
# Lanza los controladores de tráfico en los 3 workers vía SSH.
# Requiere haber copiado traffic_controller.py a cada worker.
# Uso: ./start-traffic.sh
#       ./start-traffic.sh stop   (para detenerlos)

WORKERS=("10.10.0.1" "10.10.0.2" "10.10.0.3")
USER="user"
SCRIPT="/home/user/traffic_controller.py"

if [ "${1}" = "stop" ]; then
    echo "=== Deteniendo controladores ==="
    for i in 1 2 3; do
        ip="${WORKERS[$((i-1))]}"
        echo "Parando worker $i ($ip)..."
        ssh -o StrictHostKeyChecking=no ${USER}@${ip} "pkill -f traffic_controller" 2>/dev/null
    done
    echo "Todos detenidos."
    exit 0
fi

echo "=== Lanzando controladores de tráfico ==="
for i in 1 2 3; do
    ip="${WORKERS[$((i-1))]}"
    echo "Arrancando worker $i ($ip)..."
    ssh -o StrictHostKeyChecking=no ${USER}@${ip} \
        "nohup python3 ${SCRIPT} --worker-id ${i} > /tmp/traffic_w${i}.log 2>&1 &"
done

echo ""
echo "Los 3 controladores están corriendo en background."
echo "Ver logs:  ssh user@10.10.0.X 'tail -f /tmp/traffic_wX.log'"
echo "Parar:    ./start-traffic.sh stop"
