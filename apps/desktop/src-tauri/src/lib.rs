#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod supervisor;

use supervisor::{ServiceSnapshot, ServiceSupervisor, SupervisorBridge, SupervisorResult};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, State};

struct SupervisorState {
    supervisor: Box<dyn ServiceSupervisor>,
}

impl Default for SupervisorState {
    fn default() -> Self {
        Self {
            supervisor: Box::new(SupervisorBridge::default()),
        }
    }
}

#[derive(Clone, Copy)]
enum Intent {
    Start,
    Pause,
    Stop,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum TrayCommand {
    Open,
    Start,
    Pause,
    Stop,
    Quit,
}

fn tray_command(id: &str) -> Option<TrayCommand> {
    match id {
        "open" => Some(TrayCommand::Open),
        "start" => Some(TrayCommand::Start),
        "pause" => Some(TrayCommand::Pause),
        "stop" => Some(TrayCommand::Stop),
        "quit" => Some(TrayCommand::Quit),
        _ => None,
    }
}

#[tauri::command]
fn get_service_status(state: State<'_, SupervisorState>) -> SupervisorResult<ServiceSnapshot> {
    state.supervisor.status()
}

#[tauri::command]
fn start_service(state: State<'_, SupervisorState>) -> SupervisorResult<ServiceSnapshot> {
    state.supervisor.start()
}

#[tauri::command]
fn pause_service(state: State<'_, SupervisorState>) -> SupervisorResult<ServiceSnapshot> {
    state.supervisor.pause()
}

#[tauri::command]
fn stop_service(state: State<'_, SupervisorState>) -> SupervisorResult<ServiceSnapshot> {
    state.supervisor.stop()
}

fn dispatch_intent(app: &AppHandle, intent: Intent) {
    let state = app.state::<SupervisorState>();
    let result = match intent {
        Intent::Start => state.supervisor.start(),
        Intent::Pause => state.supervisor.pause(),
        Intent::Stop => state.supervisor.stop(),
    };

    match result {
        Ok(snapshot) => {
            let _ = app.emit("service-status", snapshot);
        }
        Err(error) => {
            eprintln!("service supervisor intent failed: {error}");
        }
    }
}

fn shutdown_supervisor(app: &AppHandle) {
    let state = app.state::<SupervisorState>();
    if let Err(error) = state.supervisor.stop() {
        eprintln!("service supervisor shutdown failed: {error}");
    }
}

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn build_tray(app: &mut tauri::App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open dashboard", true, None::<&str>)?;
    let start = MenuItem::with_id(app, "start", "Start service", true, None::<&str>)?;
    let pause = MenuItem::with_id(app, "pause", "Pause service", true, None::<&str>)?;
    let stop = MenuItem::with_id(app, "stop", "Stop service", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &start, &pause, &stop, &quit])?;
    let icon = app
        .default_window_icon()
        .cloned()
        .ok_or_else(|| tauri::Error::AssetNotFound("icons/icon.ico".into()))?;

    TrayIconBuilder::new()
        .menu(&menu)
        .icon(icon)
        .show_menu_on_left_click(true)
        .tooltip("Life Context desktop shell")
        .on_menu_event(|app, event| match tray_command(event.id.as_ref()) {
            Some(TrayCommand::Open) => show_main_window(app),
            Some(TrayCommand::Start) => dispatch_intent(app, Intent::Start),
            Some(TrayCommand::Pause) => dispatch_intent(app, Intent::Pause),
            Some(TrayCommand::Stop) => dispatch_intent(app, Intent::Stop),
            Some(TrayCommand::Quit) => {
                shutdown_supervisor(app);
                app.exit(0);
            }
            None => {}
        })
        .build(app)?;

    Ok(())
}

pub fn run() {
    tauri::Builder::default()
        .manage(SupervisorState::default())
        .setup(|app| {
            build_tray(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_service_status,
            start_service,
            pause_service,
            stop_service
        ])
        .run(tauri::generate_context!())
        .expect("error while running Life Context desktop shell");
}

#[cfg(test)]
mod tests {
    use super::{tray_command, TrayCommand};

    #[test]
    fn tray_command_ids_cover_all_menu_actions() {
        assert_eq!(tray_command("open"), Some(TrayCommand::Open));
        assert_eq!(tray_command("start"), Some(TrayCommand::Start));
        assert_eq!(tray_command("pause"), Some(TrayCommand::Pause));
        assert_eq!(tray_command("stop"), Some(TrayCommand::Stop));
        assert_eq!(tray_command("quit"), Some(TrayCommand::Quit));
        assert_eq!(tray_command("unexpected"), None);
    }
}
