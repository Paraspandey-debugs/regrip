//! TLS root store and hyper-rustls HTTPS/HTTP upstream connector.

use std::fs::File;
use std::io::BufReader;
use std::path::Path;
use std::sync::Arc;

use bytes::Bytes;
use http::{HeaderMap, Request, Response, Uri};
use http_body_util::Full;
use hyper::body::Incoming;
use hyper_rustls::{HttpsConnector, HttpsConnectorBuilder};
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use rustls::crypto::ring;
use rustls::pki_types::CertificateDer;
use rustls::{ClientConfig, RootCertStore};

pub type UpstreamClient = Client<HttpsConnector<HttpConnector>, Full<Bytes>>;

/// Build a pooled hyper-rustls client supporting both `http://` and `https://` upstreams.
pub fn build_upstream_client(ca_cert_path: Option<&Path>) -> Result<UpstreamClient, String> {
    let mut roots = RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());

    if let Some(path) = ca_cert_path {
        let file = File::open(path)
            .map_err(|e| format!("Failed to open CA cert {}: {e}", path.display()))?;
        let mut reader = BufReader::new(file);
        let certs: Vec<CertificateDer<'static>> = rustls_pemfile::certs(&mut reader)
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("Failed to parse PEM CA cert {}: {e}", path.display()))?;
        for cert in certs {
            roots
                .add(cert)
                .map_err(|e| format!("Invalid CA cert {}: {e}", path.display()))?;
        }
    }

    let tls_cfg = ClientConfig::builder_with_provider(Arc::new(ring::default_provider()))
        .with_safe_default_protocol_versions()
        .map_err(|e| format!("TLS protocol config failed: {e}"))?
        .with_root_certificates(roots)
        .with_no_client_auth();

    let connector = HttpsConnectorBuilder::new()
        .with_tls_config(tls_cfg)
        .https_or_http()
        .enable_http1()
        .build();

    Ok(Client::builder(TokioExecutor::new()).build(connector))
}

/// Forward an HTTP/1.1 request to an `http://` or `https://` upstream via hyper-rustls.
pub async fn send_upstream_request(
    client: &UpstreamClient,
    upstream: &Uri,
    method: http::Method,
    target_path_and_query: &str,
    headers: &HeaderMap,
    body: Bytes,
) -> Result<Response<Incoming>, String> {
    let scheme = upstream.scheme_str().unwrap_or("http");
    let authority = upstream
        .authority()
        .ok_or_else(|| "Upstream URI missing host".to_string())?;
    let full_uri = format!("{scheme}://{authority}{target_path_and_query}");

    let mut builder = Request::builder().method(method).uri(full_uri);
    for (k, v) in headers {
        let name = k.as_str();
        if !name.eq_ignore_ascii_case("host") && !name.eq_ignore_ascii_case("content-length") {
            builder = builder.header(k, v);
        }
    }
    let out_req = builder.body(Full::new(body)).map_err(|e| e.to_string())?;
    client
        .request(out_req)
        .await
        .map_err(|e| format!("Upstream request failed: {e}"))
}
