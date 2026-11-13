#!/usr/bin/env python3
"""Turn runs/before and runs/after results into the PR screenshots.
Every number in the images comes from results.json of each run."""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
B = {r["case"]: r for r in json.load(open(RUNS / "before" / "results.json"))}
A = {r["case"]: r for r in json.load(open(RUNS / "after" / "results.json"))}
OLD, NEW = "main (37032fd)", "rama mejoras-robustez"


SCRATCH = str(HERE)


def short(s, n=96):
    # Local scratch paths (/private/tmp/... or /tmp/...) become "…".
    s = str(s).replace(SCRATCH, "…").replace(SCRATCH.removeprefix("/private"), "…")
    return s if len(s) <= n else s[: n - 1] + "…"


def ans(lst):
    return sum(1 for x in lst if x), len(lst)


def raw_shot(name, lines):
    (HERE / f"{name}.txt").write_text("\n".join(lines) + "\n")
    subprocess.run([str(HERE / "render"), str(HERE / f"{name}.txt"), str(HERE / f"{name}.png")], check=True)
    print("wrote", name)


def shot(name, title, before, after):
    raw_shot(name, [f"# {title}", "", f"## Antes · {OLD}"] + before + ["", f"## Después · {NEW}"] + after)


# 1. MCP channel and concurrency ---------------------------------------------
cs = "launch_with_trace app reads stdin; client sends 3 pings meanwhile"
cc = "build_local cancelled by the client after ~8 s (build phase sleeps 60 s)"
b, a = B[cs], A[cs]
bc, ac = B[cc], A[cc]
shot("01_canal_mcp", "Canal MCP: stdin heredado, concurrencia y cancelación",
     [f"> launch_with_trace (la app lee su stdin) y el cliente manda 3 ping mientras",
      f"- ping respondidos: {ans(b['pings_answered'])[0]} de 3; los demás no se responden nunca",
      *[f"- la app leyó del canal MCP: {short(l.replace('XCLSMOKE stdin got: ', ''), 70)}" for l in b["app_saw_on_stdin"][:2]],
      "",
      "> build_local de 60 s; a los 8 s el cliente manda ping y cancela",
      f"- el ping espera {bc['ping_latency_during_build']} s (el servidor está bloqueado)",
      f"- 5 s después de cancelar siguen vivos: {len(bc['running_5s_after_cancel'])} procesos (xcodebuild, sleep 60)",
      f"- respuesta a los {bc['response_after']} s: la compilación siguió hasta el final"],
     [f"> launch_with_trace (la app lee su stdin) y el cliente manda 3 ping mientras",
      f"+ ping respondidos: {ans(a['pings_answered'])[0]} de 3",
      f"+ la app ve: {a['app_saw_on_stdin'][0].replace('XCLSMOKE ', '')} (stdin cerrado)",
      "",
      "> build_local de 60 s; a los 8 s el cliente manda ping y cancela",
      f"+ el ping responde en {ac['ping_latency_during_build']} s",
      f"+ 5 s después de cancelar siguen vivos: {len(ac['running_5s_after_cancel'])} procesos",
      f"+ respuesta a los {ac['response_after']} s: {short(ac['response'], 60)}"])

# 2. Timeouts and hanging processes -------------------------------------------
ct = "build_local timeout_s=15 (build phase sleeps 120 s)"
ch = "launch_with_trace seconds=5, app leaves a child holding stdout (sleep 45)"
cb = "launch_with_trace .app without executable"
b, a = B[ct], A[ct]
bh, ah = B[ch], A[ch]
bb, ab = B[cb], A[cb]
shot("02_timeouts", "Timeouts y procesos colgados",
     ["> build_local con timeout_s=15 (un build phase duerme 120 s)",
      f"- {short(b['text'], 70)}",
      f"- log guardado: {b['log_saved']}; procesos huérfanos: {len(b['leftover_processes'])} ({short(b['leftover_processes'][0][-9:]) if b['leftover_processes'] else '-'})",
      "",
      "> launch_with_trace seconds=5; la app deja un hijo con su stdout (sleep 45) y sale",
      f"- la llamada tarda {bh['seconds']} s y dice exit = {bh['exit']!r}",
      "",
      "> launch_with_trace de un .app sin ejecutable",
      f"- {short(bb['text'], 80)}",
      f"- queda corriendo: {len(bb['leftover_log_stream'])} 'log stream'"],
     ["> build_local con timeout_s=15 (un build phase duerme 120 s)",
      f"+ timed_out={a['result']['timed_out']}, exit_code={a['result']['exit_code']} (SIGINT: xcodebuild cancela el build)",
      f"+ log guardado: {a['log_saved']}; procesos huérfanos: {len(a['leftover_processes'])}",
      "",
      "> launch_with_trace seconds=5; la app deja un hijo con su stdout (sleep 45) y sale",
      f"+ la llamada tarda {ah['seconds']} s; exit = {ah['exit']}, children_killed = {ah['result']['children_killed']}",
      f"+ procesos que quedan: {len(ah['leftover_processes'])}",
      "",
      "> launch_with_trace de un .app sin ejecutable",
      f"+ {short(ab['text'].replace('Error executing tool launch_with_trace: ', ''), 80)}",
      f"+ queda corriendo: {len(ab['leftover_log_stream'])} 'log stream'"])

