# Guía de Comandos para Configurar Kali Linux en VM3

Esta guía detalla los comandos utilizados para configurar Kali Linux en la máquina virtual `vm3` desde cero, asegurando que tus compañeros puedan replicar el proceso.

## 1. Investigación Inicial y Comprensión del Entorno

El primer paso fue investigar cómo obtener Kali Linux, inspeccionar la configuración existente de `cloud-init` y explorar métodos de instalación automatizada.

### Obtener Kali Linux
*   Para entender cómo obtener Kali Linux:
    ```bash
    google_web_search(query='Kali Linux official download ISO')
    ```

### Inspeccionar Configuración Existente
*   Para ver la estructura del directorio `cloud-init`:
    ```bash
    list_directory(dir_path='cloud-init')
    list_directory(dir_path='cloud-init/vm3')
    ```
*   Para leer los archivos de configuración existentes:
    ```bash
    read_file(file_path='cloud-init/vm3/user-data')
    read_file(file_path='cloud-init/vm3/network-config')
    ```

### Explorar Métodos de Instalación Automatizada
*   Para buscar métodos de creación de ISOs de arranque con `cloud-init` para Kali en QEMU:
    ```bash
    google_web_search(query='create bootable iso cloud-init kali linux qemu')
    google_web_search(query='kali linux unattended installation qemu cloud-init iso')
    ```
    *(Esta investigación determinó que usar una Imagen Cloud de Kali es el enfoque más adecuado para este proyecto.)*

## 2. Preparación del ISO de Inicio (`seed-vm3.iso`) para VM3

Este paso se centra en crear el medio de `cloud-init` necesario para la VM.

### Crear Archivo `meta-data`
*   Se crea un archivo `meta-data` para la configuración de `cloud-init`.
    ```bash
    write_file(file_path='cloud-init/vm3/meta-data', content='instance-id: vm3
local-hostname: vm3')
    ```

### Generar el `seed-vm3.iso`
*   Se genera el ISO de inicio usando `cloud-localds` con los datos de `cloud-init`.
    ```bash
    run_shell_command(command='cloud-localds seed-vm3.iso cloud-init/vm3/user-data cloud-init/vm3/meta-data', description='Crear ISO de inicio para VM3 usando datos de cloud-init.', dir_path='/home/jacr/qemu-roce')
    ```

## 3. Configuración de la Imagen Cloud de Kali para VM3

Aquí se descarga, convierte y prepara la imagen base de Kali Linux.

### Descargar la Imagen Cloud de Kali
*   Se descarga el archivo comprimido de la imagen cloud de Kali Linux (`.tar.xz`).
    ```bash
    run_shell_command(command='wget https://kali.download/cloud-images/current/kali-linux-2026.1-cloud-genericcloud-amd64.tar.xz', description='Descargar la imagen cloud de Kali Linux.', dir_path='/home/jacr/qemu-roce')
    ```

### Extraer la Imagen Cruda
*   Se extrae la imagen de disco cruda (`disk.raw`) del archivo descargado.
    ```bash
    run_shell_command(command='tar -xf kali-linux-2026.1-cloud-genericcloud-amd64.tar.xz', description='Extraer el archivo de imagen cloud de Kali Linux.', dir_path='/home/jacr/qemu-roce')
    ```

### Preparar el Archivo `vm3.qcow2`
*   Se elimina el antiguo archivo `vm3.qcow2` (que estaba causando problemas).
    ```bash
    run_shell_command(command='rm vm3.qcow2', description='Eliminar el antiguo archivo vm3.qcow2.', dir_path='/home/jacr/qemu-roce')
    ```
*   Se convierte la imagen cruda extraída (`disk.raw`) al formato QCOW2, nombrándola `vm3.qcow2` según lo requiere el script `start-vm.sh`.
    ```bash
    run_shell_command(command='qemu-img convert -f raw -O qcow2 disk.raw vm3.qcow2', description='Convertir la imagen cruda de Kali a formato QCOW2 y nombrarla vm3.qcow2.', dir_path='/home/jacr/qemu-roce')
    ```

---

Después de ejecutar estos comandos, tendrás los archivos necesarios `vm3.qcow2` y `seed-vm3.iso` en el directorio `/home/jacr/qemu-roce/`. Puedes entonces usar el script `start-vm.sh` para lanzar VM3.