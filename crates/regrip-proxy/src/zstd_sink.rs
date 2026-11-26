//! Fail-open Zstandard (.jsonl.zst) blob sink for redacted InteractionEnvelopes.

use std::fs;
use std::path::{Path, PathBuf};

use crate::envelope::InteractionEnvelope;

/// Write a redacted InteractionEnvelope to `<blob_dir>/<server>_<id>.jsonl.zst`.
pub fn write_zstd_envelope(
    blob_dir: &Path,
    envelope: &InteractionEnvelope,
) -> std::io::Result<PathBuf> {
    fs::create_dir_all(blob_dir)?;
    let line = format!("{}\n", serde_json::to_string(envelope)?);
    let compressed = zstd::stream::encode_all(line.as_bytes(), 3)?;
    let filename = format!(
        "{}_{}.jsonl.zst",
        envelope.target.server_name, envelope.interaction_id
    );
    let dest = blob_dir.join(filename);
    fs::write(&dest, compressed)?;
    Ok(dest)
}
