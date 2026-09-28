#!/usr/bin/env python3
"""Maintainer helper: pin installed runtime closure and official PyPI wheel hashes.

Run only in the reviewed development environment with packaging installed.
This does not install/upgrade anything or read user credentials.
"""
import concurrent.futures
import importlib.metadata as md
import json
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from pathlib import Path
import urllib.request


def lock():
    pending = [('mcp',frozenset()),('httpx',frozenset()),('jsonschema',frozenset())]
    visited, versions = set(), {}
    while pending:
        raw_name, extras = pending.pop()
        name = canonicalize_name(raw_name)
        if (name,extras) in visited:
            continue
        visited.add((name,extras))
        dist = md.distribution(name)
        versions[name] = dist.version
        for raw in dist.requires or []:
            req = Requirement(raw)
            contexts = extras | {''}
            if req.marker is None or any(req.marker.evaluate({'extra':extra}) for extra in contexts):
                pending.append((req.name,frozenset(req.extras)))
    def line(item):
        name, version = item
        req = urllib.request.Request(f'https://pypi.org/pypi/{name}/{version}/json', headers={'User-Agent':'hermes-video-kb-release/2.0'})
        with urllib.request.urlopen(req, timeout=30) as response:
            data=json.load(response)
        hashes=sorted({entry['digests']['sha256'] for entry in data['urls'] if entry['packagetype']=='bdist_wheel' and not entry.get('yanked')})
        if not hashes:
            raise RuntimeError(f'No supported wheel: {name}')
        return f'{name}=={version} \\\n' + ' \\\n'.join(f'    --hash=sha256:{value}' for value in hashes)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        lines=list(pool.map(line,sorted(versions.items())))
    target=Path(__file__).resolve().parent.parent / 'requirements.lock'
    target.write_text('# Fixed runtime dependencies; wheel hashes from official PyPI. macOS/Linux, Python 3.11+.\n'+'\n'.join(lines)+'\n')
    print(f'Locked {len(versions)} runtime packages with wheel hashes; testing tools excluded.')


if __name__ == '__main__':
    lock()
