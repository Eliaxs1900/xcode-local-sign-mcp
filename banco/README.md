# Banco de pruebas de la rama `mejoras-robustez`

Con esto se han hecho las pruebas y las capturas del PR. Esta rama no se fusiona: solo guarda las imágenes y el banco para repetirlo.

- `proj/`: proyecto de Xcode mínimo. `XCLSmoke` es una app de macOS sin ventana con App Sandbox; `XCLSmokeCat` es la misma para Mac Catalyst. Las dos llevan un `DEVELOPMENT_TEAM` falso, como un proyecto clonado. La app hace lo que le digan sus variables de entorno: `XCLSMOKE_PORT` / `XCLSMOKE_HOST` (conexión TCP), `XCLSMOKE_CHILD_SLEEP` (deja un hijo con su stdout), `XCLSMOKE_READ_STDIN`, `XCLSMOKE_LINES` / `XCLSMOKE_LONGLINE` (mucha salida) y `XCLSMOKE_RUN_SECONDS`. El build phase «Slow build (tests)» duerme `XCLSMOKE_SLOW` segundos.
- `harness.py`: arranca el servidor con `uv run --script`, le habla JSON-RPC por stdio como Claude Code y pasa los casos. Deja `runs/<etiqueta>/results.json`.
- `render.swift` y `make_shots.py`: generan las capturas a partir de `runs/before` y `runs/after`.

```bash
python3 harness.py after /ruta/a/server.py
python3 harness.py before /ruta/a/server-de-main.py handshake signing_status builds build_error build_injection build_timeout entitlements resign resign_nested resign_copy_safety launches launch_child launch_output launch_broken launch_simulator launch_restricted launch_stdin cancel_and_concurrency ls_snapshot
swiftc -O -o render render.swift && python3 make_shots.py
```

Con la versión de `main` no se pasa el caso `clean`: esa versión no tiene `XCODE_LOCAL_SIGN_OUT` y compilaría en `~/Desktop/xcode_local_builds`. El caso `clean` de la versión nueva borra al final todo lo que ha compilado la batería y lo da de baja en LaunchServices. Lo que crea el banco fuera de eso (copias propias, evidencias, contenedor del sandbox en `~/Library/Containers/com.example.xclsmoke*`) hay que borrarlo a mano.
