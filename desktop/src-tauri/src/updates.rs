use semver::Version;
use serde::Serialize;
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Mutex,
};
use std::time::Duration;
use tauri::{ipc::Channel, Manager};
use tauri_plugin_updater::{Update, UpdaterExt};

const RELEASES: &str = "https://github.com/devilcoolyue/cursor-panel/releases";
// Published manifests retain this path for clients installed before the rename.
const LEGACY_RELEASES: &str = "https://github.com/devilcoolyue/cursor-dashboard/releases";
const LATEST: &str = "https://api.github.com/repos/devilcoolyue/cursor-panel/releases/latest";
const PUBLIC_KEY: &str = include_str!("../../../cursor_dashboard/updates/public-key.txt");

#[tauri::command]
pub fn open_releases() -> Result<(), String> {
    #[cfg(target_os = "macos")]
    let result = std::process::Command::new("open").arg(RELEASES).spawn();
    #[cfg(target_os = "windows")]
    let result = std::process::Command::new("rundll32.exe").args(["url.dll,FileProtocolHandler", RELEASES]).spawn();
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    let result = std::process::Command::new("xdg-open").arg(RELEASES).spawn();
    result.map(|_| ()).map_err(|_| "无法打开发行记录，请在浏览器中访问项目发布页。".into())
}

#[derive(Default)]
pub struct Updates {
    pending: Mutex<Option<Update>>,
    busy: AtomicBool,
}
struct Busy<'a>(&'a AtomicBool);
impl Drop for Busy<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}
impl Updates {
    fn begin(&self) -> Result<Busy<'_>, String> {
        if self.busy.swap(true, Ordering::SeqCst) {
            return Err("正在处理更新，请稍候。".into());
        }
        Ok(Busy(&self.busy))
    }
}

#[derive(Clone, Serialize)]
pub struct Progress {
    stage: &'static str,
    downloaded: u64,
    total: Option<u64>,
}

fn valid_asset(url: &str, version: &str) -> bool {
    [RELEASES, LEGACY_RELEASES].iter().any(|origin| {
        let prefix = format!("{origin}/download/v{version}/");
        url.strip_prefix(&prefix).is_some_and(|name| {
            !name.is_empty()
                && name
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"._+-".contains(&c))
        })
    })
}

struct LatestRelease {
    version: Version,
    notes: String,
    published_at: Value,
}

fn stable_version(text: &str) -> Result<Version, String> {
    let version = Version::parse(text).map_err(|_| "发布版本无效。")?;
    if !version.pre.is_empty() || !version.build.is_empty() {
        return Err("发布版本无效。".into());
    }
    Ok(version)
}

fn rate_limit_message(reset: Option<u64>, now: u64) -> String {
    match reset.filter(|reset| *reset > now) {
        Some(reset) => format!(
            "GitHub 暂时限制了更新检查，请约 {} 分钟后重试。",
            (reset - now).div_ceil(60)
        ),
        None => "GitHub 暂时限制了更新检查，请稍后重试。".into(),
    }
}

async fn read_json(response: reqwest::Response) -> Result<Value, String> {
    let body = response.bytes().await.map_err(|_| "无法读取更新信息。")?;
    if body.len() > 262144 {
        return Err("更新信息超过大小限制。".into());
    }
    serde_json::from_slice(&body).map_err(|_| "发布信息无效。".into())
}

/// The REST API provides release notes, but GitHub limits anonymous calls per IP address.
/// When it refuses, the latest release's updater manifest still identifies the version;
/// the update package itself is verified by the updater signature either way.
async fn latest_release(client: &reqwest::Client) -> Result<Option<LatestRelease>, String> {
    use reqwest::StatusCode;
    let mut failure = "无法连接发布服务，请稍后重试。".to_string();
    match client.get(LATEST).send().await {
        Ok(response) if response.status() == StatusCode::NOT_FOUND => return Ok(None),
        Ok(response) if response.status().is_success() => {
            let release = read_json(response).await?;
            let tag = release["tag_name"].as_str().ok_or("发布版本无效。")?;
            let version = stable_version(tag.strip_prefix('v').ok_or("发布版本无效。")?)?;
            if release["draft"] == true || release["prerelease"] == true {
                return Err("发布版本无效。".into());
            }
            return Ok(Some(LatestRelease {
                version,
                notes: release["body"].as_str().unwrap_or("").into(),
                published_at: release["published_at"].clone(),
            }));
        }
        Ok(response) => {
            let status = response.status();
            failure = if status == StatusCode::FORBIDDEN || status == StatusCode::TOO_MANY_REQUESTS {
                let reset = response
                    .headers()
                    .get("x-ratelimit-reset")
                    .and_then(|value| value.to_str().ok())
                    .and_then(|value| value.parse().ok());
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|elapsed| elapsed.as_secs())
                    .unwrap_or(0);
                rate_limit_message(reset, now)
            } else {
                "发布服务暂不可用，请稍后重试。".into()
            };
        }
        Err(_) => {}
    }
    let manifest = match client
        .get(format!("{RELEASES}/latest/download/latest.json"))
        .send()
        .await
    {
        Ok(response) if response.status() == StatusCode::NOT_FOUND => return Ok(None),
        Ok(response) if response.status().is_success() => {
            read_json(response).await.map_err(|_| failure.clone())?
        }
        _ => return Err(failure),
    };
    let version = manifest["version"]
        .as_str()
        .and_then(|text| stable_version(text.strip_prefix('v').unwrap_or(text)).ok())
        .ok_or(failure)?;
    Ok(Some(LatestRelease {
        version,
        notes: manifest["notes"].as_str().unwrap_or("").into(),
        published_at: manifest["pub_date"].clone(),
    }))
}

