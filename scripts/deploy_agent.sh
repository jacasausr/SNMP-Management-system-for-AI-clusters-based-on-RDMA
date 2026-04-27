#!/bin/bash
# deploy_agent.sh — Despliega roce_agent.py en los 3 workers y reinicia snmpd

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_NAME="roce_agent.py"
AGENT="$SCRIPT_DIR/../src/agents/$AGENT_NAME"

if [ ! -f "$AGENT" ]; then
    echo "Error: $AGENT no encontrado"
    exit 1
fi

for i in 1 2 3; do
    ip="10.10.0.$i"
    echo "--- Worker $i ($ip) ---"
    scp "$AGENT" user@$ip:/tmp/
    ssh user@$ip "sudo cp /tmp/$AGENT_NAME /usr/local/bin/$AGENT_NAME && sudo chmod +x /usr/local/bin/$AGENT_NAME && sudo systemctl restart snmpd"
    echo "Verificando..."
    snmpwalk -v2c -c public $ip .1.3.6.1.4.1.99999
    echo ""
done
