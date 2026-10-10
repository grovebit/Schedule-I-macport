"""Converts the shaders and compute shaders of Unity serialized files from D3D11 to Metal and writes patched copies.

A pool of worker processes translates the vertex/fragment subprograms and, with --validate, compiles each distinct
MSL source once with the system Metal compiler, in batches (variants that fail are left out). The results are kept in
work/metal-cache (per macOS build and mtlcheck), so later runs only compile sources they haven't seen. The files go
through the pool one after another, overlapped: while the main process assembles and writes one file, the workers
already translate the next.

usage: python -I port_shaders.py <data folder> <output folder> <file>... [--validate] [--only NAME_SUBSTR] [--jobs N]
Writes <output folder>/<file>, and next to it a log (.log) and a report (.json, plus .json.metalfail.txt for sources
the Metal compiler rejected) named after the file ('/' replaced by '_').
"""
import argparse
import hashlib
import json
import multiprocessing as mp
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import UnityPy  # noqa: E402

import paths  # noqa: E402
from compute import convert_compute_shader  # noqa: E402
from convert import ConvertError, SubprogramConverter  # noqa: E402
from patch_shaders import (PLATFORM_D3D11, VALIDATE_BATCH, convert_item, convert_shader, d3d11_segments,  # noqa: E402
                           msl_hash, shader_items, validate_batch, worker_init)


def cache_path():
    """File of earlier validation results, which are only valid for this macOS build's Metal compiler and mtlcheck."""
    build = subprocess.run(['sw_vers', '-buildVersion'], capture_output=True, text=True).stdout.strip()
    with open(os.path.join(paths.TOOLS, 'mtlcheck', 'libmtlcheck.dylib'), 'rb') as f:
        lib = hashlib.sha1(f.read()).hexdigest()[:12]
    return os.path.join(paths.WORK, 'metal-cache', '%s-%s.json' % (build, lib))


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.tmp', 'w') as f:
        json.dump(data, f)
    os.replace(path + '.tmp', path)


def first_error(msg):
    return next((line for line in msg.splitlines() if 'error' in line), msg[:200]).strip()[:200]


class SerializedFile:
    """A file being converted."""

    def __init__(self, rel, args):
        self.rel = rel
        self.out = os.path.join(args.output, rel)
        self.base = os.path.join(args.output, rel.replace('/', '_'))  # of the log and report files
        self.env = UnityPy.load(os.path.join(args.data, rel))
        self.shaders = []  # (object, typetree, [(key, stage, shader name)] of the subprograms to convert)
        self.work = []  # what the pool translates: (shader index, key) + the rest of the shader_items() item
        for obj in self.env.objects:
            if obj.type.name != 'Shader':
                continue
            tt = obj.read_typetree()
            if args.only and args.only not in tt['m_ParsedForm']['m_Name']:
                continue
            if PLATFORM_D3D11 in tt['platforms']:
                items = shader_items(tt, *d3d11_segments(tt))
                self.work += [((len(self.shaders), item[0]),) + item[1:] for item in items]
                self.shaders.append((obj, tt, [(item[0], item[3], item[6]) for item in items]))
        self.translating = self.validating = None  # the pool's pending results
        self.translated = {}  # (shader index, key) -> ((metal subprogram, metal params, MSL hash) | None, error)
        self.sources = {}  # MSL hash -> (stage, MSL), for the sources not known to compile
        self.log, self.computes, self.failed_kernels = [], 0, 0


