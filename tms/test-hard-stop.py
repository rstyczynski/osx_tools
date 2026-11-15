import contextlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location('tms_service', root / 'tms-service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
with tempfile.TemporaryDirectory(prefix='tms-hardstop-live-', dir='/tmp') as temporary:
    base = Path(temporary).resolve()
    source = base / 'source'
    source.mkdir()
    (source / 'file').write_text('fixture data')
    excludes = base / 'excludes'
    excludes.write_text('')
    env = dict(os.environ, TMS_BACKUP_VOLUME=str(base / 'destination'),
               TMS_STATE_DIR=str(base / 'state'), TMS_LOG_DIR=str(base / 'logs'),
               TMS_CONFIG_FILE=str(base / 'config.json'), TMS_EMAIL_ENABLED='0',
               TMS_EXCLUDES_FILE=str(excludes))
    env['BASH_FUNC_rsync%%'] = '() { sleep 120; }'
    sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)
    runner = None
    groups = set()
    try:
        with (base / 'runner-output').open('w') as output:
            runner = subprocess.Popen([sys.executable, '-B', str(root / 'tms-service.py'),
                                       'run', '--source', str(source)], env=env,
                                      stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if runner.poll() is not None:
                    raise AssertionError((base / 'runner-output').read_text())
                table = service.process_table()
                groups = {pid for pid, info in table.items() if info['parent'] == runner.pid and info['group'] == pid}
                if groups and 'Copying full backup' in (base / 'runner-output').read_text():
                    break
                time.sleep(.05)
            else:
                raise AssertionError('Fixture backup did not enter the blocked transfer')
            config = {'backup_volume': str(base / 'destination'), 'state_dir': base / 'state', 'log_dir': base / 'logs'}
            with patch.object(service, 'configuration', return_value=config):
                assert service.hard_stop(runner.pid) == 0
            assert runner.wait(timeout=5) == -signal.SIGKILL
            assert sentinel.poll() is None, 'Unrelated process was stopped'
            assert not service.backup_active(config), 'Backup lock still held'
            state = json.loads((base / 'state/status.json').read_text())
            assert state['status'] == 'STOPPED'
            assert Path(state['summary']).is_file()
            assert list((base / 'destination/backup/source').glob('*.full')), 'Working snapshot removed'
            print('PASS: real fixture runner and transfer group hard-stopped; unrelated process alive; lock released; snapshot retained')
    finally:
        for group in groups:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
        if runner and runner.poll() is None:
            runner.kill()
            runner.wait(timeout=5)
        sentinel.kill()
        sentinel.wait(timeout=5)
