//! Loads redaction rules and storage settings from `regrip.yaml`.

use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::hmac_redactor::{RedactionAction, RedactionRule};

#[derive(Debug, Default, Deserialize)]
struct RawYamlConfig {
    storage: Option<RawStorage>,
    capture: Option<RawCapture>,
}

#[derive(Debug, Default, Deserialize)]
struct RawStorage {
    blob_dir: Option<String>,
}

#[derive(Debug, Default, Deserialize)]
struct RawCapture {
    redaction: Option<RawRedaction>,
}

#[derive(Debug, Default, Deserialize)]
struct RawRedaction {
    enabled: Option<bool>,
    hmac_secret_env: Option<String>,
    rules: Option<Vec<RawRule>>,
}

#[derive(Debug, Deserialize)]
struct RawRule {
    path: String,
    action: String,
}

/// Extracted proxy-relevant settings from `regrip.yaml`.
#[derive(Debug, Clone)]
pub struct LoadedProxySettings {
    pub redaction_enabled: bool,
    pub hmac_secret_env: String,
    pub blob_dir: Option<PathBuf>,
    pub rules: Vec<RedactionRule>,
}

impl Default for LoadedProxySettings {
    fn default() -> Self {
        Self {
            redaction_enabled: true,
            hmac_secret_env: "REGRIP_HMAC_SECRET".to_string(),
            blob_dir: None,
            rules: Vec::new(),
        }
    }
}

/// Load `capture.redaction` and `storage.blob_dir` from a `regrip.yaml` file.
pub fn load_proxy_settings(path: &Path) -> Result<LoadedProxySettings, String> {
    let content = std::fs::read_to_string(path)
        .map_err(|e| format!("Failed to read config {}: {e}", path.display()))?;
    let raw: RawYamlConfig = serde_yaml::from_str(&content)
        .map_err(|e| format!("Failed to parse YAML {}: {e}", path.display()))?;

    let mut settings = LoadedProxySettings::default();
    if let Some(storage) = raw.storage {
        settings.blob_dir = storage.blob_dir.map(PathBuf::from);
    }
    if let Some(redaction) = raw.capture.and_then(|c| c.redaction) {
        if let Some(enabled) = redaction.enabled {
            settings.redaction_enabled = enabled;
        }
        if let Some(env_name) = redaction.hmac_secret_env {
            settings.hmac_secret_env = env_name;
        }
        if let Some(raw_rules) = redaction.rules {
            settings.rules = raw_rules.into_iter().filter_map(parse_rule).collect();
        }
    }
    Ok(settings)
}

fn parse_rule(rule: RawRule) -> Option<RedactionRule> {
    let action = match rule.action.to_ascii_lowercase().as_str() {
        "drop" => RedactionAction::Drop,
        "mask" => RedactionAction::Mask,
        "hmac_token" => RedactionAction::HmacToken,
        _ => return None,
    };
    Some(RedactionRule {
        path: rule.path,
        action,
    })
}
