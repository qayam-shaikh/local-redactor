# Changelog

## 0.2.0 — UI and detection repair

- Reworked the interface with a clearer document workspace, grouped controls, page zoom, and a more consistent visual system.
- Disabled graphics-scene item interaction inside the preview so pointer drags are handled by the canvas for manual redaction.
- Added clamping of manual redaction coordinates to source image dimensions.
- Replaced the single OCR pass with enhanced, upscaled local OCR and alternate page segmentation modes.
- Added optional Microsoft Presidio + spaCy local PII recognition and Indian identifier recognizers.
- Added fallback patterns for Aadhaar-like numbers, PAN, email, Indian mobile numbers, payment cards, account numbers with context labels, and IFSC codes.
- Updated installation to install Presidio and the spaCy English model locally.
