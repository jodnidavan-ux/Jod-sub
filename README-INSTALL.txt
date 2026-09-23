JodSub for macOS
=================

1. Drag JodSub.app to Applications (or open it directly).
2. On first launch, macOS may show a security warning because this copy is
   unsigned. Open System Settings > Privacy & Security and choose Open Anyway.
3. The app opens http://127.0.0.1:8877/ in the default browser.

Projects, editing profiles, exports, and SFX customizations are stored per
machine under ~/Library/Application Support/JodSub and are not overwritten by
future app updates. API keys remain in the browser's local storage and are not
included in this package.

This build targets macOS x86_64. A universal/Apple-silicon build requires a
separate arm64 build environment. Automatic updates require an Apple Developer
signing/notarization identity and a hosted update feed; those are intentionally
not fabricated or bundled.
