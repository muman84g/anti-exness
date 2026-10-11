"""Closed nonsecret package manifest boundary; does not load native modules or IPC."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot30_shadow as entry


class PackageTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.rows=[]
        for name in sorted(entry.RUNTIME_PATHS):
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
            payload=b'fixture-source-only\n';path.write_bytes(payload)
            self.rows.append({'path':name,'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()})
        self.manifest={'schema':'bot30_shadow_runtime_manifest_v001','files':self.rows}

    def publish(self,manifest):
        path=self.root/'runtime_manifest_v001.json'
        path.write_text(json.dumps(manifest),encoding='utf-8')
        return path

    def test_closed_positive_and_byte_change_rejection(self):
        path=self.publish(self.manifest)
        with patch.object(entry,'ROOT',self.root):
            self.assertEqual(entry.verify_package(),hashlib.sha256(path.read_bytes()).hexdigest())
            (self.root/'stream_harness.py').write_bytes(b'different bytes')
            with self.assertRaisesRegex(ValueError,'runtime_source_identity_mismatch'):entry.verify_package()

    def test_forbidden_unknown_path_denied_before_source_access(self):
        bad=json.loads(json.dumps(self.manifest));bad['files'][0]['path']='live_config.py'
        self.publish(bad)
        opened=[];original=Path.open
        def watch(path,*args,**kwargs):
            opened.append(path.name)
            if path.name=='live_config.py':self.fail('protected path reached')
            return original(path,*args,**kwargs)
        with patch.object(entry,'ROOT',self.root),patch.object(Path,'open',watch):
            with self.assertRaisesRegex(ValueError,'runtime_manifest_unknown_path'):entry.verify_package()
        self.assertEqual(opened,['runtime_manifest_v001.json'])

    def test_manifest_bound_and_config_name_guard(self):
        (self.root/'runtime_manifest_v001.json').write_bytes(b'x'*131073)
        with patch.object(entry,'ROOT',self.root):
            with self.assertRaisesRegex(ValueError,'runtime_manifest_oversize'):entry.verify_package()
        with self.assertRaisesRegex(ValueError,'nonsecret_shadow_config_name_required'):
            entry.load_config(self.root/'live_config.py')

    def test_config_reparse_denied_before_open(self):
        target=self.root/'shadow_v001.json'
        with patch.object(Path,'is_symlink',lambda path:path==target),patch.object(Path,'open') as opened:
            with self.assertRaisesRegex(ValueError,'nonsecret_shadow_config_reparse'):
                entry.load_config(target)
            opened.assert_not_called()

    def test_fixed_config_rejection_then_normal_load(self):
        cfg=json.loads((entry.ROOT/'config/shadow_v001.json').read_text(encoding='utf-8'))
        ea=b'nonsecret fixture EA source\n'
        (self.root/'BotBridge_s30_readonly.mq5').write_bytes(ea)
        cfg['binding']['ea_source_sha256']=hashlib.sha256(ea).hexdigest()
        path=self.root/'shadow_v001.json'
        with patch.object(entry,'ROOT',self.root):
            cfg['symbol']='unsupported';path.write_text(json.dumps(cfg),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'config_schema_or_fixed_mode'):entry.load_config(path)
            cfg['symbol']='XAUUSD';path.write_text(json.dumps(cfg),encoding='utf-8')
            loaded,binding,identity=entry.load_config(path)
            self.assertEqual(loaded['symbol'],'XAUUSD')
            self.assertEqual(binding.ea_source_sha256,hashlib.sha256(ea).hexdigest())
            self.assertEqual(identity,hashlib.sha256(path.read_bytes()).hexdigest())


if __name__=='__main__':unittest.main()
