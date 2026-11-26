//! CLI entrypoint for regrip-proxy (stdio & Streamable HTTP capture).

use std::path::PathBuf;

use clap::Parser;

use regrip_proxy::config_loader::{load_proxy_settings, LoadedProxySettings};
use regrip_proxy::http_proxy::run_http_proxy_with_tls;
use regrip_proxy::stdio_proxy::{run_stdio_proxy, ProxyConfig};

#[derive(Debug, Parser)]
#[command(
    name = "regrip-proxy",
    about = "Regrip high-speed MCP stdio and Streamable HTTP capture proxy"
)]
struct Args {
    #[arg(long)]
    server: String,
    #[arg(long, default_value = "default-agent")]
    agent: String,
    #[arg(long)]
    session: Option<String>,
    #[arg(long)]
    blob_dir: Option<PathBuf>,
    #[arg(long)]
    config: Option<PathBuf>,
    #[arg(long, requires = "upstream")]
    listen: Option<String>,
    #[arg(long, requires = "listen")]
    upstream: Option<String>,
    #[arg(long)]
    ca_cert: Option<PathBuf>,
    #[arg(last = true, required_unless_present = "listen")]
    command: Vec<String>,
}

#[tokio::main]
async fn main() {
    let args = Args::parse();
    let settings = match resolve_settings(args.config.as_deref()) {
        Ok(s) => s,
        Err(err) => {
            eprintln!("regrip-proxy config error: {err}");
            std::process::exit(3);
        }
    };
    let secret = std::env::var(&settings.hmac_secret_env)
        .unwrap_or_else(|_| "regrip-local-default-salt".to_string());
    let blob_dir = args
        .blob_dir
        .or(settings.blob_dir)
        .unwrap_or_else(|| PathBuf::from(".regrip/blobs"));
    let cfg = ProxyConfig {
        server_name: args.server,
        agent_id: args.agent,
        session_id: args.session,
        blob_dir,
        hmac_secret: secret,
        rules: if settings.redaction_enabled {
            settings.rules
        } else {
            Vec::new()
        },
    };

    if let (Some(listen), Some(upstream)) = (args.listen, args.upstream) {
        if let Err(err) =
            run_http_proxy_with_tls(&listen, &upstream, cfg, args.ca_cert.as_deref()).await
        {
            eprintln!("regrip-proxy http error: {err}");
            std::process::exit(3);
        }
        return;
    }

    match run_stdio_proxy(&args.command, cfg).await {
        Ok(code) => std::process::exit(code),
        Err(err) => {
            eprintln!("regrip-proxy error: {err}");
            std::process::exit(3);
        }
    }
}

fn resolve_settings(explicit: Option<&std::path::Path>) -> Result<LoadedProxySettings, String> {
    if let Some(path) = explicit {
        return load_proxy_settings(path);
    }
    let default_path = PathBuf::from("regrip.yaml");
    if default_path.exists() {
        return load_proxy_settings(&default_path);
    }
    Ok(LoadedProxySettings::default())
}
