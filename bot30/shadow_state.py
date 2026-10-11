"""Bounded committed quote journal; recovery recreates business state, never unpickles it."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Callable

MAX_RECORDS = 100000
MAX_BYTES = 128 * 1024 * 1024
MAX_RECORD_BYTES = 4096
ZERO = '0' * 64
HEX = re.compile(r'^[0-9a-f]{64}$')


class JournalError(RuntimeError):
    pass


def _pairs(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise JournalError('duplicate_json_key')
        d[k] = v
    return d


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise JournalError('nonfinite_json')
    if isinstance(value, dict):
        for v in value.values(): _finite(v)
    if isinstance(value, list):
        for v in value: _finite(v)


def canonical(value):
    _finite(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def parse(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda v: (_ for _ in ()).throw(JournalError('nonfinite_json')))
    except (ValueError, UnicodeError) as exc:
        raise JournalError('invalid_json') from exc
    _finite(value)
    return value


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _validate_identity(value):
    if not isinstance(value, dict) or not value or len(canonical(value)) > 1024:
        raise JournalError('invalid_run_identity')


def _atomic(path, value, *, recovered_tail=False):
    raw = canonical(value) + b'\n'
    temp = path.with_name(path.name + '.new')
    if temp.is_symlink():
        raise JournalError('unfinished_atomic_write_preserved')
    if temp.exists():
        if not recovered_tail:
            raise JournalError('unfinished_atomic_write_preserved')
        with temp.open('rb') as stream:
            pending=stream.read(MAX_RECORD_BYTES+1)
        if len(pending)>MAX_RECORD_BYTES or not pending.endswith(b'\n') or parse(pending)!=value:
            raise JournalError('unfinished_atomic_write_preserved')
        # Only a complete matching checkpoint is published after fresh full replay.
        os.replace(temp,path)
        if os.name!='nt':
            descriptor=os.open(path.parent,os.O_RDONLY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
        return
    try:
        with temp.open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != 'nt':
            descriptor = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
    except Exception as exc:
        raise JournalError('checkpoint_write_failed') from exc


class StateLock:
    """OS-owned lock survives stale file names and releases on process exit."""
    def __init__(self, directory):
        self.path = Path(directory) / 's30_shadow.lock'
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink(): raise JournalError('lock_symlink')
        self.stream = self.path.open('a+b')
        self.stream.seek(0)
        if not self.stream.read(1): self.stream.write(b'0'); self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close(); self.stream = None
            raise JournalError('state_owner_busy') from exc
        return self

    def __exit__(self, *_):
        if self.stream is not None:
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close(); self.stream = None


class QuoteJournal:
    def __init__(self, directory, identity):
        _validate_identity(identity)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'quotes_v001.jsonl'
        self.checkpoint = self.directory / 'checkpoint_v001.json'
        self.identity = identity
        self.count = 0
        self.bytes = 0
        self.chain = ZERO
        self.business_digest = None
        self.activation_ms = None
        self.failed = False
        self.recovered = False
        for p in (self.directory, self.path, self.checkpoint):
            if p.is_symlink(): raise JournalError('state_symlink')

    def recover(self, apply: Callable[[dict], str]):
        if self.failed: raise JournalError('terminal_journal_requires_fresh_instance')
        if self.recovered: raise JournalError('recovery_already_performed')
        try:
            saved = None
            if self.checkpoint.exists():
                with self.checkpoint.open('rb') as stream:
                    raw = stream.read(MAX_RECORD_BYTES + 1)
                if len(raw) > MAX_RECORD_BYTES: raise JournalError('checkpoint_oversize')
                saved = parse(raw)
                if set(saved) != {'schema','identity','count','bytes','chain','business_digest','activation_ms'} or saved['schema'] != 'bot30_shadow_checkpoint_v001' or saved['identity'] != self.identity:
                    raise JournalError('checkpoint_identity_or_schema')
                if type(saved['count']) is not int or saved['count'] < 1:
                    raise JournalError('checkpoint_count')
                if (type(saved['bytes']) is not int or saved['bytes']<1
                    or type(saved['activation_ms']) is not int or saved['activation_ms']<=0
                    or not isinstance(saved['chain'],str) or not HEX.fullmatch(saved['chain'])
                    or not isinstance(saved['business_digest'],str) or not HEX.fullmatch(saved['business_digest'])):
                    raise JournalError('checkpoint_field_types')
            if not self.path.exists():
                if saved: raise JournalError('checkpoint_without_journal')
                if self.checkpoint.with_name(self.checkpoint.name+'.new').exists():
                    raise JournalError('unfinished_atomic_write_preserved')
                self.recovered = True; return
            saw_checkpoint = saved is None
            with self.path.open('rb') as stream:
                while True:
                    raw = stream.readline(MAX_RECORD_BYTES + 1)
                    if not raw: break
                    if len(raw)>MAX_RECORD_BYTES or not raw.endswith(b'\n'): raise JournalError('journal_partial_or_oversize')
                    row = parse(raw)
                    if set(row) != {'schema','identity','sequence','previous','quote','business_digest','activation_ms'} or row['schema']!='bot30_shadow_quote_v001' or row['identity']!=self.identity:
                        raise JournalError('journal_schema_or_identity')
                    if type(row['sequence']) is not int or row['sequence']!=self.count+1 or row['previous']!=self.chain:
                        raise JournalError('journal_order')
                    if not isinstance(row['business_digest'],str) or not HEX.fullmatch(row['business_digest']): raise JournalError('business_digest_shape')
                    if self.count==0:
                        if type(row['activation_ms']) is not int or row['activation_ms']<=0: raise JournalError('activation_shape')
                        self.activation_ms=row['activation_ms']
                    if row['activation_ms']!=self.activation_ms: raise JournalError('activation_changed')
                    if self.count>=MAX_RECORDS or self.bytes+len(raw)>MAX_BYTES: raise JournalError('journal_cap')
                    observed = apply(row['quote'])
                    if observed!=row['business_digest']: raise JournalError('restart_business_digest_mismatch')
                    self.count+=1; self.bytes+=len(raw); self.chain=hashlib.sha256(raw).hexdigest(); self.business_digest=observed
                    if saved is not None and self.count==saved['count']:
                        if self._checkpoint_value()!=saved: raise JournalError('checkpoint_prefix_mismatch')
                        saw_checkpoint=True
            if not saw_checkpoint: raise JournalError('checkpoint_prefix_missing')
            # A complete fsynced journal tail is replayed and validated; a partial tail is preserved and rejected.
            if self.count: _atomic(self.checkpoint,self._checkpoint_value(),recovered_tail=True)
            self.recovered=True
        except Exception:
            self.failed=True; raise

    def _checkpoint_value(self):
        return {'schema':'bot30_shadow_checkpoint_v001','identity':self.identity,'count':self.count,'bytes':self.bytes,'chain':self.chain,'business_digest':self.business_digest,'activation_ms':self.activation_ms}

    def _row(self, quote, business_digest, activation_ms):
        return {'schema':'bot30_shadow_quote_v001','identity':self.identity,'sequence':self.count+1,'previous':self.chain,'quote':quote,'business_digest':business_digest,'activation_ms':activation_ms}

    def preflight(self, quote, activation_ms):
        if self.failed or not self.recovered: raise JournalError('journal_not_ready')
        if type(activation_ms) is not int or activation_ms<=0 or not isinstance(quote,dict) or not quote:
            self.failed=True;raise JournalError('quote_or_activation_shape')
        raw=canonical(self._row(quote,ZERO,activation_ms))+b'\n'
        if len(raw)>MAX_RECORD_BYTES or self.count>=MAX_RECORDS or self.bytes+len(raw)>MAX_BYTES:
            self.failed=True; raise JournalError('journal_cap_preserve_history')

    def commit(self, quote, business_digest, activation_ms):
        self.preflight(quote,activation_ms)
        if not isinstance(business_digest,str) or not HEX.fullmatch(business_digest):
            self.failed=True; raise JournalError('business_digest_shape')
        if self.activation_ms is not None and activation_ms!=self.activation_ms:
            self.failed=True; raise JournalError('activation_changed')
        raw=canonical(self._row(quote,business_digest,activation_ms))+b'\n'
        try:
            with self.path.open('ab') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            self.count+=1; self.bytes+=len(raw); self.chain=hashlib.sha256(raw).hexdigest(); self.business_digest=business_digest; self.activation_ms=activation_ms
            _atomic(self.checkpoint,self._checkpoint_value())
        except Exception:
            self.failed=True; raise
