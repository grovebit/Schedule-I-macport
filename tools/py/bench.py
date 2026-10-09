"""Benchmarks the port: runs "Schedule I Bench.app" (assemble_app.py --bench) windowed in the background on a
copy of a save, waits for it to finish and prints the MacPortBench results.

The bench app has its own bundle id, so its PlayerPrefs and save folder are separate from the real game; the
user's graphics settings are copied over (display forced to windowed) and the save is copied fresh every run.

usage: python -I bench.py [--label NAME] [--res 1920x1200] [--save SAVE_DIR] [-- extra player args]
       e.g. --label no-burst -- --burst-disable-compilation
"""
import argparse
import glob
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = ROOT + '/build/Schedule I Bench.app'
WORK = ROOT + '/work/bench'
USER_DOMAIN = 'com.TVGS.ScheduleI'
BENCH_DOMAIN = USER_DOMAIN + '.bench'
USER_SAVES = os.path.expanduser('~/Library/Application Support/%s/Saves' % USER_DOMAIN)
TAG = '[MacPortBench] '


def newest_save():
    saves = glob.glob(os.path.join(USER_SAVES, '*', 'SaveGame_*'))
    if not saves:
        sys.exit('no save found in ' + USER_SAVES)
    return max(saves, key=os.path.getmtime)


def prepare_prefs(w, h):
    """Copies the user's game settings to the bench domain, windowed at w x h."""
    out = subprocess.run(['defaults', 'export', USER_DOMAIN, '-'], capture_output=True).stdout
    prefs = plistlib.loads(out) if out else {}
    prefs = {k: v for k, v in prefs.items() if not k.startswith('Screenmanager')}
    prefs.update({'DisplayMode': 0, 'Screenmanager Fullscreen mode': 3,
                  'Screenmanager Resolution Width': w, 'Screenmanager Resolution Height': h})
    path = os.path.join(WORK, 'prefs.plist')
    with open(path, 'wb') as f:
        plistlib.dump(prefs, f)
    subprocess.run(['defaults', 'delete', BENCH_DOMAIN], capture_output=True)
    subprocess.run(['defaults', 'import', BENCH_DOMAIN, path], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--label', default='run')
    ap.add_argument('--app', default=APP, help='bench app to run (e.g. a --development build for -macport-bench-profile)')
    ap.add_argument('--res', default='1920x1200')
    ap.add_argument('--save', default=None)
    ap.add_argument('--timeout', type=int, default=900)
    ap.add_argument('--cold', action='store_true', help="clear the bench app's Metal shader cache first (first-run behaviour)")
    ap.add_argument('extra', nargs='*')
    args = ap.parse_args()

    if subprocess.run(['pgrep', '-f', 'Schedule I.app/Contents/MacOS'], capture_output=True).returncode == 0:
        sys.exit('the game is running - not benchmarking while it is in use')
    if not os.path.isdir(args.app):
        sys.exit('missing %s - build it with assemble_app.py --bench' % args.app)
    os.makedirs(WORK, exist_ok=True)
    w, h = (int(x) for x in args.res.split('x'))

    src = args.save or newest_save()
    save = os.path.join(WORK, 'save', os.path.basename(src))
    shutil.rmtree(os.path.dirname(save), ignore_errors=True)
    shutil.copytree(src, save)
    prepare_prefs(w, h)
    if args.cold:
        cache = subprocess.run(['getconf', 'DARWIN_USER_CACHE_DIR'], capture_output=True, text=True).stdout.strip()
        for d in ('com.apple.metal', 'com.apple.metalfe', 'com.apple.gpuarchiver'):
            shutil.rmtree(os.path.join(cache, BENCH_DOMAIN, d), ignore_errors=True)

    log = os.path.join(WORK, 'logs', '%s-%s.log' % (time.strftime('%Y%m%d-%H%M%S'), args.label))
    os.makedirs(os.path.dirname(log), exist_ok=True)
    cmd = ['open', '-n', '-g', '-W', args.app, '--args', '-logFile', log, '-screen-fullscreen', '0',
           '-screen-width', str(w), '-screen-height', str(h), '-macport-bench', '-macport-bench-save', save,
           '-macport-bench-res', args.res] + args.extra
    print('benchmark %s: %s at %s (log %s)' % (args.label, os.path.basename(src), args.res, log), flush=True)
    t0 = time.time()
    try:
        subprocess.run(cmd, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(['pkill', '-f', 'Schedule I Bench.app/Contents/MacOS'])
        print('TIMEOUT after %ds' % args.timeout)

    results = []
    with open(log, errors='replace') as f:
        for line in f:
            if TAG not in line:
                continue
            msg = line.split(TAG, 1)[1].strip()
            if msg.startswith('RESULT '):
                r = json.loads(msg[7:])
                r.update(label=args.label, time=time.strftime('%Y-%m-%d %H:%M:%S'))
                results.append(r)
            elif msg.startswith('PROFILE'):
                print('  ' + msg[8:])
            elif not msg.startswith('resolutions'):
                print('  ' + msg)
    print('  finished in %.0fs' % (time.time() - t0))
    for r in results:
        print('  %-7s %6.1f fps  avg %6.2f  p95 %6.2f  p99 %6.2f  max %7.2f ms  >50ms %3d  >100ms %3d | cpu main %6.2f  render %6.2f  gpu %6.2f ms | %dx%d' % (
            r['phase'], r['fps'], r['ms_avg'], r['ms_p95'], r['ms_p99'], r['ms_max'], r.get('hitches_50', -1), r.get('hitches_100', -1),
            r['cpu_main_ms'], r['cpu_render_ms'], r['gpu_ms'], r['w'], r['h']))
    with open(os.path.join(WORK, 'results.jsonl'), 'a') as f:
        for r in results:
            f.write(json.dumps(r) + '\n')


if __name__ == '__main__':
    main()
