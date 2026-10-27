use rand::{rngs::OsRng, RngCore};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    path::PathBuf,
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager,
};

#[path = "connected.rs"]
mod connected;
#[path = "backup_retention.rs"]
mod backup_retention;

fn nonce() -> String {
    let mut bytes = [0u8; 32];
    OsRng.fill_bytes(&mut bytes);
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}
#[derive(Clone, Deserialize, Serialize)]
struct Ready {
    port: u16,
    pid: u32,
    protocol: u32,
    frozen: bool,
    fixture: bool,
    imports_ms: u64,
    core_ms: u64,
    listener_ms: u64,
}
fn parse_readiness(line: &str) -> Result<Ready, String> {
    let value: Value = serde_json::from_str(line)
        .map_err(|_| "后台进程在就绪前退出或返回了无效信息，请检查安装完整性。[runtime_readiness]")?;
    if let Some(error) = value.get("error") {
        let (code, message) = match error["code"].as_str() {
            Some("bootstrap") => ("bootstrap", "后台启动参数无效，请重新打开应用。"),
            Some("imports") => ("imports", "后台依赖加载失败，请重新安装完整的 Cursor Panel 安装包。"),
            Some("data_in_use") => ("data_in_use", "数据目录正在使用中，请退出其他 Cursor Panel 或维护命令后重试。"),
            Some("data_permissions") => ("data_permissions", "无法设置本地数据目录的访问权限，请检查 Windows 用户权限与目录权限。"),
            Some("data_io") => ("data_io", "无法读写本地数据目录，请检查目录权限、文件冲突与磁盘空间。"),
            Some("listener") => ("listener", "本地监听服务启动失败，请检查系统是否拦截了后台网络访问。"),
            _ => ("runtime", "本地数据初始化失败，请保留数据目录并反馈此错误。"),
        };
        let os_error = error["os_error"].as_i64()
            .map(|value| format!("; os={value}"))
            .unwrap_or_default();
        let kind = match error["kind"].as_str() {
            Some(kind @ ("KeyError" | "TypeError" | "ValueError" | "FileNotFoundError" | "FileExistsError"
                | "PermissionError" | "OSError" | "ImportError" | "ModuleNotFoundError" | "RuntimeError"
                | "Conflict" | "Locked" | "SecretError" | "OperationalError" | "DatabaseError" | "TimeoutExpired")) => format!("; {kind}"),
            _ => String::new(),
        };
        return Err(format!("{message} [{code}{os_error}{kind}]"));
    }
    serde_json::from_value(value).map_err(|_| "后台就绪信息不兼容，请重新安装完整安装包。[runtime_readiness]".into())
}
#[derive(Clone)]
struct Connection {
    token: String,
    ready: Ready,
    client: reqwest::blocking::Client,
}
impl Connection {
    fn request(&self, method: &str, path: &str, body: Option<Value>) -> Result<Value, String> {
        let method =
            reqwest::Method::from_bytes(method.as_bytes()).map_err(|_| "Invalid method")?;
        let mut request = self
            .client
            .request(
                method,
                format!("http://127.0.0.1:{}{path}", self.ready.port),
            )
            .bearer_auth(&self.token);
        if let Some(body) = body {
            if serde_json::to_vec(&body)
                .map_err(|_| "Invalid input")?
                .len()
                > 65536
            {
                return Err("Request is too large".into());
            }
            request = request.json(&body);
        }
        let response = request.send().map_err(|_| "Local backend is unavailable")?;
        let status = response.status().as_u16();
        let body: Value = if status == 204 {
            Value::Null
        } else {
            response.json().map_err(|_| "Invalid local response")?
        };
        Ok(json!({"status": status, "body": body}))
    }
}
struct Backend {
    child: Child,
    control: Option<ChildStdin>,
    connection: Option<Connection>,
    start_ms: u128,
}
impl Backend {
    fn start(_app: &tauri::AppHandle, directory: &PathBuf, fixture: bool) -> Result<Self, String> {
        let start = Instant::now();
        let suffix = if cfg!(windows) { ".exe" } else { "" };
        let executable = std::env::current_exe().map_err(|_| "Missing executable location")?;
        let parent = executable.parent().ok_or("Missing executable directory")?;
        let resources = if cfg!(target_os = "macos") {
            parent
                .parent()
                .ok_or("Missing bundle directory")?
                .join("Resources")
        } else {
            parent.to_path_buf()
        };
        let mut path = resources
            .join("runtime")
            .join(format!("cursor-local{suffix}"));
        if cfg!(debug_assertions) && !path.exists() {
            path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("runtime")
                .join(format!("cursor-local{suffix}"));
        }
        let mut command = Command::new(path);
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .env_remove("PYTHONHOME")
            .env_remove("PYTHONPATH");
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let mut child = command
            .spawn()
            .map_err(|error| format!("无法启动随包后台，请检查安装目录中的 runtime 文件夹和系统拦截记录。[runtime_spawn; os={}]", error.raw_os_error().unwrap_or(0)))?;
        let control = child.stdin.take();
        let stdout = child.stdout.take().ok_or("Missing runtime channel")?;
        let mut backend = Self {
            child,
            control,
            connection: None,
            start_ms: 0,
        };
        let token = nonce();
        writeln!(
            backend.control.as_mut().ok_or("Missing runtime channel")?,
            "{}",
            json!({"token": token, "data_dir": directory, "fixture": fixture})
        )
        .map_err(|_| "Cannot initialize the local runtime")?;
        let (sender, receiver) = mpsc::channel();
        thread::spawn(move || {
            let mut line = String::new();
            let ready = BufReader::new(stdout)
                .take(4096)
                .read_line(&mut line)
                .map_err(|_| "无法读取后台启动状态。[runtime_channel]".to_string())
                .and_then(|_| parse_readiness(&line));
            let _ = sender.send(ready);
        });
        let ready = receiver
            .recv_timeout(Duration::from_secs(60))
            .map_err(|_| "后台启动超时，请完成系统授权提示后重试。[runtime_timeout]")??;
        if ready.protocol != 1 || ready.port == 0 || ready.pid == 0 || ready.fixture != fixture {
            return Err("Incompatible local runtime".into());
        }
        let client = reqwest::blocking::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(180))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| "Cannot create native client")?;
        backend.connection = Some(Connection {
            token,
            ready,
            client,
        });
        backend.start_ms = start.elapsed().as_millis();
        Ok(backend)
    }
}
impl Drop for Backend {
    fn drop(&mut self) {
        self.control.take(); // EOF also handles unexpected shell death.
        for _ in 0..1300 {
            if matches!(self.child.try_wait(), Ok(Some(_))) {
                return;
            }
            thread::sleep(Duration::from_millis(50));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
struct Runtime {
    backend: Mutex<Option<Backend>>,
    error: Mutex<Option<String>>,
    starting: AtomicBool,
    updating: AtomicBool,
    background: AtomicBool,
    start: Instant,
    directory: PathBuf,
    log_path: Option<PathBuf>,
    report_path: Option<PathBuf>,
    reported: AtomicBool,
}
impl Runtime {
    fn log_startup(&self, phase: &str, error: Option<&str>) {
        // Overwrite one bounded report; never persist the bootstrap token,
        // exception text, database contents or raw child stderr.
        if let Some(path) = &self.log_path {
            if let Some(parent) = path.parent() {
                if fs::create_dir_all(parent).is_ok() {
                    let report = json!({
                        "version": env!("CARGO_PKG_VERSION"), "target": env!("P0_TARGET"),
                        "source_revision": option_env!("GITHUB_SHA"),
                        "time_unix": std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|time| time.as_secs()).unwrap_or(0),
                        "phase": phase, "error": error,
                    });
                    let _ = fs::write(path, serde_json::to_vec_pretty(&report).unwrap());
                }
            }
        }
    }

    fn backend_running(&self) -> Result<bool, String> {
        let mut guard = self.backend.lock().map_err(|_| "Runtime lock failed")?;
        match guard.as_mut() {
            Some(backend) => backend.child.try_wait()
                .map(|status| status.is_none())
                .map_err(|_| "Cannot inspect local runtime".into()),
            None => Ok(false),
        }
    }
}

fn start_backend(app: &tauri::AppHandle) -> Result<(), String> {
    let runtime = app.state::<Runtime>();
    if runtime.updating.load(Ordering::SeqCst) {
        return Err("Application update is in progress".into());
    }
    if runtime.starting.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_err() {
        return Ok(());
    }
    let handle = app.clone();
    thread::spawn(move || {
        let runtime = handle.state::<Runtime>();
        // Release the previous process and its pipe before reopening the same data directory.
        let previous = runtime.backend.lock().unwrap().take();
        drop(previous);
        *runtime.error.lock().unwrap() = None;
        runtime.log_startup("starting", None);
        match Backend::start(&handle, &runtime.directory, runtime.report_path.is_some()) {
            Ok(backend) => {
                if let Some(connection) = &backend.connection {
                    if let Ok(response) = connection.request("GET", "/native/status", None) {
                        if let Some(report) = &runtime.report_path {
                            let _ = fs::write(report.with_extension("startup.json"), serde_json::to_vec(&json!({"backend": connection.ready, "state": response})).unwrap());
                        }
                        runtime.background.store(response["body"]["background"].as_bool().unwrap_or(false), Ordering::SeqCst);
                    }
                }
                *runtime.backend.lock().unwrap() = Some(backend);
                runtime.log_startup("backend_started", None);
            }
            Err(error) => {
                runtime.log_startup("unavailable", Some(&error));
                if let Some(report) = &runtime.report_path {
                    let _ = fs::write(report.with_extension("startup.json"), serde_json::to_vec(&json!({"error": error})).unwrap());
                }
                *runtime.error.lock().unwrap() = Some(error);
            }
        }
        runtime.starting.store(false, Ordering::SeqCst);
    });
    Ok(())
}
fn connection(app: &tauri::AppHandle) -> Result<Connection, String> {
    if app.state::<Runtime>().updating.load(Ordering::SeqCst) {
        return Err("Application update is in progress".into());
    }
    app.state::<Runtime>()
        .backend
        .lock()
        .map_err(|_| "Runtime lock failed")?
        .as_ref()
        .and_then(|backend| backend.connection.clone())
        .ok_or("Local backend is unavailable".into())
}
fn identifier(value: Option<String>) -> Result<String, String> {
    let value = value.ok_or("Missing identifier")?;
    let parsed = uuid::Uuid::parse_str(&value).map_err(|_| "Invalid identifier")?;
    if parsed.to_string() != value {
        return Err("Invalid identifier".into());
    }
    Ok(value)
}
#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum AccountOperation {
    Bootstrap,
    Me,
    List,
    Get,
    Authorize,
    AuthorizationPoll,
    AuthorizationCancel,
    Edit,
    Delete,
    Refresh,
    Detail,
    Audit,
}
#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct Query {
    q: Option<String>,
    tag: Option<String>,
    limit: Option<u16>,
    offset: Option<u32>,
}
fn account_route(
    operation: AccountOperation,
    workspace: Option<String>,
    account: Option<String>,
    authorization: Option<String>,
    query: Query,
) -> Result<(&'static str, String), String> {
    use AccountOperation::*;
    match operation {
        Bootstrap => return Ok(("GET", "/api/v1/bootstrap".into())),
        Me => return Ok(("GET", "/api/v1/me".into())),
        _ => {}
    }
    let workspace = identifier(workspace)?;
    let base = format!("/api/v1/workspaces/{workspace}");
    let (method, path) = match operation {
        List => ("GET", format!("{base}/accounts")),
        Authorize => ("POST", format!("{base}/authorizations")),
        AuthorizationPoll => (
            "POST",
            format!("{base}/authorizations/{}/poll", identifier(authorization)?),
        ),
        AuthorizationCancel => (
            "DELETE",
            format!("{base}/authorizations/{}", identifier(authorization)?),
        ),
        Audit => ("GET", format!("{base}/audit")),
        other => {
            let id = identifier(account)?;
            let base = format!("{base}/accounts/{id}");
            match other {
                Get => ("GET", base),
                Edit => ("PATCH", base),
                Delete => ("DELETE", base),
                Refresh => ("POST", format!("{base}/refresh")),
                Detail => ("GET", format!("{base}/detail")),
                _ => return Err("Unknown operation".into()),
            }
        }
    };
    let mut url =
        reqwest::Url::parse(&format!("http://127.0.0.1{path}")).map_err(|_| "Invalid route")?;
    {
        let mut pairs = url.query_pairs_mut();
        if let Some(q) = query.q {
            if q.len() > 1024 {
                return Err("Query too long".into());
            }
            pairs.append_pair("q", &q);
        }
        if let Some(tag) = query.tag {
            if tag.len() > 512 {
                return Err("Tag too long".into());
            }
            pairs.append_pair("tag", &tag);
        }
        if let Some(limit) = query.limit {
            pairs.append_pair("limit", &limit.to_string());
        }
        if let Some(offset) = query.offset {
            pairs.append_pair("offset", &offset.to_string());
        }
    }
    Ok((
        method,
        format!(
            "{}{}",
            url.path(),
            url.query()
                .filter(|q| !q.is_empty())
                .map(|q| format!("?{q}"))
                .unwrap_or_default()
        ),
    ))
}
#[tauri::command(rename_all = "snake_case")]
async fn account_request(
    app: tauri::AppHandle,
    operation: AccountOperation,
    workspace: Option<String>,
    account: Option<String>,
    authorization: Option<String>,
    query: Option<Query>,
    body: Option<Value>,
    connection_id: Option<String>,
) -> Result<Value, String> {
    let (method, route) = account_route(
        operation,
        workspace,
        account,
        authorization,
        query.unwrap_or_default(),
    )?;
    tauri::async_runtime::spawn_blocking(move || {
        connected::request(&app, connection_id, method, &route, body)
    })
    .await
    .map_err(|_| "Native request failed")?
}
/// Accept only Cursor's desktop login confirmation page; its PKCE verifier stays in the backend.
fn cursor_login_url(url: &str) -> Result<String, String> {
    let parsed = reqwest::Url::parse(url).map_err(|_| "Invalid login address")?;
    let safe = |value: &str| {
        !value.is_empty()
            && value.len() <= 128
            && value
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"-_".contains(&c))
    };
    let pairs: Vec<_> = parsed.query_pairs().collect();
    let keys: std::collections::BTreeSet<_> = pairs.iter().map(|(key, _)| key.as_ref()).collect();
    if parsed.scheme() != "https"
        || parsed.host_str() != Some("cursor.com")
        || parsed.port().is_some()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.path() != "/loginDeepControl"
        || parsed.fragment().is_some()
        || pairs.len() != 3
        || keys != ["challenge", "mode", "uuid"].into_iter().collect()
        || !pairs.iter().all(|(_, value)| safe(value))
    {
        return Err("Invalid login address".into());
    }
    Ok(parsed.to_string())
}
#[tauri::command]
fn open_cursor_login(url: String) -> Result<(), String> {
    let url = cursor_login_url(&url)?;
    #[cfg(target_os = "macos")]
    let result = std::process::Command::new("open").arg(&url).spawn();
    #[cfg(target_os = "windows")]
    let result = std::process::Command::new("rundll32.exe")
        .args(["url.dll,FileProtocolHandler", &url])
        .spawn();
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    let result = std::process::Command::new("xdg-open").arg(&url).spawn();
    result
        .map(|_| ())
        .map_err(|_| "无法打开浏览器，请复制授权链接到浏览器中打开。".into())
}
#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum NativeOperation {
    Status,
    Unlock,
    Detect,
    CursorPaths,
    Switch,
    SwitchCommand,
    SwitchStatus,
    Backups,
    Restore,
    Background,
    Resume,
}
#[tauri::command]
async fn desktop_request(
    app: tauri::AppHandle,
    operation: NativeOperation,
    body: Option<Value>,
) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        use NativeOperation::*;
        let runtime = app.state::<Runtime>();
        if matches!(operation, Status | Unlock) {
            if runtime.starting.load(Ordering::SeqCst) {
                return Ok(json!({"status": 200, "body": {"phase": "starting", "background": false}}));
            }
            if !runtime.backend_running()? {
                if matches!(operation, Unlock) {
                    start_backend(&app)?;
                    return Ok(json!({"status": 200, "body": {"phase": "starting", "background": false}}));
                }
                let error = runtime.error.lock().map_err(|_| "Runtime lock failed")?.clone()
                    .unwrap_or_else(|| "本地后台进程已退出，请重试连接。[runtime_stopped]".into());
                return Ok(json!({"status": 200, "body": {"phase": "unavailable", "error": error, "background": false}}));
            }
        }
        let (method, path) = match operation {
            Status => ("GET", "/native/status"), Unlock => ("POST", "/native/unlock"), Detect => ("GET", "/native/cursor"),
            CursorPaths => ("PUT", "/native/cursor"),
            Switch => ("POST", "/native/switch"), SwitchStatus => ("GET", "/native/switch"), Backups => ("GET", "/native/backups"),
            SwitchCommand => ("POST", "/native/switch-command"),
            Restore => ("POST", "/native/restore"), Background => ("PUT", "/native/background"), Resume => ("POST", "/native/resume"),
        };
        let response = connection(&app)?.request(method, path, body)?;
        if response["status"] == 200 {
            if let Some(background) = response["body"]["background"].as_bool() { runtime.background.store(background, Ordering::SeqCst); }
        }
        Ok(response)
    }).await.map_err(|_| "Native request failed")?
}
#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum ArchiveOperation {
    Export,
    Import,
    Recover,
}
#[tauri::command]
async fn desktop_archive(
    app: tauri::AppHandle,
    operation: ArchiveOperation,
    workspace: Option<String>,
    password: String,
) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        if !(12..=256).contains(&password.chars().count()) {
            return Err("Invalid archive password length".into());
        }
        let dialog =
            rfd::FileDialog::new().add_filter("Cursor Panel encrypted archive", &["cursorarchive"]);
        let path = match operation {
            ArchiveOperation::Export => dialog
                .set_file_name("Cursor Panel.cursorarchive")
                .save_file(),
            _ => dialog.pick_file(),
        };
        let Some(path) = path else {
            return Ok(json!({"status": 200, "body": {"cancelled": true}}));
        };
        let (route, workspace) = match operation {
            ArchiveOperation::Export => ("/native/archive/export", Some(identifier(workspace)?)),
            ArchiveOperation::Import => ("/native/archive/import", Some(identifier(workspace)?)),
            ArchiveOperation::Recover => ("/native/recover", None),
        };
        connection(&app)?.request(
            "POST",
            route,
            Some(json!({"path": path, "password": password, "workspace_id": workspace})),
        )
    })
    .await
    .map_err(|_| "Native archive operation failed")?
}
#[tauri::command]
async fn desktop_open_backups(app: tauri::AppHandle) -> Result<(), String> {
    let path = app.state::<Runtime>().directory.join("cursor-backups");
    tauri::async_runtime::spawn_blocking(move || {
        let command = if cfg!(windows) {
            "explorer.exe"
        } else {
            "/usr/bin/open"
        };
        Command::new(command)
            .arg(path)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "Cannot open backup folder")?;
        Ok(())
    })
    .await
    .map_err(|_| "Cannot open backup folder")?
}
#[tauri::command]
fn frontend_ready(app: tauri::AppHandle, rendered_accounts: usize) -> Result<(), String> {
    let runtime = app.state::<Runtime>();
    if let Some(path) = &runtime.report_path {
        if rendered_accounts != 2 {
            return Err("Fixture DOM is incomplete".into());
        }
        if !runtime.reported.swap(true, Ordering::SeqCst) {
            let guard = runtime.backend.lock().map_err(|_| "Runtime lock failed")?;
            let backend = guard.as_ref().ok_or("Runtime unavailable")?;
            let ready = &backend
                .connection
                .as_ref()
                .ok_or("Runtime unavailable")?
                .ready;
            let report = json!({"backend": ready, "backend_start_ms": backend.start_ms, "ui_ready_ms": runtime.start.elapsed().as_millis(),
                "shell_pid": std::process::id(), "target": env!("P0_TARGET"), "frontend": {"rendered_accounts": rendered_accounts}, "keyring": {"ok": true}});
            fs::write(
                path,
                serde_json::to_vec_pretty(&report).map_err(|_| "Cannot encode report")?,
            )
            .map_err(|_| "Cannot write fixture report")?;
            let handle = app.clone();
            thread::spawn(move || {
                thread::sleep(Duration::from_secs(8));
                handle.exit(0);
            });
        }
    }
    Ok(())
}
pub(crate) fn stop_for_update(app: &tauri::AppHandle) -> Result<(), String> {
    let runtime = app.state::<Runtime>();
    if runtime.starting.load(Ordering::SeqCst) {
        return Err("本地后台正在启动，请稍后再升级。".into());
    }
    let current = connection(app).map_err(|_| "本地后台未就绪，请重新打开应用后再升级。")?;
    let status = current
        .request("GET", "/native/status", None)
        .map_err(|_| "无法确认本地后台状态，请稍后再升级。")?;
    if status["body"]["switch"]["busy"] == true {
        return Err("账号切换正在进行，请完成后再升级。".into());
    }
    if status["body"]["phase"] != "ready" && status["body"]["phase"] != "locked" {
        return Err("本地数据目录暂不可用，请完成恢复后再升级。".into());
    }
    runtime.updating.store(true, Ordering::SeqCst);
    let backend = runtime
        .backend
        .lock()
        .map_err(|_| "无法结束本地后台。")?
        .take();
    drop(backend); // Close the control pipe and wait for active work and database shutdown.
    Ok(())
}

