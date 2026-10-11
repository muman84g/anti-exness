"""No-order sampled-current Bid/Ask entrypoint. Credentials/legacy state are never read."""
from __future__ import annotations

import argparse
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path, PurePosixPath
import signal
import time
import re

from shadow_state import JournalError, QuoteJournal, StateLock, digest, parse, canonical
from shadow_bridge import (NonsecretBinding, ShadowReader, FileBridge, BridgeError,
                           clock_evidence)

ROOT=Path(__file__).resolve().parent
CONFIG_KEYS={'schema','mode','symbol','magic','poll_ms','max_rss_bytes','rss_observation_ms','state_subdirectory','binding'}
RUNTIME_PATHS={
    'bot30_shadow.py','shadow_state.py','shadow_bridge.py','portable_factory.py',
    'portable_control.py','stream_harness.py','source_bindings.json',
    'clock_contract_v002.json','BotBridge_s30_readonly.mq5',
    'vendor_runtime/research/work/improvement_v001/replay/research_runner_budget3600_v001.py',
    'vendor_runtime/research/work/discovery_v005/run_main_budget3600_v001.py',
    'vendor_runtime/backtest/code/research_entry_close.py',
    'vendor_runtime/backtest/candidate-config/variants_v001.json',
    'vendor_runtime/diagnostics/p2_priority_v001/research_hbe_p2_priority_v001.py',
    'vendor_runtime/diagnostics/structure_identification_v001/research_h_be_hedge_scope_v001.py',
    'vendor_runtime/diagnostics/mechanism_selection_v001/research_p2_count_v001.py',
    'vendor_runtime/backtest/code/native_v006/h2_engine.py',
    'vendor_runtime/backtest/code/native_v006/lifecycle_market_adapter.py',
    'vendor_runtime/backtest/code/native_v006/a3_lifecycle_boundary.py',
    'vendor_runtime/support/quote_connection.py',
    'vendor_runtime/support/r3_memory_adapter_v001.py',
    'vendor_runtime/support/quantity_rules_v004.py',
}


def load_config(path):
    if Path(path).name!='shadow_v001.json':raise ValueError('nonsecret_shadow_config_name_required')
    target=Path(path).absolute()
    for component in (target,*target.parents):
        if component.is_symlink() or (hasattr(component,'is_junction') and component.is_junction()):
            raise ValueError('nonsecret_shadow_config_reparse')
    with Path(path).open('rb') as stream: raw=stream.read(16385)
    if len(raw)>16384: raise ValueError('config_oversize')
    cfg=parse(raw)
    if (type(cfg) is not dict or set(cfg)!=CONFIG_KEYS
        or cfg['schema']!='bot30_sampled_shadow_config_v001'
        or cfg['mode']!='SAMPLED_CURRENT_BID_ASK_SHADOW'
        or cfg['poll_ms']!=250 or type(cfg['poll_ms']) is not int
        or cfg['state_subdirectory']!='s30_shadow'
        or cfg['symbol']!='XAUUSD' or type(cfg['symbol']) is not str
        or type(cfg['magic']) is not int or cfg['magic']!=30
        or type(cfg['max_rss_bytes']) is not int or cfg['max_rss_bytes']!=268435456
        or type(cfg['rss_observation_ms']) is not int or cfg['rss_observation_ms']!=1000):
        raise ValueError('config_schema_or_fixed_mode')
    payload=cfg['binding']
    fields={'binding_id','expected_company','expected_server','ea_source_sha256','time_basis','timezone','dst_conversion','clock_evidence_refs'}
    if type(payload) is not dict or set(payload)!=fields: raise ValueError('binding_schema')
    binding=NonsecretBinding(**dict(payload,clock_evidence_refs=tuple(payload['clock_evidence_refs'])))
    ea=ROOT/'BotBridge_s30_readonly.mq5'
    if hashlib.sha256(ea.read_bytes()).hexdigest()!=binding.ea_source_sha256:
        raise ValueError('ea_source_binding_mismatch')
    return cfg,binding,hashlib.sha256(raw).hexdigest()


