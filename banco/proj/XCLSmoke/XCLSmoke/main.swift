// Minimal test app for xcode-local-sign-mcp. No window: it prints, probes
// the sandbox and exits. Behaviour is driven by environment variables.
import Foundation

#if XCLSMOKE_BREAK
#error("XCLSMOKE intentional build error")
#endif

setvbuf(stdout, nil, _IOLBF, 0)
let env = ProcessInfo.processInfo.environment
print("XCLSMOKE start pid=\(getpid()) mode=\(env["XCLSMOKE_MODE"] ?? "default")")
print("XCLSMOKE home=\(NSHomeDirectory())")

// Network probe: TCP connect to 127.0.0.1:<port>. Without
// com.apple.security.network.client the App Sandbox denies it.
if let p = env["XCLSMOKE_PORT"], let port = UInt16(p) {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    var addr = sockaddr_in()
    addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = port.bigEndian
    let host = env["XCLSMOKE_HOST"] ?? "127.0.0.1"
    addr.sin_addr.s_addr = inet_addr(host)
    // Non-blocking connect with a 2 s limit: an unreachable host must not
    // stall the test, and "in progress" already means the sandbox allowed it.
    _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK)
    let rc = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    let e = errno
    if rc == 0 {
        print("XCLSMOKE RESULT connect \(host) ok")
    } else if e == EINPROGRESS {
        var pfd = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
        let n = poll(&pfd, 1, 2000)
        var soerr: Int32 = 0
        var len = socklen_t(MemoryLayout<Int32>.size)
        getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &len)
        if n == 0 {
            print("XCLSMOKE RESULT connect \(host) allowed by the sandbox (no answer in 2 s)")
        } else if soerr == 0 {
            print("XCLSMOKE RESULT connect \(host) ok")
        } else {
            print("XCLSMOKE RESULT connect \(host) allowed, then errno=\(soerr) \(String(cString: strerror(soerr)))")
        }
    } else {
        print("XCLSMOKE RESULT connect \(host) errno=\(e) \(String(cString: strerror(e)))")
    }
    close(fd)
}

// A child that inherits stdout and outlives the app (like a crash reporter
// or helper daemon would).
if let s = env["XCLSMOKE_CHILD_SLEEP"] {
    var pid: pid_t = 0
    var argv: [UnsafeMutablePointer<CChar>?] = [strdup("/bin/sleep"), strdup(s), nil]
    let rc = posix_spawn(&pid, "/bin/sleep", nil, nil, &argv, environ)
    print("XCLSMOKE spawned child pid=\(pid) rc=\(rc)")
}

// Output volume, for checking how much a tool returns.
if let n = env["XCLSMOKE_LINES"], let count = Int(n) {
    for i in 0..<count { print("XCLSMOKE line \(i) " + String(repeating: "x", count: 40)) }
}
if let n = env["XCLSMOKE_LONGLINE"], let len = Int(n) {
    print("XCLSMOKE long " + String(repeating: "y", count: len))
}

// Read whatever arrives on stdin (an app that reads its standard input).
if env["XCLSMOKE_READ_STDIN"] != nil {
    Thread.detachNewThread {
        while let line = readLine() {
            print("XCLSMOKE stdin got: " + String(line.prefix(120)))
        }
        print("XCLSMOKE stdin EOF")
    }
}

if let s = env["XCLSMOKE_RUN_SECONDS"], let secs = Double(s) {
    Thread.sleep(forTimeInterval: secs)
}
print("XCLSMOKE end")
exit(0)
