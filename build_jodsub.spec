# PyInstaller build for the local-first JodSub macOS application.
hidden = [
    "numpy", "soundfile", "torch", "transformers", "whisperx",
    "faster_whisper", "laonlp", "whisper", "pyannote.audio",
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
    ],
    hiddenimports=hidden,
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="JodSub", console=True)
app = BUNDLE(exe, name="JodSub.app", icon="assets/JodSub.icns", bundle_identifier="com.jodsub.app")
