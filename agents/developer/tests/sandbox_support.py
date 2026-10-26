"""Shared fixture helpers for explicit OS integration tests only."""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path


def require_sandbox():
    """Skip unavailable OS integration unless the caller requires it explicitly."""
    reason = None
    binary = shutil.which("bwrap")
    if not binary:
        reason = "Bubblewrap is not installed"
    elif os.getuid() == 0:
        reason = "Sandbox integration must run as an unprivileged host user"
    else:
        command = [binary, "--unshare-all", "--unshare-user", "--disable-userns",
                   "--die-with-parent", "--new-session", "--uid", "65534", "--gid", "65534",
                   "--cap-drop", "ALL", "--clearenv"]
        for name in ("/usr", "/lib", "/lib64"):
            if Path(name).exists():
                command += ["--ro-bind", name, name]
        command += ["/usr/bin/true"]
        try:
            probe = subprocess.run(command, capture_output=True, timeout=10,
                                   env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        except (OSError, subprocess.TimeoutExpired):
            reason = "Cannot start Bubblewrap namespace probe"
        else:
            if probe.returncode:
                reason = "Kernel/supervisor denies Bubblewrap namespaces: " + probe.stderr.decode("utf-8", "replace")[:1000]
    if reason:
        if os.environ.get("CODER_REQUIRE_SANDBOX") == "1":
            raise AssertionError(reason)
        raise unittest.SkipTest(reason)


FAKE_OPENCODE = '''#!/usr/bin/python3
import http.client,json,os,pathlib,socket,subprocess,sys,time
if sys.argv[1:] == ['--version']:
    print('fake-opencode-1.0')
    raise SystemExit(0)
if sys.argv[1:] == ['run','--help']:
    print('opencode run --format --model --agent')
    raise SystemExit(0)
assert sys.argv[1:8] == ['run','--format','json','--model','gateway/local-model','--agent','coder']
assert sys.argv[8] == '--'
task=json.loads(sys.argv[9].split('Validated task JSON:\\n',1)[1])
assert os.getuid() == 65534
assert 'TEST_HOST_SECRET' not in os.environ
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/workspace/.git').exists()
assert not pathlib.Path('/workspace/.env').exists()
assert not pathlib.Path('/home/adam/.ssh').exists()
assert not pathlib.Path('/usr/bin/git').exists()
assert not pathlib.Path('/bin/sh').exists()
print(json.dumps({'type':'sandbox_probe','uid':os.getuid(),'netns':os.readlink('/proc/self/ns/net')}))
for target in ['/escape','/workspace/undeclared.py','/control/opencode.json']:
    try:
        pathlib.Path(target).write_text('bad')
    except OSError:
        pass
    else:
        raise AssertionError('write was allowed: '+target)
if pathlib.Path('/workspace/readme.txt').exists():
    try:
        pathlib.Path('/workspace/readme.txt').write_text('bad')
    except OSError:
        pass
    else:
        raise AssertionError('read-only context was writable')
with socket.socket() as s:
    s.settimeout(.2)
    try:
        s.connect(('192.0.2.1',443))
    except OSError:
        pass
    else:
        raise AssertionError('outbound network available')
if task['task_id'] == 'timeout':
    subprocess.Popen(['/usr/bin/python3','-c',
        "import pathlib,time; time.sleep(3); pathlib.Path('/workspace/src/app.py').write_text('SURVIVED')"],
        start_new_session=True)
    time.sleep(10)
if task['task_id'] == 'crash':
    raise SystemExit(9)
if task['task_id'].startswith('negative-'):
    allowed = pathlib.Path('/workspace/src/app.py')
    context = pathlib.Path('/workspace/readme.txt')
    control = pathlib.Path('/control/opencode.json')
    assert allowed.read_text() == 'VALUE = 1\\n'
    assert context.read_text() == 'Read only context\\n'
    original_config = control.read_bytes()
    kind = task['task_id'].removeprefix('negative-')

    def denied(name, operation):
        try:
            operation()
        except OSError as error:
            print(json.dumps({'type':'negative_probe','operation':name,'errno':error.errno}))
        else:
            raise AssertionError('OS allowed forbidden operation: '+name)

    if kind == 'atomic-tmp':
        replacement = pathlib.Path('/tmp/replacement')
        replacement.write_text('ATTACK')
        denied('replace allowed mount from tmp', lambda: os.replace(replacement,allowed))
        denied('replace read-only context from tmp', lambda: os.replace(replacement,context))
        denied('replace control config from tmp', lambda: os.replace(replacement,control))
    elif kind == 'atomic-neighbor':
        replacement = pathlib.Path('/workspace/src/app.py.tmp')
        denied('create neighboring temporary file', lambda: replacement.write_text('ATTACK'))
        denied('rename mount to neighboring path', lambda: os.replace(allowed,replacement))
    elif kind == 'symlink':
        denied('unlink allowed mount', allowed.unlink)
        denied('symlink inside workspace', lambda: os.symlink('/tmp/replacement','/workspace/src/injected.py'))
        # Creating aliases in private /tmp is allowed; following one cannot make
        # a read-only source or control mount writable.
        alias = pathlib.Path('/tmp/context-alias')
        alias.symlink_to(context)
        denied('write read-only context via symlink', lambda: alias.write_text('ATTACK'))
        alias.unlink()
        alias.symlink_to(control)
        denied('write control via symlink', lambda: alias.write_text('ATTACK'))
    elif kind == 'hardlink':
        alias = pathlib.Path('/tmp/context-hardlink')
        try:
            os.link(context,alias)
        except OSError as error:
            print(json.dumps({'type':'negative_probe','operation':'hardlink context to tmp','errno':error.errno}))
        else:
            # The link alone is not an escape if its target remains read-only.
            denied('write context via hardlink', lambda: alias.write_text('ATTACK'))
            alias.unlink()
        denied('hardlink inside workspace', lambda: os.link(allowed,'/workspace/src/injected.py'))
    elif kind == 'read-only':
        for name,target in [('context',context),('control',control)]:
            denied('truncate '+name, lambda target=target: os.truncate(target,0))
            denied('chmod '+name, lambda target=target: target.chmod(0o777))
        denied('write source-shaped undeclared path', lambda: pathlib.Path('/workspace/src/other.py').write_text('ATTACK'))
    elif kind == 'host-canary':
        canary = pathlib.Path(next(value.removeprefix('HOST_CANARY=') for value in task['acceptance_criteria'] if value.startswith('HOST_CANARY=')))
        denied('read host canary directly', canary.read_bytes)
        alias = pathlib.Path('/tmp/secret-alias')
        alias.symlink_to(canary)
        denied('read host canary via private symlink', alias.read_bytes)
        denied('read host root through private proc', lambda: pathlib.Path('/proc/1/root',str(canary).lstrip('/')).read_bytes())
    else:
        raise AssertionError('Unknown negative fixture')
    assert allowed.read_text() == 'VALUE = 1\\n'
    assert context.read_text() == 'Read only context\\n'
    assert control.read_bytes() == original_config
if task['task_id'] == 'channel':
    connection = http.client.HTTPConnection('127.0.0.1',8765,timeout=3)
    connection.request('POST','/v1/chat/completions',json.dumps({'model':'local-model'}))
    response = connection.getresponse()
    assert response.status == 200 and response.read() == b'{"ok":true}'
    connection.close()
if task['task_id'] == 'clarify':
    reply={'status':'NEEDS_CLARIFICATION','message':'Need a config decision','required_files':['config.json']}
else:
    pathlib.Path('/workspace/src/app.py').write_text('VALUE = 2\\n')
    if 'new.py' in task['allowed_write_files']:
        pathlib.Path('/workspace/new.py').write_text('NEW = 1\\n')
    reply={'status':'COMPLETED','message':'Prescribed edit finished','required_files':[]}
print(json.dumps({'type':'text','part':{'text':json.dumps(reply)}}))
'''