#[tauri::command]
pub async fn check_update(app: tauri::AppHandle) -> Result<Value, String> {
    let state = app.state::<Updates>();
    let _busy = state.begin()?;
    *state.pending.lock().unwrap() = None;
    let current = app.package_info().version.clone();
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .user_agent("Cursor-Panel-Updates")
        .build()
        .map_err(|_| "无法建立更新连接。")?;
    let mut result = json!({"current_version": current.to_string(), "latest_version": null,
        "available": false, "installable": false, "notes": "", "release_url": RELEASES, "published_at": null});
    let Some(release) = latest_release(&client).await? else {
        return Ok(result);
    };
    let version = release.version;
    let version_text = version.to_string();
    result["latest_version"] = json!(version_text);
    result["available"] = json!(version > current);
    result["notes"] = json!(release.notes.chars().take(12000).collect::<String>());
    result["release_url"] = json!(format!("{RELEASES}/tag/v{version}"));
    result["published_at"] = release.published_at;
    if version <= current {
        return Ok(result);
    }
    let endpoint = format!("{RELEASES}/download/v{version}/latest.json")
        .parse()
        .map_err(|_| "更新地址无效。")?;
    let updater = app
        .updater_builder()
        .pubkey(PUBLIC_KEY.trim())
        .endpoints(vec![endpoint])
        .map_err(|_| "更新配置无效。")?
        .timeout(Duration::from_secs(300))
        .build()
        .map_err(|_| "无法初始化更新器。")?;
    match updater.check().await {
        Ok(Some(update))
            if update.version == version_text
                && valid_asset(update.download_url.as_str(), &version_text) =>
        {
            result["installable"] = json!(true);
            *state.pending.lock().unwrap() = Some(update);
        }
        Ok(None)
        | Err(tauri_plugin_updater::Error::ReleaseNotFound)
        | Err(tauri_plugin_updater::Error::TargetNotFound(_))
        | Err(tauri_plugin_updater::Error::TargetsNotFound(_)) => {}
        _ => return Err("无法验证更新包信息，请稍后重新检查。".into()),
    }
    Ok(result)
}

#[tauri::command]
pub async fn install_update(
    app: tauri::AppHandle,
    version: String,
    progress: Channel<Progress>,
) -> Result<(), String> {
    let state = app.state::<Updates>();
    let _busy = state.begin()?;
    let update = state
        .pending
        .lock()
        .unwrap()
        .as_ref()
        .filter(|item| item.version == version)
        .cloned()
        .ok_or("请先检查更新，再安装对应版本。")?;
    let mut downloaded = 0;
    let _ = progress.send(Progress {
        stage: "downloading",
        downloaded,
        total: None,
    });
    let bytes = update
        .download(
            |chunk, total| {
                downloaded += chunk as u64;
                let _ = progress.send(Progress {
                    stage: "downloading",
                    downloaded,
                    total,
                });
            },
            || {},
        )
        .await
        .map_err(|_| "下载或签名校验失败，当前应用未被替换，请重试。")?;
    let _ = progress.send(Progress {
        stage: "installing",
        downloaded,
        total: Some(downloaded),
    });
    let handle = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        crate::app::stop_for_update(&handle)?;
        if let Err(error) = crate::app::backup_for_update(&handle) {
            crate::app::resume_after_update_failure(&handle);
            return Err(error);
        }
        if update.install(bytes).is_err() {
            crate::app::resume_after_update_failure(&handle);
            return Err("安装未完成，已恢复本地后台，请检查安装权限后重试。".to_string());
        }
        handle.restart();
    })
    .await
    .map_err(|_| "更新安装中断，请重新打开应用。")?
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn updates_only_use_assets_from_the_selected_release() {
        for origin in [RELEASES, LEGACY_RELEASES] {
            assert!(valid_asset(
                &format!("{origin}/download/v1.2.3/Cursor.Panel_aarch64.app.tar.gz"),
                "1.2.3"
            ));
            for url in [
                "https://evil.test/update",
                "file:///tmp/update",
                "https://github.com/other-owner/cursor-panel/releases/download/v1.2.3/update",
                &format!("{origin}/download/v1.2.2/update"),
                &format!("{origin}/download/v1.2.3/../update"),
                &format!("{origin}/download/v1.2.3/update?token=x"),
                &format!("{origin}/download/v1.2.3/"),
                &format!("{origin}/download/v1.2.3/update#fragment"),
            ] {
                assert!(!valid_asset(url, "1.2.3"));
            }
        }
    }
    #[test]
    fn rate_limits_report_the_reset_wait_and_versions_are_stable() {
        assert_eq!(rate_limit_message(Some(1_000 + 61), 1_000), "GitHub 暂时限制了更新检查，请约 2 分钟后重试。");
        assert_eq!(rate_limit_message(Some(1_000 + 1), 1_000), "GitHub 暂时限制了更新检查，请约 1 分钟后重试。");
        for reset in [None, Some(999)] {
            assert_eq!(rate_limit_message(reset, 1_000), "GitHub 暂时限制了更新检查，请稍后重试。");
        }
        assert!(stable_version("0.0.11").is_ok());
        for text in ["0.0.11-beta.1", "0.0.11+build", "latest"] {
            assert!(stable_version(text).is_err());
        }
    }
}
