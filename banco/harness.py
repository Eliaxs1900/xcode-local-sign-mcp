#!/usr/bin/env python3
"""MCP test bench for xcode-local-sign-mcp.

Talks raw JSON-RPC over stdio to `uv run --script <server.py>` (the same way
Claude Code does), runs the same cases against any version of the server and
writes runs/<label>/results.json plus a readable report.txt.

usage: harness.py <label> <server.py> [case ...]
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

SCR = Path(__file__).resolve().parent
PROJ = SCR / "proj" / "XCLSmoke"
WS_ROOT = SCR / "proj"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/"
              "LaunchServices.framework/Versions/A/Support/lsregister")


# --------------------------------------------------------------------------
class Client:
    def __init__(self, script: Path, out: Path, env: dict | None = None):
        self.errf = open(out / "server_stderr.txt", "w")
        self.p = subprocess.Popen(["uv", "run", "--offline", "--script", str(script)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=self.errf, env={**os.environ, **(env or {})})
        self.msgs: dict = {}
        self.cv = threading.Condition()
        self.next_id = 1
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.p.stdout:
            try:
                m = json.loads(line)
            except Exception:
                continue
            with self.cv:
                if "id" in m and ("result" in m or "error" in m):
                    self.msgs[m["id"]] = (m, time.monotonic())
                    self.cv.notify_all()

    def send(self, obj):
        self.p.stdin.write((json.dumps(obj) + "\n").encode())
        self.p.stdin.flush()

    def start(self, method, params=None):
        i = self.next_id
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": i, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        return i, time.monotonic()

    def wait(self, i, timeout):
        end = time.monotonic() + timeout
        with self.cv:
            while i not in self.msgs:
                left = end - time.monotonic()
                if left <= 0:
                    return None, None
                self.cv.wait(left)
            return self.msgs[i]

    def request(self, method, params=None, timeout=900):
        i, t0 = self.start(method, params)
        m, t1 = self.wait(i, timeout)
        return m, (round(t1 - t0, 1) if t1 else None)

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(10)
        except Exception:
            self.p.kill()


def tool_result(m):
    """(is_error, text, parsed_json_or_None) of a tools/call response."""
    if m is None:
        return True, "<no response>", None
    if "error" in m:
        return True, json.dumps(m["error"]), None
    res = m["result"]
    text = "".join(c.get("text", "") for c in res.get("content", []))
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    return bool(res.get("isError")), text, parsed


def procs(*needles):
    out = subprocess.run(["ps", "-axo", "pid,ppid,etime,command"], capture_output=True, text=True).stdout
    me = str(os.getpid())
    return [l.strip()[:200] for l in out.splitlines()[1:]
            if any(n in l for n in needles) and " ps -axo" not in l and l.split()[0] != me]


def kill_matching(*needles):
    for l in procs(*needles):
        try:
            os.kill(int(l.split()[0]), 9)
        except Exception:
            pass


def ents_of(path) -> dict:
    r = subprocess.run(["codesign", "-d", "--entitlements", "-", "--xml", str(path)], capture_output=True)
    try:
        return plistlib.loads(r.stdout) if r.stdout.strip() else {}
    except Exception:
        return {"_raw": r.stdout.decode(errors="replace")}


def ls_registered(path_prefix: str) -> list[str]:
    out = subprocess.run([LSREGISTER, "-dump"], capture_output=True, text=True).stdout
    hits = set()
    for l in out.splitlines():
        l = l.strip()
        if l.startswith("path:") and path_prefix in l:
            hits.add(l.split("path:", 1)[1].strip().split(" (")[0])
    return sorted(hits)


class Listener:
    """TCP listener on 127.0.0.1 so the app's connect() has a peer."""

    def __init__(self):
        self.s = socket.socket()
        self.s.bind(("127.0.0.1", 0))
        self.s.listen(16)
        self.port = self.s.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.s.accept()
                c.close()
            except Exception:
                return