# 3. Input validation -----------------------------------------------------------
ci = "build_local extra_settings=['clean']"
cp = "resign_adhoc copy_to=<existing non-.app folder>"
csf = "resign_adhoc copy_to == app_path"
cf = "resign_adhoc copy_to=<an .app it did not create>"
cm = "show_entitlements(missing app)"
cv = "launch_with_trace on an iOS Simulator .app"
E = "Error executing tool "
shot("03_validacion", "Validación de entradas",
     ["> build_local(extra_settings=['clean'])",
      f"- aceptado; xcodebuild compila y después limpia: apps = {B[ci]['apps_reported']}, en disco: {B[ci]['apps_left_on_disk']}",
      "> resign_adhoc(copy_to=<carpeta que ya existe, con important.txt>)",
      f"- important.txt sigue: {B[cp]['important_txt_still_there']}; la carpeta ahora tiene {B[cp]['folder_now_holds']}",
      "> resign_adhoc(copy_to = app_path)",
      f"- la app original sigue: {B[csf]['app_still_exists']}",
      "> resign_adhoc(copy_to=<un .app que no creó el servidor>)",
      f"- intacto: {B[cf]['foreign_app_untouched']} (sustituido por la copia)",
      "> show_entitlements(<ruta que no existe>)",
      f"- devuelve {B[cm]['result']} sin error (parece «sin entitlements»)",
      "> launch_with_trace(<app de simulador iOS>)",
      f"- exit {B[cv]['exit']}: {short(B[cv]['console'][-1], 70)}"],
     ["> build_local(extra_settings=['clean'])",
      f"+ {short(A[ci]['text'].replace(E + 'build_local: ', ''), 96)}",
      "> resign_adhoc(copy_to=<carpeta que ya existe, con important.txt>)",
      f"+ rechazado; important.txt sigue: {A[cp]['important_txt_still_there']}",
      "> resign_adhoc(copy_to = app_path)",
      f"+ rechazado; la app original sigue: {A[csf]['app_still_exists']}",
      "> resign_adhoc(copy_to=<un .app que no creó el servidor>)",
      f"+ rechazado; intacto: {A[cf]['foreign_app_untouched']}",
      "> show_entitlements(<ruta que no existe>)",
      f"+ {short(A[cm]['result'].replace(E + 'show_entitlements: ', ''), 50)}",
      "> launch_with_trace(<app de simulador iOS>)",
      f"+ {short(A[cv]['text'].replace(E + 'launch_with_trace: ', ''), 96)}"])

# 4. Nested code -------------------------------------------------------------
cn = "resign_adhoc app with XPC service, helper app and dylibs"
b, a = B[cn], A[cn]
keys = lambda d: sorted(k.replace("com.apple.security.", "") for k in d) if d else "{}"
shot("04_codigo_anidado", "resign_adhoc: código anidado (servicio XPC, app auxiliar, dylibs)",
     [f"- verify_ok = {b['verify_ok']}: {short(b['verify'].split(': ', 1)[-1].splitlines()[0], 60)}",
      f"- (en {short(b['verify'].splitlines()[-1].split('/')[-1], 40)})",
      f"- entitlements del XPC: {keys(b['xpc_entitlements_before'])} -> {keys(b['xpc_entitlements_after'])}",
      f"- la app auxiliar verifica: {b['helper_app_verify']}"],
     [f"+ verify_ok = {a['verify_ok']}",
      f"+ entitlements del XPC: {keys(a['xpc_entitlements_before'])} -> {keys(a['xpc_entitlements_after'])}",
      f"+ entitlements de la app auxiliar: {keys(a['helper_app_entitlements_after'])}; verifica: {a['helper_app_verify']}"])

# 5. Output size ------------------------------------------------------------
co = "launch_with_trace 5000 lines + one 200 000-char line"
ce = "build_local compile error"
b, a = B[co], A[co]
shot("05_salida", "Tamaño de las respuestas",
     ["> launch_with_trace: la app escribe 5000 líneas y una de 200 000 caracteres",
      f"- respuesta: {b['response_chars']:,} caracteres; línea más larga devuelta: {b['longest_returned_line']:,}".replace(",", " "),
      "- las líneas que quita console_filter no se guardan en ningún sitio",
      "> build_local con un error de compilación: «errores» devueltos",
      *[f"- {short(e.split('/')[-1] if '/' in e else e, 90)}" for e in B[ce]["errors"]]],
     ["> launch_with_trace: la app escribe 5000 líneas y una de 200 000 caracteres",
      f"+ respuesta: {a['response_chars']:,} caracteres; línea más larga devuelta: {a['longest_returned_line']}".replace(",", " "),
      f"+ console_lines_total = {a['totals']['console_lines_total']}; todo en console_full.txt",
      "> build_local con un error de compilación: «errores» devueltos",
      *[f"+ {short(e.split('/')[-1] if '/' in e else e, 90)}" for e in A[ce]["errors"]]])

