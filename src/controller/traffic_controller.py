#!/usr/bin/env python3
"""
Controlador de tráfico para simulación de entrenamiento distribuido.

Simula el patrón de tráfico de un cluster de entrenamiento IA:
  1. Fase compute (forward + backward pass): sin tráfico de red
  2. Fase communicate (ring all-reduce): ráfagas RDMA al vecino del anillo

Ring all-reduce con 3 workers:
  W1 → W2 → W3 → W1

Cada worker:
  - Escucha conexiones del vecino anterior (servidor ib_write_bw en bucle)
  - Envía ráfagas al vecino siguiente (cliente ib_write_bw)

Uso:
  python3 traffic_controller.py --worker-id 1
  python3 traffic_controller.py --worker-id 2
  python3 traffic_controller.py --worker-id 3

El worker 3 simula degradación por "malware" (fase extra de sleep).
"""

import argparse
import logging
import os
import random
import signal
import subprocess
import sys
import time
import threading

# --- Configuración del anillo ---

RING = {
    1: {"ip": "10.10.0.1", "listen_port": 18501, "send_to": ("10.10.0.2", 18502)},
    2: {"ip": "10.10.0.2", "listen_port": 18502, "send_to": ("10.10.0.3", 18503)},
    3: {"ip": "10.10.0.3", "listen_port": 18503, "send_to": ("10.10.0.1", 18501)},
}

# --- Parámetros de tráfico ---

RXE_DEVICE = "rxe0"
GID_INDEX = 1

# Fase compute: simula forward + backward pass
COMPUTE_TIME_BASE = 3.0        # segundos base
COMPUTE_TIME_VARIANCE = 2.0    # ±varianza (uniforme)

# Fase communicate: parámetros de ib_write_bw
MSG_SIZE = 65536               # 64 KB por mensaje (gradiente típico)
DURATION = 2                   # duración de cada ráfaga (segundos)
TX_DEPTH = 32                  # profundidad de cola de envío

# Worker 3 (hackeado): degradación extra
HACKED_EXTRA_DELAY = 0.5       # segundos extra por iteración (simula carga de minería)

# Pausa entre iteraciones
INTER_ITERATION_BASE = 0.1
INTER_ITERATION_VARIANCE = 0.2

# --- Logg-ing ---

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [W%(worker_id)s] %(message)s",
    datefmt="%H:%M:%S",
)


