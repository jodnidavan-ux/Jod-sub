# PyInstaller build for the local-first JodSub macOS application.
import certifi

hidden = [
    "numpy", "soundfile", "torch", "transformers", "whisperx",
    "faster_whisper", "laonlp", "whisper", "pyannote.audio",
    "webview", "webview.platforms.cocoa", "certifi",
]

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("index.html", "."),
        ("editing_style.py", "."),
        ("sfx", "sfx"),
        ("bin/ffmpeg", "bin"),
        ("assets/JodSub.icns", "."),
        (certifi.where(), "certifi"),
    ],
    hiddenimports=hidden,
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="JodSub", console=False)
app = BUNDLE(exe, name="JodSub.app", icon="assets/JodSub.icns", bundle_identifier="com.jodsub.app")
