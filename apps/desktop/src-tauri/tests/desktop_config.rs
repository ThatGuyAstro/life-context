use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};

fn config_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn read_config(name: &str) -> Value {
    let path = config_dir().join(name);
    let text = fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|error| panic!("parse {}: {error}", path.display()))
}

fn string_field<'a>(value: &'a Value, field: &str) -> &'a str {
    value
        .get(field)
        .and_then(Value::as_str)
        .unwrap_or_else(|| panic!("configuration field {field:?} must be a string"))
}

#[test]
fn desktop_build_paths_match_vite_and_tauri_layout() {
    let config = read_config("tauri.conf.json");
    let build = config.get("build").expect("base build config");

    assert_eq!(string_field(build, "devUrl"), "http://127.0.0.1:1420");
    assert_eq!(string_field(build, "beforeDevCommand"), "npm run dev");
    assert_eq!(string_field(build, "beforeBuildCommand"), "npm run build");

    let frontend_dist = Path::new(string_field(build, "frontendDist"));
    assert!(frontend_dist.is_relative());
    let resolved_dist = config_dir().join(frontend_dist);
    assert_eq!(
        resolved_dist.file_name().and_then(|name| name.to_str()),
        Some("dist")
    );
    let resolved_parent = resolved_dist
        .parent()
        .expect("frontendDist must have a parent directory");
    let config_root = config_dir();
    let desktop_root = config_root
        .parent()
        .expect("src-tauri must be inside the desktop app");
    assert_eq!(
        fs::canonicalize(resolved_parent).expect("canonicalize frontendDist parent"),
        fs::canonicalize(desktop_root).expect("canonicalize desktop app root"),
        "frontendDist must resolve to apps/desktop/dist, not src-tauri/dist"
    );

    let vite_config = fs::read_to_string(config_dir().join("..").join("vite.config.ts"))
        .expect("read desktop Vite config");
    assert!(vite_config.contains("base: \"./\""));
    assert!(vite_config.contains("outDir: \"dist\""));
    assert!(vite_config.contains("port: 1420"));
}

#[test]
fn windows_release_config_has_packaged_runtime_resources() {
    let config = read_config("tauri.windows.conf.json");
    let bundle = config.get("bundle").expect("Windows bundle config");

    assert_eq!(bundle.get("active").and_then(Value::as_bool), Some(true));
    let targets = bundle
        .get("targets")
        .and_then(Value::as_array)
        .expect("Windows bundle targets");
    assert_eq!(targets.len(), 2);
    assert_eq!(targets[0].as_str(), Some("msi"));
    assert_eq!(targets[1].as_str(), Some("nsis"));
    assert_eq!(
        bundle
            .get("icon")
            .and_then(Value::as_array)
            .and_then(|icons| icons.first())
            .and_then(Value::as_str),
        Some("icons/icon.ico")
    );

    let icon_path = config_dir().join("icons/icon.ico");
    let icon_bytes = fs::read(&icon_path)
        .unwrap_or_else(|error| panic!("read {}: {error}", icon_path.display()));
    assert!(
        icon_bytes.starts_with(&[0, 0, 1, 0]),
        "Windows bundle icon must be an ICO file"
    );

    let resources = bundle
        .get("resources")
        .and_then(Value::as_object)
        .expect("Windows bundle resources");
    for (source, target) in resources {
        let source_path = config_dir().join(source);
        assert!(
            source_path.is_dir(),
            "configured bundle resource source does not exist: {}",
            source_path.display()
        );
        let target_path = Path::new(target.as_str().expect("resource target must be a string"));
        assert!(target_path.is_relative());
        assert!(!target_path
            .components()
            .any(|component| matches!(component, std::path::Component::ParentDir)));
    }

    let windows = bundle.get("windows").expect("Windows installer settings");
    assert_eq!(
        windows.get("allowDowngrades").and_then(Value::as_bool),
        Some(false)
    );
    assert_eq!(
        windows
            .get("webviewInstallMode")
            .and_then(|mode| mode.get("type"))
            .and_then(Value::as_str),
        Some("downloadBootstrapper")
    );
}

#[test]
fn desktop_frame_policy_rejects_remote_http_and_clipboard_permissions() {
    let config = read_config("tauri.conf.json");
    let csp = config
        .get("app")
        .and_then(|app| app.get("security"))
        .and_then(|security| security.get("csp"))
        .and_then(Value::as_str)
        .expect("desktop CSP must be a string");
    assert!(csp.contains("frame-src https: http://127.0.0.1:* http://localhost:*"));
    assert!(!csp.contains("frame-src http:"));

    let app_source = fs::read_to_string(config_dir().join("..").join("src/App.tsx"))
        .expect("read desktop app source");
    assert!(app_source.contains("isAllowedDashboardUrl"));
    assert!(!app_source.contains("clipboard-read"));
    assert!(!app_source.contains("clipboard-write"));
}

#[test]
fn dashboard_target_policy_allows_loopback_http_or_remote_https_without_credentials() {
    let app_source = fs::read_to_string(config_dir().join("..").join("src/App.tsx"))
        .expect("read desktop app source");
    assert!(app_source.contains("if (parsed.username || parsed.password)"));
    assert!(app_source.contains("if (parsed.protocol === \"https:\")"));
    assert!(app_source
        .contains("return parsed.protocol === \"http:\" && isLoopbackHost(parsed.hostname);"));
    assert!(app_source.contains(
        "const DEFAULT_SERVER_URL = \"http://127.0.0.1:5173/?api=http://127.0.0.1:8000\";"
    ));
}