# 6. What the AI sees -------------------------------------------------------
hb, ha = B["handshake"], A["handshake"]
desc = lambda h: (sum(t["params_described"] for t in h["tools"].values()), sum(t["params"] for t in h["tools"].values()))
ann = lambda h: sum(1 for t in h["tools"].values() if t["annotations"])
kr = "launch_with_trace app with a restricted entitlement"
cw = "signing_status(workspace in parent folder)"
shot("06_descripciones", "Lo que ve la IA: descripciones, esquema y pistas",
     [f"- herramientas: {len(hb['tools'])}; instrucciones del servidor: {'sí' if hb['instructions'] else 'ninguna'}",
      f"- parámetros con descripción: {desc(hb)[0]} de {desc(hb)[1]}; herramientas con anotaciones: {ann(hb)}",
      "> app firmada con un entitlement restringido (iCloud) y lanzada",
      f"- exit = {B[kr]['exit']}, sin más explicación",
      "> signing_status(<carpeta con un workspace que apunta a XCLSmoke/XCLSmoke.xcodeproj>)",
      f"- project_settings = {B[cw]['project_settings']}"],
     [f"+ herramientas: {len(ha['tools'])} (nueva: clean_builds); instrucciones del servidor: sí",
      f"+ parámetros con descripción: {desc(ha)[0]} de {desc(ha)[1]}; herramientas con anotaciones: {ann(ha)}",
      "+ límites en el esquema: seconds 1-600, timeout_s 10-7200",
      "> app firmada con un entitlement restringido (iCloud) y lanzada",
      f"+ exit = {A[kr]['exit']}, exit_signal = {A[kr]['exit_signal']}",
      f"+ hint: {short(A[kr]['hints'][0], 92)}",
      f"+ system_log: {short(' | '.join(A[kr]['system_log'][-2:]).split('] ')[-1], 80)}",
      "> signing_status(<carpeta con un workspace que apunta a XCLSmoke/XCLSmoke.xcodeproj>)",
      f"+ project_settings: {len(A[cw]['project_settings'])} ajustes, p. ej. DEVELOPMENT_TEAM = {A[cw]['project_settings'].get('DEVELOPMENT_TEAM')}"])

# 7. Cleanup -------------------------------------------------------------------
dry = A["clean_builds() (dry run)"]["result"]
rm = A["clean_builds(delete=True)"]
items = rm["result"]["items"]
shot("07_limpieza", "Limpieza: clean_builds y LaunchServices",
     [f"- al acabar la batería quedan {B['LaunchServices: .app bundles of this run registered']['count']} apps de prueba registradas en LaunchServices",
      "- el servidor no tiene forma de listarlas ni de darlas de baja"],
     [f"> clean_builds()  (en seco)",
      f"+ {len(dry['items'])} elementos, {dry['total_mb']} MB: {sum(1 for i in dry['items'] if i['kind'] == 'build')} carpetas de compilación y {sum(1 for i in dry['items'] if i['kind'] == 'copy')} copias; no borra nada",
      f"+ next: {short(dry['next'], 90)}",
      "> clean_builds(delete=true)",
      f"+ borrados: {sum(1 for i in items if i.get('deleted'))} de {len(items)}; dados de baja: {sum(len(i.get('unregistered', [])) for i in items)} apps",
      f"+ apps de la prueba registradas en LaunchServices: {rm['registered_in_launchservices_before']} -> {rm['registered_in_launchservices_after']}",
      "+ no toca lo que no creó el servidor: evidencias, Foreign.app, la copia del usuario"])

# 8. Sandbox A/B (the tool's main use) ---------------------------------------
cd_, ca_ = "launch_with_trace sandbox without network.client", "launch_with_trace with network.client"
d, al = A[cd_], A[ca_]
syslog = d["system_log"] or ["(vacío esta vez: macOS no repite el aviso de la misma app durante un rato)"]
rs = A["resign_adhoc +network.client (copy)"]
raw_shot("08_sandbox_ab", [
    "# Uso normal: A/B de un permiso del sandbox",
    "",
    f"## {NEW} (lo mismo que hacía antes, ahora sin los fallos de arriba)",
    "> resign_adhoc(XCLSmoke.app, add={\"com.apple.security.network.client\": true}, copy_to=XCLSmoke_net.app)",
    f"+ signed = {rs['signed']}, verify_ok = {rs['verify_ok']}",
    "",
    "> launch_with_trace(XCLSmoke.app)       sin network.client",
    *[f"- consola: {l.replace('XCLSMOKE RESULT ', '')}" for l in d["console"]],
    *[f"- system_log: {short(l.split('(Sandbox) ')[-1], 90)}" for l in syslog[:1]],
    "",
    "> launch_with_trace(XCLSmoke_net.app)   con network.client",
    *[f"+ consola: {l.replace('XCLSMOKE RESULT ', '')}" for l in al["console"]]])
