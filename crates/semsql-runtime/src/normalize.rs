//! NL normalisation — runs before Stage 0a/0b.
//!
//! - NFC unicode normalisation.
//! - Lowercase.
//! - Collapse runs of whitespace to single spaces.
//! - Strip control + zero-width characters.
//!
//! Anything richer (tokenisation, lemmatisation) is the model's job — keep
//! this layer dumb so it remains a deterministic, reproducible boundary.

use unicode_normalization::UnicodeNormalization;

/// Normalise an NL query for downstream matching.
pub fn normalize(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut last_was_ws = false;
    for ch in input.nfc() {
        if is_zero_width(ch) {
            continue;
        }
        if ch.is_whitespace() {
            // Tab and newline are control chars *and* whitespace — handle the
            // whitespace meaning first so they collapse rather than disappear.
            if !last_was_ws && !out.is_empty() {
                out.push(' ');
            }
            last_was_ws = true;
            continue;
        }
        if ch.is_control() {
            continue;
        }
        for lc in ch.to_lowercase() {
            out.push(lc);
        }
        last_was_ws = false;
    }
    if out.ends_with(' ') {
        out.pop();
    }
    out
}

fn is_zero_width(c: char) -> bool {
    matches!(
        c,
        '\u{200B}' | '\u{200C}' | '\u{200D}' | '\u{2060}' | '\u{FEFF}'
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn collapses_whitespace_and_lowercases() {
        assert_eq!(normalize("  Bleeding\tMoney  "), "bleeding money");
    }

    #[test]
    fn strips_zero_width() {
        assert_eq!(normalize("ten\u{200B}ants"), "tenants");
    }

    #[test]
    fn nfc_normalises() {
        // "café" composed vs. decomposed must collapse.
        let composed = "café";
        let decomposed = "cafe\u{0301}";
        assert_eq!(normalize(composed), normalize(decomposed));
    }
}
