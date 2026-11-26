//! High-speed MCP capture proxy and pre-persistence PII/secret redactor.

pub mod config_loader;
pub mod envelope;
pub mod hmac_redactor;
pub mod http_codec;
pub mod http_proxy;
pub mod stdio_proxy;
pub mod zstd_sink;
