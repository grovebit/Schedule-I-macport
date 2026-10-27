# Schedule I — Mac port (unofficial)

I'm a huge fan of the game and have put quite a few hours into it. Unfortunately, I only own a Mac, and playing
through emulation isn't a satisfying experience. I made this tool for everyone in the same position. Thanks to
TVGS for creating such a great game.

Play [Schedule I](https://store.steampowered.com/app/3164500/Schedule_I/) natively on an Apple Silicon Mac. This
tool turns **your own** Steam copy of the Windows version into a Mac app, right on your Mac.

> Unofficial: not affiliated with or endorsed by TVGS. This repository contains no game files.
> The app you build contains the game, so **don't share it**.

## What you need

- A Mac with Apple Silicon (M1 or newer) and macOS 15 or later
- About 11 GB of free disk space
- Schedule I on Steam: the **Windows version**, on the **`alternate`** branch
  (Steam → Schedule I → Properties → Game Versions & Betas → `alternate`)

## Build

In Terminal, in this folder:

```sh
./build.sh
```

- It finds the game in your Steam libraries, or asks you to pick the game folder.
- The first build takes about 10 minutes and downloads about 6 GB into `deps/` (nothing is installed anywhere
  else).
- If the Xcode Command Line Tools are missing, it starts their installer: run `./build.sh` again when it's done.

The app is `build/Schedule I.app`. It keeps up with the game: when you open it after Steam updated the game, it
rebuilds itself first (a minute or two, as only what changed is redone), then starts the game. It uses this folder
for that, so keep it. After updating this repository, run `./build.sh` again.

If the game or this folder is on an external drive (or in Documents, Desktop or Downloads), macOS asks during such an
update whether Schedule I may access its files: allow it, the rebuild needs them.

**Rather not use Terminal?** [`launcher-app/`](launcher-app/) is a small Mac app that does the setup, game updates
and settings with buttons.

## Play

1. Start Steam, logged in to the account that owns the game.
2. Open `build/Schedule I.app`.

**Saves** are in `~/Library/Application Support/com.TVGS.ScheduleI/Saves`; Steam Cloud doesn't sync them. To
continue a Windows save, copy its folder from `%USERPROFILE%\AppData\LocalLow\TVGS\Schedule I\Saves` on the PC.

**Low frame rate?** Render the game at a lower resolution, upscaled with FSR (menus stay sharp):

```sh
defaults write com.TVGS.ScheduleI MacPortRenderScale -float 0.75   # 75% resolution
defaults delete com.TVGS.ScheduleI MacPortRenderScale              # back to full resolution
```

## Troubleshooting

| You see | What to do |
| --- | --- |
| "… is the IL2CPP build" | Switch the game to the `alternate` branch in Steam, let it update, then run `./build.sh` again. |
| "Couldn't find the Windows version of Schedule I" | Run `./build.sh` again and pick the game folder, or name it: `./build.sh --game "/path/to/Schedule I"`. |
| "the game files use Unity …" (or another version) | The game was updated to a version the port doesn't support yet: update this repository and try again. |
| "Couldn't update Schedule I" when you open the app | The app couldn't rebuild itself after a game update; the message says why. **Play Anyway** starts the previous version, **Show Log** opens `work/app-update.log`. |
| "Operation not permitted" | macOS didn't let the app read the game or this folder: allow it in System Settings → Privacy & Security → Files & Folders → Schedule I. |
| Short stutters the first time you see a place | Normal: macOS prepares the graphics once, then remembers them. |

## For developers

### How it works

- **Unity player:** the Windows player is replaced by Unity 2022.3.62f2's macOS (arm64) player and Mono
  libraries.
- **Shaders:** D3D11 → Metal, translated by `tools/dxbc2metal` (a fork of Unity's HLSLcc in `tools/hlslcc`) and
  checked with the Metal compiler (`tools/mtlcheck`).
- **Burst code:** recompiled for arm64 with the game's Burst version (`tools/py/burst_aot.py`).
- **Steamworks.NET and the Input System:** rebuilt for macOS (`work/build/`), with the script tables Unity put
  into the game's copies (`tools/csharp/monoscripttypes`).
- **Game code (Assembly-CSharp):** unchanged, except that compute shader buffer calls go through `MacPortCompat`.
- **Port options** (render scale): applied at startup by `MacPortRuntime`.
- **Updates:** the app's executable is `tools/launcher` instead of Unity's stub. Before starting Unity's player, it
  compares the game folder with the file list recorded in the app (sizes and dates only, so macOS doesn't ask for
  permission) and runs `./build.sh` if the game changed, with a progress panel. `--check` only reports whether it
  changed: `build/Schedule\ I.app/Contents/MacOS/Schedule\ I --check`.

### The build

- `tools/py/build.py` runs the steps. Its downloads are pinned and checksummed.
- Each step is skipped when its inputs haven't changed. `work/metal-cache` remembers which shaders the Metal
  compiler accepted, so after a game update only new shaders are compiled.
- The app is assembled next to the old one and only replaces it when complete, so a build that fails or is stopped
  leaves the old app working. Builds of the same folder wait for each other.
- `./build.sh --help` lists the options: `--game`, `--out`, `--development`, `--no-burst`, `--force`, `--bench`.
- Conversion logs are in `work/patched/`.
- Only the port's source is tracked. `deps/`, `build/` and `work/` are generated, apart from the project files in
  `work/build/`.

### Benchmark

```sh
./build.sh --bench
deps/venv/bin/python -I tools/py/bench.py --label test
```

It plays a copy of your newest save in a window and adds the frame times to `work/bench/results.jsonl`.

### Limitations

- Shader passes that need geometry or tessellation shaders are dropped, as Metal has neither (the tessellated
  water falls back to the normal one).
- Converted shaders use full float precision.
- Tested on an M3 Max. Not tested yet: multiplayer, gamepads.

## License

The port's code is MIT-licensed (`LICENSE`); `tools/hlslcc` keeps its own MIT and BSD licenses. Schedule I
belongs to TVGS. Unity, .NET and the packages `build.sh` downloads belong to their owners and aren't included
here.
