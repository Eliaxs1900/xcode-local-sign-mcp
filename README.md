# xcode-local-sign-mcp

Servidor **MCP** (Model Context Protocol) que deja a un asistente como Claude **compilar, firmar y lanzar apps de Xcode en tu Mac sin certificado de desarrollador de Apple**, y guardar una **traza de log** como prueba de lo que pasó.

Sirve, por ejemplo, para clonar un proyecto ajeno, que trae el `DEVELOPMENT_TEAM` de su autor, y que en tu Mac no compila o compila una app que el sandbox no deja arrancar. Con firma **ad-hoc** (`-`) la app se compila y se ejecuta en local con sus permisos reales y con el App Sandbox activo.

- Va en un único archivo, [`server.py`](server.py). Se ejecuta con [`uv`](https://docs.astral.sh/uv/), que instala las dependencias solo.
- Solo usa las herramientas de Apple que ya trae Xcode: `xcodebuild`, `codesign`, `security`, `log`.
- Licencia **MIT**.

## Herramientas

| Herramienta | Qué hace |
|---|---|
| `signing_status` | Identidades de firma del llavero y, si le pasas un proyecto, su equipo, estilo de firma y ajustes de sandbox y red. |
| `build_local` | `xcodebuild` con firma ad-hoc (equipo vaciado). Devuelve la ruta del `.app`, sus *entitlements*, los errores y pistas para fallos típicos: falta el Metal Toolchain, librerías que son punteros de Git LFS, carreras al resolver paquetes. |
| `show_entitlements` | Los *entitlements* que lleva firmados un `.app`, que son los que aplica el sandbox. |
| `resign_adhoc` | Vuelve a firmar un `.app` en ad-hoc añadiendo o quitando *entitlements*, en una copia si quieres. Sirve para comparar el antes y el después de un permiso del sandbox sin recompilar. Firma antes los frameworks y dylibs internos y conserva el *hardened runtime*. |
| `launch_with_trace` | Ejecuta la app con variables de entorno. Captura su consola (filtrable) y, en paralelo, las líneas del kernel y del sandbox sobre ese proceso. Lo guarda todo en una carpeta de evidencias. |

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

**3. Pídeselo al asistente**, por ejemplo: *«Compila este proyecto para Mac Catalyst sin certificado y lánzalo guardando la traza»*.

## Límites

- **Los tests de interfaz (XCUITest/XCTest) en macOS siguen necesitando un certificado de desarrollo**: Xcode rechaza la firma ad-hoc al ejecutar tests. Se consigue uno gratis iniciando sesión con un Apple ID en Xcode ▸ Settings ▸ Accounts.
- Las apps firmadas ad-hoc **solo funcionan en tu Mac**. No sirven para distribuirlas.
- Si la firma original lleva *hardened runtime*, `resign_adhoc` añade `com.apple.security.cs.disable-library-validation`. Así las librerías internas firmadas en ad-hoc pueden cargarse. Es solo para uso local.
- Requiere macOS con Xcode instalado.