# --------------------------------------------------------------------------
class Suite:
    def __init__(self, label: str, script: Path):
        self.label = label
        self.R = SCR / "runs" / label
        if self.R.exists():
            shutil.rmtree(self.R)
        self.R.mkdir(parents=True)
        self.root = self.R / "root"          # XCODE_LOCAL_SIGN_OUT (new version only)
        self.c = Client(script, self.R, {"XCODE_LOCAL_SIGN_OUT": str(self.root)})
        self.results: list[dict] = []
        self.listener = Listener()
        self.app_mac = self.R / "dd_mac/Build/Products/Debug/XCLSmoke.app"
        self.app_net = self.R / "XCLSmoke_net.app"

    def rec(self, case, **kw):
        kw = {"case": case, **kw}
        self.results.append(kw)
        print(json.dumps(kw, ensure_ascii=False)[:600], flush=True)
        (self.R / "results.json").write_text(json.dumps(self.results, indent=2, ensure_ascii=False))

    def call(self, name, args, timeout=900):
        m, dt = self.c.request("tools/call", {"name": name, "arguments": args}, timeout)
        err, text, js = tool_result(m)
        return err, text, js, dt

    # -- cases -------------------------------------------------------------
    def handshake(self):
        m, dt = self.c.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "harness", "version": "1"}}, 120)
        self.c.notify("notifications/initialized")
        init = m["result"]
        m2, dt2 = self.c.request("tools/list", {}, 60)
        tools = m2["result"]["tools"]
        per_tool = {}
        for t in tools:
            props = t["inputSchema"].get("properties", {})
            per_tool[t["name"]] = {
                "params": len(props),
                "params_described": sum(1 for p in props.values() if p.get("description")),
                "annotations": t.get("annotations"),
                "description_chars": len(t.get("description") or ""),
            }
        (self.R / "tools_list.json").write_text(json.dumps(m2["result"], indent=2, ensure_ascii=False))
        self.rec("handshake", seconds=dt, server=init.get("serverInfo"),
                 protocol=init.get("protocolVersion"),
                 instructions=(init.get("instructions") or "")[:200] or None,
                 tools=per_tool)

    def signing_status(self):
        err, text, js, dt = self.call("signing_status", {})
        self.rec("signing_status()", seconds=dt, error=err,
                 identity_count=js and js.get("identity_count"), xcode=js and js.get("xcode"),
                 text=None if js else text[:300])
        err, text, js, dt = self.call("signing_status", {"project": str(PROJ)})
        self.rec("signing_status(project)", seconds=dt, error=err,
                 schemes=js and js.get("list", {}).get("project", {}).get("schemes"),
                 project_settings=js and js.get("project_settings"),
                 text=None if js else text[:300])
        err, text, js, dt = self.call("signing_status", {"project": str(WS_ROOT)})
        self.rec("signing_status(workspace in parent folder)", seconds=dt, error=err,
                 schemes=js and js.get("list", {}).get("workspace", {}).get("schemes"),
                 project_settings=js and js.get("project_settings"),
                 project_files=js and js.get("project_files"),
                 text=None if js else text[:300])

    def builds(self):
        err, text, js, dt = self.call("build_local", {
            "project": str(PROJ), "scheme": "XCLSmoke", "destination": "platform=macOS",
            "derived_data": str(self.R / "dd_mac")})
        self.rec("build_local macOS", seconds=dt, error=err, ok=js and js.get("ok"),
                 apps=js and list(js.get("apps", {})), entitlements=js and list(js.get("apps", {}).values()),
                 response_chars=len(text), text=None if js else text[:400])
        err, text, js, dt = self.call("build_local", {
            "project": str(PROJ), "scheme": "XCLSmokeCat",
            "derived_data": str(self.R / "dd_cat")})
        self.rec("build_local Mac Catalyst (default destination)", seconds=dt, error=err,
                 ok=js and js.get("ok"), apps=js and list(js.get("apps", {})),
                 response_chars=len(text), text=None if js else text[:400])

    def build_error(self):
        err, text, js, dt = self.call("build_local", {
            "project": str(PROJ), "scheme": "XCLSmoke", "destination": "platform=macOS",
            "derived_data": str(self.R / "dd_err"),
            "extra_settings": ["SWIFT_ACTIVE_COMPILATION_CONDITIONS=XCLSMOKE_BREAK"]})
        self.rec("build_local compile error", seconds=dt, error=err, ok=js and js.get("ok"),
                 errors=js and js.get("errors"), log_tail=js and js.get("log_tail"),
                 response_chars=len(text), text=None if js else text[:400])

    def build_injection(self):
        # "clean" is not KEY=VALUE: xcodebuild takes it as a second action.
        err, text, js, dt = self.call("build_local", {
            "project": str(PROJ), "scheme": "XCLSmoke", "destination": "platform=macOS",
            "derived_data": str(self.R / "dd_inject"), "extra_settings": ["clean"]})
        apps_on_disk = sorted(str(p) for p in (self.R / "dd_inject").glob("Build/Products/*/*.app"))
        self.rec("build_local extra_settings=['clean']", seconds=dt, error=err,
                 ok=js and js.get("ok"), apps_reported=js and list(js.get("apps", {})),
                 apps_left_on_disk=apps_on_disk, text=text[:400] if (err or not js) else None)

    def build_timeout(self):
        kill_matching("sleep 120")
        err, text, js, dt = self.call("build_local", {
            "project": str(PROJ), "scheme": "XCLSmoke", "destination": "platform=macOS",
            "derived_data": str(self.R / "dd_timeout"), "extra_settings": ["XCLSMOKE_SLOW=120"],
            "timeout_s": 15})
        time.sleep(3)
        left = procs("sleep 120")
        log = self.R / "dd_timeout" / "build_local.log"
        self.rec("build_local timeout_s=15 (build phase sleeps 120 s)", seconds=dt, error=err,
                 timed_out=js and js.get("timed_out"), log_saved=log.exists(),
                 leftover_processes=left, text=text[:300] if (err or not js) else None,
                 result=js and {k: js.get(k) for k in ("ok", "exit_code", "timed_out", "hints")})
        kill_matching("sleep 120")

    def entitlements(self):
        err, text, js, dt = self.call("show_entitlements", {"app_path": str(self.app_mac)})
        self.rec("show_entitlements(app)", seconds=dt, error=err, result=js if js is not None else text[:300])
        err, text, js, dt = self.call("show_entitlements", {"app_path": str(self.R / "does_not_exist.app")})
        self.rec("show_entitlements(missing app)", seconds=dt, error=err, result=js if js is not None else text[:300])

    def resign(self):
        err, text, js, dt = self.call("resign_adhoc", {
            "app_path": str(self.app_mac), "add": {"com.apple.security.network.client": True},
            "copy_to": str(self.app_net)})
        self.rec("resign_adhoc +network.client (copy)", seconds=dt, error=err,
                 signed=js and js.get("signed"), verify_ok=js and js.get("verify_ok"),
                 entitlements=js and js.get("entitlements"), text=None if js else text[:400])

    def resign_nested(self):
        # App with an XPC service (own sandbox entitlements) and a dylib inside.
        src = self.R / "nested_src.app"
        subprocess.run(["ditto", str(self.app_mac), str(src)], check=True)
        work = self.R / "nested_work"
        work.mkdir(exist_ok=True)
        (work / "helper.c").write_text("int main(void){return 0;}\n")
        (work / "lib.c").write_text("int xcls(void){return 1;}\n")
        xpc = src / "Contents/XPCServices/Helper.xpc"
        (xpc / "Contents/MacOS").mkdir(parents=True)
        subprocess.run(["clang", "-o", str(xpc / "Contents/MacOS/Helper"), str(work / "helper.c")], check=True)
        (xpc / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.example.xclsmoke.helper", "CFBundleExecutable": "Helper",
            "CFBundlePackageType": "XPC!", "CFBundleName": "Helper", "CFBundleVersion": "1",
            "XPCService": {"ServiceType": "Application"}}))
        xent = work / "helper.entitlements"
        xent.write_bytes(plistlib.dumps({"com.apple.security.app-sandbox": True,
                                         "com.apple.security.network.client": True}))
        subprocess.run(["codesign", "--force", "--sign", "-", "--entitlements", str(xent), str(xpc)], check=True)
        fw = src / "Contents/Frameworks"
        fw.mkdir(parents=True, exist_ok=True)
        subprocess.run(["clang", "-dynamiclib", "-o", str(fw / "libxcls.dylib"), str(work / "lib.c")], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", str(fw / "libxcls.dylib")], check=True)
        # A helper app (login item) with its own sandbox and a library inside,
        # left as the linker signed it.
        helper = src / "Contents/Library/LoginItems/HelperApp.app"
        (helper / "Contents/MacOS").mkdir(parents=True)
        (helper / "Contents/Frameworks").mkdir(parents=True)
        subprocess.run(["clang", "-o", str(helper / "Contents/MacOS/HelperApp"), str(work / "helper.c")], check=True)
        subprocess.run(["clang", "-dynamiclib", "-o", str(helper / "Contents/Frameworks/libhelper.dylib"),
                        str(work / "lib.c")], check=True)
        (helper / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.example.xclsmoke.helperapp", "CFBundleExecutable": "HelperApp",
            "CFBundlePackageType": "APPL", "CFBundleName": "HelperApp", "CFBundleVersion": "1"}))
        hent = work / "helperapp.entitlements"
        hent.write_bytes(plistlib.dumps({"com.apple.security.app-sandbox": True}))
        subprocess.run(["codesign", "--force", "--sign", "-", "--entitlements", str(hent), str(helper)], check=True)
        before = ents_of(xpc)
        before_helper = ents_of(helper)
        out = self.R / "nested_out.app"
        err, text, js, dt = self.call("resign_adhoc", {"app_path": str(src), "copy_to": str(out)})
        after = ents_of(out / "Contents/XPCServices/Helper.xpc")
        after_helper = ents_of(out / "Contents/Library/LoginItems/HelperApp.app")
        hv = subprocess.run(["codesign", "--verify", "--strict", str(out / "Contents/Library/LoginItems/HelperApp.app")],
                            capture_output=True, text=True)
        self.rec("resign_adhoc app with XPC service, helper app and dylibs", seconds=dt, error=err,
                 signed=js and js.get("signed"), verify_ok=js and js.get("verify_ok"),
                 verify=js and js.get("verify"),
                 nested_errors=js and js.get("nested_errors"),
                 xpc_entitlements_before=before, xpc_entitlements_after=after,
                 helper_app_entitlements_before=before_helper, helper_app_entitlements_after=after_helper,
                 helper_app_verify=hv.returncode == 0, helper_app_verify_msg=hv.stderr.strip()[-200:],
                 text=None if js else text[:400])

    def resign_copy_safety(self):
        precious = self.R / "precious"
        precious.mkdir(exist_ok=True)
        (precious / "important.txt").write_text("do not delete\n")
        err, text, js, dt = self.call("resign_adhoc", {"app_path": str(self.app_mac), "copy_to": str(precious)})
        self.rec("resign_adhoc copy_to=<existing non-.app folder>", seconds=dt, error=err,
                 important_txt_still_there=(precious / "important.txt").exists(),
                 folder_now_holds=sorted(p.name for p in precious.iterdir()) if precious.exists() else None,
                 text=text[:300])
        selfapp = self.R / "self.app"
        subprocess.run(["ditto", str(self.app_mac), str(selfapp)], check=True)
        err, text, js, dt = self.call("resign_adhoc", {"app_path": str(selfapp), "copy_to": str(selfapp)})
        self.rec("resign_adhoc copy_to == app_path", seconds=dt, error=err,
                 app_still_exists=selfapp.exists(), text=text[:300])
        err, text, js, dt = self.call("resign_adhoc", {
            "app_path": str(self.app_mac), "add": {"com.example.bad": None},
            "copy_to": str(self.R / "none_value.app")})
        self.rec("resign_adhoc add={key: null}", seconds=dt, error=err, text=text[:300])
        # Replacing: an earlier copy made by resign_adhoc yes, someone else's app no.
        again = self.R / "again.app"
        r1 = self.call("resign_adhoc", {"app_path": str(self.app_mac), "copy_to": str(again)})
        r2 = self.call("resign_adhoc", {"app_path": str(self.app_mac), "copy_to": str(again)})
        self.rec("resign_adhoc same copy_to twice", first_error=r1[0], second_error=r2[0],
                 text=r2[1][:200] if r2[0] else None)
        foreign = self.R / "Foreign.app"
        subprocess.run(["ditto", str(self.app_mac), str(foreign)], check=True)
        (foreign / "Contents/marker.txt").write_text("user's app\n")
        err, text, js, dt = self.call("resign_adhoc", {"app_path": str(self.app_mac), "copy_to": str(foreign)})
        self.rec("resign_adhoc copy_to=<an .app it did not create>", seconds=dt, error=err,
                 foreign_app_untouched=(foreign / "Contents/marker.txt").exists(), text=text[:300])

    def launches(self):
        port = self.listener.port
        err, text, js, dt = self.call("launch_with_trace", {
            "app_path": str(self.app_mac), "env": {"XCLSMOKE_PORT": str(port), "XCLSMOKE_RUN_SECONDS": "3"}, "seconds": 10,
            "evidence_dir": str(self.R / "ev_deny")})
        self.rec("launch_with_trace sandbox without network.client", seconds=dt, error=err,
                 exit=js and js.get("exit"),
                 console=js and [l for l in js.get("console", []) if "RESULT" in l],
                 system_log=js and [l[-150:] for l in js.get("system_log", []) if "deny" in l][:3],
                 text=None if js else text[:400])
        err, text, js, dt = self.call("launch_with_trace", {
            "app_path": str(self.app_net), "env": {"XCLSMOKE_PORT": str(port), "XCLSMOKE_RUN_SECONDS": "3"}, "seconds": 10,
            "evidence_dir": str(self.R / "ev_allow")})
        self.rec("launch_with_trace with network.client", seconds=dt, error=err,
                 exit=js and js.get("exit"),
                 console=js and [l for l in js.get("console", []) if "RESULT" in l],
                 system_log_deny=js and [l for l in js.get("system_log", []) if "deny" in l][:3],
                 text=None if js else text[:400])

    def launch_child(self):
        kill_matching("sleep 45")
        err, text, js, dt = self.call("launch_with_trace", {
            "app_path": str(self.app_mac), "env": {"XCLSMOKE_CHILD_SLEEP": "45"}, "seconds": 5,
            "evidence_dir": str(self.R / "ev_child")})
        time.sleep(1)
        self.rec("launch_with_trace seconds=5, app leaves a child holding stdout (sleep 45)",
                 seconds=dt, error=err, exit=js and js.get("exit"),
                 leftover_processes=procs("sleep 45"),
                 result=js and {k: js.get(k) for k in ("exit", "seconds", "children_killed")},
                 text=None if js else text[:300])
        kill_matching("sleep 45")

    def launch_output(self):
        err, text, js, dt = self.call("launch_with_trace", {
            "app_path": str(self.app_mac), "env": {"XCLSMOKE_LINES": "5000", "XCLSMOKE_LONGLINE": "200000"},
            "seconds": 10, "evidence_dir": str(self.R / "ev_big")})
        longest = max((len(l) for l in (js or {}).get("console", [])), default=0)
        self.rec("launch_with_trace 5000 lines + one 200 000-char line", seconds=dt, error=err,
                 response_chars=len(text), console_lines_returned=js and len(js.get("console", [])),
                 longest_returned_line=longest,
                 totals=js and {k: js.get(k) for k in ("console_lines_total", "console_full")},
                 text=None if js else text[:300])

    def launch_broken(self):
        broken = self.R / "Nope.app"
        (broken / "Contents").mkdir(parents=True, exist_ok=True)
        (broken / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "Nope"}))
        err, text, js, dt = self.call("launch_with_trace", {"app_path": str(broken), "seconds": 5,
                                                             "evidence_dir": str(self.R / "ev_broken")})
        time.sleep(1)
        self.rec("launch_with_trace .app without executable", seconds=dt, error=err, text=text[:300],
                 leftover_log_stream=procs('CONTAINS "Nope"'))
        kill_matching('CONTAINS "Nope"')

    def launch_simulator(self):
        err, text, js, dt = self.call("build_local", {
            "project": str(PROJ), "scheme": "XCLSmokeCat", "destination": "generic/platform=iOS Simulator",
            "derived_data": str(self.R / "dd_sim")})
        apps = list((js or {}).get("apps", {}))
        self.rec("build_local iOS Simulator", seconds=dt, error=err, ok=js and js.get("ok"), apps=apps,
                 text=None if js else text[:300])
        if apps:
            err, text, js, dt = self.call("launch_with_trace", {"app_path": apps[0], "seconds": 5,
                                                                 "evidence_dir": str(self.R / "ev_sim")})
            self.rec("launch_with_trace on an iOS Simulator .app", seconds=dt, error=err,
                     exit=js and js.get("exit"), console=js and js.get("console", [])[-5:],
                     text=text[:400] if (err or not js) else None)

    def launch_restricted(self):
        # A restricted entitlement without a provisioning profile: AMFI kills it at exec.
        err, text, js, dt = self.call("resign_adhoc", {
            "app_path": str(self.app_mac), "copy_to": str(self.R / "restricted.app"),
            "add": {"com.apple.developer.icloud-container-identifiers": ["iCloud.com.example.xclsmoke"]}})
        self.rec("resign_adhoc + restricted entitlement", seconds=dt, error=err,
                 signed=js and js.get("signed"), text=None if js else text[:300])
        err, text, js, dt = self.call("launch_with_trace", {
            "app_path": str(self.R / "restricted.app"), "seconds": 5,
            "evidence_dir": str(self.R / "ev_restricted")})
        self.rec("launch_with_trace app with a restricted entitlement", seconds=dt, error=err,
                 exit=js and js.get("exit"), exit_signal=js and js.get("exit_signal"),
                 hints=js and js.get("hints"), console=js and js.get("console", [])[-3:],
                 system_log=js and js.get("system_log", [])[-3:], text=None if js else text[:300])
        # evidence_dir always: the old version accepts 1000 s and would write to ~/Desktop.
        err, text, js, dt = self.call("launch_with_trace", {"app_path": str(self.app_mac), "seconds": 1000,
                                                             "evidence_dir": str(self.R / "ev_1000")})
        self.rec("launch_with_trace seconds=1000", seconds=dt, error=err, text=text[:300])

    def launch_stdin(self):
        i, t0 = self.c.start("tools/call", {"name": "launch_with_trace", "arguments": {
            "app_path": str(self.app_mac), "seconds": 8,
            "env": {"XCLSMOKE_READ_STDIN": "1", "XCLSMOKE_RUN_SECONDS": "6"},
            "evidence_dir": str(self.R / "ev_stdin")}})
        time.sleep(3)
        pings = [self.c.start("ping", {})[0] for _ in range(3)]
        m, t1 = self.c.wait(i, 120)
        err, text, js = tool_result(m)
        answered = []
        for pid_ in pings:
            pm, pt = self.c.wait(pid_, 15)
            answered.append(pm is not None)
        console = (js or {}).get("console", [])
        self.rec("launch_with_trace app reads stdin; client sends 3 pings meanwhile",
                 seconds=round(t1 - t0, 1) if t1 else None, error=err,
                 pings_answered=answered,
                 app_saw_on_stdin=[l for l in console if "stdin" in l][:4])

    def cancel_and_concurrency(self):
        kill_matching("sleep 60")
        dd = self.R / "dd_cancel"
        i, t0 = self.c.start("tools/call", {"name": "build_local", "arguments": {
            "project": str(PROJ), "scheme": "XCLSmoke", "destination": "platform=macOS",
            "derived_data": str(dd), "extra_settings": ["XCLSMOKE_SLOW=60"]}})
        time.sleep(8)
        p_id, p_t0 = self.c.start("ping", {})
        self.c.notify("notifications/cancelled", {"requestId": i, "reason": "harness cancel"})
        time.sleep(5)
        running_after_cancel = procs("sleep 60", str(dd))
        m, t1 = self.c.wait(i, 180)
        err, text, js = tool_result(m)
        pm, p_t1 = self.c.wait(p_id, 180)
        ping_latency = round(p_t1 - p_t0, 1) if p_t1 else None
        time.sleep(2)
        self.rec("build_local cancelled by the client after ~8 s (build phase sleeps 60 s)",
                 ping_latency_during_build=ping_latency,
                 response_after=round(t1 - t0, 1) if t1 else None,
                 response=text[:160],
                 running_5s_after_cancel=running_after_cancel,
                 leftover_after_response=procs("sleep 60", str(dd)))
        kill_matching("sleep 60")

    def clean(self):
        err, text, js, dt = self.call("build_local", {"project": str(PROJ), "scheme": "XCLSmoke",
                                                      "destination": "platform=macOS"})
        self.rec("build_local with default folder (XCODE_LOCAL_SIGN_OUT)", seconds=dt, error=err,
                 ok=js and js.get("ok"), apps=js and list(js.get("apps", {})), text=None if js else text[:300])
        reg_before = ls_registered(str(self.R))
        err, text, js, dt = self.call("clean_builds", {})
        self.rec("clean_builds() (dry run)", seconds=dt, error=err, result=js if js is not None else text[:400])
        err, text, js, dt = self.call("clean_builds", {"delete": True})
        reg_after = ls_registered(str(self.R))
        self.rec("clean_builds(delete=True)", seconds=dt, error=err,
                 result=js if js is not None else text[:400],
                 registered_in_launchservices_before=len(reg_before),
                 registered_in_launchservices_after=len(reg_after),
                 still_registered=reg_after)

    def ls_snapshot(self):
        reg = ls_registered(str(self.R))
        self.rec("LaunchServices: .app bundles of this run registered", count=len(reg), paths=reg)


ALL = ["handshake", "signing_status", "builds", "build_error", "build_injection", "build_timeout",
       "entitlements", "resign", "resign_nested", "resign_copy_safety", "launches", "launch_child",
       "launch_output", "launch_broken", "launch_simulator", "launch_restricted", "launch_stdin", "cancel_and_concurrency",
       "clean", "ls_snapshot"]

if __name__ == "__main__":
    label, script = sys.argv[1], Path(sys.argv[2]).resolve()
    cases = sys.argv[3:] or ALL
    s = Suite(label, script)
    t = time.time()
    try:
        if cases[0] != "handshake":
            s.handshake()
        for name in cases:
            try:
                getattr(s, name)()
            except Exception as e:  # keep going; record the harness failure
                s.rec(name, harness_exception=repr(e))
    finally:
        s.c.close()
    print(f"done in {time.time() - t:.0f} s -> {s.R / 'results.json'}")