pub(crate) fn resume_after_update_failure(app: &tauri::AppHandle) {
    let runtime = app.state::<Runtime>();
    match Backend::start(app, &runtime.directory, false) {
        Ok(backend) => *runtime.backend.lock().unwrap() = Some(backend),
        Err(error) => *runtime.error.lock().unwrap() = Some(error),
    }
    runtime.updating.store(false, Ordering::SeqCst);
}

pub(crate) fn backup_for_update(app: &tauri::AppHandle) -> Result<(), String> {
    fn copy_directory(source: &std::path::Path, target: &std::path::Path) -> std::io::Result<()> {
        fs::create_dir_all(target)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(target, fs::Permissions::from_mode(0o700))?;
        }
        for entry in fs::read_dir(source)? {
            let entry = entry?;
            let kind = entry.file_type()?;
            if kind.is_symlink() {
                return Err(std::io::Error::other("Backup cannot contain symlinks"));
            }
            let destination = target.join(entry.file_name());
            if kind.is_dir() {
                copy_directory(&entry.path(), &destination)?;
            } else if kind.is_file() {
                fs::copy(entry.path(), destination)?;
            }
        }
        Ok(())
    }
    let directory = app.state::<Runtime>().directory.clone();
    let backups = app
        .path()
        .app_cache_dir()
        .map_err(|_| "无法找到升级备份目录。")?
        .join("update-backups");
    let id = nonce();
    let temporary = backups.join(format!(".partial-{id}"));
    if copy_directory(&directory, &temporary).and_then(|_| fs::rename(&temporary, backups.join(&id))).is_err() {
        let _ = fs::remove_dir_all(temporary);
        return Err("升级前备份未完成，当前应用未被替换，请检查磁盘空间后重试。".into());
    }
    backup_retention::prune(&backups, &id, 3, 1024 * 1024 * 1024)
        .map_err(|_| "升级备份已保存，但旧备份清理失败；请检查备份目录权限和磁盘空间。")?;
    Ok(())
}