class TrafficController:
    """Controla la simulación de tráfico de un worker."""

    def __init__(self, worker_id: int):
        self.worker_id = worker_id
        self.config = RING[worker_id]
        self.is_hacked = (worker_id == 3)
        self.running = True
        self.server_process = None
        self.server_thread = None
        self.iteration = 0

        # Adapter para logging con worker_id
        self.logger = logging.getLogger(f"worker{worker_id}")
        self._log_extra = {"worker_id": worker_id}

    def log(self, msg: str, *args):
        self.logger.info(msg, *args, extra=self._log_extra)

    def log_warn(self, msg: str, *args):
        self.logger.warning(msg, *args, extra=self._log_extra)

    # --- Servidor ib_write_bw (recibe datos del vecino anterior) ---

    def _run_server_loop(self):
        """Bucle que relanza el servidor ib_write_bw tras cada conexión."""
        port = self.config["listen_port"]
        self.log("Servidor escuchando en puerto %d", port)

        while self.running:
            cmd = [
                "ib_write_bw",
                "-d", RXE_DEVICE,
                "-x", str(GID_INDEX),
                "-p", str(port),
                "-s", str(MSG_SIZE),
                "-D", str(DURATION),
                ]
            try:
                self.server_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.server_process.wait()
            except Exception as e:
                if self.running:
                    self.log_warn("Error en servidor: %s", e)
                    time.sleep(1)

            # Pequeña pausa antes de relanzar
            if self.running:
                time.sleep(0.2)

        self.log("Servidor detenido")

    def start_server(self):
        """Arranca el servidor en un hilo separado."""
        self.server_thread = threading.Thread(
            target=self._run_server_loop,
            daemon=True,
        )
        self.server_thread.start()

    # --- Cliente ib_write_bw (envía datos al vecino siguiente) ---

    def _send_to_neighbor(self) -> bool:
        """Ejecuta ib_write_bw como cliente hacia el vecino siguiente.

        Returns:
            True si la transferencia fue exitosa.
        """
        target_ip, target_port = self.config["send_to"]

        cmd = [
            "ib_write_bw",
            "-d", RXE_DEVICE,
            "-x", str(GID_INDEX),
            "-p", str(target_port),
            "-s", str(MSG_SIZE),
            "-D", str(DURATION),
            target_ip,
        ]

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=DURATION + 10,
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            self.log_warn("Timeout enviando a %s:%d", target_ip, target_port)
            return False
        except Exception as e:
            self.log_warn("Error enviando a %s:%d — %s", target_ip, target_port, e)
            return False

    # --- Ciclo de entrenamiento ---

    def _compute_phase(self):
        """Simula forward + backward pass (solo CPU, sin red)."""
        duration = COMPUTE_TIME_BASE + random.uniform(
            -COMPUTE_TIME_VARIANCE, COMPUTE_TIME_VARIANCE
        )
        duration = max(1.0, duration)  # mínimo 1 segundo

        self.log(
            "Iteración %d — compute (%.1fs)",
            self.iteration,
            duration,
        )
        time.sleep(duration)

    def _communicate_phase(self):
        """Simula ring all-reduce: envía gradientes al vecino siguiente.

        En un ring all-reduce real hay 2*(k-1) pasos (reduce-scatter + all-gather).
        Con 3 workers = 4 pasos. Simulamos 2 ráfagas (reduce-scatter + all-gather).
        """
        target_ip, target_port = self.config["send_to"]
        self.log(
            "Iteración %d — all-reduce → %s:%d",
            self.iteration,
            target_ip,
            target_port,
        )

        # Reduce-scatter
        success = self._send_to_neighbor()
        if not success:
            self.log_warn("Reduce-scatter falló, reintentando en 2s...")
            time.sleep(2)
            self._send_to_neighbor()

        # Pequeña pausa entre fases (simula sincronización)
        time.sleep(random.uniform(0.1, 0.3))

        # All-gather
        success = self._send_to_neighbor()
        if not success:
            self.log_warn("All-gather falló, continuando...")

    def _hacked_overhead(self):
        """Simula la degradación causada por la minería en el worker hackeado.

        El worker 3 tarda un poco más en cada iteración porque parte
        de sus recursos van a la criptominería. Esto lo convierte
        en el straggler natural del cluster.
        """
        delay = HACKED_EXTRA_DELAY + random.uniform(0, 0.3)
        time.sleep(delay)

    def run(self):
        """Bucle principal del controlador."""
        self.log("=== Controlador iniciado ===")
        if self.is_hacked:
            self.log("MODO HACKEADO — degradación activa (+%.1fs/iter)", HACKED_EXTRA_DELAY)

        # Arrancar servidor en background
        self.start_server()

        # Esperar a que el servidor esté listo
        time.sleep(2)

        # Espera inicial aleatoria para que los workers no arranquen
        # exactamente a la vez (simula arranque escalonado real)
        startup_jitter = random.uniform(0, 1.0)
        self.log("Esperando %.1fs antes de comenzar...", startup_jitter)
        time.sleep(startup_jitter)

        while self.running:
            self.iteration += 1

            try:
                # 1. Fase compute
                self._compute_phase()

                if not self.running:
                    break

                # 2. Fase communicate (all-reduce)
                self._communicate_phase()

                if not self.running:
                    break

                # 3. Overhead del malware (solo worker 3)
                if self.is_hacked:
                    self._hacked_overhead()

                # 4. Pausa inter-iteración
                pause = INTER_ITERATION_BASE + random.uniform(
                    0, INTER_ITERATION_VARIANCE
                )
                time.sleep(pause)

            except Exception as e:
                self.log_warn("Error en iteración %d: %s", self.iteration, e)
                time.sleep(2)

        self.log("=== Controlador detenido tras %d iteraciones ===", self.iteration)

    def stop(self):
        """Detiene el controlador y mata el servidor."""
        self.running = False

        if self.server_process and self.server_process.poll() is None:
            self.server_process.terminate()
            try:
                self.server_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.server_process.kill()


def main():
    parser = argparse.ArgumentParser(
        description="Controlador de tráfico para simulación de entrenamiento IA"
    )
    parser.add_argument(
        "--worker-id",
        type=int,
        choices=[1, 2, 3],
        required=True,
        help="ID del worker (1, 2 o 3)",
    )
    args = parser.parse_args()

    controller = TrafficController(args.worker_id)

    def shutdown(sig, frame):
        controller.log("Recibida señal de parada")
        controller.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    controller.run()


if __name__ == "__main__":
    main()