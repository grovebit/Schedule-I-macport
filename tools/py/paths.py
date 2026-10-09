"""Repository layout shared by the build scripts (deps/ is filled by build.py, see the pins there)."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEPS = os.path.join(ROOT, 'deps')
WORK = os.path.join(ROOT, 'work')
BUILD = os.path.join(ROOT, 'build')
TOOLS = os.path.join(ROOT, 'tools')

DATA_NAME = 'Schedule I_Data'
GAME_DIR_FILE = os.path.join(WORK, 'game-dir.txt')  # game folder of the last build

DOTNET = os.path.join(DEPS, 'dotnet', 'dotnet')
UNITY_MONO = os.path.join(DEPS, 'unity-mono')  # Unity editor's MonoBleedingEdge: macOS class libraries, mono
UNITY_MAC = os.path.join(DEPS, 'unity-mac-support')  # Unity's macOS Mono player module (arm64 variations)
STEAMWORKS_NET = os.path.join(DEPS, 'steamworks.net')
INPUT_SYSTEM = os.path.join(DEPS, 'inputsystem')
BURST = os.path.join(DEPS, 'burst')


def saved_game_dir():
    """Game folder of the last build, or None."""
    try:
        with open(GAME_DIR_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None
