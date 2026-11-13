# xcode-local-sign-mcp

Servidor **MCP** (Model Context Protocol) que deja a un asistente como Claude **compilar, firmar y lanzar apps de Xcode en tu Mac sin certificado de desarrollador de Apple**, y guardar una **traza de log** como prueba de lo que pasó.

Sirve, por ejemplo, para clonar un proyecto ajeno, que trae el `DEVELOPMENT_TEAM` de su autor, y que en tu Mac no compila o compila una app que el sandbox no deja arrancar. Con firma **ad-hoc** (`-`) la app se compila y se ejecuta en local con sus permisos reales y con el App Sandbox activo.

- Va en un único archivo, [`server.py`](server.py). Se ejecuta con [`uv`](https://docs.astral.sh/uv/), que instala las dependencias solo.
- Solo usa las herramientas de Apple que ya trae macOS y Xcode: `xcodebuild`, `codesign`, `security`, `log`, `ditto`, `lsregister`.
- Licencia **MIT**.

## Herramientas

| Herramienta | Qué hace |
|---|---|
| `signing_status` | Identidades de firma del llavero y, si le pasas un proyecto o workspace, sus esquemas, su equipo, su estilo de firma y sus ajustes de sandbox y red. Con un workspace lee los proyectos que referencia. No cambia nada. |
| `build_local` | `xcodebuild` con firma ad-hoc (equipo vaciado). Devuelve la ruta del `.app`, sus *entitlements*, los errores y pistas para fallos típicos: falta el Metal Toolchain, librerías que son punteros de Git LFS, carreras al resolver paquetes. |
| `show_entitlements` | Los *entitlements* que lleva firmados un `.app`, que son los que aplica el sandbox. Da error si la ruta no existe o no está firmada. |
| `resign_adhoc` | Vuelve a firmar un `.app` en ad-hoc añadiendo o quitando *entitlements*, en una copia si quieres. Sirve para comparar el antes y el después de un permiso del sandbox sin recompilar. Firma antes el código interno (frameworks, dylibs, extensiones, servicios XPC y apps auxiliares); las extensiones, los servicios XPC y las apps auxiliares conservan sus propios *entitlements*. Mantiene el *hardened runtime*. |
| `launch_with_trace` | Ejecuta la app con variables de entorno. Captura su consola (filtrable) y, en paralelo, las líneas del kernel y del sandbox sobre ese proceso. Lo guarda todo en una carpeta de evidencias. |
| `clean_builds` | Lista lo que han creado `build_local` y `resign_adhoc` (carpetas de compilación y copias), con su tamaño. Con `delete=true` lo borra y da de baja sus apps en LaunchServices. |

Cada parámetro lleva su descripción en el esquema que recibe el asistente, y el servidor le envía unas instrucciones con el flujo habitual: `signing_status` → `build_local` → `resign_adhoc` → `launch_with_trace` → `clean_builds`.

## Ejemplo real: demostrar un fallo de sandbox

Caso real: issue [laanlabs/openshape3d#118](https://github.com/laanlabs/openshape3d/issues/118). En Mac, «Report a Bug» fallaba con *Operation not permitted*. Con `resign_adhoc` quitando y poniendo `com.apple.security.network.client`, y `launch_with_trace` en cada caso:

Sin `network.client`:
```
ISSUE118 RESULT underlying: Error Domain=NSPOSIXErrorDomain Code=1 "Operation not permitted"
kernel: (Sandbox) Sandbox: openshape3d(97219) deny(1) network-outbound remote:*:443
```
Con `network.client`:
```
ISSUE118 RESULT underlying: http(status: 403, message: "Permission denied on resource project …")
```
La petición llega al servidor, y el 403 es el esperado con un proyecto de prueba.

## Cómo se usa

**1. Instala `uv`** (una sola vez):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**2. Añade el servidor a Claude Code:**
```bash
claude mcp add -s user xcode-local-sign -- uv run --script /ruta/a/xcode-local-sign-mcp/server.py
```
En Claude Desktop o en otro cliente, el comando es `uv` con los argumentos `run --script /ruta/a/server.py`.

Si cambias `server.py`, reinicia el servidor (en Claude Code, `/mcp` y reconectar, o abre una sesión nueva): el código se carga al arrancar.

**3. Pídeselo al asistente**, por ejemplo: *«Compila este proyecto para Mac Catalyst sin certificado y lánzalo guardando la traza»*.

## Dónde deja las cosas y cómo limpiarlas

Todo va a `~/Desktop/xcode_local_builds`, o a la carpeta que indiques en la variable de entorno `XCODE_LOCAL_SIGN_OUT`:

```bash
claude mcp add -s user xcode-local-sign -e XCODE_LOCAL_SIGN_OUT=/ruta/a/builds -- uv run --script /ruta/a/server.py
```

- Una carpeta de compilación por esquema y destino (`<esquema>_<destino>`), que se reutiliza, así que recompilar es incremental. Dentro está `build_local.log`, que se escribe mientras compila.
- `evidence/`: una carpeta por lanzamiento con `console_full.txt` (toda la consola), `console.txt` (la filtrada), `system_log.txt` y `entitlements.json`.
- `.xcode-local-sign.json`: el registro de lo que ha creado el servidor, también las copias de `resign_adhoc` y las carpetas `derived_data` que creó fuera de aquí.

Xcode **registra en LaunchServices cada app que compila**, así que las copias de prueba acaban apareciendo en Spotlight, el Launchpad y «Abrir con». Cuando termines, pide al asistente que ejecute `clean_builds`: primero lista lo que borraría y, cuando le digas que sí, lo borra con `delete=true` y da de baja cada `.app` con `lsregister -u`.

`clean_builds` solo borra lo que creó el servidor: nunca una carpeta `derived_data` que ya existía ni una app que no sea una copia suya (y `resign_adhoc` tampoco sustituye nada que no sea una copia suya). No toca las evidencias ni los contenedores del sandbox, en `~/Library/Containers/<bundle id>`.

## Tiempos, cancelación y procesos

- Las órdenes se lanzan sin shell, cada una en su propio grupo de procesos y con la entrada estándar cerrada. La app nunca lee el canal MCP del servidor.
- Si una compilación pasa de `timeout_s` o el cliente cancela la petición, `xcodebuild` recibe primero SIGINT, que cancela también sus *build phases*, y después SIGTERM y SIGKILL. El log parcial se conserva y el resultado dice `timed_out`.
- Una compilación larga no bloquea el servidor: mientras tanto atiende otras peticiones.
- `launch_with_trace` para la app al acabar el tiempo y también los procesos que haya dejado en marcha (`children_killed`).
- Las respuestas van acotadas: líneas de hasta 400 caracteres y solo las últimas 60 de consola, 40 del log del sistema y 25 errores, con sus totales. El detalle completo está en los ficheros.

## Límites

- **Los tests de interfaz (XCUITest/XCTest) en macOS siguen necesitando un certificado de desarrollo**: Xcode rechaza la firma ad-hoc al ejecutar tests. Se consigue uno gratis iniciando sesión con un Apple ID en Xcode ▸ Settings ▸ Accounts.
- Las apps firmadas ad-hoc **solo funcionan en tu Mac**. No sirven para distribuirlas.
- Si la firma original lleva *hardened runtime*, `resign_adhoc` añade `com.apple.security.cs.disable-library-validation`. Así las librerías internas firmadas en ad-hoc pueden cargarse. Es solo para uso local.
- Los *entitlements* restringidos (`com.apple.developer.*`, `keychain-access-groups`…) necesitan un perfil de aprovisionamiento: con firma ad-hoc el sistema mata la app al arrancar (SIGKILL). `launch_with_trace` lo indica, y con `resign_adhoc` se pueden quitar.
- Un `system_log` vacío **no demuestra** que el sandbox no denegara nada: macOS pierde el aviso si la app sale justo después de la denegación y deja de repetir avisos de la misma app durante un rato. La primera prueba es el error que da la propia app en su consola.
- `launch_with_trace` ejecuta apps de macOS y Mac Catalyst. Una compilación para el simulador de iOS se ejecuta con `xcrun simctl`, y la herramienta lo explica si se lo pasas.
- Requiere macOS con Xcode instalado.
