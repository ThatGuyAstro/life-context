#[path = "../src/supervisor.rs"]
mod supervisor;

use serde::Deserialize;
use std::env;
use std::ffi::{OsStr, OsString};
use std::fs;
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use supervisor::{ServiceStatus, ServiceSupervisor, SupervisorBridge};

const PYTHON_STUB: &str = r###"
from pathlib import Path
import json
import os
from socket import AF_INET, SO_REUSEADDR, SOCK_STREAM, SOL_SOCKET, socket
import sys
import time

output = Path(os.environ["LIFE_CONTEXT_SMOKE_OUTPUT"]) / "__MARKER__.json"
output.parent.mkdir(parents=True, exist_ok=True)
listener = socket(AF_INET, SOCK_STREAM)
listener.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
requested_port = int(os.environ["__PORT_ENV__"])
listener.bind(("127.0.0.1", requested_port))
listener.listen(16)
bound_port = listener.getsockname()[1]
listener.setblocking(False)
output.write_text(
    json.dumps(
        {
            "module": "__MARKER__",
            "pid": os.getpid(),
            "cwd": os.getcwd(),
            "pythonpath": os.environ.get("PYTHONPATH"),
            "unbuffered": os.environ.get("PYTHONUNBUFFERED"),
            "life_context_root": os.environ.get("LIFE_CONTEXT_ROOT"),
            "context_server_port": os.environ.get("CONTEXT_SERVER_PORT"),
            "collector_server_url": os.environ.get("COLLECTOR_SERVER_URL"),
            "collector_roots": os.environ.get("COLLECTOR_ROOTS"),
            "collector_smoke_port": os.environ.get("COLLECTOR_SMOKE_PORT"),
            "requested_port": os.environ.get("__PORT_ENV__"),
            "bound_port": bound_port,
            "executable": sys.executable,
        }
    ),
    encoding="utf-8",
)

while True:
    if os.environ.get("LIFE_CONTEXT_SMOKE_EXIT_MODULE") == "__MARKER__":
        sys.exit(17)
    try:
        connection, _ = listener.accept()
    except BlockingIOError:
        time.sleep(0.05)
    else:
        connection.close()
"###;

fn test_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

