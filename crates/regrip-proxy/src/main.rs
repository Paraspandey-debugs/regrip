//! CLI entrypoint for regrip-proxy.

use std::path::PathBuf;

use clap::Parser;

use regrip_proxy::stdio_proxy::{run_stdio_proxy, ProxyConfig};

#[derive(Debug, Parser)]
#[command(
    name = "regrip-proxy",
    about = "Regrip high-speed MCP stdio capture proxy"
)]
struct Args {
    #[arg(long)]
    server: String,
    #[arg(long, default_value = "default-agent")]
    agent: String,
    #[arg(long)]
    session: Option<String>,
    #[arg(long, default_value = ".regrip/blobs")]
    blob_dir: PathBuf,
    #[arg(last = true, required = true)]
    command: Vec<String>,
}

#[tokio::main]
async fn main() {
    let args = Args::parse();
    let secret = std::env::var("REGRIP_HMAC_SECRET")
        .unwrap_or_else(|_| "regrip-local-default-salt".to_string());
    let cfg = ProxyConfig {
        server_name: args.server,
        agent_id: args.agent,
        session_id: args.session,
        blob_dir: args.blob_dir,
        hmac_secret: secret,
        rules: Vec::new(),
    };
    match run_stdio_proxy(&args.command, cfg).await {
        Ok(code) => std::process::exit(code),
        Err(err) => {
            eprintln!("regrip-proxy error: {err}");
            std::process::exit(3);
        }
    }
}