pub fn run() {
    let started = Instant::now();
    let args: Vec<_> = std::env::args_os().collect();
    let report = args
        .iter()
        .position(|arg| arg == "--desktop-smoke")
        .and_then(|index| args.get(index + 1))
        .map(PathBuf::from);
    let mut builder = tauri::Builder::default();
    // Verification owns an isolated temporary database and must not activate a user's running app.
    if report.is_none() {
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }));
    }
    let application = builder
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(crate::updates::Updates::default())
        .setup(move |app| {
            let directory = if let Some(report) = &report {
                report
                    .parent()
                    .ok_or("Missing fixture directory")?
                    .join("fixture-data")
            } else {
                app.path().app_data_dir()?
            };
            let log_path = if let Some(report) = &report {
                report.parent().map(|parent| parent.join("logs/startup.json"))
            } else {
                app.path().app_log_dir().ok().map(|directory| directory.join("startup.json"))
            };
            app.manage(Runtime {
                backend: Mutex::new(None),
                error: Mutex::new(None),
                starting: AtomicBool::new(false),
                updating: AtomicBool::new(false),
                background: AtomicBool::new(false),
                start: started,
                directory: directory.clone(),
                log_path,
                report_path: report,
                reported: AtomicBool::new(false),
            });
            let show = MenuItem::with_id(app, "show", "打开 Cursor Panel", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出 Cursor Panel", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;
            TrayIconBuilder::new()
                .icon(
                    app.default_window_icon()
                        .ok_or("Missing application icon")?
                        .clone(),
                )
                .tooltip("Cursor Panel")
                .menu(&menu)
                .on_menu_event(|app, event| {
                    if event.id.as_ref() == "quit" {
                        app.exit(0);
                    }
                    if event.id.as_ref() == "show" {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)?;
            start_backend(app.handle())?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            account_request,
            desktop_request,
            desktop_archive,
            desktop_open_backups,
            open_cursor_login,
            connected::connection_request,
            connected::remote_manage,
            crate::updates::check_update,
            crate::updates::install_update,
            crate::updates::open_releases,
            frontend_ready
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let runtime = window.state::<Runtime>();
                if runtime.background.load(Ordering::SeqCst) {
                    api.prevent_close();
                    let _ = window.hide();
                } else {
                    window.app_handle().exit(0);
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("Cannot initialize Cursor Panel");
    application.run(|handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            handle.state::<Runtime>().backend.lock().unwrap().take();
        }
    });
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn startup_failure_preserves_category_without_forwarding_arbitrary_details() {
        let failure = parse_readiness(r#"{"error":{"code":"data_permissions","os_error":5,"message":"private credential"}}"#).err().unwrap();
        assert!(failure.contains("data_permissions; os=5"));
        assert!(!failure.contains("private credential"));
        let unknown = parse_readiness(r#"{"error":{"code":"private credential"}}"#).err().unwrap();
        assert!(unknown.contains("[runtime]"));
        assert!(!unknown.contains("private credential"));
        assert!(parse_readiness("").is_err());
        assert!(parse_readiness(r#"{"port":1234,"pid":42,"protocol":1,"frozen":true,"fixture":false,"imports_ms":1,"core_ms":2,"listener_ms":3}"#).is_ok());
    }
    #[test]
    fn fixed_routes_reject_url_and_path_injection() {
        assert!(account_route(
            AccountOperation::Get,
            Some("http://example.test".into()),
            None,
            None,
            Query::default()
        )
        .is_err());
        let id = "00000000-0000-0000-0000-000000000001".to_string();
        assert!(account_route(
            AccountOperation::Get,
            Some(id.clone()),
            Some("../native/recover".into()),
            None,
            Query::default()
        )
        .is_err());
        assert!(account_route(
            AccountOperation::AuthorizationPoll,
            Some(id.clone()),
            None,
            Some("../../accounts".into()),
            Query::default()
        )
        .is_err());
        assert_eq!(
            account_route(
                AccountOperation::AuthorizationCancel,
                Some(id.clone()),
                None,
                Some(id.clone()),
                Query::default()
            )
            .unwrap(),
            ("DELETE", format!("/api/v1/workspaces/{id}/authorizations/{id}"))
        );
        let (_, path) = account_route(
            AccountOperation::List,
            Some(id),
            None,
            None,
            Query {
                q: Some("&admin=true#".into()),
                ..Query::default()
            },
        )
        .unwrap();
        assert!(path.ends_with("?q=%26admin%3Dtrue%23"));
    }
    #[test]
    fn browser_login_opens_only_cursor_confirmation_pages() {
        let valid = "https://cursor.com/loginDeepControl?challenge=abc_DEF-1&uuid=0f8fad5b-d9cb-469f-a165-70867728950e&mode=login";
        assert_eq!(cursor_login_url(valid).unwrap(), valid);
        for url in [
            "http://cursor.com/loginDeepControl?challenge=a&uuid=b&mode=login",
            "https://cursor.com.evil.test/loginDeepControl?challenge=a&uuid=b&mode=login",
            "https://user@cursor.com/loginDeepControl?challenge=a&uuid=b&mode=login",
            "https://cursor.com:8443/loginDeepControl?challenge=a&uuid=b&mode=login",
            "https://cursor.com/settings?challenge=a&uuid=b&mode=login",
            "https://cursor.com/loginDeepControl?challenge=a&uuid=b&mode=login#x",
            "https://cursor.com/loginDeepControl?challenge=a&uuid=b&mode=login&next=https://evil.test",
            "https://cursor.com/loginDeepControl?challenge=a&challenge=b&mode=login",
            "https://cursor.com/loginDeepControl?challenge=a%20b&uuid=b&mode=login",
            "file:///etc/passwd",
        ] {
            assert!(cursor_login_url(url).is_err(), "{url}");
        }
    }
}
