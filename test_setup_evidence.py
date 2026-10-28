import json
import unittest
from setup_evidence import ExactFootprints
from setup_review import observable_sequence, validate_annotation, paper_outcome


def trade(ts, tid, p='100.01', v='0.1', side='Buy'):
    return {'topic':'publicTrade.SOLUSDT', 'data':[dict(T=ts, i=tid, p=p, v=v, S=side)]}


class FootprintTests(unittest.TestCase):
    def test_exact_price_decimal_volume_dedup_and_idle_flush(self):
        f = ExactFootprints(['SOLUSDT'])
        f.process('SOLUSDT', {}, 60_000)
        for i in range(3):
            f.process('SOLUSDT', trade(61_000+i, str(i)), 61_000+i)
        f.process('SOLUSDT', trade(61_001, '1'), 61_004)
        f.process('SOLUSDT', trade(62_000, 's', '100.02', '0.7', 'Sell'), 62_000)
        out=f.process('SOLUSDT', {}, 125_000)
        self.assertEqual(out[0]['buy_volume'], '0.3')
        self.assertEqual(out[0]['sell_volume'], '0.7')
        self.assertEqual(out[0]['trade_count'], 4)
        self.assertTrue(out[0]['complete_observation'])
        self.assertEqual(len(out[0]['levels']), 2)

    def test_startup_gap_late_and_caps_are_never_eligible(self):
        f=ExactFootprints(['SOLUSDT'], level_limit=1, id_limit=1)
        f.process('SOLUSDT', trade(61_000,'a'),61_000)
        f.process('SOLUSDT', trade(62_000,'b','101.1'),62_000)
        r=f.interrupt(63_000,'CONNECTION_GAP')[0]
        self.assertFalse(r['complete_observation'])
        self.assertEqual(set(r['quality_flags']),{'STARTUP_OR_RECOVERY_PARTIAL','LEVEL_CAP','DEDUPE_CAP','CONNECTION_GAP'})
        f.process('SOLUSDT', trade(181_000,'a'),181_000)
        f.process('SOLUSDT',{},245_000)
        late=f.process('SOLUSDT',trade(181_100,'late'),246_000)
        self.assertEqual(late[0]['kind'],'footprint_late_trade')

    def test_every_coin_memory_caps(self):
        f=ExactFootprints([f'C{i}USDT' for i in range(16)],level_limit=32,id_limit=64)
        for s in f.symbols:
            f.process(s, {}, 60_000)
            for i in range(200):
                f.process(s,trade(61_000+i,str(i),str(100+i/100)),61_000+i)
        self.assertLessEqual(f.status()['retained_levels'],16*32)
        self.assertLessEqual(f.status()['retained_trade_ids'],16*64)
        self.assertTrue(all(not r['complete_observation'] for r in f.interrupt(70_000,'TEST_END')))

    def test_invalid_and_future_trades_explicit(self):
        f=ExactFootprints(['SOLUSDT'])
        for p in ['NaN','-1','Infinity']:
            self.assertEqual(f.process('SOLUSDT',trade(1000,p,p),1000)[0]['kind'],'footprint_quality_error')
        self.assertEqual(f.process('SOLUSDT',trade(999_999,'future'),1000)[0]['kind'],'footprint_quality_error')


class ReviewTests(unittest.TestCase):
    def test_profile_requires_full_session_and_late_records_revoke_it(self):
        import tempfile,tarfile,gzip,io
        from pathlib import Path
        from build_profiles import build
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            records=[dict(kind='candle_1m',symbol='SOLUSDT',candle=dict(t=i*60000,v='1' if i==1 else '0')) for i in range(1440)]
            records.append(dict(kind='footprint_1m',symbol='SOLUSDT',minute_ms=60000,available_ms=125000,
                buy_volume='1',sell_volume='0',quality_flags=[],levels=[['100.01','1','0',1,0]]))
            for suffix in ['good','late']:
                if suffix=='late':records.append(dict(kind='footprint_late_trade',symbol='SOLUSDT',minute_ms=60000))
                raw=gzip.compress(b''.join(json.dumps(r).encode()+b'\n' for r in records))
                path=root/(suffix+'.tar')
                with tarfile.open(path,'w') as tar:
                    info=tarfile.TarInfo('sample.jsonl.gz');info.size=len(raw);tar.addfile(info,io.BytesIO(raw))
                report=build(path,root/(suffix+'.sqlite'))
                self.assertEqual(report['complete_session_profiles'],int(suffix=='good'))

    def test_live_context_is_frozen_and_disconnect_censors_watch(self):
        from recorder import SetupFilter,compact
        f=SetupFilter(['SOLUSDT']);t=120_000
        f.zones['SOLUSDT'].append(dict(kind='FVG',side='bull',low=99,high=101,
                                      formed_ms=0,available_ms=60_000))
        p=trade(t,'a')
        out=[json.loads(x) for x in f.process('SOLUSDT',p,compact(dict(kind='raw')),t)]
        packet=next(x for x in out if x.get('kind')=='setup_context')
        self.assertEqual(packet['qualification'],'NOT_QUALIFIED')
        self.assertEqual(packet['confluence']['elliott'],'UNCONFIRMED')
        end=[json.loads(x) for x in f.interrupt(t+1000,'CONNECTION_GAP')]
        self.assertEqual(next(x for x in end if x['kind']=='setup_watch_end')['qualification'],'CENSORED_DATA_GAP')
        self.assertIsNone(f.setup_watches['SOLUSDT'])

    def test_unknown_or_future_confluence_blocks(self):
        self.assertTrue(validate_annotation(None,100))
        a={'recorded_ms':1,'rule_version':'test'}
        for layer in ('elliott','smc_context','poi','inducement','mss','profile','footprint'):
            a[layer]={'state':'CONFIRMED','available_ms':2,'evidence_ref':'sha256:test'}
        self.assertEqual(validate_annotation(a,100),[])
        a['elliott']['available_ms']=101
        self.assertIn('NONCAUSAL_ELLIOTT',validate_annotation(a,100))

    def test_stop_target_ambiguity_and_missing_followup_censored(self):
        p=dict(entry_ms=60_000,expiry_ms=180_000,side='LONG',entry=100,stop=99,targets=[101,102])
        k=dict(t=60_000,o=100,h=102,l=98,c=100)
        self.assertEqual(paper_outcome(p,[k])['state'],'AMBIGUOUS_STOP_AND_TARGET_SAME_MINUTE')
        self.assertEqual(paper_outcome(p,[])['state'],'CENSORED_INSUFFICIENT_FOLLOWUP')
        k['h']=100.5;k['l']=99.5
        self.assertEqual(paper_outcome(p,[k,dict(k,t=180_000)])['state'],'CENSORED_INSUFFICIENT_FOLLOWUP')
        self.assertEqual(paper_outcome(dict(p,entry_ms=60_001),[k])['state'],'CENSORED_INTRAMINUTE_ENTRY_REQUIRES_RAW_REPLAY')


if __name__=='__main__':unittest.main()