class Converter:
    def __init__(self, args):
        self.args = args
        self.cache = cache_path() if args.validate else None
        self.verdicts = load_json(self.cache) if self.cache else {}  # MSL hash -> '' if it compiles, else the error
        self.known = len(self.verdicts)
        self.conv = SubprogramConverter()
        # (the workers read the verdicts themselves: big arguments would make starting each one wait for the last)
        self.pool = mp.get_context('spawn').Pool(args.jobs, initializer=worker_init, initargs=(self.cache,))
        self.threads = ThreadPoolExecutor(8)  # LZ4 releases the GIL, so threads compress the shader blobs in parallel

    def close(self):
        self.pool.terminate()
        self.threads.shutdown()

    def save_verdicts(self):
        if self.cache:
            save_json(self.cache, self.verdicts)

    def start(self, rel):
        """Loads a file and queues the translation of its subprograms; converts its compute shaders meanwhile."""
        f = SerializedFile(rel, self.args)
        f.translating = self.pool.map_async(convert_item, f.work, chunksize=16)
        f.work = None
        known = len(self.verdicts)
        self.convert_computes(f)
        if len(self.verdicts) != known:
            self.save_verdicts()
        return f

    def convert_computes(self, f):
        """Converts the compute shaders of a file. Kernel sources not validated before are taken to compile at first;
        then they are validated together and the shaders using them converted again."""
        todo = {}  # MSL hash -> (MSL hash, stage, MSL) of the sources not validated before

        def convert(obj):
            tt = obj.read_typetree()
            unknown = []

            def validate(src, stage, label):
                h = msl_hash(stage, src)
                if h not in self.verdicts:
                    todo[h] = (h, stage, src)
                    unknown.append(h)
                elif self.verdicts[h]:
                    raise ConvertError('metal: ' + first_error(self.verdicts[h]))
            st = convert_compute_shader(tt, validate if self.cache else None)
            return obj, tt, st, bool(unknown)

        shaders = [convert(obj) for obj in f.env.objects if obj.type.name == 'ComputeShader']
        if todo:
            items = list(todo.values())
            for i in range(0, len(items), VALIDATE_BATCH):
                self.verdicts.update(validate_batch(items[i:i + VALIDATE_BATCH]))
            shaders = [convert(obj) if unknown else (obj, tt, st, unknown) for obj, tt, st, unknown in shaders]
        for obj, tt, st, _ in shaders:
            if st.get('skipped'):
                continue
            obj.save_typetree(tt)
            f.computes += 1
            f.failed_kernels += st['failed']
            f.log.append('%-70s kernels=%-4d failed=%d %s' % (st['name'][:70], st['kernels'], st['failed'],
                                                              list(st['errors'].items())[:1]))

    def collect(self, f):
        """Waits for a file's translated subprograms and queues the validation of the sources not seen before."""
        for key, res, err in f.translating.get():
            f.translated[key] = (res[:3] if res else None), err
            if res and res[3] is not None:
                f.sources.setdefault(res[2], (key[1][2], res[3]))
        f.translating = None
        todo = [(h, stage, src) for h, (stage, src) in f.sources.items() if h not in self.verdicts] if self.cache else []
        if todo:
            batches = [todo[i:i + VALIDATE_BATCH] for i in range(0, len(todo), VALIDATE_BATCH)]
            f.validating = self.pool.map_async(validate_batch, batches, chunksize=1)

    def write(self, f):
        """Assembles a file's shaders once its sources are validated and writes the file, its log and report."""
        if f.validating:
            self.verdicts.update(verdict for batch in f.validating.get() for verdict in batch)
            self.save_verdicts()
        log, report, failures = [], [], []
        for i, (obj, tt, keys) in enumerate(f.shaders):
            results = []
            for ck, stage, name in keys:
                res, err = f.translated[(i, ck)]
                msg = self.verdicts.get(res[2]) if res else None
                fail = None
                if msg:
                    err = 'metal compile failed: ' + first_error(msg)
                    fail = (name, stage, msg, f.sources[res[2]][1])
                    res = None
                results.append((ck, res[:2] if res else None, err, fail))
            st = convert_shader(tt, self.conv, results=results, failures=failures, map_=self.threads.map)
            obj.save_typetree(tt)
            st['path_id'] = obj.path_id
            report.append(st)
            log.append('%-70s conv=%-6d fail=%-4d drop=%-4d entries=%-6d blob=%d' % (
                st['name'][:70], st['converted'], st['failed'], st['dropped_stage'], st.get('entries', 0), st.get('blob', 0)))
            log += ['      %5d x %s' % (n, e) for e, n in st['errors'].items()]
        log += f.log

        data = next(iter(f.env.files.values())).save()
        os.makedirs(os.path.dirname(f.out), exist_ok=True)
        with open(f.out + '.tmp', 'wb') as out:
            out.write(data)
        os.replace(f.out + '.tmp', f.out)
        log.append('wrote %s (%d bytes)' % (f.out, len(data)))
        with open(f.base + '.log', 'w') as out:
            out.write('\n'.join(log) + '\n')
        with open(f.base + '.json', 'w') as out:
            json.dump(report, out, indent=1)
        if failures:
            with open(f.base + '.json.metalfail.txt', 'w') as out:
                for n, stage, msg, src in failures:
                    out.write('##### %s %s\n%s\n----- source\n%s\n\n' % (n, stage, msg, src))
        elif os.path.exists(f.base + '.json.metalfail.txt'):
            os.remove(f.base + '.json.metalfail.txt')

        parts = ['%d shaders (%d variants)' % (len(report), sum(st['converted'] for st in report))] if report else []
        parts += ['%d compute shaders' % f.computes] if f.computes else []
        failed = sum(st['failed'] for st in report) + f.failed_kernels
        print('%s: %s%s' % (f.rel, ', '.join(parts) or 'no shaders',
                            '; %d left out, see %s.log' % (failed, f.base) if failed else ''), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('data', help='folder of the files to convert')
    ap.add_argument('output', help='folder for the converted files')
    ap.add_argument('files', nargs='+', help='files to convert, relative to the data folder')
    ap.add_argument('--validate', action='store_true')
    ap.add_argument('--only', default=None)
    ap.add_argument('--jobs', type=int, default=os.cpu_count() or 4)
    args = ap.parse_args()

    t0 = time.time()
    conv = Converter(args)
    try:
        # The pool works first come, first served: the next file's translations are queued before the current file's
        # validations, so it stays busy while the current file is assembled and written.
        current = conv.start(args.files[0])
        for rel in args.files[1:] + [None]:
            upcoming = conv.start(rel) if rel else None
            conv.collect(current)
            conv.write(current)
            current = upcoming
    finally:
        conv.close()
    checked = ', %d new shader sources checked by the Metal compiler' % (len(conv.verdicts) - conv.known) if args.validate else ''
    print('converted in %d:%02d%s' % (*divmod(round(time.time() - t0), 60), checked))


if __name__ == '__main__':
    mp.freeze_support()
    main()
