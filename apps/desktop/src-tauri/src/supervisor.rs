use serde::{Deserialize, Serialize};
use std::env;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ServiceStatus {
    Stopped,
    Starting,
    Running,
    Paused,
    Stopping,
    Error,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ServiceSnapshot {
    pub status: ServiceStatus,
    pub detail: String,
    pub updated_at_ms: u64,
}

pub type SupervisorResult<T> = Result<T, String>;

/// The only native boundary that should know how the service is supervised.
pub trait ServiceSupervisor: Send + Sync {
    fn status(&self) -> SupervisorResult<ServiceSnapshot>;
    fn start(&self) -> SupervisorResult<ServiceSnapshot>;
    fn pause(&self) -> SupervisorResult<ServiceSnapshot>;
    fn stop(&self) -> SupervisorResult<ServiceSnapshot>;
}

#[derive(Debug)]
struct RuntimeState {
    status: ServiceStatus,
    detail: String,
    updated_at_ms: u64,
    server: Option<Child>,
    collector: Option<Child>,
}

#[derive(Debug)]
pub struct SupervisorBridge {
    state: Mutex<RuntimeState>,
}

impl Default for SupervisorBridge {
    fn default() -> Self {
        Self {
            state: Mutex::new(RuntimeState {
                status: ServiceStatus::Stopped,
                detail: "Local Python server and collector are stopped.".into(),
                updated_at_ms: now_ms(),
                server: None,
                collector: None,
            }),
        }
    }
}

impl SupervisorBridge {
    fn has_service_modules(root: &Path) -> bool {
        root.join("services").join("context_server").is_dir()
            && root.join("services").join("collector").is_dir()
    }

    fn canonical_service_root(candidate: PathBuf) -> Option<PathBuf> {
        if !Self::has_service_modules(&candidate) {
            return None;
        }

        fs::canonicalize(candidate).ok()
    }

    fn repo_root() -> SupervisorResult<PathBuf> {
        if let Ok(value) = env::var("LIFE_CONTEXT_ROOT") {
            let candidate = PathBuf::from(value);
            if let Some(root) = Self::canonical_service_root(candidate) {
                return Ok(root);
            }
        }

        // Windows bundles place configured resources beside the executable.
        // The extra resources/ candidates also cover unpacked/development
        // layouts without making the source checkout path the only fallback.
        if let Ok(executable) = env::current_exe() {
            if let Some(executable_dir) = executable.parent() {
                let mut candidates = vec![
                    executable_dir.to_path_buf(),
                    executable_dir.join("resources"),
                ];
                if let Some(parent) = executable_dir.parent() {
                    candidates.push(parent.join("resources"));
                }
                for candidate in candidates {
                    if let Some(root) = Self::canonical_service_root(candidate) {
                        return Ok(root);
                    }
                }
            }
        }

        let source_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("..");
        if let Some(root) = Self::canonical_service_root(source_root) {
            return Ok(root);
        }

        Err("could not locate the bundled Life Context services; set LIFE_CONTEXT_ROOT to the repository or packaged resource root".into())
    }

    fn python_path(root: &Path) -> OsString {
        let mut paths = vec![root.to_path_buf()];
        if let Some(existing) = env::var_os("PYTHONPATH") {
            paths.extend(env::split_paths(&existing));
        }
        env::join_paths(paths).unwrap_or_else(|_| root.as_os_str().to_os_string())
    }

    fn spawn_module(module: &str) -> SupervisorResult<Child> {
        let root = Self::repo_root()?;
        let python = env::var("LIFE_CONTEXT_PYTHON").unwrap_or_else(|_| "python".into());
        let pythonpath = Self::python_path(&root);
        Command::new(python)
            .args(["-m", module])
            .current_dir(&root)
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONPATH", pythonpath)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|error| format!("could not start {module}: {error}"))
    }

    fn reap_exited(state: &mut RuntimeState) {
        let mut server_exited = false;
        let mut collector_exited = false;
        if let Some(child) = state.server.as_mut() {
            if child.try_wait().ok().flatten().is_some() {
                state.server = None;
                server_exited = true;
            }
        }
        if let Some(child) = state.collector.as_mut() {
            if child.try_wait().ok().flatten().is_some() {
                state.collector = None;
                collector_exited = true;
            }
        }
        if server_exited || collector_exited {
            state.status = ServiceStatus::Error;
            state.detail = match (server_exited, collector_exited) {
                (true, true) => "Server and collector exited unexpectedly.".into(),
                (true, false) => "Server exited unexpectedly; collector remains running.".into(),
                (false, true) => "Collector exited unexpectedly; server remains running.".into(),
                (false, false) => unreachable!("an exit must have been observed"),
            };
            state.updated_at_ms = now_ms();
        }
    }

    fn stop_child(child: &mut Option<Child>) {
        if let Some(process) = child.as_mut() {
            let _ = process.kill();
            let _ = process.wait();
        }
        *child = None;
    }
}

impl Drop for SupervisorBridge {
    fn drop(&mut self) {
        if let Ok(state) = self.state.get_mut() {
            Self::stop_child(&mut state.collector);
            Self::stop_child(&mut state.server);
        }
    }
}

impl ServiceSupervisor for SupervisorBridge {
    fn status(&self) -> SupervisorResult<ServiceSnapshot> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "supervisor state lock poisoned".to_string())?;

        Self::reap_exited(&mut state);
        if state.status != ServiceStatus::Error
            && state.server.is_none()
            && state.collector.is_none()
        {
            state.status = ServiceStatus::Stopped;
        } else if state.status != ServiceStatus::Error
            && state.collector.is_none()
            && state.status == ServiceStatus::Running
        {
            state.status = ServiceStatus::Paused;
        }

        Ok(snapshot_from(&state))
    }

    fn start(&self) -> SupervisorResult<ServiceSnapshot> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "supervisor state lock poisoned".to_string())?;
        Self::reap_exited(&mut state);
        if state.server.is_none() {
            state.server = Some(Self::spawn_module("services.context_server")?);
        }
        if state.collector.is_none() {
            state.collector = Some(Self::spawn_module("services.collector")?);
        }
        state.status = ServiceStatus::Running;
        state.detail = "Local Python server and five-second collector are running.".into();
        state.updated_at_ms = now_ms();
        Ok(snapshot_from(&state))
    }

    fn pause(&self) -> SupervisorResult<ServiceSnapshot> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "supervisor state lock poisoned".to_string())?;
        Self::reap_exited(&mut state);
        let crashed = state.status == ServiceStatus::Error;
        Self::stop_child(&mut state.collector);
        if crashed {
            state.status = ServiceStatus::Error;
        } else {
            state.status = if state.server.is_some() {
                ServiceStatus::Paused
            } else {
                ServiceStatus::Stopped
            };
            state.detail = "Collector paused; local context server remains available.".into();
        }
        state.updated_at_ms = now_ms();
        Ok(snapshot_from(&state))
    }

    fn stop(&self) -> SupervisorResult<ServiceSnapshot> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "supervisor state lock poisoned".to_string())?;
        Self::stop_child(&mut state.collector);
        Self::stop_child(&mut state.server);
        state.status = ServiceStatus::Stopped;
        state.detail = "Local Python server and collector stopped cleanly.".into();
        state.updated_at_ms = now_ms();
        Ok(snapshot_from(&state))
    }
}

fn snapshot_from(state: &RuntimeState) -> ServiceSnapshot {
    ServiceSnapshot {
        status: state.status,
        detail: state.detail.clone(),
        updated_at_ms: state.updated_at_ms,
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as u64)
        .unwrap_or_default()
}