struct EnvironmentRestore {
    values: Vec<(&'static str, Option<OsString>)>,
}

impl EnvironmentRestore {
    fn capture(names: &[&'static str]) -> Self {
        Self {
            values: names
                .iter()
                .map(|name| (*name, env::var_os(name)))
                .collect(),
        }
    }
}

impl Drop for EnvironmentRestore {
    fn drop(&mut self) {
        for (name, value) in &self.values {
            match value {
                Some(value) => env::set_var(name, value),
                None => env::remove_var(name),
            }
        }
    }
}

struct FixtureRoot {
    path: PathBuf,
}

impl FixtureRoot {
    fn new() -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock is before the Unix epoch")
            .as_nanos();
        let path = env::temp_dir().join(format!(
            "life-context-supervisor-smoke-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir_all(&path).expect("create supervisor smoke fixture root");

        for (package, marker) in [("context_server", "server"), ("collector", "collector")] {
            let package_root = path.join("services").join(package);
            fs::create_dir_all(&package_root).expect("create Python fixture package");
            fs::write(package_root.join("__init__.py"), b"").expect("write Python package marker");
            let port_environment = match marker {
                "server" => "CONTEXT_SERVER_PORT",
                "collector" => "COLLECTOR_SMOKE_PORT",
                _ => unreachable!("fixture marker must have a smoke port environment"),
            };
            fs::write(
                package_root.join("__main__.py"),
                PYTHON_STUB
                    .replace("__MARKER__", marker)
                    .replace("__PORT_ENV__", port_environment),
            )
            .expect("write Python fixture module");
        }

        Self { path }
    }

    fn evidence_path(&self, marker: &str) -> PathBuf {
        self.path.join("evidence").join(format!("{marker}.json"))
    }
}

impl Drop for FixtureRoot {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

struct StopOnDrop<'a>(&'a SupervisorBridge);

impl Drop for StopOnDrop<'_> {
    fn drop(&mut self) {
        let _ = self.0.stop();
    }
}

#[derive(Debug, Deserialize)]
struct ChildEvidence {
    module: String,
    pid: u32,
    cwd: String,
    pythonpath: Option<String>,
    unbuffered: Option<String>,
    life_context_root: Option<String>,
    context_server_port: Option<String>,
    collector_server_url: Option<String>,
    collector_roots: Option<String>,
    collector_smoke_port: Option<String>,
    requested_port: String,
    bound_port: u16,
    executable: String,
}

fn resolve_python() -> PathBuf {
    let probes: [(&str, &[&str]); 3] = [
        ("python", &["-c", "import sys; print(sys.executable)"]),
        ("python3", &["-c", "import sys; print(sys.executable)"]),
        ("py", &["-3", "-c", "import sys; print(sys.executable)"]),
    ];

    for (program, arguments) in probes {
        let Ok(output) = Command::new(program).args(arguments).output() else {
            continue;
        };
        if !output.status.success() {
            continue;
        }
        let Some(path) = String::from_utf8_lossy(&output.stdout)
            .lines()
            .next()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
        else {
            continue;
        };
        if path.is_file() {
            return path;
        }
    }

    panic!("a Python 3 executable is required for the supervisor lifecycle smoke test");
}

fn canonical(path: &Path) -> PathBuf {
    fs::canonicalize(path)
        .unwrap_or_else(|error| panic!("could not canonicalize {}: {error}", path.display()))
}

fn wait_until(label: &str, timeout: Duration, mut condition: impl FnMut() -> bool) {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if condition() {
            return;
        }
        thread::sleep(Duration::from_millis(50));
    }
    panic!("timed out waiting for {label}");
}

fn process_is_alive(pid: u32) -> bool {
    if cfg!(windows) {
        let Ok(output) = Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}"), "/FO", "CSV", "/NH"])
            .output()
        else {
            return false;
        };
        return output.status.success()
            && String::from_utf8_lossy(&output.stdout).contains(&format!("\"{pid}\""));
    }