def verify_package():
    path=ROOT/'runtime_manifest_v001.json'
    with path.open('rb') as stream:raw=stream.read(131073)
    if len(raw)>131072:raise ValueError('runtime_manifest_oversize')
    manifest=parse(raw)
    if type(manifest) is not dict or set(manifest)!={'schema','files'} or manifest['schema']!='bot30_shadow_runtime_manifest_v001':
        raise ValueError('runtime_manifest_schema')
    if type(manifest['files']) is not list or len(manifest['files'])!=len(RUNTIME_PATHS):
        raise ValueError('runtime_manifest_closure_count')
    seen=set()
    # Validate the entire closed path set before any source stat/read, including forbidden paths.
    for item in manifest['files']:
        if type(item) is not dict or set(item)!={'path','sha256','bytes'}:raise ValueError('manifest_item_schema')
        name=item['path']
        if type(name) is not str or name not in RUNTIME_PATHS:raise ValueError('runtime_manifest_unknown_path')
        if (type(item['bytes']) is not int or not 0<item['bytes']<=262144
            or type(item['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',item['sha256'])):
            raise ValueError('manifest_identity_shape')
        rel=PurePosixPath(name)
        if rel.is_absolute() or '..' in rel.parts or '\\' in name or name in seen:raise ValueError('manifest_path')
        seen.add(name)
    if seen!=RUNTIME_PATHS:raise ValueError('runtime_manifest_closure_missing')
    for item in manifest['files']:
        rel=PurePosixPath(item['path']);target=ROOT.joinpath(*rel.parts)
        current=ROOT
        for part in rel.parts:
            current=current/part
            if current.is_symlink() or (hasattr(current,'is_junction') and current.is_junction()):raise ValueError('manifest_reparse')
        with target.open('rb') as stream:data=stream.read(item['bytes']+1)
        if type(item['bytes']) is not int or len(data)!=item['bytes'] or hashlib.sha256(data).hexdigest()!=item['sha256']:
            raise ValueError('runtime_source_identity_mismatch')
    return hashlib.sha256(raw).hexdigest()


def _logger(directory):
    directory.mkdir(parents=True,exist_ok=True)
    logger=logging.getLogger('bot30_shadow');logger.setLevel(logging.INFO)
    handler=RotatingFileHandler(directory/'shadow_v001.log',maxBytes=1048576,backupCount=3,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'));logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    return logger


def run(args):
    source_sha=verify_package();cfg,binding,cfg_sha=load_config(args.config)
    state=Path(args.state_dir).absolute()
    logs=Path(args.log_dir).absolute()
    if state.name!='s30_shadow' or logs.name!='s30_shadow' or state==logs:
        raise ValueError('dedicated_shadow_directory_required')
    for target in (state,logs):
        for part in (target,*target.parents):
            if part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()):raise ValueError('runtime_directory_reparse')
    identity={'source_sha256':source_sha,'config_sha256':cfg_sha,'binding_sha256':binding.binding_sha256}
    run_id='bot30_cent_os_shadow_control_'+digest(identity)[:24]
    identity['run_id']=run_id
    logger=_logger(logs)
    # No native module import until source package and nonsecret configuration validate.
    from portable_control import PortableControl
    import psutil
    process=psutil.Process(os.getpid())
    stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,stop)
    control=None
    with StateLock(state):
        terminal=state/'terminal_v001.json'
        if terminal.exists():raise JournalError('preserved_terminal_requires_operator_recovery')
        journal=QuoteJournal(state,identity)
        control=PortableControl(journal=journal,run_identity=run_id,binding_config=binding)
        try:
            control.recover()
            reader=ShadowReader(FileBridge(args.bridge_dir),symbol=cfg['symbol'],magic=cfg['magic'],binding=binding)
            observation=time.monotonic();next_status=observation;next_error=observation
            baseline_rss=process.memory_info().rss
            logger.info('shadow_start sampled_current no_order committed_quotes=%d baseline_rss=%d',journal.count,baseline_rss)
            while not stopping:
                start=time.monotonic()
                if start>=observation:
                    rss=process.memory_info().rss
                    if rss>cfg['max_rss_bytes']:raise JournalError('rss_budget_stop_preserve_state')
                    observation=start+cfg['rss_observation_ms']/1000
                try:
                    info=reader.sampled_quote()
                    if info is not None:
                        clocks=clock_evidence(info,time.time_ns()//1000000).asdict()
                        accepted=control.on_quote(info,clocks,info.verified_binding)
                        if not accepted and start>=next_error:
                            logger.info('not_evaluated reason=control_prequote_rejection committed_quotes=%d',journal.count)
                            next_error=start+30
                    if start>=next_status:
                        logger.info('shadow_sampled committed_quotes=%d broker_mutations=0',journal.count)
                        next_status=start+60
                except BridgeError as exc:
                    # Transport/binding rejection happens before native consume or journal advance.
                    if start>=next_error:
                        logger.info('not_evaluated reason=%s committed_quotes=%d',type(exc).__name__,journal.count)
                        next_error=start+30
                delay=cfg['poll_ms']/1000-(time.monotonic()-start)
                if delay>0:time.sleep(delay)
        except Exception as exc:
            # Persist cause type only, without broker snapshots or sensitive exception payloads.
            value={'schema':'bot30_shadow_terminal_v001','identity':identity,'committed_quotes':journal.count,'reason':type(exc).__name__,'observed_utc_ms':time.time_ns()//1000000}
            with terminal.open('xb') as stream:
                stream.write(canonical(value)+b'\n');stream.flush();os.fsync(stream.fileno())
            logger.error('terminal_preserved reason=%s committed_quotes=%d',type(exc).__name__,journal.count)
            raise
        finally:
            if control is not None:control.close()
    return 0


def main():
    parser=argparse.ArgumentParser(description='bot30 no-order sampled-current shadow')
    parser.add_argument('--config',default=os.environ.get('BOT30_CONFIG',str(ROOT/'config/shadow_v001.json')))
    parser.add_argument('--state-dir',default=os.environ.get('BOT30_STATE_DIR',str(ROOT/'state/s30_shadow')))
    parser.add_argument('--log-dir',default=os.environ.get('BOT30_LOG_DIR',str(ROOT/'logs/s30_shadow')))
    parser.add_argument('--bridge-dir',default=os.environ.get('BOT30_BRIDGE_DIR','/root/.wine/drive_c/Program Files/MetaTrader 5/MQL5/Files'))
    parser.add_argument('--verify-package',action='store_true',help='Check shipped nonsecret bytes only; no IPC/native state.')
    args=parser.parse_args()
    if args.verify_package:
        print(verify_package());return 0
    return run(args)


if __name__=='__main__':raise SystemExit(main())