    Command::new("kill")
        .args(["-0", &pid.to_string()])
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

fn free_loopback_ports(count: usize) -> Vec<u16> {
    let listeners: Vec<TcpListener> = (0..count)
        .map(|_| TcpListener::bind("127.0.0.1:0").expect("bind disposable loopback port"))
        .collect();
    let ports = listeners
        .iter()
        .map(|listener| {
            listener
                .local_addr()
                .expect("read disposable loopback port")
                .port()
        })
        .collect();
    drop(listeners);
    ports
}

fn port_is_listening(port: u16) -> bool {
    TcpStream::connect_timeout(
        &SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(100),
    )
    .is_ok()
}

fn read_evidence(path: &Path) -> ChildEvidence {
    let contents =
        fs::read_to_string(path).unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    serde_json::from_str(&contents)
        .unwrap_or_else(|error| panic!("parse {}: {error}", path.display()))
}

fn assert_evidence(
    path: &Path,
    fixture_root: &Path,
    inherited_pythonpath: &Path,
    python: &Path,
    marker: &str,
    server_port: u16,
    collector_port: u16,
) -> ChildEvidence {
    let evidence = read_evidence(path);
    assert_eq!(evidence.module, marker);
    assert!(evidence.pid > 0, "fixture child must report a PID");
    assert_eq!(canonical(Path::new(&evidence.cwd)), canonical(fixture_root));
    let pythonpath = evidence
        .pythonpath
        .as_deref()
        .expect("supervisor must set PYTHONPATH");
    let pythonpath_entries: Vec<PathBuf> = env::split_paths(OsStr::new(pythonpath)).collect();
    assert_eq!(pythonpath_entries.len(), 2);
    assert_eq!(canonical(&pythonpath_entries[0]), canonical(fixture_root));
    assert_eq!(
        canonical(&pythonpath_entries[1]),
        canonical(inherited_pythonpath)
    );
    assert_eq!(evidence.unbuffered.as_deref(), Some("1"));
    assert_eq!(
        canonical(Path::new(
            evidence
                .life_context_root
                .as_deref()
                .expect("supervisor must preserve LIFE_CONTEXT_ROOT"),
        )),
        canonical(fixture_root)
    );
    let expected_server_port = server_port.to_string();
    let expected_collector_port = collector_port.to_string();
    let expected_bound_port = if marker == "server" {
        server_port
    } else {
        collector_port
    };
    let expected_collector_url = format!("https://collector.example.test:{server_port}");
    assert_eq!(
        evidence.context_server_port.as_deref(),
        Some(expected_server_port.as_str())
    );
    assert_eq!(
        evidence.collector_server_url.as_deref(),
        Some(expected_collector_url.as_str())
    );
    assert_eq!(
        evidence.collector_roots.as_deref(),
        Some("C:\\CaptureRoot;D:\\Notes")
    );
    assert_eq!(
        evidence.collector_smoke_port.as_deref(),
        Some(expected_collector_port.as_str())
    );
    assert_eq!(
        evidence.requested_port,
        if marker == "server" {
            expected_server_port.clone()
        } else {
            expected_collector_port.clone()
        }
    );
    assert_eq!(evidence.bound_port, expected_bound_port);
    assert_eq!(
        canonical(Path::new(&evidence.executable)),
        canonical(python)
    );
    evidence
}

fn assert_tray_state(snapshot: &supervisor::ServiceSnapshot, status: ServiceStatus, detail: &str) {
    assert_eq!(snapshot.status, status);
    assert_eq!(snapshot.detail, detail);
    assert!(snapshot.updated_at_ms > 0);
}

#[test]
fn supervisor_process_smoke_honors_overrides_tracks_tray_state_and_cleans_children_and_ports() {
    let _lock = test_lock().lock().expect("supervisor smoke test lock");
    let _environment = EnvironmentRestore::capture(&[
        "LIFE_CONTEXT_ROOT",
        "LIFE_CONTEXT_PYTHON",
        "LIFE_CONTEXT_SMOKE_OUTPUT",
        "PYTHONPATH",
        "CONTEXT_SERVER_PORT",
        "COLLECTOR_SERVER_URL",
        "COLLECTOR_ROOTS",
        "COLLECTOR_SMOKE_PORT",
    ]);
    let fixture = FixtureRoot::new();
    let python = resolve_python();
    let inherited_pythonpath = fixture.path.join("inherited-pythonpath");
    fs::create_dir_all(&inherited_pythonpath).expect("create inherited PYTHONPATH fixture");
    let server_evidence = fixture.evidence_path("server");
    let collector_evidence = fixture.evidence_path("collector");
    let [server_port, collector_port] = free_loopback_ports(2).try_into().unwrap();

    env::set_var("LIFE_CONTEXT_ROOT", &fixture.path);
    env::set_var("LIFE_CONTEXT_PYTHON", &python);
    env::set_var("LIFE_CONTEXT_SMOKE_OUTPUT", fixture.path.join("evidence"));
    env::set_var("PYTHONPATH", &inherited_pythonpath);
    env::set_var("CONTEXT_SERVER_PORT", server_port.to_string());
    env::set_var(
        "COLLECTOR_SERVER_URL",
        format!("https://collector.example.test:{server_port}"),
    );
    env::set_var("COLLECTOR_ROOTS", r"C:\CaptureRoot;D:\Notes");
    env::set_var("COLLECTOR_SMOKE_PORT", collector_port.to_string());

    let supervisor = SupervisorBridge::default();
    let _cleanup = StopOnDrop(&supervisor);

    assert_tray_state(
        &supervisor.status().expect("initial status"),
        ServiceStatus::Stopped,
        "Local Python server and collector are stopped.",
    );
    assert_tray_state(
        &supervisor.pause().expect("pause while stopped"),
        ServiceStatus::Stopped,
        "Collector paused; local context server remains available.",
    );

    let started = supervisor.start().expect("start service");
    assert_tray_state(
        &started,
        ServiceStatus::Running,
        "Local Python server and five-second collector are running.",
    );
    wait_until("both child evidence files", Duration::from_secs(5), || {
        server_evidence.is_file() && collector_evidence.is_file()
    });

    let server = assert_evidence(
        &server_evidence,
        &fixture.path,
        &inherited_pythonpath,
        &python,
        "server",
        server_port,
        collector_port,
    );
    let collector = assert_evidence(
        &collector_evidence,
        &fixture.path,
        &inherited_pythonpath,
        &python,
        "collector",
        server_port,
        collector_port,
    );
    wait_until("server child after start", Duration::from_secs(2), || {
        process_is_alive(server.pid)
    });
    wait_until(
        "collector child after start",
        Duration::from_secs(2),
        || process_is_alive(collector.pid),
    );
    wait_until("server port after start", Duration::from_secs(2), || {
        port_is_listening(server.bound_port)
    });
    wait_until("collector port after start", Duration::from_secs(2), || {
        port_is_listening(collector.bound_port)
    });

    let paused = supervisor.pause().expect("pause service");
    assert_tray_state(
        &paused,
        ServiceStatus::Paused,
        "Collector paused; local context server remains available.",
    );
    wait_until(
        "collector cleanup after pause",
        Duration::from_secs(2),
        || !process_is_alive(collector.pid),
    );
    wait_until(
        "collector port cleanup after pause",
        Duration::from_secs(2),
        || !port_is_listening(collector.bound_port),
    );
    assert!(
        process_is_alive(server.pid),
        "pause must leave the server child running"
    );
    assert!(
        port_is_listening(server.bound_port),
        "pause must leave the server port listening"
    );
    assert_tray_state(
        &supervisor.status().expect("paused status"),
        ServiceStatus::Paused,
        "Collector paused; local context server remains available.",
    );

    fs::remove_file(&collector_evidence).expect("remove stale collector evidence");
    let resumed = supervisor
        .start()
        .expect("restart collector from paused state");
    assert_tray_state(
        &resumed,
        ServiceStatus::Running,
        "Local Python server and five-second collector are running.",
    );
    wait_until(
        "collector evidence after resume",
        Duration::from_secs(5),
        || collector_evidence.is_file(),
    );
    let resumed_collector = assert_evidence(
        &collector_evidence,
        &fixture.path,
        &inherited_pythonpath,
        &python,
        "collector",
        server_port,
        collector_port,
    );
    wait_until(
        "collector child after resume",
        Duration::from_secs(2),
        || process_is_alive(resumed_collector.pid),
    );
    wait_until(
        "collector port after resume",
        Duration::from_secs(2),
        || port_is_listening(resumed_collector.bound_port),
    );

    let stopped = supervisor.stop().expect("stop service");
    assert_tray_state(
        &stopped,
        ServiceStatus::Stopped,
        "Local Python server and collector stopped cleanly.",
    );
    wait_until("server cleanup after stop", Duration::from_secs(2), || {
        !process_is_alive(server.pid)
    });
    wait_until(
        "collector cleanup after stop",
        Duration::from_secs(2),
        || !process_is_alive(resumed_collector.pid),
    );
    wait_until(
        "server port cleanup after stop",
        Duration::from_secs(2),
        || !port_is_listening(server.bound_port),
    );
    wait_until(
        "collector port cleanup after stop",
        Duration::from_secs(2),
        || !port_is_listening(resumed_collector.bound_port),
    );
    assert_tray_state(
        &supervisor.status().expect("stopped status"),
        ServiceStatus::Stopped,
        "Local Python server and collector stopped cleanly.",
    );
    assert_tray_state(
        &supervisor.stop().expect("idempotent stop"),
        ServiceStatus::Stopped,
        "Local Python server and collector stopped cleanly.",
    );
    println!(
        "supervisor smoke cleanup verified: server_pid={} collector_pids=[{}, {}] server_port={} collector_port={}",
        server.pid, collector.pid, resumed_collector.pid, server.bound_port, resumed_collector.bound_port
    );
}

#[test]
fn supervisor_reports_crashed_server_and_restarts_only_the_missing_child() {
    let _lock = test_lock().lock().expect("supervisor smoke test lock");
    let _environment = EnvironmentRestore::capture(&[
        "LIFE_CONTEXT_ROOT",
        "LIFE_CONTEXT_PYTHON",
        "LIFE_CONTEXT_SMOKE_OUTPUT",
        "PYTHONPATH",
        "CONTEXT_SERVER_PORT",
        "COLLECTOR_SERVER_URL",
        "COLLECTOR_ROOTS",
        "COLLECTOR_SMOKE_PORT",
        "LIFE_CONTEXT_SMOKE_EXIT_MODULE",
    ]);
    let fixture = FixtureRoot::new();
    let python = resolve_python();
    let inherited_pythonpath = fixture.path.join("inherited-pythonpath");
    fs::create_dir_all(&inherited_pythonpath).expect("create inherited PYTHONPATH fixture");
    let server_evidence = fixture.evidence_path("server");
    let [server_port, collector_port] = free_loopback_ports(2).try_into().unwrap();

    env::set_var("LIFE_CONTEXT_ROOT", &fixture.path);
    env::set_var("LIFE_CONTEXT_PYTHON", &python);
    env::set_var("LIFE_CONTEXT_SMOKE_OUTPUT", fixture.path.join("evidence"));
    env::set_var("PYTHONPATH", &inherited_pythonpath);
    env::set_var("CONTEXT_SERVER_PORT", server_port.to_string());
    env::set_var(
        "COLLECTOR_SERVER_URL",
        format!("https://collector.example.test:{server_port}"),
    );
    env::set_var("COLLECTOR_ROOTS", r"C:\CaptureRoot;D:\Notes");
    env::set_var("COLLECTOR_SMOKE_PORT", collector_port.to_string());
    env::set_var("LIFE_CONTEXT_SMOKE_EXIT_MODULE", "server");

    let supervisor = SupervisorBridge::default();
    let _cleanup = StopOnDrop(&supervisor);
    supervisor
        .start()
        .expect("start service with crashing server");
    wait_until("crashed server evidence", Duration::from_secs(5), || {
        server_evidence.is_file()
    });
    let crashed_server = read_evidence(&server_evidence);
    wait_until("server child exit", Duration::from_secs(2), || {
        !process_is_alive(crashed_server.pid)
    });
    let crashed = supervisor.status().expect("status after server crash");
    assert_tray_state(
        &crashed,
        ServiceStatus::Error,
        "Server exited unexpectedly; collector remains running.",
    );
    assert!(
        port_is_listening(collector_port),
        "collector must remain available while the server is restarted"
    );

    env::remove_var("LIFE_CONTEXT_SMOKE_EXIT_MODULE");
    fs::remove_file(&server_evidence).expect("remove stale server evidence");
    let restarted = supervisor.start().expect("restart missing server child");
    assert_tray_state(
        &restarted,
        ServiceStatus::Running,
        "Local Python server and five-second collector are running.",
    );
    wait_until(
        "server evidence after restart",
        Duration::from_secs(5),
        || server_evidence.is_file(),
    );
    let restarted_server = read_evidence(&server_evidence);
    assert_ne!(restarted_server.pid, crashed_server.pid);
    wait_until("server after restart", Duration::from_secs(2), || {
        process_is_alive(restarted_server.pid)
    });
    supervisor.stop().expect("stop restarted service");
    wait_until(
        "server cleanup after restart",
        Duration::from_secs(2),
        || !process_is_alive(restarted_server.pid),
    );
}
